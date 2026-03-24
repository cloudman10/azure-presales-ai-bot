import logging
import os

import anthropic

from app.agents import pricing_agent

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
    Returns {"reply": str, "type": "conversation" | "pricing"}
    """
    # Get or create session history
    if session_id not in sessions:
        sessions[session_id] = []
    history = sessions[session_id]

    # Append user message
    history.append({"role": "user", "content": message})

    # Route to pricing agent if: message matches pricing keywords OR session
    # already has history (conversation in progress — maintain routing context)
    if is_pricing_request(message) or len(history) > 1:
        logger.debug("session=%s routing to pricing_agent", session_id)
        result = await pricing_agent.run(history)
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
