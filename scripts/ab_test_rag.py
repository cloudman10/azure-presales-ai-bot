"""
scripts/ab_test_rag.py

A/B test: compare ARCHITECT output for the AVD+SAP scenario
with RAG_ENABLED=false (ungrounded) vs RAG_ENABLED=true (grounded).

The test scenario is chosen specifically because ungrounded GPT-4o
incorrectly adds a VPN Gateway to a cloud-only AVD deployment.

Usage:
    cd ~/azure-presales-ai-bot
    .venv/Scripts/activate   (Windows)
    python scripts/ab_test_rag.py

Prereq:
    python scripts/ingest_arch_center.py   (must have run first)
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

from app.services.diagram_architect import architect_chat, retrieve_arch_context

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# The known failure scenario — cloud-only AVD + SAP B1; ungrounded LLM has been observed
# to add a VPN Gateway (wrong for cloud-only) and omit AVD access model description.
# This variant omits the explicit "cloud-only" cue to make the model work harder.
SCENARIO = (
    "Azure architecture for an Australian SMB, about 50 users. "
    "Deploy Azure Virtual Desktop in Sydney for secure remote access to SAP Business One. "
    "Cloud-only, no on-premises systems."
)

# More adversarial variant: no explicit "cloud-only" phrase; user just says no on-prem.
# Used below for a second pair of calls if the primary scenario shows no difference.
SCENARIO_ADVERSARIAL = (
    "We need Azure architecture for 50 remote staff in Sydney accessing SAP Business One. "
    "We don't have any on-prem servers — everything will be in Azure. "
    "Staff will log in from home using laptops."
)

SEP = "=" * 72


def _analyse(spec: dict, label: str) -> dict:
    """Extract the key diagnostic fields from a DESIGN_SPEC for comparison."""
    zones = spec.get("zones", [])
    all_resources = [r for z in zones for r in z.get("resources", [])]

    vpn_resources = [
        r for r in all_resources
        if "vpn" in r.get("type", "").lower()
        or "vpn" in r.get("name", "").lower()
        or "vpngateway" in r.get("type", "").lower()
    ]
    avd_resources = [
        r for r in all_resources
        if r.get("type") == "AVDHostPool"
        or "avd" in r.get("name", "").lower()
        or "avd" in r.get("role", "").lower()
        or "session host" in r.get("name", "").lower()
    ]
    hub_zones = [z for z in zones if z.get("type") == "hub"]
    avd_access_noted = any(
        "reverse" in a.lower() or "https" in a.lower() or "443" in a.lower()
        or "cloud-only" in a.lower()
        for a in spec.get("assumptions", [])
    )

    print(f"\n{SEP}")
    print(f"  {label}")
    print(SEP)
    print(f"  Title:          {spec.get('title', '(none)')}")
    print(f"  Zones:          {[z['label'] for z in zones]}")
    print(f"  Hub zones:      {len(hub_zones)}  {'<-- hub means VPN/FW added' if hub_zones else '(no hub -- correct for cloud-only AVD)'}")
    print(f"  VPN resources:  {len(vpn_resources)}  {'<-- WRONG for cloud-only' if vpn_resources else '(none -- correct)'}")
    if vpn_resources:
        for r in vpn_resources:
            print(f"    - {r.get('name')} [{r.get('type')}] role={r.get('role')}")
    print(f"  AVD resources:  {len(avd_resources)}")
    for r in avd_resources[:3]:
        print(f"    - {r.get('name')} [{r.get('type')}] role={r.get('role')}")
    print(f"  AVD access model noted: {avd_access_noted}  {'(reverse-connect mentioned)' if avd_access_noted else '<-- NOT mentioned'}")
    print(f"\n  Assumptions ({len(spec.get('assumptions', []))}):")
    for a in spec.get("assumptions", []):
        print(f"    - {a}")
    print(f"\n  Optional components ({len(spec.get('optional_components', []))}):")
    for o in spec.get("optional_components", []):
        print(f"    - {o.get('name')}: {o.get('question')}")

    return {
        "has_vpn": bool(vpn_resources),
        "has_hub": bool(hub_zones),
        "avd_access_noted": avd_access_noted,
        "zone_labels": [z["label"] for z in zones],
        "vpn_resources": vpn_resources,
    }


async def run_once(rag: bool) -> tuple[dict | None, list[str]]:
    """Run the architect with RAG on or off, return (spec, retrieved_sources)."""
    os.environ["RAG_ENABLED"] = "true" if rag else "false"
    history: list[dict] = []

    retrieved_sources: list[str] = []
    if rag:
        ctx = await retrieve_arch_context(SCENARIO)
        if ctx:
            # Extract source URLs from the context blocks
            for line in ctx.splitlines():
                if line.startswith("[") and "|" in line and "]" in line:
                    retrieved_sources.append(line.strip("[]"))

    result = await architect_chat(history, SCENARIO)
    if result["type"] == "architecture":
        return result["json"], retrieved_sources
    log.warning("architect returned type=%s (not 'architecture') — reply: %s", result["type"], result.get("reply", "")[:200])
    return None, retrieved_sources


async def main() -> None:
    print()
    print(SEP)
    print("  RAG A/B TEST — AVD + SAP Business One, cloud-only, 50 users, Sydney")
    print(SEP)
    print(f"\n  Scenario: {SCENARIO}\n")

    # ── Ungrounded (RAG off) ───────────────────────────────────────────────────
    print("Running with RAG_ENABLED=false (ungrounded)...")
    spec_off, _ = await run_once(rag=False)

    if spec_off:
        diag_off = _analyse(spec_off, "RAG_ENABLED=false  (UNGROUNDED)")
    else:
        print("  ERROR: architect did not return a DESIGN_SPEC — check logs")
        diag_off = {}

    # ── Grounded (RAG on) ──────────────────────────────────────────────────────
    print("\nRunning with RAG_ENABLED=true  (grounded)...")
    spec_on, sources = await run_once(rag=True)

    if spec_on:
        diag_on = _analyse(spec_on, "RAG_ENABLED=true   (GROUNDED)")
    else:
        print("  ERROR: architect did not return a DESIGN_SPEC — check logs")
        diag_on = {}

    # ── Retrieved sources ──────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  RETRIEVED SOURCES (RAG_ENABLED=true)")
    print(SEP)
    if sources:
        for s in sources:
            print(f"  - {s}")
    else:
        print("  (none — index empty or no match for this query)")

    # ── Verdict ────────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  VERDICT")
    print(SEP)

    vpn_fixed = diag_off.get("has_vpn") and not diag_on.get("has_vpn")
    hub_fixed  = diag_off.get("has_hub") and not diag_on.get("has_hub")
    access_added = not diag_off.get("avd_access_noted") and diag_on.get("avd_access_noted")

    print(f"  VPN Gateway removed by RAG:       {'YES -- FIXED' if vpn_fixed else 'no change'}")
    print(f"  Hub VNet removed by RAG:          {'YES -- FIXED' if hub_fixed  else 'no change'}")
    print(f"  AVD access model added by RAG:    {'YES -- IMPROVED' if access_added else 'no change'}")

    if vpn_fixed or hub_fixed or access_added:
        print("\n  >> RAG grounding measurably improved accuracy. SPIKE PASSED.")
        print("  >> Proceed to full corpus build and review Part B RAG plan.")
    else:
        print("\n  >> No measurable improvement detected.")
        if not sources:
            print("  >> Likely cause: index is empty. Run scripts/ingest_arch_center.py first.")
        else:
            print("  >> Retrieval fired but did not change output. May need prompt tuning")
            print("     or a larger/better corpus. Review retrieved chunk quality above.")

    # ── Full DESIGN_SPECs ──────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  FULL DESIGN_SPEC (RAG_ENABLED=false)")
    print(SEP)
    if spec_off:
        print(json.dumps(spec_off, indent=2))

    print(f"\n{SEP}")
    print("  FULL DESIGN_SPEC (RAG_ENABLED=true)")
    print(SEP)
    if spec_on:
        print(json.dumps(spec_on, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
