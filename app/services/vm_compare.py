"""
app/services/vm_compare.py

Live per-region VM price comparison.
Reuses _get_arm_skus_for_region() from azure_pricing.py (1hr cache).
Fetches Retail Prices API in bulk per region; caches 30 min.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from app.services.azure_pricing import (
    PRICING_API_BASE,
    PRICING_API_VERSION,
    _get_with_retry,
    _get_arm_skus_for_region,
)

log = logging.getLogger(__name__)

HOURS      = 730   # hours per month
PAGE_LIMIT = 200   # max Retail API pages per fetch

# ── Per-region caches ─────────────────────────────────────────────────────────
_price_cache: dict[str, tuple[float, dict]] = {}  # region -> (ts, {sku: price_dict})
_price_locks: dict[str, asyncio.Lock] = {}
_PRICE_TTL = 1800  # 30 min


def _price_lock(region: str) -> asyncio.Lock:
    if region not in _price_locks:
        _price_locks[region] = asyncio.Lock()
    return _price_locks[region]


# ── ARM gate helpers ──────────────────────────────────────────────────────────

def _process_arm_gate(raw_skus: list[dict]) -> tuple[set, set, set]:
    """
    Process raw ARM SKU JSON (from _get_arm_skus_for_region) into three sets:
    (in_arm, arm64, quota_required)
    """
    in_arm: set[str] = set()
    arm64:  set[str] = set()
    quota:  set[str] = set()

    for s in raw_skus:
        if s.get("resourceType") != "virtualMachines":
            continue
        name = s.get("name", "")
        if not name:
            continue
        in_arm.add(name)

        for cap in s.get("capabilities", []):
            if cap.get("name") == "CpuArchitectureType" and cap.get("value", "").lower() == "arm64":
                arm64.add(name)
                break

        for r in s.get("restrictions", []):
            r_type = str(r.get("type", "")).upper()
            zones  = (r.get("restrictionInfo") or {}).get("zones") or []
            if r_type.endswith("LOCATION") and not zones:
                quota.add(name)
                break

    return in_arm, arm64, quota


def _arm_specs(raw_skus: list[dict]) -> dict[str, dict]:
    """Extract vCPUs, RAM, temp storage from raw ARM SKU capabilities."""
    specs: dict[str, dict] = {}
    for s in raw_skus:
        if s.get("resourceType") != "virtualMachines":
            continue
        name = s.get("name", "")
        if not name:
            continue
        caps = {c["name"]: c.get("value") for c in s.get("capabilities", [])}
        try:
            vcpus = int(caps.get("vCPUs", 0) or 0)
        except (ValueError, TypeError):
            vcpus = 0
        try:
            ram_gb = int(float(caps.get("MemoryGB", 0) or 0))
        except (ValueError, TypeError):
            ram_gb = 0
        try:
            tmp_mb = int(caps.get("MaxResourceVolumeMB", 0) or 0)
            temp_gb = tmp_mb // 1024 if tmp_mb > 0 else 0
        except (ValueError, TypeError):
            temp_gb = 0
        specs[name] = {"vcpus": vcpus, "ram_gb": ram_gb, "temp_storage_gb": temp_gb}
    return specs


_SERIES_RE = re.compile(r"^Standard_([A-Za-z]+)\d", re.IGNORECASE)


def _series(sku_name: str) -> str:
    m = _SERIES_RE.match(sku_name)
    return m.group(1).upper() if m else "?"


# ── Retail Prices bulk fetch ──────────────────────────────────────────────────

async def _bulk_fetch(region: str, price_type: str) -> list[dict]:
    odata = (
        f"serviceName eq 'Virtual Machines' "
        f"and armRegionName eq '{region}' "
        f"and priceType eq '{price_type}'"
    )
    items: list[dict] = []
    next_url: str | None = PRICING_API_BASE
    params: dict | None  = {"api-version": PRICING_API_VERSION, "$filter": odata}
    pages = 0

    async with httpx.AsyncClient(timeout=60) as client:
        while next_url and pages < PAGE_LIMIT:
            resp = await _get_with_retry(client, next_url, params=params,
                                         headers={"Accept": "application/json"})
            if not resp.is_success:
                log.error("Retail API HTTP %d region=%s priceType=%s", resp.status_code, region, price_type)
                break
            data = resp.json()
            items.extend(data.get("Items") or [])
            next_url = data.get("NextPageLink")
            params   = None
            pages   += 1

    log.info("_bulk_fetch region=%s priceType=%s: %d pages, %d items", region, price_type, pages, len(items))
    return items


def _parse_prices(consumption: list[dict], reservation: list[dict]) -> dict[str, dict]:
    prices: dict[str, dict] = {}

    def _entry(arm: str) -> dict:
        if arm not in prices:
            prices[arm] = {
                "linux_payg":    None, "linux_sp_1yr": None, "linux_sp_3yr": None,
                "linux_spot":    None,
                "linux_ri_1yr":  None, "linux_ri_3yr": None,
                "windows_payg":  None,
                "windows_ri_1yr": None, "windows_ri_3yr": None,
            }
        return prices[arm]

    for it in consumption:
        arm    = it.get("armSkuName", "").strip()
        sku_n  = it.get("skuName", "")
        product = it.get("productName", "")
        retail = it.get("retailPrice", 0.0)
        if not arm or not retail:
            continue
        is_win  = "Windows" in product
        is_spot = "Spot" in sku_n
        is_low  = "Low Priority" in sku_n
        e = _entry(arm)
        if is_spot and not is_win:
            cur = e["linux_spot"]
            if cur is None or retail < cur.get("retailPrice", 9999):
                e["linux_spot"] = it
        elif not is_spot and not is_low:
            if is_win:
                if e["windows_payg"] is None:
                    e["windows_payg"] = it
            else:
                if e["linux_payg"] is None:
                    e["linux_payg"] = it
                    for sp in (it.get("savingsPlan") or []):
                        term = sp.get("term", "")
                        r    = sp.get("retailPrice")
                        if r:
                            if "1 Year" in term:
                                e["linux_sp_1yr"] = r
                            elif "3 Year" in term:
                                e["linux_sp_3yr"] = r

    for it in reservation:
        arm    = it.get("armSkuName", "").strip()
        retail = it.get("retailPrice", 0.0)
        if not arm or not retail:
            continue
        term   = it.get("reservationTerm", "") or it.get("skuName", "")
        is_1yr = "1 Year" in term
        is_3yr = "3 Year" in term or "3 Years" in term
        if not (is_1yr or is_3yr):
            continue
        monthly = retail / 12 if is_1yr else retail / 36
        e = _entry(arm)
        if is_1yr and e["linux_ri_1yr"] is None:
            e["linux_ri_1yr"] = monthly
        elif is_3yr and e["linux_ri_3yr"] is None:
            e["linux_ri_3yr"] = monthly

    return prices


async def get_region_prices(region: str) -> dict[str, dict]:
    """Return {sku_name: price_dict} for a region. Cached 30 min."""
    cached = _price_cache.get(region)
    if cached and time.monotonic() - cached[0] < _PRICE_TTL:
        return cached[1]

    async with _price_lock(region):
        cached = _price_cache.get(region)
        if cached and time.monotonic() - cached[0] < _PRICE_TTL:
            return cached[1]

        consumption, reservation = await asyncio.gather(
            _bulk_fetch(region, "Consumption"),
            _bulk_fetch(region, "Reservation"),
        )
        result = _parse_prices(consumption, reservation)
        _price_cache[region] = (time.monotonic(), result)
        log.info("Prices cached for region=%s: %d SKUs", region, len(result))
        return result


# ── Main compare entry point ──────────────────────────────────────────────────

async def compare_live(
    region:    str,
    os_type:   str,    # "Linux" or "Windows"
    vcpus_min: int = 1,
    vcpus_max: int = 512,
    ram_min:   int = 0,
    ram_max:   int = 12288,
) -> list[dict]:
    """
    Fetch and return all VM price rows for the given region+OS, applying the
    ARM deployability gate (arm64+Windows excluded, quota-required badged).
    Filtered by vcpus/ram; sorted by payg_monthly asc.
    """
    raw_arm, prices = await asyncio.gather(
        _get_arm_skus_for_region(region),
        get_region_prices(region),
    )

    in_arm, arm64, quota = _process_arm_gate(raw_arm)
    specs = _arm_specs(raw_arm)
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows: list[dict] = []

    for sku_name, p in prices.items():
        if in_arm and sku_name not in in_arm:
            continue

        is_arm64 = sku_name in arm64
        is_quota = sku_name in quota

        if os_type == "Windows" and is_arm64:
            continue

        spec    = specs.get(sku_name, {})
        vcpus   = spec.get("vcpus", 0)
        ram_gb  = spec.get("ram_gb", 0)
        temp_gb = spec.get("temp_storage_gb", 0)

        if vcpus < vcpus_min or vcpus > vcpus_max:
            continue
        if ram_min > 0 and ram_gb < ram_min:
            continue
        if ram_max < 12288 and ram_gb > ram_max:
            continue

        base = {
            "sku_name":        sku_name,
            "region":          region,
            "os":              os_type,
            "vcpus":           vcpus,
            "ram_gb":          ram_gb,
            "temp_storage_gb": temp_gb,
            "series":          _series(sku_name),
            "architecture":    "Arm64" if is_arm64 else "x64",
            "quota_required":  is_quota,
            "price_updated_at": updated_at,
        }

        if os_type == "Linux":
            lp = p.get("linux_payg")
            if not lp:
                continue
            hr   = lp["retailPrice"]
            sp1  = p.get("linux_sp_1yr")
            sp3  = p.get("linux_sp_3yr")
            spot = p.get("linux_spot")
            rows.append({
                **base,
                "payg_hourly":    round(hr, 6),
                "payg_monthly":   round(hr * HOURS, 4),
                "sp_1yr_monthly": round(sp1 * HOURS, 4) if sp1 else None,
                "sp_3yr_monthly": round(sp3 * HOURS, 4) if sp3 else None,
                "ri_1yr_monthly": round(p["linux_ri_1yr"], 4) if p.get("linux_ri_1yr") else None,
                "ri_3yr_monthly": round(p["linux_ri_3yr"], 4) if p.get("linux_ri_3yr") else None,
                "spot_hourly":    round(spot["retailPrice"], 6) if spot else None,
            })
        else:
            wp = p.get("windows_payg")
            if not wp:
                continue
            hr_w = wp["retailPrice"]
            sp1  = p.get("linux_sp_1yr")
            sp3  = p.get("linux_sp_3yr")
            ri1  = p.get("linux_ri_1yr")
            ri3  = p.get("linux_ri_3yr")
            rows.append({
                **base,
                "payg_hourly":    round(hr_w, 6),
                "payg_monthly":   round(hr_w * HOURS, 4),
                "sp_1yr_monthly": round(sp1 * HOURS, 4) if sp1 else None,
                "sp_3yr_monthly": round(sp3 * HOURS, 4) if sp3 else None,
                "ri_1yr_monthly": round(ri1, 4) if ri1 else None,
                "ri_3yr_monthly": round(ri3, 4) if ri3 else None,
                "spot_hourly":    None,
            })

    rows.sort(key=lambda r: (r.get("payg_monthly") is None, r.get("payg_monthly") or 0))
    log.info("compare_live region=%s os=%s: %d rows", region, os_type, len(rows))
    return rows


# ── Azure region catalogue ────────────────────────────────────────────────────

AZURE_REGIONS: list[dict] = [
    # United States
    {"code": "eastus",           "city": "Virginia",           "label": "East US (Virginia)",             "geographyGroup": "United States"},
    {"code": "eastus2",          "city": "Virginia",           "label": "East US 2 (Virginia)",           "geographyGroup": "United States"},
    {"code": "westus",           "city": "California",         "label": "West US (California)",           "geographyGroup": "United States"},
    {"code": "westus2",          "city": "Washington",         "label": "West US 2 (Washington)",         "geographyGroup": "United States"},
    {"code": "westus3",          "city": "Arizona",            "label": "West US 3 (Arizona)",            "geographyGroup": "United States"},
    {"code": "centralus",        "city": "Iowa",               "label": "Central US (Iowa)",              "geographyGroup": "United States"},
    {"code": "northcentralus",   "city": "Illinois",           "label": "North Central US (Illinois)",    "geographyGroup": "United States"},
    {"code": "southcentralus",   "city": "Texas",              "label": "South Central US (Texas)",       "geographyGroup": "United States"},
    {"code": "westcentralus",    "city": "Wyoming",            "label": "West Central US (Wyoming)",      "geographyGroup": "United States"},
    # Canada
    {"code": "canadacentral",    "city": "Toronto",            "label": "Canada Central (Toronto)",       "geographyGroup": "Canada"},
    {"code": "canadaeast",       "city": "Quebec City",        "label": "Canada East (Quebec City)",      "geographyGroup": "Canada"},
    # South America
    {"code": "brazilsouth",      "city": "São Paulo",          "label": "Brazil South (São Paulo)",       "geographyGroup": "South America"},
    {"code": "brazilsoutheast",  "city": "Rio de Janeiro",     "label": "Brazil Southeast (Rio de Janeiro)", "geographyGroup": "South America"},
    {"code": "mexicocentral",    "city": "Querétaro",          "label": "Mexico Central (Querétaro)",     "geographyGroup": "South America"},
    # Europe
    {"code": "northeurope",      "city": "Dublin",             "label": "North Europe (Dublin)",          "geographyGroup": "Europe"},
    {"code": "westeurope",       "city": "Amsterdam",          "label": "West Europe (Amsterdam)",        "geographyGroup": "Europe"},
    {"code": "uksouth",          "city": "London",             "label": "UK South (London)",              "geographyGroup": "Europe"},
    {"code": "ukwest",           "city": "Cardiff",            "label": "UK West (Cardiff)",              "geographyGroup": "Europe"},
    {"code": "germanywestcentral","city": "Frankfurt",         "label": "Germany West Central (Frankfurt)","geographyGroup": "Europe"},
    {"code": "germanynorth",     "city": "Berlin",             "label": "Germany North (Berlin)",         "geographyGroup": "Europe"},
    {"code": "swedencentral",    "city": "Gävle",              "label": "Sweden Central (Gävle)",         "geographyGroup": "Europe"},
    {"code": "francecentral",    "city": "Paris",              "label": "France Central (Paris)",         "geographyGroup": "Europe"},
    {"code": "francesouth",      "city": "Marseille",          "label": "France South (Marseille)",       "geographyGroup": "Europe"},
    {"code": "switzerlandnorth", "city": "Zurich",             "label": "Switzerland North (Zurich)",     "geographyGroup": "Europe"},
    {"code": "switzerlandwest",  "city": "Geneva",             "label": "Switzerland West (Geneva)",      "geographyGroup": "Europe"},
    {"code": "norwayeast",       "city": "Oslo",               "label": "Norway East (Oslo)",             "geographyGroup": "Europe"},
    {"code": "norwaywest",       "city": "Stavanger",          "label": "Norway West (Stavanger)",        "geographyGroup": "Europe"},
    {"code": "polandcentral",    "city": "Warsaw",             "label": "Poland Central (Warsaw)",        "geographyGroup": "Europe"},
    {"code": "italynorth",       "city": "Milan",              "label": "Italy North (Milan)",            "geographyGroup": "Europe"},
    {"code": "spaincentral",     "city": "Madrid",             "label": "Spain Central (Madrid)",         "geographyGroup": "Europe"},
    {"code": "austriaeast",      "city": "Vienna",             "label": "Austria East (Vienna)",          "geographyGroup": "Europe"},
    # Asia Pacific
    {"code": "australiaeast",    "city": "Sydney",             "label": "Australia East (Sydney)",        "geographyGroup": "Asia Pacific"},
    {"code": "australiasoutheast","city": "Melbourne",         "label": "Australia Southeast (Melbourne)","geographyGroup": "Asia Pacific"},
    {"code": "australiacentral", "city": "Canberra",           "label": "Australia Central (Canberra)",   "geographyGroup": "Asia Pacific"},
    {"code": "australiacentral2","city": "Canberra",           "label": "Australia Central 2 (Canberra)", "geographyGroup": "Asia Pacific"},
    {"code": "japaneast",        "city": "Tokyo",              "label": "Japan East (Tokyo)",             "geographyGroup": "Asia Pacific"},
    {"code": "japanwest",        "city": "Osaka",              "label": "Japan West (Osaka)",             "geographyGroup": "Asia Pacific"},
    {"code": "koreacentral",     "city": "Seoul",              "label": "Korea Central (Seoul)",          "geographyGroup": "Asia Pacific"},
    {"code": "koreasouth",       "city": "Busan",              "label": "Korea South (Busan)",            "geographyGroup": "Asia Pacific"},
    {"code": "eastasia",         "city": "Hong Kong",          "label": "East Asia (Hong Kong)",          "geographyGroup": "Asia Pacific"},
    {"code": "southeastasia",    "city": "Singapore",          "label": "Southeast Asia (Singapore)",     "geographyGroup": "Asia Pacific"},
    {"code": "centralindia",     "city": "Pune",               "label": "Central India (Pune)",           "geographyGroup": "Asia Pacific"},
    {"code": "southindia",       "city": "Chennai",            "label": "South India (Chennai)",          "geographyGroup": "Asia Pacific"},
    {"code": "westindia",        "city": "Mumbai",             "label": "West India (Mumbai)",            "geographyGroup": "Asia Pacific"},
    {"code": "newzealandnorth",  "city": "Auckland",           "label": "New Zealand North (Auckland)",   "geographyGroup": "Asia Pacific"},
    {"code": "taiwannorth",      "city": "Taipei",             "label": "Taiwan North (Taipei)",          "geographyGroup": "Asia Pacific"},
    {"code": "malaysiawest",     "city": "Kuala Lumpur",       "label": "Malaysia West (Kuala Lumpur)",   "geographyGroup": "Asia Pacific"},
    # Middle East
    {"code": "uaenorth",         "city": "Dubai",              "label": "UAE North (Dubai)",              "geographyGroup": "Middle East"},
    {"code": "uaecentral",       "city": "Abu Dhabi",          "label": "UAE Central (Abu Dhabi)",        "geographyGroup": "Middle East"},
    {"code": "qatarcentral",     "city": "Doha",               "label": "Qatar Central (Doha)",           "geographyGroup": "Middle East"},
    {"code": "israelcentral",    "city": "Tel Aviv",           "label": "Israel Central (Tel Aviv)",      "geographyGroup": "Middle East"},
    {"code": "saudiarabianorth", "city": "Riyadh",             "label": "Saudi Arabia North (Riyadh)",    "geographyGroup": "Middle East"},
    # Africa
    {"code": "southafricanorth", "city": "Johannesburg",       "label": "South Africa North (Johannesburg)", "geographyGroup": "Africa"},
    {"code": "southafricawest",  "city": "Cape Town",          "label": "South Africa West (Cape Town)",  "geographyGroup": "Africa"},
]
