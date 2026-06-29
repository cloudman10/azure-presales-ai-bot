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
        if not product.startswith("Virtual Machines"):
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
        product = it.get("productName", "")
        if not product.startswith("Virtual Machines"):
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


# ── Azure region catalogue — dynamic discovery ───────────────────────────────
#
# The region list is built at runtime by probing the Azure Retail Prices API
# for all armRegionName values that have VM Consumption pricing.  Results are
# cached for 24 h so repeated calls are free.  A _REGION_META lookup table
# supplies friendly city names and geography groupings; regions discovered by
# the probe but absent from that table still appear, auto-labelled from their
# code.  Sovereign/specialty clouds (usgov*, jioindia*, deloscloud*) are
# excluded.  No manual list editing is needed when Azure adds or removes
# commercial regions.

_REGION_TTL = 86400  # 24 h

_region_cache_state: tuple[float, list[dict]] | None = None
_region_lock_obj:    asyncio.Lock | None = None


def _get_region_lock() -> asyncio.Lock:
    global _region_lock_obj
    if _region_lock_obj is None:
        _region_lock_obj = asyncio.Lock()
    return _region_lock_obj


# Sovereign / specialty cloud prefixes — never shown in the dropdown.
_EXCLUDE_PREFIXES = ("usgov", "jioindia", "deloscloud")

# Probe SKU: Standard_D4s_v5 is available in every commercial Azure region.
# Its paginated result set (~4 pages) yields all armRegionName values.
_PROBE_SKU = "Standard_D4s_v5"

# Friendly metadata for all known commercial regions.
# New regions discovered by the probe but absent here appear auto-labelled
# from their code (see _label_from_code).  Omitting a code here never hides
# it from the dropdown — only the sovereign exclusion list does that.
# Dead regions (taiwannorth, saudiarabianorth) are intentionally absent:
# they have no VM pricing in the Retail API so the probe never returns them.
_REGION_META: dict[str, tuple[str, str]] = {  # code -> (city, geographyGroup)
    # United States
    "eastus":             ("Virginia",        "United States"),
    "eastus2":            ("Virginia",        "United States"),
    "westus":             ("California",      "United States"),
    "westus2":            ("Washington",      "United States"),
    "westus3":            ("Arizona",         "United States"),
    "centralus":          ("Iowa",            "United States"),
    "northcentralus":     ("Illinois",        "United States"),
    "southcentralus":     ("Texas",           "United States"),
    "westcentralus":      ("Wyoming",         "United States"),
    # Canada
    "canadacentral":      ("Toronto",         "Canada"),
    "canadaeast":         ("Quebec City",     "Canada"),
    # South America
    "brazilsouth":        ("São Paulo",       "South America"),
    "brazilsoutheast":    ("Rio de Janeiro",  "South America"),
    "mexicocentral":      ("Querétaro",       "South America"),
    "chilecentral":       ("Santiago",        "South America"),
    # Europe
    "northeurope":        ("Dublin",          "Europe"),
    "westeurope":         ("Amsterdam",       "Europe"),
    "uksouth":            ("London",          "Europe"),
    "ukwest":             ("Cardiff",         "Europe"),
    "germanywestcentral": ("Frankfurt",       "Europe"),
    "germanynorth":       ("Berlin",          "Europe"),
    "swedencentral":      ("Gävle",           "Europe"),
    "swedensouth":        ("Malmö",           "Europe"),
    "francecentral":      ("Paris",           "Europe"),
    "francesouth":        ("Marseille",       "Europe"),
    "switzerlandnorth":   ("Zurich",          "Europe"),
    "switzerlandwest":    ("Geneva",          "Europe"),
    "norwayeast":         ("Oslo",            "Europe"),
    "norwaywest":         ("Stavanger",       "Europe"),
    "polandcentral":      ("Warsaw",          "Europe"),
    "italynorth":         ("Milan",           "Europe"),
    "spaincentral":       ("Madrid",          "Europe"),
    "austriaeast":        ("Vienna",          "Europe"),
    "belgiumcentral":     ("Brussels",        "Europe"),
    "denmarkeast":        ("Copenhagen",      "Europe"),
    # Asia Pacific
    "australiaeast":      ("Sydney",          "Asia Pacific"),
    "australiasoutheast": ("Melbourne",       "Asia Pacific"),
    "australiacentral":   ("Canberra",        "Asia Pacific"),
    "australiacentral2":  ("Canberra",        "Asia Pacific"),
    "japaneast":          ("Tokyo",           "Asia Pacific"),
    "japanwest":          ("Osaka",           "Asia Pacific"),
    "koreacentral":       ("Seoul",           "Asia Pacific"),
    "koreasouth":         ("Busan",           "Asia Pacific"),
    "eastasia":           ("Hong Kong",       "Asia Pacific"),
    "southeastasia":      ("Singapore",       "Asia Pacific"),
    "centralindia":       ("Pune",            "Asia Pacific"),
    "southindia":         ("Chennai",         "Asia Pacific"),
    "westindia":          ("Mumbai",          "Asia Pacific"),
    "indiasouthcentral":  ("Hyderabad",       "Asia Pacific"),
    "newzealandnorth":    ("Auckland",        "Asia Pacific"),
    "indonesiacentral":   ("Jakarta",         "Asia Pacific"),
    "malaysiawest":       ("Kuala Lumpur",    "Asia Pacific"),
    # Middle East
    "uaenorth":           ("Dubai",           "Middle East"),
    "uaecentral":         ("Abu Dhabi",       "Middle East"),
    "qatarcentral":       ("Doha",            "Middle East"),
    "israelcentral":      ("Tel Aviv",        "Middle East"),
    "israelnorthwest":    ("Haifa",           "Middle East"),
    # Africa
    "southafricanorth":   ("Johannesburg",    "Africa"),
    "southafricawest":    ("Cape Town",       "Africa"),
}

