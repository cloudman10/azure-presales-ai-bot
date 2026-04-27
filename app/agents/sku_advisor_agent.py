"""
app/agents/sku_advisor_agent.py

SKU Advisor Agent — handles scenario-based queries where the user doesn't
know the VM name.  Collects requirements from natural language, searches the
Azure AI Search vm-skus index for matching SKUs, fetches live pricing for
the top 3, and returns a formatted recommendation list.

Routing: orchestrator calls detect_scenario_query() first.  If True, the
message is handled here instead of going to pricing_agent.
"""

import logging
import os
import re

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery

from app.services.azure_pricing import fetch_prices, fetch_temp_storage_gb
from app.utils.pricing_calculator import HOURS_PER_MONTH, find_price
from app.utils.region_normalizer import display_region, extract_region

logger = logging.getLogger(__name__)

SEARCH_INDEX   = "vm-skus"
DEFAULT_REGION = "australiaeast"

# ── SKU pattern: letter + digits (+ optional suffix/version) ──────────────────
_SKU_RE = re.compile(
    r'\b(?:Standard_)?[A-Za-z]\d+[A-Za-z]*(?:_v\d+)?\b',
    re.IGNORECASE,
)

# ── Keyword sets ──────────────────────────────────────────────────────────────

_USER_WORDS = {"users", "concurrent", "connections", "seats", "user"}

_WORKLOAD_WORDS = {
    "web app", "web server", "database", "sql server", "sql", "sap",
    "application", "app server", "api", "microservice", "microservices",
    "devtest", "dev/test", "dev test", "vdi", "virtual desktop", "desktop",
}

_SCENARIO_WORDS = {
    "build", "host", "migrate", "run", "deploy",
    "need a server", "need vms", "need a vm",
    # User uncertainty phrases — no SKU name known
    "don't know the vm", "don't know which vm", "don't know the name",
    "don't know the exact", "not sure which vm", "not sure what vm",
    "unsure which vm", "need a recommendation", "recommend a vm",
    "what vm", "which vm should", "which vm do",
}

_RESOURCE_WORDS = {
    "cores", "vcpu", "vcpus", "ram", "memory",
    "gb ram", "tb storage", "gb storage",
}

_SERIES_LABELS = {
    "D": "General Purpose",
    "E": "Memory Optimised",
    "F": "Compute Optimised",
    "B": "Burstable/Cost Optimised",
}

_PREFERRED_SERIES_ORDER = ["D", "E", "F", "B"]


def _sku_series(sku_name: str) -> str:
    """Return the VM series letter from a SKU name (e.g. Standard_D4s_v5 → 'D')."""
    m = re.match(r'(?:Standard_)?([A-Za-z])', sku_name or "")
    return m.group(1).upper() if m else "?"


# ─────────────────────────────────────────────────────────────────────────────
# 1. detect_scenario_query
# ─────────────────────────────────────────────────────────────────────────────

def detect_scenario_query(message: str) -> bool:
    """
    Returns True if the message describes a workload scenario without
    naming a concrete VM SKU.

    Returns False if a recognisable SKU pattern is already present
    (letter + digits) — those go straight to the pricing agent.
    """
    lower = message.lower()

    # If message already contains a concrete SKU → let pricing_agent handle it
    if _SKU_RE.search(message):
        return False

    for phrase in _WORKLOAD_WORDS | _SCENARIO_WORDS:
        if phrase in lower:
            return True

    words = set(lower.split())
    if words & _USER_WORDS:
        return True
    if words & _RESOURCE_WORDS:
        return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# 2. parse_requirements
# ─────────────────────────────────────────────────────────────────────────────

