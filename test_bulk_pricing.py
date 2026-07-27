"""
Standalone test script for bulk VM pricing.

Usage:
    python test_bulk_pricing.py <path-to-xlsx>

Outputs:
    - Summary table printed to stdout
    - azure-vm-bulk-pricing.xlsx written to the current directory

Performance note:
    fetch_vm_prices_for_region is patched here to cache by (region, os_type)
    so the full ~15-page catalog is fetched at most once per region/OS combo
    (typically 2-4 fetches total for a mixed AU/USA Windows/Linux sheet).
"""

import asyncio
import os
import sys

# ── Load .env before any app imports ─────────────────────────────────────────
for _line in open(os.path.join(os.path.dirname(__file__), ".env")):
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip())

# ── Patch fetch_vm_prices_for_region BEFORE any advisor imports ───────────────
# This ensures the expensive "fetch ALL VMs for region/OS" call is cached,
# so _pick_vms_from_prices re-uses the same list for every unique (vcpus, ram)
# combination in the same region/OS.
import app.services.azure_pricing as _az

_vm_prices_cache: dict = {}
_orig_fetch_vm_prices = _az.fetch_vm_prices_for_region


async def _cached_fetch_vm_prices_for_region(region: str, os_type: str, **kwargs):
    key = (region, os_type)
    if key not in _vm_prices_cache:
        print(f"  [API] Fetching all {os_type} VM prices for {region} ...", flush=True)
        _vm_prices_cache[key] = await _orig_fetch_vm_prices(region, os_type, **kwargs)
        print(f"         → {len(_vm_prices_cache[key])} records cached", flush=True)
    else:
        print(f"  [cache] {os_type}/{region} ({len(_vm_prices_cache[key])} records)", flush=True)
    return _vm_prices_cache[key]


_az.fetch_vm_prices_for_region = _cached_fetch_vm_prices_for_region

# ── Now safe to import app services ──────────────────────────────────────────
from app.services.bulk_pricing import (
    generate_bulk_excel,
    parse_inventory_xlsx,
    process_inventory,
)


# ── Formatting helpers ────────────────────────────────────────────────────────

