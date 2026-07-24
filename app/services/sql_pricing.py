"""
SQL Server VM pricing — Phase 1 (PAYG compute only).

The SQL Server license is a global per-vCPU hourly charge, billed on top of
the base VM compute cost. Rates are perfectly linear and region-independent.

Source: Retail Prices API, serviceName="Virtual Machines Licenses",
        armRegionName="" (global), type="Consumption".
Confirmed 2026-07-24 — rates unchanged since SQL Server 2022 GA.
"""

HOURS_PER_MONTH = 730

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
    """SQL Server license component of the total hourly cost.

    AHB (Azure Hybrid Benefit) = BYOL — $0 license charge from Azure.
    Express edition is always free.
    Minimum billing unit: 4 vCPUs for VMs with 1–2 vCPUs (confirmed from
    the API's "1-4 vCPU VM" bucket which equals 4 × per-vCPU rate).
    VMs with 3+ vCPUs are billed at exact count × per-vCPU rate.
    """
    if sql_ahb or edition == "Express":
        return 0.0
    rate = SQL_RATES.get(edition, 0.0)
    billed_vcpus = 4 if vcpus <= 2 else vcpus
    return billed_vcpus * rate


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
