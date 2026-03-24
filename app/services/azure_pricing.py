import logging
import re

import httpx

logger = logging.getLogger(__name__)

PRICING_API_BASE = "https://prices.azure.com/api/retail/prices"
PRICING_API_VERSION = "2023-01-01-preview"
TIMEOUT_SECONDS = 12


def _sku_to_meter_name(sku: str) -> str:
    """Convert Standard_D2_v3 → 'D2 v3' (meterName format for older series)."""
    result = re.sub(r'^Standard_', '', sku, flags=re.IGNORECASE)
    result = re.sub(r'_v(\d+)$', r' v\1', result, flags=re.IGNORECASE)
    result = result.replace('_', ' ')
    return result


async def fetch_prices(region: str, sku: str) -> list[dict]:
    """
    Fetch Azure retail prices for a VM SKU in a given region.

    Pass 1: query by armSkuName (works for most modern series v4, v5, B, etc.)
    Pass 2: fallback by meterName (handles older series Dv2, Dv3, DSv2, F, FS, etc.)
    """
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        # Pass 1 — armSkuName query
        filter1 = (
            f"serviceName eq 'Virtual Machines' "
            f"and armRegionName eq '{region}' "
            f"and armSkuName eq '{sku}'"
        )
        response1 = await client.get(
            PRICING_API_BASE,
            params={"api-version": PRICING_API_VERSION, "$filter": filter1},
            headers={"Accept": "application/json"},
        )
        if not response1.is_success:
            raise Exception(f"Azure API HTTP {response1.status_code}")

        items1 = response1.json().get("Items") or []
        if items1:
            logger.debug("fetch_prices pass=1 sku=%s region=%s items=%d", sku, region, len(items1))
            return items1

        # Pass 2 — meterName fallback for older series
        meter_name = _sku_to_meter_name(sku)
        filter2 = (
            f"serviceName eq 'Virtual Machines' "
            f"and armRegionName eq '{region}' "
            f"and meterName eq '{meter_name}'"
        )
        response2 = await client.get(
            PRICING_API_BASE,
            params={"api-version": PRICING_API_VERSION, "$filter": filter2},
            headers={"Accept": "application/json"},
        )
        if not response2.is_success:
            raise Exception(f"Azure API HTTP {response2.status_code}")

        items2 = response2.json().get("Items") or []
        logger.debug(
            "fetch_prices pass=2 (meterName fallback) sku=%s meterName=%s region=%s items=%d",
            sku, meter_name, region, len(items2),
        )
        return items2