_GEO_ORDER = [
    "United States", "Canada", "South America", "Europe",
    "Asia Pacific", "Middle East", "Africa",
]

# Geographic suffix tokens — compound forms must precede their components.
_GEO_SUFFIXES: dict[str, str] = {
    "northcentral": "North Central",
    "southcentral": "South Central",
    "westcentral":  "West Central",
    "northeast":    "Northeast",
    "northwest":    "Northwest",
    "southeast":    "Southeast",
    "southwest":    "Southwest",
    "north":        "North",
    "south":        "South",
    "east":         "East",
    "west":         "West",
    "central":      "Central",
}


def _label_from_code(code: str) -> str:
    """Derive a display label for region codes not in _REGION_META.
    'finlandeast' -> 'Finland East', 'indiasouthcentral' -> 'India South Central'."""
    m = re.search(r"\d+$", code)
    num, rest = (m.group(), code[: m.start()]) if m else ("", code)
    geo = ""
    for suffix in _GEO_SUFFIXES:           # compound suffixes tried first
        if rest.endswith(suffix) and len(rest) > len(suffix):
            geo, rest = suffix, rest[: -len(suffix)]
            break
    parts = (
        ([rest.upper() if rest in ("us", "uk", "uae") else rest.capitalize()] if rest else [])
        + ([_GEO_SUFFIXES[geo]] if geo else [])
        + ([num] if num else [])
    )
    return " ".join(parts) if parts else code.capitalize()


