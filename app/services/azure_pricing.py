import asyncio
import logging
import os
import random
import re

import httpx

logger = logging.getLogger(__name__)

PRICING_API_BASE = "https://prices.azure.com/api/retail/prices"
PRICING_API_VERSION = "2023-01-01-preview"
TIMEOUT_SECONDS = 12

# Retry delays (seconds) on HTTP 429. Caps at 32s after the initial ramp.
# Jitter of 0-1s is added to each delay to avoid thundering herd.
_RETRY_DELAYS = (2, 4, 8, 16, 32, 32, 32, 32)


async def _get_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """
    GET with exponential backoff + jitter on HTTP 429.
    Retries up to 8 times: 2s, 4s, 8s, 16s, 32s, 32s, 32s, 32s (+ 0-1s jitter each).
    """
    for attempt, delay in enumerate(_RETRY_DELAYS):
        resp = await client.get(url, **kwargs)
        if resp.status_code != 429:
            return resp
        jitter = random.random()  # 0.0–1.0 s
        wait = delay + jitter
        logger.warning(
            "Azure Pricing API 429 — attempt %d/%d, retrying in %.1fs",
            attempt + 1, len(_RETRY_DELAYS), wait,
        )
        await asyncio.sleep(wait)
    return await client.get(url, **kwargs)  # final attempt after last sleep


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
        response1 = await _get_with_retry(
            client, PRICING_API_BASE,
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
        response2 = await _get_with_retry(
            client, PRICING_API_BASE,
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


async def fetch_vm_prices_for_region(
    region: str,
    os_type: str,
    max_pages: int = 15,
) -> list[dict]:
    """
    Page through the Azure Retail Prices API and return every Consumption-tier
    VM price record for the given region and OS.  Callers filter by vCPU / RAM
    in Python so no SKU names are hardcoded here.
    """
    # The Retail Prices API does not reliably support `not contains()` in OData.
    # For Windows we CAN narrow the query; for Linux we fetch all and filter in Python.
    os_clause = " and contains(productName, 'Windows')" if os_type == "Windows" else ""
    odata_filter = (
        f"serviceName eq 'Virtual Machines' "
        f"and armRegionName eq '{region}' "
        f"and priceType eq 'Consumption'"
        f"{os_clause}"
    )

    all_items: list[dict] = []
    next_url: str | None = PRICING_API_BASE
    params: dict | None = {"api-version": PRICING_API_VERSION, "$filter": odata_filter}
    pages = 0

    async with httpx.AsyncClient(timeout=30) as client:
        while next_url and pages < max_pages:
            resp = await _get_with_retry(
                client, next_url,
                params=params,
                headers={"Accept": "application/json"},
            )
            if not resp.is_success:
                logger.error(
                    "fetch_vm_prices_for_region: HTTP %d region=%s", resp.status_code, region
                )
                break
            data = resp.json()
            all_items.extend(data.get("Items") or [])
            next_url = data.get("NextPageLink")
            params = None   # NextPageLink is a self-contained URL
            pages += 1

    logger.info(
        "fetch_vm_prices_for_region: region=%s os=%s pages=%d items=%d",
        region, os_type, pages, len(all_items),
    )
    return all_items


async def _get_arm_token() -> str | None:
    """Return a Bearer token for management.azure.com via MSI or service principal."""
    try:
        tenant_id     = os.environ.get('AZURE_TENANT_ID', '')
        client_id     = os.environ.get('AZURE_CLIENT_ID', '')
        client_secret = os.environ.get('AZURE_CLIENT_SECRET', '')
        if not all([tenant_id, client_id, client_secret]):
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    'http://169.254.169.254/metadata/identity/oauth2/token',
                    params={'api-version': '2018-02-01', 'resource': 'https://management.azure.com/'},
                    headers={'Metadata': 'true'},
                )
                return resp.json().get('access_token') if resp.is_success else None
        else:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(
                    f'https://login.microsoftonline.com/{tenant_id}/oauth2/token',
                    data={'grant_type': 'client_credentials', 'client_id': client_id,
                          'client_secret': client_secret, 'resource': 'https://management.azure.com/'},
                )
                return resp.json().get('access_token') if resp.is_success else None
    except Exception:
        return None


async def fetch_temp_storage_gb(sku: str, region: str) -> int | None:
    subscription_id = os.environ.get('AZURE_SUBSCRIPTION_ID', '')
    if not subscription_id:
        return None
    token = await _get_arm_token()
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            url = f"https://management.azure.com/subscriptions/{subscription_id}/providers/Microsoft.Compute/skus"
            resp = await client.get(
                url,
                params={'api-version': '2021-07-01', '$filter': f"location eq '{region}'"},
                headers={'Authorization': f'Bearer {token}'},
            )
            if not resp.is_success:
                return None
            for s in resp.json().get('value', []):
                if s.get('name') == sku and s.get('resourceType') == 'virtualMachines':
                    for cap in s.get('capabilities', []):
                        if cap.get('name') == 'MaxResourceVolumeMB':
                            mb = int(cap.get('value', 0))
                            return mb // 1024 if mb > 0 else None
    except Exception:
        return None
    return None


# ── Managed Disk pricing ──────────────────────────────────────────────────────

HOURS_PER_MONTH = 730

DISK_TIER_SIZES_GIB = {
    1: 4, 2: 8, 3: 16, 4: 32, 6: 64, 10: 128, 15: 256,
    20: 512, 30: 1024, 40: 2048, 50: 4096, 60: 8192, 70: 16384, 80: 32767,
}

DISK_TYPES = {
    "standard_hdd":   {"product": "Standard HDD Managed Disks", "prefix": "S",  "min_tier": 4,    "flat": False},
    "standard_ssd":   {"product": "Standard SSD Managed Disks", "prefix": "E",  "min_tier": 1,    "flat": False},
    "premium_ssd":    {"product": "Premium SSD Managed Disks",  "prefix": "P",  "min_tier": 1,    "flat": True},
    "premium_ssd_v2": {"product": "Azure Premium SSD v2",       "prefix": None, "min_tier": None, "flat": True},
}


async def fetch_disk_tier_prices(region: str, disk_type: str) -> dict[str, float]:
    """{tier_name: monthly_usd} for a tiered disk type.
    LRS, Consumption, base '… LRS Disk' meter only (excludes Disk Mount/Burst/Snapshots)."""
    cfg = DISK_TYPES[disk_type]
    odata_filter = (
        f"serviceName eq 'Storage' and armRegionName eq '{region}' "
        f"and productName eq '{cfg['product']}'"
    )
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        resp = await _get_with_retry(
            client, PRICING_API_BASE,
            params={"api-version": PRICING_API_VERSION, "$filter": odata_filter},
            headers={"Accept": "application/json"},
        )
    if not resp.is_success:
        raise Exception(f"Azure API HTTP {resp.status_code}")
    pat = re.compile(rf"^({cfg['prefix']}\d+) LRS Disk$")
    prices: dict[str, float] = {}
    for it in resp.json().get("Items", []):
        if it.get("type") != "Consumption" or "LRS" not in it.get("skuName", ""):
            continue
        m = pat.match(it.get("meterName", ""))
        if m:
            prices[m.group(1)] = it["retailPrice"]
    return prices


async def fetch_v2_capacity_rate(region: str) -> float | None:
    """Premium SSD v2 provisioned-capacity rate as USD/GiB/month.
    Capacity only — IOPS/throughput above free baseline (3000 IOPS, 125 MB/s) is Phase 2."""
    odata_filter = (
        f"serviceName eq 'Storage' and armRegionName eq '{region}' "
        f"and productName eq 'Azure Premium SSD v2'"
    )
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        resp = await _get_with_retry(
            client, PRICING_API_BASE,
            params={"api-version": PRICING_API_VERSION, "$filter": odata_filter},
            headers={"Accept": "application/json"},
        )
    if not resp.is_success:
        raise Exception(f"Azure API HTTP {resp.status_code}")
    for it in resp.json().get("Items", []):
        if (it.get("type") == "Consumption"
                and it.get("meterName") == "Premium LRS Provisioned Capacity"
                and "Confidential" not in it.get("skuName", "")):
            return it["retailPrice"] * HOURS_PER_MONTH
    return None


async def vm_supports_premium(sku: str, region: str) -> bool:
    """True if VM SKU exposes PremiumIO=True in ARM Compute capabilities.
    Returns False on any failure (safe default → Standard only)."""
    token = await _get_arm_token()
    sub   = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    if not token or not sub:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            url  = f"https://management.azure.com/subscriptions/{sub}/providers/Microsoft.Compute/skus"
            resp = await client.get(
                url,
                params={"api-version": "2021-07-01", "$filter": f"location eq '{region}'"},
                headers={"Authorization": f"Bearer {token}"},
            )
            if not resp.is_success:
                return False
            for s in resp.json().get("value", []):
                if s.get("name") == sku and s.get("resourceType") == "virtualMachines":
                    for cap in s.get("capabilities", []):
                        if cap.get("name") == "PremiumIO":
                            return str(cap.get("value", "")).lower() == "true"
    except Exception:
        return False
    return False
