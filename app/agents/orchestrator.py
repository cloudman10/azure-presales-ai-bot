import logging
import os

import anthropic

from app.agents import pricing_agent
from app.agents.sku_advisor_agent import detect_scenario_query
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
    "i don't know", "i dont know", "not sure", "no idea",
    "you choose", "you pick", "recommend", "suggest",
    "which vm", "which one", "help me choose", "help me pick",
]


def detect_sku_uncertainty(message: str) -> bool:
    lower = message.lower()
    return any(phrase in lower for phrase in _UNCERTAINTY_PHRASES)


def _extract_state_from_history(history: list[dict]) -> dict:
    """Scan conversation history for region and OS already stated by the user."""
    found: dict = {"region": None, "os": None}
    for msg in history:
        if msg.get("role") != "user":
            continue
        text = msg["content"]
        if not found["region"]:
            r = extract_region(text)
            if r:
                found["region"] = r["arm_name"]
        if not found["os"]:
            lower = text.lower()
            if "windows" in lower:
                found["os"] = "Windows"
            elif any(w in lower for w in ("linux", "ubuntu", "centos", "rhel", "debian")):
                found["os"] = "Linux"
        if found["region"] and found["os"]:
            break
    return found


PRICING_KEYWORDS = [
    "price", "pricing", "cost", "how much", "estimate",
    "payg", "reserved", "ri", "hybrid benefit",
    "vm", "virtual machine", "d4s", "e8s", "standard_",
    "windows", "linux", "per month", "per hour",
]


def is_pricing_request(message: str) -> bool:
    lower = message.lower()
    return any(keyword in lower for keyword in PRICING_KEYWORDS)


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
    # Route to advisor if: mid-flow, awaiting a pick selection, or new scenario query
    state_key = f"{session_id}_advisor_state"
    picks_key = f"{session_id}_advisor_picks"

    in_advisor_flow = bool(
        sessions.get(state_key) and
        any(v is not None for v in sessions[state_key].values())
    )
    has_advisor_picks = bool(sessions.get(picks_key))

    if has_advisor_picks or in_advisor_flow or detect_scenario_query(message) or detect_sku_uncertainty(message):
        # Pre-seed advisor state with region/OS from earlier in the conversation
        if not in_advisor_flow and not has_advisor_picks:
            pre = _extract_state_from_history(history)
            if pre["region"] or pre["os"]:
                seed = sessions.get(state_key) or {
                    "vcpus": None, "ram_gb": None, "users": None,
                    "workload": None, "region": None, "os": None,
                }
                if pre["region"] and not seed.get("region"):
                    seed["region"] = pre["region"]
                if pre["os"] and not seed.get("os"):
                    seed["os"] = pre["os"]
                sessions[state_key] = seed
                logger.debug("session=%s advisor pre-seeded: region=%s os=%s",
                             session_id, pre["region"], pre["os"])
        logger.debug("session=%s routing to sku_advisor_agent (in_flow=%s, uncertainty=%s)", session_id, in_advisor_flow, detect_sku_uncertainty(message))
        result = await sku_advisor_agent.run(history, session_id, sessions)

    elif is_pricing_request(message) or len(history) > 1:
        logger.debug("session=%s routing to pricing_agent", session_id)
        result = await pricing_agent.run(history)
        if result.get("handoff") == "sku_advisor":
            logger.debug("session=%s pricing_agent handoff → sku_advisor_agent", session_id)
            result = await sku_advisor_agent.run(history, session_id, sessions)

    else:
        logger.debug("session=%s routing to orchestrator (general)", session_id)
        result = await _call_claude(history)

    # Append assistant reply to history
    history.append({"role": "assistant", "content": result["reply"]})

    return result


async def _call_claude(messages: list[dict]) -> dict:
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    text = response.content[0].text if response.content else ""
    return {"reply": text, "type": "conversation"}
