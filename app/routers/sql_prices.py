"""
app/routers/sql_prices.py

SQL Server VM pricing — Phase 1 (PAYG compute only).
GET /api/sql-vm-prices   — 4-part breakdown of VM cost when running SQL Server

Query params:
  region        Azure region slug (default: australiaeast)
  edition       Enterprise | Standard | Web | Express  (default: Enterprise)
  sql_ahb       bool — SQL Server AHB (BYOL → $0 SQL license from Azure)
  windows_ahb   bool — Windows Server AHB (compute billed at Linux base rate)
  vcpus_min     int  — lower bound on vCPU count
  vcpus_max     int  — upper bound on vCPU count
  ram_min       int  — minimum RAM in GiB
  top           int  — max SKUs returned (sorted by total_payg_monthly asc)

Each result row shows a 4-component hourly breakdown:
  base_compute_hourly  — bare compute (Linux/base rate, no OS charge)
  windows_os_hourly    — Windows Server license surcharge ($0 if Windows AHB)
  sql_license_hourly   — SQL Server license ($0 if Express or SQL AHB)
  total_payg_hourly    — sum of above three
  total_payg_monthly   — total × 730 hours

Filters applied:
  - retired eq false
  - architecture ne 'Arm64'  (SQL Server does not run on Arm64)
  - both Windows and Linux prices must be present (Cloud Services / Windows-only SKUs dropped)
"""

import logging
import os
from collections import defaultdict

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient

from app.services.sql_pricing import EDITIONS, HOURS_PER_MONTH, price_breakdown

logger = logging.getLogger(__name__)
router = APIRouter()

_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "")
_API_KEY  = os.getenv("AZURE_SEARCH_API_KEY", "")
_INDEX    = "vm-sku-prices"


def _client() -> SearchClient:
    return SearchClient(_ENDPOINT, _INDEX, AzureKeyCredential(_API_KEY))


@router.get("")
async def sql_vm_prices(
    region:      str  = Query("australiaeast"),
    edition:     str  = Query("Enterprise", description="Enterprise | Standard | Web | Express"),
    sql_ahb:     bool = Query(False, description="SQL Server AHB — bring your own SQL license ($0 SQL charge)"),
    windows_ahb: bool = Query(False, description="Windows Server AHB — no Windows OS surcharge, compute at Linux rate"),
    vcpus_min:   int  = Query(1,    ge=1),
    vcpus_max:   int  = Query(128,  le=512),
    ram_min:     int  = Query(0,    ge=0),
    top:         int  = Query(20,   ge=1, le=200),
):
    """
    Per-SKU SQL Server VM total cost broken into 4 components (all PAYG).

    AHB axes are independent:
      sql_ahb=true      → sql_license_hourly = $0 (BYOL)
      windows_ahb=true  → windows_os_hourly = $0, compute at Linux base rate

    Arm64 SKUs are always excluded (SQL Server does not support Arm64).
    Results sorted by total_payg_monthly ascending.
    """
    if edition not in EDITIONS:
        return JSONResponse(
            status_code=422,
            content={"error": f"edition must be one of {sorted(EDITIONS)}"},
        )

    try:
        odata = (
            f"region eq '{region}' "
            f"and vcpus ge {vcpus_min} "
            f"and vcpus le {vcpus_max} "
            f"and retired eq false "
            f"and architecture ne 'Arm64'"
        )
        if ram_min > 0:
            odata += f" and ram_gb ge {ram_min}"

        # Fetch both Windows and Linux rows so we can compute all AHB combinations.
        # top=1000 safely covers any region (max ~250 unique x64 SKUs × 2 OS rows).
        raw = _client().search(
            search_text="*",
            filter=odata,
            order_by=["vcpus asc", "payg_hourly asc"],
            top=1000,
            select=[
                "sku_name", "os", "vcpus", "ram_gb",
                "series", "architecture",
                "payg_hourly",
            ],
        )

        # Group by sku_name; collect Windows and Linux PAYG rates separately.
        by_sku: dict[str, dict] = defaultdict(dict)
        for doc in raw:
            d   = {k: v for k, v in dict(doc).items() if not k.startswith("@")}
            sn  = d["sku_name"]
            os_ = d.get("os", "")
            by_sku[sn].update({
                "sku_name":     sn,
                "vcpus":        d.get("vcpus"),
                "ram_gb":       d.get("ram_gb"),
                "series":       d.get("series"),
                "architecture": d.get("architecture"),
            })
            if os_ == "Windows":
                by_sku[sn]["windows_payg_hourly"] = d.get("payg_hourly")
            elif os_ == "Linux":
                by_sku[sn]["linux_payg_hourly"] = d.get("payg_hourly")

        results = []
        for info in by_sku.values():
            vcpus = info.get("vcpus") or 0
            win_h = info.get("windows_payg_hourly")
            lnx_h = info.get("linux_payg_hourly")

            # Both prices required: enables the 4-part breakdown and guards against
            # Windows-only SKUs that cannot have their OS surcharge isolated.
            if win_h is None or lnx_h is None:
                continue

            bd = price_breakdown(vcpus, edition, sql_ahb, windows_ahb, win_h, lnx_h)
            results.append({
                "sku_name":             info["sku_name"],
                "vcpus":                vcpus,
                "ram_gb":               info.get("ram_gb"),
                "series":               info.get("series"),
                "architecture":         info.get("architecture"),
                # Raw prices (reference)
                "windows_payg_hourly":  round(win_h, 6),
                "linux_payg_hourly":    round(lnx_h, 6),
                # 4-part breakdown
                "base_compute_hourly":  bd["base_compute_hourly"],
                "windows_os_hourly":    bd["windows_os_hourly"],
                "sql_license_hourly":   bd["sql_license_hourly"],
                "total_payg_hourly":    bd["total_payg_hourly"],
                "total_payg_monthly":   round(bd["total_payg_hourly"] * HOURS_PER_MONTH, 2),
            })

        results.sort(key=lambda r: (r["total_payg_monthly"], r["vcpus"]))
        results = results[:top]

        return {
            "count":       len(results),
            "region":      region,
            "edition":     edition,
            "sql_ahb":     sql_ahb,
            "windows_ahb": windows_ahb,
            "results":     results,
        }

    except Exception as exc:
        logger.exception("sql_vm_prices failed region=%s edition=%s", region, edition)
        return JSONResponse(status_code=500, content={"error": str(exc)})
