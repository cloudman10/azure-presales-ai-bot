"""
scripts/ab_value_test.py

Value A/B test: for each scenario, run ARCHITECT twice (RAG off / on) and
compare DESIGN_SPECs.  Reports retrieved sources and per-scenario verdict.

Usage:
    python scripts/ab_value_test.py
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

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-8s %(message)s")

SEP  = "=" * 76
SEP2 = "-" * 76

SCENARIOS = [
    {
        "label": "AKS BASELINE",
        "scenario": (
            "AKS baseline cluster for a production microservices app on Azure, "
            "Australia East. Needs to be production-grade with proper security "
            "and observability."
        ),
        "checks": {
            "System node pool": lambda r: any(
                "system" in r.get("role", "").lower() or "system" in r.get("name", "").lower()
                for r in _all_resources(r)
            ),
            "User node pool": lambda r: sum(
                1 for res in _all_resources(r)
                if r.get("type") == "AKSCluster" or
                   ("node" in res.get("name", "").lower() and "user" in res.get("name", "").lower())
            ) > 0,
            "Azure CNI or network policy": lambda r: any(
                "cni" in res.get("role", "").lower() or
                "network policy" in res.get("role", "").lower() or
                "azure cni" in res.get("role", "").lower()
                for res in _all_resources(r)
            ) or any(
                "cni" in p.lower() or "network policy" in p.lower()
                for p in r.get("design_principles", [])
            ),
            "Ingress controller": lambda r: any(
                "ingress" in res.get("name", "").lower() or
                "ingress" in res.get("role", "").lower()
                for res in _all_resources(r)
            ),
            "Container Registry (ACR)": lambda r: any(
                "registry" in res.get("name", "").lower() or
                "acr" in res.get("name", "").lower() or
                res.get("type") in ("AzureService",) and "registry" in res.get("role", "").lower()
                for res in _all_resources(r)
            ),
            "Key Vault CSI / secrets": lambda r: any(
                "key vault" in res.get("name", "").lower() or
                res.get("type") == "KeyVault"
                for res in _all_resources(r)
            ),
            "Azure Monitor / Container Insights": lambda r: any(
                "monitor" in res.get("name", "").lower() or
                "container insight" in res.get("role", "").lower() or
                res.get("type") in ("AzureMonitor", "ApplicationInsights", "LogAnalyticsWorkspace")
                for res in _all_resources(r)
            ),
            "Private endpoint or VNet integration": lambda r: any(
                res.get("type") == "PrivateEndpoint" or
                "private" in res.get("name", "").lower()
                for res in _all_resources(r)
            ),
        },
    },
    {
        "label": "SAP S/4HANA HA",
        "scenario": (
            "SAP S/4HANA production deployment on Azure for 500 users, "
            "high availability required, Australia East."
        ),
        "checks": {
            "ASCS/ERS or Central Services": lambda r: any(
                "ascs" in res.get("name", "").lower() or
                "central services" in res.get("name", "").lower() or
                "ascs" in res.get("role", "").lower()
                for res in _all_resources(r)
            ),
            "HANA DB (M-series or SAP DB)": lambda r: any(
                "hana" in res.get("name", "").lower() or
                "hana" in res.get("role", "").lower() or
                "m-series" in res.get("role", "").lower() or
                "sap db" in res.get("name", "").lower()
                for res in _all_resources(r)
            ),
            "App Server VM": lambda r: any(
                ("app" in res.get("name", "").lower() or "application" in res.get("name", "").lower())
                and res.get("type") == "VirtualMachine"
                for res in _all_resources(r)
            ),
            "HA / Availability Zones noted": lambda r: any(
                "availab" in p.lower() or "zone" in p.lower() or "ha" in p.lower()
                for p in r.get("design_principles", []) + r.get("assumptions", [])
            ) or any(
                "ha" in res.get("role", "").lower() or
                "availab" in res.get("role", "").lower()
                for res in _all_resources(r)
            ),
            "Shared storage (NFS/ANF/Premium)": lambda r: any(
                "shared" in res.get("role", "").lower() or
                "nfs" in res.get("role", "").lower() or
                "netapp" in res.get("role", "").lower() or
                "transport" in res.get("role", "").lower()
                for res in _all_resources(r)
            ),
            "Hub VNet (mandatory for SAP HA)": lambda r: any(
                z.get("type") == "hub" for z in r.get("zones", [])
            ),
            "Load Balancer (for HA clustering)": lambda r: any(
                res.get("type") == "LoadBalancer" or
                "load balancer" in res.get("name", "").lower()
                for res in _all_resources(r)
            ),
        },
    },
    {
        "label": "HYBRID CONNECTIVITY DECISION",
        "scenario": (
            "We need to connect our on-premises data centre in Sydney to Azure. "
            "We currently have a 10 Gbps MPLS circuit. We run SQL Server databases "
            "and need very low latency for replication. Which connectivity option "
            "should we use, and design the architecture."
        ),
        "checks": {
            "ExpressRoute (not VPN) recommended": lambda r: any(
                res.get("type") == "ExpressRouteGateway" or
                "expressroute" in res.get("name", "").lower() or
                "expressroute" in res.get("role", "").lower()
                for res in _all_resources(r)
            ),
            "On-premises zone present": lambda r: any(
                z.get("type") == "onprem" for z in r.get("zones", [])
            ),
            "Hub VNet present": lambda r: any(
                z.get("type") == "hub" for z in r.get("zones", [])
            ),
            "SQL Server / database workload": lambda r: any(
                res.get("type") in ("SQLDatabase", "SQLManagedInstance", "VirtualMachine") and
                ("sql" in res.get("name", "").lower() or "sql" in res.get("role", "").lower())
                for res in _all_resources(r)
            ),
        },
    },
]


def _all_resources(spec: dict) -> list[dict]:
    return [r for z in spec.get("zones", []) for r in z.get("resources", [])]


async def run_scenario(scenario_text: str, rag: bool) -> tuple[dict | None, list[str]]:
    os.environ["RAG_ENABLED"] = "true" if rag else "false"
    history: list[dict] = []

    sources: list[str] = []
    if rag:
        ctx = await retrieve_arch_context(scenario_text)
        if ctx:
            for line in ctx.splitlines():
                line = line.strip()
                if line.startswith("[") and "|" in line and line.endswith("]"):
                    sources.append(line[1:-1])
        if not sources and ctx:
            # fallback: grab [Source: ...] lines
            for line in ctx.splitlines():
                if line.startswith("[") and "|" in line:
                    sources.append(line.strip("[]"))

    result = await architect_chat(history, scenario_text)
    if result["type"] == "architecture":
        return result["json"], sources
    return None, sources


def score_checks(spec: dict, checks: dict) -> dict[str, bool]:
    if spec is None:
        return {k: False for k in checks}
    return {label: fn(spec) for label, fn in checks.items()}


def print_scores(label_off: str, scores_off: dict, label_on: str, scores_on: dict) -> int:
    """Print check table; return count of checks improved by RAG."""
    improved = 0
    all_keys = list(scores_off.keys())
    col_w = max(len(k) for k in all_keys) + 2

    header = f"  {'Check':<{col_w}}  {'Ungrounded':^12}  {'Grounded':^12}  Delta"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for k in all_keys:
        off = scores_off[k]
        on  = scores_on[k]
        off_s = "YES" if off else "no"
        on_s  = "YES" if on  else "no"
        delta = ""
        if on and not off:
            delta = "<-- IMPROVED by RAG"
            improved += 1
        elif off and not on:
            delta = "<-- REGRESSED"
        print(f"  {k:<{col_w}}  {off_s:^12}  {on_s:^12}  {delta}")
    return improved


async def main() -> None:
    results: list[dict] = []

    for s in SCENARIOS:
        label    = s["label"]
        scenario = s["scenario"]
        checks   = s["checks"]

        print(f"\n{SEP}")
        print(f"  SCENARIO: {label}")
        print(SEP)
        print(f"  Query: {scenario}")

        print(f"\n  Running RAG_ENABLED=false...")
        spec_off, _     = await run_scenario(scenario, rag=False)
        print(f"  Running RAG_ENABLED=true...")
        spec_on, sources = await run_scenario(scenario, rag=True)

        scores_off = score_checks(spec_off, checks)
        scores_on  = score_checks(spec_on,  checks)
        improved   = print_scores("Ungrounded", scores_off, "Grounded", scores_on)

        print(f"\n  Retrieved sources:")
        if sources:
            for src in sources:
                print(f"    - {src}")
        else:
            print("    (none)")

        verdict = (
            "IMPROVED" if improved > 0
            else "NO IMPROVEMENT"
        )
        print(f"\n  Verdict: {verdict} ({improved}/{len(checks)} checks improved by RAG)")

        results.append({
            "label": label,
            "improved": improved,
            "total_checks": len(checks),
            "sources": sources,
            "spec_off": spec_off,
            "spec_on": spec_on,
        })

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  OVERALL SUMMARY")
    print(SEP)
    for r in results:
        status = "IMPROVED" if r["improved"] > 0 else "no change"
        print(f"  {r['label']:<40} {status}  ({r['improved']}/{r['total_checks']} checks)")

    any_improvement = any(r["improved"] > 0 for r in results)
    if any_improvement:
        print("\n  >> RAG shows measurable value. FULL CORPUS BUILD: PROCEED.")
    else:
        print("\n  >> RAG shows no measurable value for these scenarios.")
        print("  >> Consider: larger corpus, better chunking, or semantic/vector search.")

    # ── Full specs ────────────────────────────────────────────────────────────
    for r in results:
        print(f"\n{SEP}")
        print(f"  FULL DESIGN_SPEC [{r['label']}]  RAG_ENABLED=false")
        print(SEP)
        if r["spec_off"]:
            print(json.dumps(r["spec_off"], indent=2))
        else:
            print("  (no DESIGN_SPEC returned)")

        print(f"\n{SEP}")
        print(f"  FULL DESIGN_SPEC [{r['label']}]  RAG_ENABLED=true")
        print(SEP)
        if r["spec_on"]:
            print(json.dumps(r["spec_on"], indent=2))
        else:
            print("  (no DESIGN_SPEC returned)")


if __name__ == "__main__":
    asyncio.run(main())
