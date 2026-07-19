"""
scripts/validate_retrieval.py

Validate the hybrid retrieval upgrade:
  1. AKS query: do retrieved chunks now contain system node pool, user node pool,
     Azure CNI, ACR private endpoint specifics?
  2. SAP cloud-only: does grounded output still inject an unrequested on-prem zone?

Usage:
    python scripts/validate_retrieval.py
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["RAG_ENABLED"] = "true"

from app.services.diagram_architect import architect_chat, retrieve_arch_context

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-8s %(message)s")

SEP  = "=" * 76
SEP2 = "-" * 76

AKS_QUERY = (
    "AKS baseline cluster for a production microservices app on Azure, "
    "Australia East. Needs to be production-grade with proper security and observability."
)

SAP_QUERY = (
    "SAP S/4HANA production deployment on Azure for 500 users, "
    "high availability required, Australia East. Cloud-only deployment."
)

AKS_KEYWORDS = [
    "system node pool",
    "user node pool",
    "azure cni",
    "acr",
    "private endpoint",
    "ingress controller",
    "container registry",
    "node pool",
]


def _check_keywords(chunks_text: str) -> list[str]:
    lower = chunks_text.lower()
    return [kw for kw in AKS_KEYWORDS if kw in lower]


async def validate_aks_retrieval() -> None:
    print(f"\n{SEP}")
    print("  VALIDATION 1: AKS Retrieval Quality")
    print(SEP)
    print(f"  Query: {AKS_QUERY}")
    print()

    ctx = await retrieve_arch_context(AKS_QUERY, top_k=5)

    if not ctx:
        print("  ERROR: No context retrieved — check AZURE_SEARCH_ENDPOINT and index.")
        return

    chunks = ctx.split("\n\n---\n\n")
    print(f"  Retrieved {len(chunks)} chunk(s):\n")
    for i, chunk in enumerate(chunks, 1):
        lines = chunk.splitlines()
        header = lines[0] if lines else "(no header)"
        body   = "\n    ".join(lines[1:6]) if len(lines) > 1 else "(no body)"
        print(f"  [{i}] {header}")
        print(f"    {body}")
        if len(lines) > 6:
            print(f"    ... ({len(lines) - 6} more lines)")
        print()

    found = _check_keywords(ctx)
    missing = [kw for kw in AKS_KEYWORDS if kw not in found]

    print(f"  Keyword check against retrieved chunks:")
    for kw in AKS_KEYWORDS:
        status = "FOUND" if kw in found else "missing"
        print(f"    {kw:<30} {status}")

    print()
    if len(found) >= 4:
        print(f"  PASS: {len(found)}/{len(AKS_KEYWORDS)} AKS sub-pattern keywords found in retrieved chunks.")
        print("  Hybrid retrieval is surfacing specific AKS baseline sub-patterns.")
    else:
        print(f"  PARTIAL: {len(found)}/{len(AKS_KEYWORDS)} keywords found.")
        print(f"  Missing: {missing}")


async def validate_sap_grounding() -> None:
    print(f"\n{SEP}")
    print("  VALIDATION 2: SAP Cloud-Only -- No Spurious On-Prem Zone")
    print(SEP)
    print(f"  Query: {SAP_QUERY}")
    print()

    history: list[dict] = []
    result = await architect_chat(history, SAP_QUERY)

    if result["type"] != "architecture":
        print("  ERROR: ARCHITECT did not return a DESIGN_SPEC.")
        print(f"  Reply: {result.get('reply', '')[:300]}")
        return

    spec = result["json"]
    zones = spec.get("zones", [])
    zone_types  = [z.get("type") for z in zones]
    zone_labels = [z.get("label") for z in zones]

    onprem_zones = [z for z in zones if z.get("type") == "onprem"]
    hub_zones    = [z for z in zones if z.get("type") == "hub"]
    spoke_zones  = [z for z in zones if z.get("type") == "spoke"]

    all_resources = [r for z in zones for r in z.get("resources", [])]
    vpn_resources = [
        r for r in all_resources
        if "vpn" in r.get("type", "").lower() or "vpngateway" in r.get("type", "").lower()
        or "vpn" in r.get("name", "").lower()
    ]
    expressroute_resources = [
        r for r in all_resources
        if "expressroute" in r.get("type", "").lower() or "expressroute" in r.get("name", "").lower()
    ]

    print(f"  Zones returned: {zone_labels}")
    print(f"  Zone types:     {zone_types}")
    print()
    print(f"  On-prem zones:      {len(onprem_zones)}  {'<-- SPURIOUS' if onprem_zones else '(none - correct)'}")
    if onprem_zones:
        for z in onprem_zones:
            print(f"    - {z.get('label')} [type={z.get('type')}]")
    print(f"  Hub zones:          {len(hub_zones)}  {'(hub present)' if hub_zones else '(none)'}")
    print(f"  Spoke zones:        {len(spoke_zones)}")
    print(f"  VPN resources:      {len(vpn_resources)}  {'<-- unexpected for cloud-only' if vpn_resources else '(none)'}")
    print(f"  ExpressRoute:       {len(expressroute_resources)}  {'<-- unexpected for cloud-only' if expressroute_resources else '(none)'}")

    print()
    assumptions = spec.get("assumptions", [])
    print(f"  Assumptions ({len(assumptions)}):")
    for a in assumptions:
        print(f"    - {a}")

    print()
    optionals = spec.get("optional_components", [])
    print(f"  Optional components ({len(optionals)}):")
    for o in optionals:
        print(f"    - {o.get('name')}: {o.get('question')}")

    print()
    if not onprem_zones and not vpn_resources and not expressroute_resources:
        print("  PASS: No on-prem zone, no VPN Gateway, no ExpressRoute in cloud-only SAP design.")
        print("  Grounding discipline instruction is working correctly.")
    elif onprem_zones:
        print("  FAIL: On-prem zone injected despite cloud-only request.")
        print("  Grounding discipline did not prevent over-grounding.")
    else:
        print("  PARTIAL: No on-prem zone, but unexpected connectivity resources found.")

    print()
    print(f"  Full spec title: {spec.get('title', '(none)')}")


async def main() -> None:
    print()
    print(SEP)
    print("  HYBRID RETRIEVAL VALIDATION")
    print("  (897 chunks, 400-char size, BM25 + HNSW vector, top-k=5 with diversity)")
    print(SEP)

    await validate_aks_retrieval()
    await validate_sap_grounding()

    print(f"\n{SEP}")
    print("  DONE")
    print(SEP)


if __name__ == "__main__":
    asyncio.run(main())
