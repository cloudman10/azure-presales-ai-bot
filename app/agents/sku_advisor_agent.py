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

from app.services.azure_pricing import fetch_prices
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
}

_RESOURCE_WORDS = {
    "cores", "vcpu", "vcpus", "ram", "memory",
    "gb ram", "tb storage", "gb storage",
}


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
    if "windows" in lower:
        os_type = "Windows"
    elif any(w in lower for w in ("linux", "ubuntu", "centos", "rhel", "debian")):
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

    Filters applied:
      - retired eq false
      - vcpus ge N  (if specified)
      - ram_gb ge N (if specified)

    Full-text search on use_cases for the workload type keyword.
    Returns up to 5 results sorted vcpus asc, ram_gb asc.
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

    # Build OData filter
    filters = ["retired eq false"]
    vcpus  = requirements.get("vcpus")
    ram_gb = requirements.get("ram_gb")
    if vcpus:
        filters.append(f"vcpus ge {vcpus}")
    if ram_gb:
        filters.append(f"ram_gb ge {ram_gb}")
    odata_filter = " and ".join(filters)

    # Search text — use workload as the free-text query against use_cases / description
    workload    = requirements.get("workload") or "general"
    search_text = workload

    try:
        results = client.search(
            search_text=search_text,
            filter=odata_filter,
            search_fields=["use_cases", "description"],
            order_by=["vcpus asc", "ram_gb asc"],
            top=5,
        )
        raw = [dict(r) for r in results]

        def generation_score(sku: dict) -> int:
            name = sku.get("sku_name", "")
            if "_v5" in name or "_v6" in name or "_v7" in name:
                return 3
            if "_v4" in name or "_v3" in name:
                return 2
            if "_v2" in name:
                return 1
            return 0  # v1 or no version — oldest

        # Exclude Promo and Basic variants
        filtered = [s for s in raw if "Promo" not in s.get("sku_name", "") and "Basic" not in s.get("sku_name", "")]

        # Sort by generation descending, then vcpus asc
        sorted_skus = sorted(filtered, key=lambda x: (-generation_score(x), x.get("vcpus", 0)))

        return sorted_skus[:5]
    except Exception as e:
        logger.error("Azure AI Search query failed: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# 5. format_recommendations
# ─────────────────────────────────────────────────────────────────────────────

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

    # Headings that describe why each option was chosen
    _LABELS = [
        "Recommended",
        "Memory optimised",
        "Cost optimised",
    ]

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
        label           = _LABELS[i] if i < len(_LABELS) else ""

        heading = f"Option {i + 1} — {sku_name}"
        if label:
            heading += f" ({label})"
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
        'Which option would you like full pricing details for? '
        'Reply with **1**, **2**, or **3**'
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Agent entry point
# ─────────────────────────────────────────────────────────────────────────────

_EMPTY_STATE = lambda: {
    "vcpus": None, "ram_gb": None, "users": None,
    "workload": None, "region": None, "os": None,
}


async def run(messages: list[dict], session_id: str, sessions: dict) -> dict:
    """
    Main entry point called by the orchestrator.

    Implements a strict 4-state conversation machine:
      State 1 → collect sizing (vCPUs/RAM or user count + workload)
      State 2 → collect region
      State 3 → collect OS
      State 4 → search SKUs and show top-3 recommendations with live pricing
    """
    # Extract the most recent user message
    user_message = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_message = m["content"]
            break

    logger.info("sku_advisor: message='%s'", user_message[:80])

    # ── Load or initialise per-session advisor state ───────────────────────────
    state_key = f"{session_id}_advisor_state"
    state: dict = sessions.get(state_key) or _EMPTY_STATE()

    # ── Parse current message and merge into state (never overwrite with None) ─
    reqs = parse_requirements(user_message)

    if reqs["vcpus"] and not state["vcpus"]:
        state["vcpus"] = reqs["vcpus"]
    if reqs["ram_gb"] and not state["ram_gb"]:
        state["ram_gb"] = reqs["ram_gb"]
    if reqs["users"] and not state["users"]:
        state["users"] = reqs["users"]
    # Only accept non-default workload from parsing
    if reqs["workload"] and reqs["workload"] != "general" and not state["workload"]:
        state["workload"] = reqs["workload"]

    # Use the richer extract_region for region detection
    if not state["region"]:
        region_match = extract_region(user_message)
        if region_match:
            state["region"] = region_match["arm_name"]
        elif reqs["region"]:
            state["region"] = reqs["region"]

    if reqs["os"] and not state["os"]:
        state["os"] = reqs["os"]

    logger.info("sku_advisor: state=%s", state)

    # ── STATE 1: sizing ────────────────────────────────────────────────────────
    has_sizing = (state["vcpus"] and state["ram_gb"]) or state["users"]
    if not has_sizing:
        sessions[state_key] = state
        return {
            "reply": (
                "How many vCPUs and how much RAM do you need? "
                "Or describe your workload — e.g. '500 concurrent users running a web app' "
                "or '8 vCPUs and 32 GB RAM'."
            ),
            "type": "advisor",
        }

    # ── STATE 2: region ────────────────────────────────────────────────────────
    if not state["region"]:
        sessions[state_key] = state
        return {
            "reply": "Which Azure region would you like to deploy in? (e.g. Australia East, East US, West Europe, Singapore)",
            "type": "advisor",
        }

    # ── STATE 3: OS ────────────────────────────────────────────────────────────
    if not state["os"]:
        sessions[state_key] = state
        return {
            "reply": "Windows or Linux?",
            "type": "advisor",
        }

    # ── STATE 4: all collected — search and recommend ──────────────────────────
    # Estimate vCPUs/RAM from user count if still missing
    if state["users"] and not state["vcpus"] and not state["ram_gb"]:
        specs = estimate_specs_from_users(state["users"], state["workload"])
        state["vcpus"]  = specs["vcpus"]
        state["ram_gb"] = specs["ram_gb"]
        logger.info("sku_advisor: estimated from %d users → %s", state["users"], specs)

    skus = search_skus(state)
    if not skus:
        # Reset state so user can try again with different specs
        sessions[state_key] = _EMPTY_STATE()
        return {
            "reply": (
                "I couldn't find VMs matching those requirements in my index. "
                "Could you clarify the workload type or specs? "
                "For example: '4 vCPUs and 16 GB RAM for a web app in Australia East'."
            ),
            "type": "advisor",
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

    # Reset state after recommendations so follow-up starts fresh
    sessions[state_key] = _EMPTY_STATE()

    return {"reply": reply, "type": "advisor"}
