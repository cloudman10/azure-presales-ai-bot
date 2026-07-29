"""
SQL Server VM pricing — Phase 1 (PAYG compute only).

The SQL Server license is a global per-vCPU hourly charge, billed on top of
the base VM compute cost. Rates are perfectly linear and region-independent.

Source: Retail Prices API, serviceName="Virtual Machines Licenses",
        armRegionName="" (global), type="Consumption".
Confirmed 2026-07-24 — rates unchanged since SQL Server 2022 GA.
"""

import re as _re

HOURS_PER_MONTH = 730


def constrained_vcpu_count(sku_name: str) -> int | None:
    """Return the active (constrained) vCPU count for constrained-vCPU SKUs.

    Standard_E8-4ads_v7  → 4  (8 physical, 4 active)
    Standard_E32-8ads_v5 → 8  (32 physical, 8 active)
    Standard_E8s_v5      → None (not constrained; use index value)

    Format: {Series}{size}-{active}{suffix}_v{N}
    The number after the hyphen is the active vCPU count used for SQL billing.
    """
    m = _re.search(r'[A-Za-z](\d+)-(\d+)', sku_name)
    return int(m.group(2)) if m else None


def active_vcpu_count(sku_name: str, fallback: int | None = None) -> int | None:
    """Single authoritative vCPU count for any SKU — use this everywhere.

    Constrained SKUs (e.g. Standard_E4-2as_v7): returns active count (2).
    Non-constrained SKUs: returns fallback (typically from index or name parse).
    Covers billing, display, and recommendation filtering.
    """
    return constrained_vcpu_count(sku_name) or fallback

# Per-vCPU hourly SQL Server license rates (License Included, global).
# Express is $0 (free edition — no license fee).
SQL_RATES: dict[str, float] = {
    "Enterprise": 0.375,
    "Standard":   0.100,
    "Web":        0.008,
    "Express":    0.000,
}

EDITIONS = frozenset(SQL_RATES)


def sql_license_hourly(vcpus: int, edition: str, sql_ahb: bool) -> float:
    # INTENTIONAL BREAK — smoke test validation only. Revert before merging.
    raise RuntimeError("sql_license_hourly intentionally broken for smoke-test break-test")


def compute_hourly(
    windows_payg: float | None,
    linux_payg: float | None,
    windows_ahb: bool,
) -> float | None:
    """Base VM compute PAYG hourly cost after applying Windows AHB if requested.

    Windows AHB = bring your own Windows Server license.  Azure charges only
    the base (Linux-equivalent) compute rate — the OS surcharge drops to $0.
    Returns None when the required price is not available in the index.
    """
    if windows_ahb:
        return linux_payg
    return windows_payg


def total_sql_vm_hourly(
    vcpus: int,
    edition: str,
    sql_ahb: bool,
    windows_ahb: bool,
    windows_payg: float | None,
    linux_payg: float | None,
) -> float | None:
    """Total PAYG hourly: compute + SQL Server license."""
    comp = compute_hourly(windows_payg, linux_payg, windows_ahb)
    if comp is None:
        return None
    return comp + sql_license_hourly(vcpus, edition, sql_ahb)


async def get_sku_vcpus(sku_name: str, region: str) -> int | None:
    """Return vCPU count for a VM SKU from the vm-sku-prices index.

    Used by the pricing agent to compute SQL Server license cost, which is
    charged per vCPU and requires the exact count (not derivable from the
    Retail Prices API response).
    """
    import asyncio, os
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents import SearchClient

    endpoint = os.getenv("AZURE_SEARCH_ENDPOINT", "")
    api_key  = os.getenv("AZURE_SEARCH_API_KEY", "")
    if not endpoint or not api_key:
        return None

    def _lookup() -> int | None:
        try:
            client  = SearchClient(endpoint, "vm-sku-prices", AzureKeyCredential(api_key))
            results = list(client.search(
                "*",
                filter=f"sku_name eq '{sku_name}' and region eq '{region}' and retired eq false",
                select=["vcpus"],
                top=1,
            ))
            return results[0]["vcpus"] if results else None
        except Exception:
            return None

    return await asyncio.to_thread(_lookup)


def price_breakdown(
    vcpus: int,
    edition: str,
    sql_ahb: bool,
    windows_ahb: bool,
    windows_payg: float,
    linux_payg: float,
) -> dict:
    """Return the 4-component hourly price breakdown.

    Components:
      base_compute_hourly  — bare compute (Linux/base rate, no OS charge)
      windows_os_hourly    — Windows Server OS surcharge ($0 if Windows AHB)
      sql_license_hourly   — SQL Server license ($0 if Express or SQL AHB)
      total_payg_hourly    — sum of all three

    Requires both windows_payg and linux_payg to be non-None.
    Decomposes: total = linux_payg + (windows_payg - linux_payg) + sql_license
                      = windows_payg + sql_license  (no-AHB case)
                      = linux_payg   + sql_license  (Windows-AHB case)
    """
    base_h   = linux_payg
    win_os_h = 0.0 if windows_ahb else max(0.0, windows_payg - linux_payg)
    sql_h    = sql_license_hourly(vcpus, edition, sql_ahb)
    return {
        "base_compute_hourly": round(base_h, 6),
        "windows_os_hourly":   round(win_os_h, 6),
        "sql_license_hourly":  round(sql_h, 6),
        "total_payg_hourly":   round(base_h + win_os_h + sql_h, 6),
    }