def parse_requirements(message: str) -> dict:
    """
    Extract structured requirements from a natural-language message.

    Returns:
        vcpus       : int | None
        ram_gb      : int | None
        users       : int | None
        workload    : "web" | "database" | "sap" | "devtest" | "vdi" | "general" | None
        region      : str | None   (ARM region name)
        os          : "Linux" | "Windows" | None
        storage_gb  : int | None
    """
    lower = message.lower()

    # ── vCPUs ────────────────────────────────────────────────────────────────
    vcpus = None
    m = re.search(r'(\d+)\s*(?:v?cpu|vcore|core)s?', lower)
    if m:
        vcpus = int(m.group(1))

    # ── RAM ─────────────────────────────────────────────────────────────────
    ram_gb = None
    m = re.search(r'(\d+)\s*(?:gb|gib)\s*(?:ram|memory)', lower)
    if not m:
        m = re.search(r'(\d+)\s*(?:ram|memory)', lower)
    if not m:
        m = re.search(r'(\d+)\s*gb\b', lower)
    if m:
        ram_gb = int(m.group(1))

    # ── User count ───────────────────────────────────────────────────────────
    users = None
    m = re.search(r'(\d[\d,]*)\s*(?:concurrent\s+)?(?:users?|connections?|seats?)', lower)
    if m:
        users = int(m.group(1).replace(',', ''))

    # ── Workload type ────────────────────────────────────────────────────────
    workload = None
    if any(w in lower for w in ("sap", "hana")):
        workload = "sap"
    elif any(w in lower for w in ("sql server", "sql", "database", "db", "postgres", "mysql", "oracle")):
        workload = "database"
    elif any(w in lower for w in ("vdi", "virtual desktop", "desktop", "rdp")):
        workload = "vdi"
    elif any(w in lower for w in ("dev/test", "devtest", "dev test", "development", "test env")):
        workload = "devtest"
    elif any(w in lower for w in ("web app", "web server", "website", "webapp", "api", "microservice")):
        workload = "web"
    else:
        workload = "general"

    # ── Region ───────────────────────────────────────────────────────────────
    region = None
    region_map = {
        "sydney":         "australiaeast",
        "australia east": "australiaeast",
        "melbourne":      "australiasoutheast",
        "singapore":      "southeastasia",
        "tokyo":          "japaneast",
        "east us":        "eastus",
        "west us":        "westus",
        "west europe":    "westeurope",
        "north europe":   "northeurope",
        "uk south":       "uksouth",
    }
    for phrase, arm in region_map.items():
        if phrase in lower:
            region = arm
            break

    # ── OS ───────────────────────────────────────────────────────────────────
    os_type = None
    raw = message.lower().strip().replace(" ", "")
    if any(w in raw for w in ["window", "windwos", "windoes", "widnows", "win"]):
        os_type = "Windows"
    elif any(w in raw for w in ["linux", "linus", "linx", "lin"]):
        os_type = "Linux"

    # ── Storage ──────────────────────────────────────────────────────────────
    storage_gb = None
    m = re.search(r'(\d+)\s*tb\s*(?:storage|disk)?', lower)
    if m:
        storage_gb = int(m.group(1)) * 1024
    else:
        m = re.search(r'(\d+)\s*gb\s*(?:storage|disk)', lower)
        if m:
            storage_gb = int(m.group(1))

    return {
        "vcpus":      vcpus,
        "ram_gb":     ram_gb,
        "users":      users,
        "workload":   workload,
        "region":     region,
        "os":         os_type,
        "storage_gb": storage_gb,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. estimate_specs_from_users
# ─────────────────────────────────────────────────────────────────────────────

def estimate_specs_from_users(users: int, workload: str | None) -> dict:
    """
    Map user count + workload type to minimum vCPU / RAM specs.
    Returns {"vcpus": int, "ram_gb": int}.
    """
    w = (workload or "general").lower()

    if w == "sap":
        if users >= 500:
            return {"vcpus": 32, "ram_gb": 256}
        return {"vcpus": 16, "ram_gb": 128}

    if w == "database":
        if users >= 500:
            return {"vcpus": 16, "ram_gb": 64}
        if users >= 200:
            return {"vcpus": 8,  "ram_gb": 32}
        return {"vcpus": 4, "ram_gb": 16}

    if w == "devtest":
        return {"vcpus": 2, "ram_gb": 8}

    if w == "vdi":
        # ~1 vCPU / 2 GB per concurrent user, minimum D4s
        vcpus  = max(4, min(users // 10, 64))
        ram_gb = max(16, min(users // 5, 256))
        return {"vcpus": vcpus, "ram_gb": ram_gb}

    if w == "web":
        if users >= 1000:
            return {"vcpus": 8, "ram_gb": 32}
        if users >= 500:
            return {"vcpus": 4, "ram_gb": 16}
        return {"vcpus": 2, "ram_gb": 4}

    # general
    if users >= 500:
        return {"vcpus": 8, "ram_gb": 32}
    return {"vcpus": 4, "ram_gb": 16}


# ─────────────────────────────────────────────────────────────────────────────
# 4. search_skus
# ─────────────────────────────────────────────────────────────────────────────

def search_skus(requirements: dict) -> list[dict]:
    """
    Query the Azure AI Search vm-skus index for matching active SKUs.

    Runs one search per series family (D/E/F/B) using name-prefix OData filters,
    takes the best result from each family, then fills remaining slots from the
    combined candidate pool. Always prefers newer generations (v5/v6/v7).
    """
    endpoint = os.environ.get("AZURE_SEARCH_ENDPOINT", "")
    api_key  = os.environ.get("AZURE_SEARCH_API_KEY", "")
    if not endpoint or not api_key:
        logger.error("AZURE_SEARCH_ENDPOINT or AZURE_SEARCH_API_KEY not set")
        return []

    client = SearchClient(
        endpoint=endpoint,
        index_name=SEARCH_INDEX,
        credential=AzureKeyCredential(api_key),
    )

    # Base OData filter
    base_filters = ["retired eq false"]
    vcpus  = requirements.get("vcpus")
    ram_gb = requirements.get("ram_gb")
    if vcpus:
        base_filters.append(f"vcpus ge {vcpus}")
    if ram_gb:
        base_filters.append(f"ram_gb ge {ram_gb}")
    base_filter = " and ".join(base_filters)

    # (name_lo, name_hi_exclusive, search_text)
    _SERIES_SEARCHES = [
        ("Standard_D", "Standard_E", "general purpose web server"),
        ("Standard_E", "Standard_F", "memory optimised database"),
        ("Standard_F", "Standard_G", "compute optimised"),
        ("Standard_B", "Standard_C", "burstable cost saving"),
    ]

    def generation_score(sku: dict) -> int:
        name = sku.get("sku_name", "")
        if "_v5" in name or "_v6" in name or "_v7" in name:
            return 3
        if "_v4" in name or "_v3" in name:
            return 2
        if "_v2" in name:
            return 1
        return 0

    def clean_and_sort(raw: list[dict]) -> list[dict]:
        filtered = [s for s in raw
                    if "Promo" not in s.get("sku_name", "")
                    and "Basic" not in s.get("sku_name", "")]
        filtered.sort(key=lambda x: (-generation_score(x), x.get("vcpus", 0)))
        return filtered

    per_family: list[dict | None] = []
    all_candidates: list[dict] = []

    for lo, hi, search_text in _SERIES_SEARCHES:
        series_filter = f"{base_filter} and sku_name ge '{lo}' and sku_name lt '{hi}'"
        try:
            results = client.search(
                search_text=search_text,
                filter=series_filter,
                search_fields=["use_cases", "description"],
                order_by=["vcpus asc", "ram_gb asc"],
                top=3,
            )
            clean = clean_and_sort([dict(r) for r in results])
            per_family.append(clean[0] if clean else None)
            all_candidates.extend(clean)
        except Exception as e:
            logger.error("Azure AI Search query failed for series %s: %s", lo, e)
            per_family.append(None)

    # One representative per family first, then fill from the rest
    seen: set[str] = set()
    merged: list[dict] = []
    for sku in per_family:
        if sku is not None:
            name = sku.get("sku_name", "")
            if name not in seen:
                seen.add(name)
                merged.append(sku)

    all_candidates.sort(key=lambda x: (-generation_score(x), x.get("vcpus", 0)))
    for sku in all_candidates:
        if len(merged) >= 5:
            break
        name = sku.get("sku_name", "")
        if name not in seen:
            seen.add(name)
            merged.append(sku)

    return merged[:5]


# ─────────────────────────────────────────────────────────────────────────────
# 5. format_recommendations
# ─────────────────────────────────────────────────────────────────────────────

def _label_for_sku(sku_name: str) -> str:
    """Return a recommendation label based on the SKU series letter."""
    return _SERIES_LABELS.get(_sku_series(sku_name), "Specialised")


def format_recommendations(
    skus: list[dict],
    requirements: dict,
    prices: list[dict | None],   # one entry per SKU (None = no price found)
) -> str:
    """
    Format the top-3 SKU recommendations with live pricing.
    """
    region      = requirements.get("region") or DEFAULT_REGION
    os_type     = requirements.get("os") or "Linux"
    region_disp = display_region(region)

    top3 = skus[:3]
    if not top3:
        return (
            "I couldn't find matching VMs in the index for those requirements. "
            "Please refine your specs or contact your Azure team."
        )

    lines = ["Based on your requirements, here are my top VM recommendations:\n"]

    for i, sku_doc in enumerate(top3):
        sku_name        = sku_doc.get("sku_name", "Unknown")
        vcpus           = sku_doc.get("vcpus", "?")
        ram_gb          = sku_doc.get("ram_gb", "?")
        temp_storage_gb = sku_doc.get("temp_storage_gb", 0)
        use_cases       = sku_doc.get("use_cases", "")
        label           = _label_for_sku(sku_name)

        heading = f"Option {i + 1} — {sku_name} ({label})"
        lines.append(heading)

        # Specs line
        temp_str = f"{temp_storage_gb} GB temp SSD" if temp_storage_gb else "no local temp disk"
        lines.append(f"  {vcpus} vCPUs | {ram_gb} GB RAM | {temp_str}")

        # Use-case blurb (first ~80 chars)
        if use_cases:
            short = use_cases[:90].rstrip()
            lines.append(f"  Best for: {short}")

        # Pricing
        price_items = prices[i] if i < len(prices) else None
        if price_items:
            payg = find_price(price_items, os_type, "Consumption")
            if payg:
                hr  = payg["retailPrice"]
                mo  = hr * HOURS_PER_MONTH
                cur = payg.get("currencyCode", "USD")
                lines.append(f"  {cur} {hr:.4f}/hr  |  {cur} {mo:.2f}/month ({os_type}, {region_disp})")
            else:
                lines.append(f"  Pricing unavailable for {os_type} in {region_disp}")
        else:
            lines.append(f"  Pricing unavailable — verify at azure.com/pricing")

        lines.append("")   # blank line between options

    lines.append(
        "Which option would you like full pricing details for? "
        "Reply with **1**, **2**, or **3** — or type **all** to see full pricing for all three."
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Agent entry point
# ─────────────────────────────────────────────────────────────────────────────

def _parse_selection(msg: str) -> list[int] | None:
    """
    Returns a list of 0-based indices for the chosen option(s), or None.
    Handles: "1", "option 1", "go with 2", "all", "all three",
             "what about option 2", "provide me pricing for option 2",
             "pricing for 2", "option 2 pricing", "show me 3", etc.
    """
    lower = msg.strip().lower()
    if re.search(r'\ball\b', lower):
        return [0, 1, 2]
    # Explicit selection phrases — matched before bare-digit fallback
    m = re.search(
        r'(?:'
        r'what\s+about\s+(?:option\s+)?'
        r'|show\s+me\s+(?:option\s+)?'
        r'|(?:provide|get|fetch|give)\s+(?:me\s+)?(?:(?:full\s+)?pricing\s+for\s+)?(?:option\s+)?'
        r'|pricing\s+for\s+(?:option\s+)?'
        r'|option\s+'
        r')([123])\b',
        lower,
    )
    if m:
        return [int(m.group(1)) - 1]
    # Bare digit: "1", "2", "3", "go with 2", "I'll take 3", etc.
    m = re.search(r'\b([123])\b', lower)
    if m:
        return [int(m.group(1)) - 1]
    return None


async def _show_full_pricing(skus: list[str], region: str, os_type: str) -> str:
    """Fetch live pricing and format full breakdown for each chosen SKU."""
    from app.agents.pricing_agent import _format_pricing

    parts = []
    for sku_name in skus:
        try:
            items    = await fetch_prices(region, sku_name)
            temp_gb  = await fetch_temp_storage_gb(sku_name, region)
            params   = {"sku": sku_name, "region": region, "os": os_type,
                        "qty": 1, "wants_hb": False}
            parts.append(_format_pricing(params, items, temp_gb))
        except Exception as e:
            parts.append(f"Could not fetch pricing for {sku_name}: {e}")

    return "\n\n---\n\n".join(parts)


_EMPTY_STATE = lambda: {
    "vcpus": None, "ram_gb": None, "users": None,
    "workload": None, "region": None, "os": None,
}

# Affirmative words that, when typed alone in STATE 5, map to "option 1"
_AFFIRMATIVES = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay",
    "go", "proceed", "fetch", "now", "please", "do it",
})


async def run(messages: list[dict], session_id: str, sessions: dict) -> dict:
    """
    Pure Python state machine. No LLM calls anywhere in this agent.
    All external calls are Azure AI Search + Azure Pricing API only.

      State 1 → collect sizing (vCPUs/RAM or user count)
      State 2 → collect region
      State 3 → collect OS
      State 4 → search_skus() + fetch_prices() + format_recommendations()
      State 5 → user picks 1/2/3/all → _show_full_pricing()
    """
    # ── Extract latest user message ────────────────────────────────────────────
    user_message = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_message = m["content"]
            break

    logger.info("sku_advisor: message='%s'", user_message[:80])

    # ── STATE 5: user is selecting from previously shown recommendations ───────
    picks_key = f"{session_id}_advisor_picks"
    picks = sessions.get(picks_key)
    if picks:
        selection = _parse_selection(user_message)
        # Treat standalone affirmatives ("yes", "ok", "sure", …) as option 1.
        if selection is None and user_message.strip().lower() in _AFFIRMATIVES:
            selection = [0]
        if selection is not None:
            # Intentionally do NOT pop picks_key here.  Keeping it alive means
            # the user can immediately follow up with "what about option 3?"
            # without being re-asked for region or OS by the pricing agent.
            # picks_key is only replaced when STATE 4 generates new recommendations.
            chosen_skus = [picks["skus"][i] for i in selection if i < len(picks["skus"])]
            reply = await _show_full_pricing(chosen_skus, picks["region"], picks["os"])
            return {"reply": reply, "type": "pricing"}
        # No option digit found — if the user is starting a new scenario query
        # clear picks and fall through to the state machine; otherwise prompt.
        if detect_scenario_query(user_message) or detect_sku_uncertainty(user_message):
            sessions.pop(picks_key, None)
            # fall through to state machine below
        else:
            return {
                "reply": "Please reply with **1**, **2**, or **3** — or type **all** to see full pricing for all three.",
                "type": "conversation",
            }

    # ── Load or initialise per-session state ───────────────────────────────────
    state_key = f"{session_id}_advisor_state"
    state: dict = sessions.get(state_key) or _EMPTY_STATE()

    # ── Phase 1: on fresh entry seed from the very first user message ─────────
    # The opening message most often carries all context (specs, region, OS)
    # even when the conversation was originally handled by another agent.
    # Doing this explicitly before the full-history loop ensures nothing is
    # missed when the advisor is entered mid-conversation.
    if not any(v is not None for v in state.values()):
        _first = next(
            (m["content"] for m in messages if m.get("role") == "user"), ""
        )
        if _first:
            _r0  = parse_requirements(_first)
            _rm0 = extract_region(_first)
            if _r0["vcpus"]:                                      state["vcpus"]    = _r0["vcpus"]
            if _r0["ram_gb"]:                                     state["ram_gb"]   = _r0["ram_gb"]
            if _r0["users"]:                                      state["users"]    = _r0["users"]
            if _r0["workload"] and _r0["workload"] != "general":  state["workload"] = _r0["workload"]
            if _r0["os"]:                                         state["os"]       = _r0["os"]
            if _rm0:                                              state["region"]   = _rm0["arm_name"]
            elif _r0["region"]:                                   state["region"]   = _r0["region"]
            logger.debug(
                "sku_advisor: seeded from first msg → vcpus=%s region=%s os=%s",
                state["vcpus"], state["region"], state["os"],
            )

    # ── Phase 2: scan all user messages oldest-first to fill any gaps ─────────
    for _msg in messages:
        if _msg.get("role") != "user":
            continue
        _text = _msg["content"]
        _r = parse_requirements(_text)
        if _r["vcpus"] and not state["vcpus"]:
            state["vcpus"] = _r["vcpus"]
        if _r["ram_gb"] and not state["ram_gb"]:
            state["ram_gb"] = _r["ram_gb"]
        if _r["users"] and not state["users"]:
            state["users"] = _r["users"]
        if _r["workload"] and _r["workload"] != "general" and not state["workload"]:
            state["workload"] = _r["workload"]
        if _r["os"] and not state["os"]:
            state["os"] = _r["os"]
        if not state["region"]:
            _rm = extract_region(_text)
            if _rm:
                state["region"] = _rm["arm_name"]
            elif _r["region"]:
                state["region"] = _r["region"]

    logger.info("sku_advisor: state=%s", state)
    sessions[state_key] = state   # persist after every turn

    # ── STATE 1: need sizing ───────────────────────────────────────────────────
    if not (state["vcpus"] or state["ram_gb"] or state["users"]):
        return {
            "reply": "How many vCPUs and how much RAM do you need? (e.g. '4 cores 16GB' or '8 vCPUs 32GB RAM')",
            "type": "conversation",
        }

    # ── STATE 2: need region ───────────────────────────────────────────────────
    if not state["region"]:
        return {
            "reply": "Which Azure region would you like to deploy in? (e.g. Sydney, Singapore, London, East US)",
            "type": "conversation",
        }

    # ── STATE 3: need OS ───────────────────────────────────────────────────────
    if not state["os"]:
        return {
            "reply": "Windows or Linux?",
            "type": "conversation",
        }

    # ── STATE 4: all collected — search + price + format (no LLM) ─────────────
    # Derive vCPU/RAM from user count if only users were given
    if state["users"] and not state["vcpus"] and not state["ram_gb"]:
        specs = estimate_specs_from_users(state["users"], state["workload"])
        state["vcpus"]  = specs["vcpus"]
        state["ram_gb"] = specs["ram_gb"]
        logger.info("sku_advisor: estimated from %d users → %s", state["users"], specs)

    skus = search_skus(state)

    if not skus:
        sessions[state_key] = _EMPTY_STATE()   # reset so user can retry
        return {
            "reply": (
                "I couldn't find VMs matching those requirements. "
                "Try adjusting the specs — e.g. '4 vCPUs 16GB RAM for a web app'."
            ),
            "type": "conversation",
        }

    top3 = skus[:3]
    prices: list[list[dict] | None] = []
    for sku_doc in top3:
        sku_name = sku_doc.get("sku_name", "")
        try:
            items = await fetch_prices(state["region"], sku_name)
            prices.append(items)
        except Exception as e:
            logger.warning("sku_advisor: price fetch failed for %s: %s", sku_name, e)
            prices.append(None)

    reply = format_recommendations(top3, state, prices)

    # Store picks so the user can select 1/2/3/all in the next turn
    sessions[picks_key] = {
        "skus":   [s.get("sku_name") for s in top3],
        "region": state["region"],
        "os":     state["os"],
    }
    sessions[state_key] = _EMPTY_STATE()   # reset advisor state
    return {"reply": reply, "type": "advisor"}