def _trunc(s: str, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _money(v) -> str:
    if v is None:
        return "     —"
    return f"{v:>10,.2f}"


def _print_table(rows: list[dict]):
    """Print a compact summary table to stdout."""
    # Column widths
    W = {
        "num":   4,  "name": 30, "region": 15, "sku": 22,
        "vcpu":  4,  "ram":   7, "os":      8, "sql": 12,
        "disk": 11,  "total": 11,
    }

    def _sep():
        print(
            "+" + "+".join("-" * (w + 2) for w in W.values()) + "+",
            flush=True,
        )

    def _row(*vals):
        parts = []
        for val, (_, w) in zip(vals, W.items()):
            s = str(val) if val is not None else ""
            parts.append(f" {s:<{w}} ")
        print("|" + "|".join(parts) + "|", flush=True)

    _sep()
    _row("#", "VM Name", "Region", "Resolved SKU", "vCPU", "RAM GB",
         "OS", "SQL Edition", "OS Disk", "Total/mo")
    _sep()
    for i, r in enumerate(rows, start=1):
        _row(
            i,
            _trunc(r["vm_name"], W["name"]),
            _trunc(r["region_label"], W["region"]),
            _trunc(r["sku_name"], W["sku"]),
            r["matched_vcpus"],
            r.get("matched_ram_gb", "?"),
            r["os_type"],
            _trunc(r["sql_display"] or "—", W["sql"]),
            f"{r['ssd_tier']}/{r['ssd_gib']}G",
            f"${r['monthly_total']:>8,.2f}",
        )
    _sep()


def _print_detail(rows: list[dict]):
    """Print full cost breakdown per VM."""
    hdr = (
        f"{'#':>4}  {'VM Name':<28}  {'SKU':<22}  "
        f"{'Compute':>10}  {'OS Lic':>9}  {'SQL Lic':>9}  {'Disk':>9}  "
        f"{'PAYG':>10}  {'1yr RI':>10}  {'3yr RI':>10}"
    )
    print(hdr, flush=True)
    print("-" * len(hdr), flush=True)
    for i, r in enumerate(rows, start=1):
        print(
            f"{i:>4}  {_trunc(r['vm_name'], 28):<28}  {_trunc(r['sku_name'], 22):<22}  "
            f"${r['monthly_compute']:>9,.2f}  "
            f"${r['monthly_os_license']:>8,.2f}  "
            f"${r['monthly_sql']:>8,.2f}  "
            f"${r['monthly_disk']:>8,.2f}  "
            f"${r['monthly_total']:>9,.2f}  "
            f"${r['monthly_total_ri1']:>9,.2f}  "
            f"${r['monthly_total_ri3']:>9,.2f}",
            flush=True,
        )
    print("-" * len(hdr), flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(xlsx_path: str) -> None:
    print(f"\n{'='*70}", flush=True)
    print(f"  BULK VM PRICING  —  {os.path.basename(xlsx_path)}", flush=True)
    print(f"{'='*70}\n", flush=True)

    with open(xlsx_path, "rb") as f:
        file_bytes = f.read()

    # ── Parse pass (no network) ───────────────────────────────────────────────
    print("Parsing inventory sheet ...", flush=True)
    rows, parse_warnings = parse_inventory_xlsx(file_bytes)
    print(f"  → {len(rows)} valid VM rows found\n", flush=True)

    if not rows:
        print("No rows to price. Exiting.", flush=True)
        return

    # Show the unique spec combinations that will hit the API
    from collections import Counter
    specs = Counter(
        (r["region_label"], r["os_type"], r["vcpus_req"], r["ram_gb_req"])
        for r in rows
    )
    print(f"Unique (region, OS, vCPU, RAM) combos: {len(specs)}", flush=True)
    for (reg, os_, v, m), cnt in sorted(specs.items()):
        print(f"  {reg:16s} {os_:8s} {v:3d} vCPU / {m:5d} GB  ×{cnt}", flush=True)

    # ── Pricing pass (network calls) ──────────────────────────────────────────
    print(f"\nFetching prices (API calls will appear below) ...", flush=True)
    result = await process_inventory(file_bytes)

    priced_rows = result["rows"]
    totals      = result["totals"]
    warnings    = result["warnings"]

    # ── Summary table ─────────────────────────────────────────────────────────
    print(f"\n{'─'*70}", flush=True)
    print("  SUMMARY", flush=True)
    print(f"{'─'*70}", flush=True)
    _print_table(priced_rows)

    # ── Detailed cost breakdown ───────────────────────────────────────────────
    print(f"\n{'─'*70}", flush=True)
    print("  COST BREAKDOWN (monthly USD, PAYG, License-Included)", flush=True)
    print(f"{'─'*70}", flush=True)
    _print_detail(priced_rows)

    # ── Grand total ───────────────────────────────────────────────────────────
    print(f"\n{'═'*70}", flush=True)

    def _pct_save(base: float, discounted: float) -> str:
        if base <= 0:
            return " —"
        return f"save {(base - discounted) / base * 100:.0f}%"

    payg = totals['monthly_total']
    ri1  = totals['monthly_total_ri1']
    ri3  = totals['monthly_total_ri3']
    print(
        f"  GRAND TOTAL ({result['vm_count']} VMs)\n"
        f"    Compute (PAYG): ${totals['monthly_compute']:>12,.2f}/mo\n"
        f"    OS License:     ${totals['monthly_os_license']:>12,.2f}/mo\n"
        f"    SQL License:    ${totals['monthly_sql']:>12,.2f}/mo\n"
        f"    Disk:           ${totals['monthly_disk']:>12,.2f}/mo\n"
        f"    {'─'*43}\n"
        f"    PAYG total:     ${payg:>12,.2f}/mo   "
        f"Annual: ${totals['annual_total']:>13,.2f}/yr\n"
        f"    1yr RI total:   ${ri1:>12,.2f}/mo   "
        f"Annual: ${totals['annual_total_ri1']:>13,.2f}/yr   ({_pct_save(payg, ri1)} vs PAYG)\n"
        f"    3yr RI total:   ${ri3:>12,.2f}/mo   "
        f"Annual: ${totals['annual_total_ri3']:>13,.2f}/yr   ({_pct_save(payg, ri3)} vs PAYG)",
        flush=True,
    )
    print(f"{'═'*70}\n", flush=True)
    print(
        "Note: OS disk only (no data disks). Standard SSD LRS. PAYG + RI rates.\n"
        "      RI discount on compute only — OS/SQL license at PAYG rate.\n"
        "      Azure Hybrid Benefit NOT applied (License-Included for all rows).",
        flush=True,
    )

    # ── Spot-check: row 1 RI math ─────────────────────────────────────────────
    if priced_rows:
        r1 = priced_rows[0]

        def _pct(base, val):
            return f"-{(base - val) / base * 100:.1f}%" if base > 0 else "n/a"

        print(f"{'─'*70}", flush=True)
        print(f"  SPOT-CHECK: Row 1 — {r1['vm_name']} ({r1['sku_name']})", flush=True)
        print(f"{'─'*70}", flush=True)
        payg_c = r1['monthly_compute']
        ri1_c  = r1['monthly_compute_ri1']
        ri3_c  = r1['monthly_compute_ri3']
        print(
            f"    Compute PAYG:    ${payg_c:>9,.2f}/mo\n"
            f"    Compute 1yr RI:  ${ri1_c:>9,.2f}/mo  ({_pct(payg_c, ri1_c)})\n"
            f"    Compute 3yr RI:  ${ri3_c:>9,.2f}/mo  ({_pct(payg_c, ri3_c)})\n"
            f"    OS License:      ${r1['monthly_os_license']:>9,.2f}/mo  (PAYG, unchanged)\n"
            f"    SQL License:     ${r1['monthly_sql']:>9,.2f}/mo  (PAYG, unchanged)\n"
            f"    Disk:            ${r1['monthly_disk']:>9,.2f}/mo  (unchanged)\n"
            f"    {'─'*35}\n"
            f"    Total PAYG:      ${r1['monthly_total']:>9,.2f}/mo\n"
            f"    Total 1yr RI:    ${r1['monthly_total_ri1']:>9,.2f}/mo  ({_pct(r1['monthly_total'], r1['monthly_total_ri1'])})\n"
            f"    Total 3yr RI:    ${r1['monthly_total_ri3']:>9,.2f}/mo  ({_pct(r1['monthly_total'], r1['monthly_total_ri3'])})",
            flush=True,
        )
        print(f"{'─'*70}\n", flush=True)

    # ── Sanity bound: matched SKU must not wildly exceed the requested spec ──────
    # Active vCPUs <= max(4 × requested, 8); matched RAM <= 8 × requested.
    # ND128isr (128 vCPU for 4 vCPU req = 32×) would break this; E8ads_v7 (2×) is fine.
    sanity_failures = []
    from app.services.sql_pricing import active_vcpu_count as _avc
    for r in priced_rows:
        phys  = r.get("matched_vcpus") or r["vcpus_req"]
        active = _avc(r["sku_name"], phys) or phys
        max_active = max(r["vcpus_req"] * 4, 8)
        if active > max_active:
            sanity_failures.append(
                f"{r['vm_name']}: active_vcpus={active} > {max_active} (4× req={r['vcpus_req']})"
            )
        mram = r.get("matched_ram_gb")
        if mram and mram > r["ram_gb_req"] * 8:
            sanity_failures.append(
                f"{r['vm_name']}: matched_ram={mram} GB > 8× req={r['ram_gb_req']} GB"
            )
    if sanity_failures:
        print(f"\n{'!'*70}", flush=True)
        print("  SANITY FAILURES (matched SKU wildly exceeds spec)", flush=True)
        print(f"{'!'*70}", flush=True)
        for msg in sanity_failures:
            print(f"  FAIL  {msg}", flush=True)
        raise AssertionError(f"{len(sanity_failures)} sanity failure(s) — fix SKU matching before deploying")
    else:
        print("\n  SANITY CHECK PASSED: all matched SKUs within 4× requested vCPUs / 8× requested RAM", flush=True)

    # ── Warnings ──────────────────────────────────────────────────────────────
    if warnings:
        print(f"\n{'─'*70}", flush=True)
        print(f"  WARNINGS ({len(warnings)})", flush=True)
        print(f"{'─'*70}", flush=True)
        for w in warnings:
            print(f"  ⚠  {w}", flush=True)

    # ── Excel export ──────────────────────────────────────────────────────────
    import time
    xlsx_bytes = generate_bulk_excel(result)
    base_dir = os.path.dirname(xlsx_path)
    for candidate in ["azure-vm-bulk-pricing.xlsx",
                      f"azure-vm-bulk-pricing-{int(time.time())}.xlsx"]:
        out_path = os.path.join(base_dir, candidate)
        try:
            with open(out_path, "wb") as f:
                f.write(xlsx_bytes)
            print(f"\n  Excel report written → {out_path}", flush=True)
            break
        except PermissionError:
            print(f"  [skip] {out_path} is locked — trying next name", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_bulk_pricing.py <path-to-inventory.xlsx>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