def _guess_geo(code: str) -> str:
    """Return a geographyGroup for region codes absent from _REGION_META."""
    _MAP: list[tuple[str, tuple[str, ...]]] = [
        ("United States",  ("eastus", "westus", "centralus", "northcentralus",
                            "southcentralus", "westcentralus")),
        ("Canada",         ("canada",)),
        ("South America",  ("brazil", "chile", "mexico", "colombia", "peru", "argentina")),
        ("Europe",         ("northeurope", "westeurope", "uk", "france", "germany",
                            "sweden", "switzer", "norway", "poland", "italy", "spain",
                            "austria", "belgium", "denmark", "finland", "greece",
                            "ireland", "portugal", "romania")),
        ("Asia Pacific",   ("australia", "japan", "korea", "eastasia", "southeastasia",
                            "india", "newzealand", "indonesia", "malaysia", "taiwan",
                            "thailand", "philippines", "vietnam", "singapore")),
        ("Middle East",    ("uae", "qatar", "israel", "saudi", "oman",
                            "bahrain", "jordan", "kuwait")),
        ("Africa",         ("southafrica", "nigeria", "egypt", "kenya", "ghana")),
    ]
    for group, prefixes in _MAP:
        if any(code.startswith(p) for p in prefixes):
            return group
    return "Other"


async def _probe_retail_regions() -> set[str]:
    """Paginate the Retail API for _PROBE_SKU to discover all commercial regions."""
    odata = (
        f"serviceName eq 'Virtual Machines' "
        f"and armSkuName eq '{_PROBE_SKU}' "
        f"and priceType eq 'Consumption'"
    )
    found: set[str] = set()
    next_url: str | None = PRICING_API_BASE
    params: dict | None  = {"api-version": PRICING_API_VERSION, "$filter": odata}

    async with httpx.AsyncClient(timeout=30) as client:
        while next_url:
            resp = await _get_with_retry(client, next_url, params=params,
                                         headers={"Accept": "application/json"})
            data = resp.json()
            for it in (data.get("Items") or []):
                code = it.get("armRegionName", "")
                prod = it.get("productName", "")
                if (code
                        and prod.startswith("Virtual Machines")
                        and not any(code.startswith(ex) for ex in _EXCLUDE_PREFIXES)):
                    found.add(code)
            next_url = data.get("NextPageLink")
            params   = None

    log.info("Region probe: discovered %d commercial regions", len(found))
    return found


def _build_region_list(codes: set[str]) -> list[dict]:
    rows: list[dict] = []
    for code in codes:
        meta = _REGION_META.get(code)
        city, geo = meta if meta else ("", _guess_geo(code))
        base  = _label_from_code(code)
        label = f"{base} ({city})" if city else base
        rows.append({"code": code, "city": city, "label": label, "geographyGroup": geo})
    rows.sort(key=lambda r: (
        _GEO_ORDER.index(r["geographyGroup"]) if r["geographyGroup"] in _GEO_ORDER else 99,
        r["label"],
    ))
    return rows


async def get_region_list() -> list[dict]:
    """
    Return [{code, city, label, geographyGroup}] for all commercial Azure regions
    that have VM pricing.  Cached 24 h; falls back to _REGION_META on probe failure.

    Discovery logic:
      union(probe_results, _REGION_META.keys()) — so known regions are never lost
      if the probe misses them, and new regions appear automatically once the probe
      finds them (next cache refresh, at most 24 h after GA).
    """
    global _region_cache_state
    cached = _region_cache_state
    if cached and time.monotonic() - cached[0] < _REGION_TTL:
        return cached[1]

    async with _get_region_lock():
        cached = _region_cache_state
        if cached and time.monotonic() - cached[0] < _REGION_TTL:
            return cached[1]

        try:
            discovered = await _probe_retail_regions()
            # Belt-and-suspenders: union with known regions so probe gaps
            # never silently drop a region from the dropdown.
            all_codes  = discovered | set(_REGION_META)
            result     = _build_region_list(all_codes)
        except Exception:
            log.exception("Region probe failed; serving stale/fallback list")
            result = (cached[1] if cached
                      else _build_region_list(set(_REGION_META)))

        _region_cache_state = (time.monotonic(), result)
        log.info("Region list ready: %d regions", len(result))
        return result
