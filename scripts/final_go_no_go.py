"""
scripts/final_go_no_go.py

FINAL go/no-go A/B test for the RAG spike.

Part 0: show retrieved chunks for AKS query after query expansion.
Part A: AKS baseline -- grounded vs ungrounded; does RAG add node-pool/CNI/ACR specifics?
Part B: Azure OpenAI landing zone (enterprise) -- does RAG help a long-tail scenario?

Verdict: if grounded clearly wins on >=1 scenario, recommend FULL CORPUS BUILD.
         Otherwise recommend PIVOT to prompt-checklist hybrid.

Usage:
    python scripts/final_go_no_go.py
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

SEP  = "=" * 80
SEP2 = "-" * 80

AKS_SCENARIO = (
    "AKS baseline cluster for a production microservices app on Azure, "
    "Australia East. Needs to be production-grade with proper security and observability."
)

OPENAI_SCENARIO = (
    "Azure OpenAI landing zone for an enterprise. We need to host a GPT-4o deployment "
    "privately with no internet exposure, connect it to our internal applications, and "
    "ensure compliance with data residency in Australia. Expected: 100 concurrent developers."
)


def _all_resources(spec: dict) -> list[dict]:
    return [r for z in spec.get("zones", []) for r in z.get("resources", [])]


def _field_text(spec: dict) -> str:
    """Flatten all text fields for keyword scanning."""
    parts: list[str] = []
    for r in _all_resources(spec):
        parts.extend([r.get("name", ""), r.get("role", ""), r.get("type", "")])
    parts.extend(spec.get("design_principles", []))
    parts.extend(spec.get("assumptions", []))
    for ss in spec.get("shared_services", []):
        parts.extend([ss.get("name", ""), ss.get("purpose", "")])
    return " ".join(parts).lower()


# ── AKS-specific checks ────────────────────────────────────────────────────────

AKS_CHECKS: dict[str, callable] = {
    "System node pool":           lambda t: "system node pool" in t or ("system" in t and "node" in t),
    "User node pool":             lambda t: "user node pool" in t or ("user" in t and "node pool" in t),
    "Azure CNI / network plugin": lambda t: "azure cni" in t or "cni" in t or "network plugin" in t,
    "Ingress controller":         lambda t: "ingress controller" in t or ("ingress" in t and "nginx" in t),
    "Container Registry (ACR)":   lambda t: "container registry" in t or "acr" in t,
    "Key Vault / CSI secrets":    lambda t: "key vault" in t or "csi" in t or "keyvault" in t,
    "Azure Monitor / Container Insights": lambda t: "container insights" in t or "log analytics" in t or "azure monitor" in t,
    "Private cluster / endpoint": lambda t: "private cluster" in t or ("private" in t and "endpoint" in t),
    "Network policy":             lambda t: "network policy" in t,
}

# ── OpenAI-specific checks ─────────────────────────────────────────────────────

OPENAI_CHECKS: dict[str, callable] = {
    "Private endpoint for OpenAI":    lambda t: "private endpoint" in t or "privateendpoint" in t,
    "No public internet exposure":    lambda t: "no public" in t or "private only" in t or "internet-facing" not in t,
    "VNet integration":               lambda t: "vnet" in t or "virtual network" in t or "subnet" in t,
    "API Management or gateway":      lambda t: "api management" in t or "apim" in t,
    "Private DNS zone":               lambda t: "private dns" in t or "privatednz" in t or "dns zone" in t,
    "Content filtering / responsible AI": lambda t: "content filter" in t or "responsible ai" in t or "content policy" in t,
    "Azure Monitor / logging":        lambda t: "monitor" in t or "log analytics" in t or "diagnostics" in t,
}


async def run_once(scenario: str, rag: bool) -> tuple[dict | None, list[str], str]:
    """Run architect once; return (spec, retrieved_sources, retrieved_chunks_text)."""
    os.environ["RAG_ENABLED"] = "true" if rag else "false"
    history: list[dict] = []

    sources: list[str] = []
    chunks_text = ""
    if rag:
        ctx = await retrieve_arch_context(scenario)
        chunks_text = ctx
        if ctx:
            for line in ctx.splitlines():
                if line.startswith("[") and "|" in line and line.endswith("]"):
                    sources.append(line[1:-1])

    result = await architect_chat(history, scenario)
    if result["type"] == "architecture":
        return result["json"], sources, chunks_text
    return None, sources, chunks_text


def score(spec: dict | None, checks: dict[str, callable]) -> dict[str, bool]:
    if spec is None:
        return {k: False for k in checks}
    t = _field_text(spec)
    return {k: fn(t) for k, fn in checks.items()}


def print_comparison(checks: dict, scores_off: dict, scores_on: dict) -> tuple[int, int]:
    improved = regressed = 0
    col = max(len(k) for k in checks) + 2
    print(f"  {'Check':<{col}}  {'Ungrounded':^12}  {'Grounded':^12}  Delta")
    print("  " + "-" * (col + 34))
    for k in checks:
        off_s = "YES" if scores_off[k] else "no"
        on_s  = "YES" if scores_on[k]  else "no"
        delta = ""
        if scores_on[k] and not scores_off[k]:
            delta = "<-- ADDED by RAG"
            improved += 1
        elif scores_off[k] and not scores_on[k]:
            delta = "<-- REGRESSED"
            regressed += 1
        print(f"  {k:<{col}}  {off_s:^12}  {on_s:^12}  {delta}")
    return improved, regressed


def show_chunks(ctx: str, max_chunks: int = 5) -> None:
    if not ctx:
        print("  (no context retrieved)")
        return
    chunks = ctx.split("\n\n---\n\n")[:max_chunks]
    for i, chunk in enumerate(chunks, 1):
        lines = chunk.splitlines()
        header = lines[0] if lines else "(no header)"
        # Show up to 4 content lines
        body_lines = [ln for ln in lines[1:] if ln.strip()][:4]
        body = "\n    ".join(body_lines) if body_lines else "(empty)"
        print(f"  [{i}] {header}")
        print(f"    {body}")
        print()


async def run_scenario(
    label: str,
    scenario: str,
    checks: dict[str, callable],
) -> dict:
    print(f"\n{SEP}")
    print(f"  SCENARIO {label}: {scenario[:80]}...")
    print(SEP)

    # --- Retrieval preview (with expansion) ---
    print("\n  STEP 0: Retrieved chunks (grounded, with query expansion)")
    print(SEP2)
    ctx = await retrieve_arch_context(scenario)
    show_chunks(ctx)

    # Check which AKS/OpenAI keywords appear in retrieved chunks
    ctx_lower = ctx.lower()
    kw_hits = [k for k, fn in checks.items() if fn(ctx_lower)]
    print(f"  Check keywords found in retrieved chunks: {len(kw_hits)}/{len(checks)}")
    for k in kw_hits:
        print(f"    + {k}")
    missing = [k for k in checks if k not in kw_hits]
    if missing:
        print(f"  Still missing from chunks:")
        for k in missing:
            print(f"    - {k}")

    # --- Ungrounded ---
    print(f"\n  STEP 1: RAG_ENABLED=false (ungrounded)")
    print(SEP2)
    spec_off, _, _ = await run_once(scenario, rag=False)
    scores_off = score(spec_off, checks)
    passed_off = sum(scores_off.values())
    print(f"  Ungrounded: {passed_off}/{len(checks)} checks pass")
    zones_off = [z.get("label") for z in (spec_off or {}).get("zones", [])]
    print(f"  Zones: {zones_off}")

    # --- Grounded ---
    print(f"\n  STEP 2: RAG_ENABLED=true (grounded)")
    print(SEP2)
    spec_on, sources, chunks_on = await run_once(scenario, rag=True)
    scores_on = score(spec_on, checks)
    passed_on = sum(scores_on.values())
    print(f"  Grounded: {passed_on}/{len(checks)} checks pass")
    zones_on = [z.get("label") for z in (spec_on or {}).get("zones", [])]
    print(f"  Zones: {zones_on}")
    if sources:
        print(f"  Retrieved sources:")
        for s in sources:
            print(f"    - {s}")

    # --- Comparison ---
    print(f"\n  COMPARISON")
    print(SEP2)
    improved, regressed = print_comparison(checks, scores_off, scores_on)

    # --- Verdict ---
    print()
    if improved > 0 and regressed == 0:
        verdict = f"GROUNDED WINS (+{improved} checks, 0 regressions)"
    elif improved > 0 and regressed > 0:
        verdict = f"MIXED (grounded added {improved}, regressed {regressed})"
    elif improved == 0 and regressed == 0:
        verdict = "NO DIFFERENCE"
    else:
        verdict = f"GROUNDED WORSE (0 improvements, {regressed} regressions)"

    print(f"  Verdict for {label}: {verdict}")
    print(f"  Ungrounded: {passed_off}/{len(checks)}  Grounded: {passed_on}/{len(checks)}")

    return {
        "label": label,
        "improved": improved,
        "regressed": regressed,
        "scores_off": scores_off,
        "scores_on": scores_on,
        "spec_off": spec_off,
        "spec_on": spec_on,
        "sources": sources,
        "verdict": verdict,
    }


async def main() -> None:
    print()
    print(SEP)
    print("  FINAL GO/NO-GO A/B TEST")
    print("  Index: arch-center-spike | 897 chunks | 400-char | hybrid BM25+vector | top-k=5")
    print("  Query expansion: active (workload-detected technical term injection)")
    print(SEP)

    result_a = await run_scenario("A (AKS)",       AKS_SCENARIO,    AKS_CHECKS)
    result_b = await run_scenario("B (OpenAI LZ)", OPENAI_SCENARIO, OPENAI_CHECKS)

    # ── Overall summary ────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  OVERALL SUMMARY")
    print(SEP)
    for r in [result_a, result_b]:
        print(f"  {r['label']:<18}  {r['verdict']}")

    any_win = result_a["improved"] > 0 or result_b["improved"] > 0
    clean_wins = (result_a["improved"] > 0 and result_a["regressed"] == 0) or \
                 (result_b["improved"] > 0 and result_b["regressed"] == 0)

    print()
    if clean_wins:
        print("  >> DECISION: GROUNDED WINS on >= 1 scenario with no regressions.")
        print("  >> RECOMMENDATION: FULL CORPUS BUILD — proceed to Phase 2.")
        print("  >> Next: expand corpus beyond spike set, run long-tail value test.")
    elif any_win:
        print("  >> DECISION: Mixed result — RAG helps but also hurts in some cases.")
        print("  >> RECOMMENDATION: Fix regressions first (tighten grounding discipline),")
        print("  >> then re-evaluate before full corpus build.")
    else:
        print("  >> DECISION: Grounded does NOT beat ungrounded on either scenario.")
        print("  >> RECOMMENDATION: PIVOT to prompt-checklist hybrid.")
        print("  >> RAG infrastructure is working but retrieval content not sufficient.")

    # ── Full DESIGN_SPECs ─────────────────────────────────────────────────────
    for r in [result_a, result_b]:
        print(f"\n{SEP}")
        print(f"  FULL DESIGN_SPEC [{r['label']}] RAG_ENABLED=false")
        print(SEP)
        print(json.dumps(r["spec_off"], indent=2) if r["spec_off"] else "  (none)")
        print(f"\n{SEP}")
        print(f"  FULL DESIGN_SPEC [{r['label']}] RAG_ENABLED=true")
        print(SEP)
        print(json.dumps(r["spec_on"], indent=2) if r["spec_on"] else "  (none)")


if __name__ == "__main__":
    asyncio.run(main())
