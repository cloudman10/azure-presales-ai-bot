"""
app/routers/vm_prices.py

Stage 1 data layer: query vm-sku-prices index.

GET /api/vm-prices/search?region=australiaeast&os=Linux&vcpus_min=4&vcpus_max=8
GET /api/vm-prices/sku/{sku_name}   — all pricing tiers for one SKU
"""

import logging
import os

from fastapi import APIRouter, Path, Query
from fastapi.responses import JSONResponse

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient

logger = logging.getLogger(__name__)
router = APIRouter()

_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "")
_API_KEY  = os.getenv("AZURE_SEARCH_API_KEY", "")
_INDEX    = "vm-sku-prices"

_SORT_ALLOWLIST = {
    "payg_monthly", "sp_1yr_monthly", "ri_1yr_monthly",
    "vcpus", "ram_gb", "sku_name",
}


def _client() -> SearchClient:
    return SearchClient(_ENDPOINT, _INDEX, AzureKeyCredential(_API_KEY))


@router.get("/search")
async def search_prices(
    region:    str = Query("australiaeast", description="Azure region"),
    os:        str = Query("Linux",         description="'Linux' or 'Windows'"),
    vcpus_min: int = Query(1,               ge=1),
    vcpus_max: int = Query(128,             le=512),
    ram_min:   int = Query(0,               ge=0),
    sort_by:   str = Query("payg_monthly",  description="Field to sort ascending"),
    top:       int = Query(20,              ge=1, le=100),
):
    """
    Query VM prices from vm-sku-prices index. Returns specs + all pricing tiers.
    """
    try:
        order_field = sort_by if sort_by in _SORT_ALLOWLIST else "payg_monthly"
        odata = (
            f"region eq '{region}' "
            f"and os eq '{os}' "
            f"and vcpus ge {vcpus_min} "
            f"and vcpus le {vcpus_max} "
            f"and retired eq false"
        )
        if ram_min > 0:
            odata += f" and ram_gb ge {ram_min}"

        results = _client().search(
            search_text="*",
            filter=odata,
            order_by=[f"{order_field} asc"],
            top=top,
            select=[
                "sku_name", "region", "os", "vcpus", "ram_gb", "temp_storage_gb",
                "series", "payg_hourly", "payg_monthly",
                "sp_1yr_monthly", "sp_3yr_monthly",
                "ri_1yr_monthly", "ri_3yr_monthly",
                "spot_hourly", "price_updated_at",
            ],
        )
        docs = [{k: v for k, v in dict(r).items() if not k.startswith("@")} for r in results]
        return {
            "count": len(docs),
            "region": region,
            "os": os,
            "filter": odata,
            "results": docs,
        }
    except Exception as exc:
        logger.exception("vm_prices search failed")
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/sku/{sku_name:path}")
async def get_sku_prices(
    sku_name: str = Path(description="Full SKU name e.g. Standard_D4s_v5"),
):
    """Return all pricing rows for a single SKU (both Linux and Windows)."""
    try:
        results = _client().search(
            search_text="*",
            filter=f"sku_name eq '{sku_name}' and retired eq false",
            top=10,
            select=[
                "sku_name", "region", "os", "vcpus", "ram_gb",
                "payg_monthly", "sp_1yr_monthly", "sp_3yr_monthly",
                "ri_1yr_monthly", "ri_3yr_monthly", "spot_hourly",
                "price_updated_at",
            ],
        )
        docs = [{k: v for k, v in dict(r).items() if not k.startswith("@")} for r in results]
        return {"sku_name": sku_name, "count": len(docs), "results": docs}
    except Exception as exc:
        logger.exception("vm_prices sku lookup failed: %s", sku_name)
        return JSONResponse(status_code=500, content={"error": str(exc)})
