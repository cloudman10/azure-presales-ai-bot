"""
app/services/diagram_architect.py

AI-driven architecture discovery agent.
Conducts a multi-turn conversation via Azure AI Foundry / GPT-4o,
asking ONE clarifying question per turn until it has enough to emit
a valid architecture JSON prefixed with ARCHITECTURE_JSON:.
"""

import json
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

ARCH_MARKER = "ARCHITECTURE_JSON:"

SYSTEM_PROMPT = """\
You are an Azure solution architecture discovery assistant.

Your goal: gather requirements through conversation, then produce a valid Azure architecture JSON.

Rules:
1. Ask exactly ONE clear, concise question per turn while still gathering information.
2. Do NOT list multiple questions in one response.
3. When you have enough to produce a complete architecture, output ONLY this — nothing before,
   nothing after:
   ARCHITECTURE_JSON: <json on a single line>
4. Always ask for the Azure region if the user has not stated one.
5. Use ONLY the supported resource types below — never invent others.
6. A valid architecture needs at least 2 resources and 1 connection.

Supported resource types (exact strings only):
  VirtualMachine | LoadBalancer | SQLDatabase | StorageAccount | AppService | VirtualNetwork

Schema:
{
  "title": "<descriptive title including region>",
  "region": "<Azure region display name, e.g. Australia East>",
  "resources": [
    {"id": "<short-id e.g. lb1 web1 db1 stor1 vnet1 app1>", "type": "<supported type>", "name": "<display name>"}
  ],
  "connections": [
    {"from": "<resource id>", "to": "<resource id>"}
  ]
}

Connection rules: every id in connections must exist in resources.

Examples (emit as a single line exactly like this):

Simple PaaS web app:
ARCHITECTURE_JSON: {"title":"Web App - Australia East","region":"Australia East","resources":[{"id":"app1","type":"AppService","name":"Web App"},{"id":"db1","type":"SQLDatabase","name":"Azure SQL"}],"connections":[{"from":"app1","to":"db1"}]}

3-tier IaaS with load balancer:
ARCHITECTURE_JSON: {"title":"3-Tier App - East US","region":"East US","resources":[{"id":"lb1","type":"LoadBalancer","name":"Load Balancer"},{"id":"web1","type":"VirtualMachine","name":"Web VM 1"},{"id":"web2","type":"VirtualMachine","name":"Web VM 2"},{"id":"db1","type":"SQLDatabase","name":"Azure SQL"}],"connections":[{"from":"lb1","to":"web1"},{"from":"lb1","to":"web2"},{"from":"web1","to":"db1"},{"from":"web2","to":"db1"}]}
"""

_JSON_RE = re.compile(
    rf"{re.escape(ARCH_MARKER)}\s*(\{{.*\}})",
    re.DOTALL,
)


async def chat(history: list[dict], message: str) -> dict:
    """
    Single turn of the architecture discovery conversation.

    Mutates history in place (appends user + assistant messages).
    Returns one of:
      {"type": "question", "reply": "<question text>"}
      {"type": "architecture", "json": <dict>}
    """
    history.append({"role": "user", "content": message})
    raw = await _call_foundry(history)
    history.append({"role": "assistant", "content": raw})

    match = _JSON_RE.search(raw)
    if match:
        try:
            arch_json = json.loads(match.group(1))
            return {"type": "architecture", "json": arch_json}
        except json.JSONDecodeError as exc:
            logger.error("diagram_architect: invalid JSON from model: %s | raw=%s", exc, raw[:300])

    return {"type": "question", "reply": raw}


async def _call_foundry(history: list[dict]) -> str:
    endpoint   = os.environ["AZURE_OPENAI_ENDPOINT"]
    api_key    = os.environ["AZURE_OPENAI_KEY"]
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    url = (
        f"{endpoint}/openai/deployments/{deployment}"
        f"/chat/completions?api-version=2024-02-01"
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend({"role": m["role"], "content": m["content"]} for m in history)

    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.post(
            url,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json={"model": deployment, "messages": messages, "max_tokens": 1024},
        )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]
