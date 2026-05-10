import logging
import os
import re

import anthropic

from app.agents import pricing_agent
from app.agents.sku_advisor_agent import detect_scenario_query, parse_requirements
from app.utils.region_normalizer import extract_region

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an Azure presales assistant specialising in VM pricing.

Your job:
- Help users get Azure VM pricing estimates
- Route all VM pricing requests to the Pricing Agent
- For greetings or general Azure questions, respond helpfully and briefly
- For anything unrelated to Azure, politely redirect the user

You have access to a Pricing Agent that can look up real Azure VM pricing.
When the user asks about VM pricing, costs, or estimates, hand off to the
Pricing Agent immediately — do not try to answer pricing questions yourself.

Never make up prices or specifications.
Keep responses concise and professional."""

_UNCERTAINTY_PHRASES = [
    "i don't know", "i dont know", "don't know", "dont know",
    "not sure", "no idea",
    "you choose", "you pick", "recommend", "suggest",
    "which vm", "which one", "help me choose", "help me pick",
]


def detect_sku_uncertainty(message: str) -> bool:
    lower = message.lower()
    return any(phrase in lower for phrase in _UNCERTAINTY_PHRASES)


def _extract_state_from_history(history: list[dict]) -> dict:
    """Scan conversation history for all advisor fields already stated by the user."""
    found: dict = {
        "vcpus": None, "ram_gb": None, "users": None,
        "workload": None, "region": None, "os": None,
    }
    for msg in history:
        if msg.get("role") != "user":
            continue
        text = msg["content"]
        r = parse_requirements(text)
        if r["vcpus"] and not found["vcpus"]:
            found["vcpus"] = r["vcpus"]
        if r["ram_gb"] and not found["ram_gb"]:
            found["ram_gb"] = r["ram_gb"]
        if r["users"] and not found["users"]:
            found["users"] = r["users"]
        if r["workload"] and r["workload"] != "general" and not found["workload"]:
            found["workload"] = r["workload"]
        if r["os"] and not found["os"]:
            found["os"] = r["os"]
        if not found["region"]:
            rm = extract_region(text)
            if rm:
                found["region"] = rm["arm_name"]
            elif r["region"]:
                found["region"] = r["region"]
    return found


PRICING_KEYWORDS = [
    "price", "pricing", "cost", "how much", "estimate",
    "payg", "reserved", "ri", "hybrid benefit",
    "vm", "virtual machine", "d4s", "e8s", "standard_",
    "windows", "linux", "per month", "per hour",
]

_OS_KEYWORDS = re.compile(
    r'\b(windows|linux|ubuntu|redhat|red\s+hat|centos|suse|debian)\b',
    re.IGNORECASE,
)

_SKU_PAT = re.compile(
    r'\b(?:Standard_)?[A-Za-z]\d+[A-Za-z-]*(?:_v\d+)?\b',
    re.IGNORECASE,
)

_BARE_NUMBER_RE = re.compile(r'^\d+$')

_PRICING_INTENT = re.compile(
    r'\b(price|pricing|cost|estimate|vm|virtual\s+machine|need|want|looking\s+for|how\s+much)\b',
    re.IGNORECASE,
)


def _has_os_intent_without_sku(message: str) -> bool:
    """True when the message expresses OS + pricing intent but names no SKU.

    "i need vm pricing for windows" → True  (route to advisor)
    "windows"                       → False (answering a question, no intent)
    "D4s_v5 windows sydney"         → False (SKU present, route to pricing_agent)
    """
    if not _OS_KEYWORDS.search(message):
        return False
    if _SKU_PAT.search(message):
        return False
    return bool(_PRICING_INTENT.search(message))


def is_pricing_request(message: str) -> bool:
    lower = message.lower()
    return any(keyword in lower for keyword in PRICING_KEYWORDS)


def _has_recent_pricing_output(history: list[dict]) -> bool:
    """True if a full pricing estimate block appears in a recent assistant message."""
    for msg in reversed(history[-6:]):
        if (msg.get("role") == "assistant"
                and "=== Azure VM Pricing Estimate ===" in msg.get("content", "")):
            return True
    return False


def _looks_like_option_pick(message: str) -> bool:
    """True if message is selecting option 1/2/3 from a recommendation list.

    Used to distinguish "option 2" (→ advisor STATE 5) from "same for linux?"
    or "what about Sydney?" (→ pricing_agent follow-up).
    """
    lower = message.strip().lower()
    if lower in {
        "yes", "yeah", "yep", "yup", "sure", "ok", "okay",
        "go", "proceed", "fetch", "now", "please", "do it", "all",
    }:
        return True
    # Matches standalone 1/2/3 but NOT numbers like 16 or 32
    return bool(re.search(r'\b[123]\b', lower))


async def run(session_id: str, message: str, sessions: dict) -> dict:
    """
    Orchestrator entry point for all user messages.

    Routing priority:
      1. detect_scenario_query → sku_advisor_agent  (no SKU name, workload-based)
      2. is_pricing_request    → pricing_agent       (has SKU / price keywords)
      3. fallback              → general Claude conversation

    Returns {"reply": str, "type": "conversation" | "pricing" | "advisor"}
    """
    from app.agents import sku_advisor_agent

    # Get or create session history
    if session_id not in sessions:
        sessions[session_id] = []
    history = sessions[session_id]

    # Append user message
    history.append({"role": "user", "content": message})

    # ── Routing ───────────────────────────────────────────────────────────────
    state_key = f"{session_id}_advisor_state"
    picks_key = f"{session_id}_advisor_picks"

    # True whenever the advisor state dict exists for this session, even if all
    # values are still None (i.e. STATE 1 was just triggered).  Without this,
    # an all-None state causes in_advisor_flow=False and the next user message
    # leaks to pricing_agent, which re-asks vCPUs/RAM via LLM.
    in_advisor_flow = bool(sessions.get(state_key))
    has_advisor_picks = bool(sessions.get(picks_key))

    # Route to advisor when:
    #   • actively collecting requirements (in_advisor_flow)
    #   • new workload/scenario/uncertainty query
    #   • picks are live AND message is an option pick (1/2/3/yes/all)
    #
    # has_advisor_picks alone is NOT sufficient — a follow-up like
    # "same for linux?" or "what about Sydney?" must reach pricing_agent,
    # not get stuck in STATE 5 returning "please pick 1/2/3".
    wants_advisor = (
        in_advisor_flow
        or detect_scenario_query(message)
        or detect_sku_uncertainty(message)
        or _has_os_intent_without_sku(message)
        or (has_advisor_picks and _looks_like_option_pick(message))
    )

    # Bare digits (e.g. "1") must never kick off a fresh pricing collection flow.
    # They're option picks or conversation continuations — route to general Claude
    # so the full history context is preserved.
    _is_bare_number = bool(_BARE_NUMBER_RE.match(message.strip()))

    if wants_advisor:
        # Pre-seed advisor state with everything the user has already mentioned
        if not in_advisor_flow and not has_advisor_picks:
            pre = _extract_state_from_history(history)
            if any(v is not None for v in pre.values()):
                seed = sessions.get(state_key) or {
                    "vcpus": None, "ram_gb": None, "users": None,
                    "workload": None, "region": None, "os": None,
                }
                for k, v in pre.items():
                    if v is not None and not seed.get(k):
                        seed[k] = v
                sessions[state_key] = seed
                logger.debug(
                    "session=%s advisor pre-seeded: vcpus=%s ram_gb=%s region=%s os=%s",
                    session_id, seed.get("vcpus"), seed.get("ram_gb"),
                    seed.get("region"), seed.get("os"),
                )
        logger.debug("session=%s routing to sku_advisor_agent (in_flow=%s, uncertainty=%s)",
                     session_id, in_advisor_flow, detect_sku_uncertainty(message))
        result = await sku_advisor_agent.run(history, session_id, sessions)

    elif is_pricing_request(message) or _has_recent_pricing_output(history) or (len(history) > 1 and not _is_bare_number):
        logger.debug("session=%s routing to pricing_agent", session_id)
        result = await pricing_agent.run(history)
        if result.get("handoff") == "sku_advisor":
            logger.debug("session=%s pricing_agent handoff → sku_advisor_agent", session_id)
            # pricing_agent extracted known context at the moment of handoff —
            # use those values first.  Fall back to a full history scan for any
            # field that pricing_agent couldn't find (e.g. user mentioned it
            # before the pricing flow started).
            pre = _extract_state_from_history(history)
            seed = {
                "vcpus":    result.get("known_vcpus")  or pre.get("vcpus"),
                "ram_gb":   result.get("known_ram_gb") or pre.get("ram_gb"),
                "users":    pre.get("users"),
                "workload": pre.get("workload"),
                "region":   result.get("known_region") or pre.get("region"),
                "os":       result.get("known_os")     or pre.get("os"),
            }
            sessions[state_key] = seed
            logger.debug(
                "session=%s handoff pre-seeded: region=%s os=%s vcpus=%s",
                session_id, seed.get("region"), seed.get("os"), seed.get("vcpus"),
            )
            result = await sku_advisor_agent.run(history, session_id, sessions)

    else:
        logger.debug("session=%s routing to orchestrator (general)", session_id)
        result = await _call_claude(history)

    # Append assistant reply to history
    history.append({"role": "assistant", "content": result["reply"]})

    return result


async def _call_claude(messages: list[dict]) -> dict:
    try:
        client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        text = response.content[0].text if response.content else ""
        return {"reply": text, "type": "conversation"}
    except Exception as e:
        logger.error("_call_claude failed: %s", e)
        return {
            "reply": (
                "I'm not able to answer that right now, but I can help with Azure VM pricing. "
                "Try asking for a specific VM SKU or describe your requirements."
            ),
            "type": "conversation",
        }
