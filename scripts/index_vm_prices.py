"""
scripts/index_vm_prices.py

Populate the vm-sku-prices Azure AI Search index with pricing data.

Stage 1 scope: australiaeast, Linux + Windows.

Uses 2 parallel bulk API calls (Consumption + Reservation for the whole region)
instead of ~1185 per-SKU calls.  SKU spec list is read from the existing vm-skus
index — no ARM dependency.

Run:
    cd ~/azure-presales-ai-bot
    .venv/Scripts/activate   (Windows)   or  source .venv/bin/activate  (Linux)
    python scripts/index_vm_prices.py
"""

import asyncio
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.services.azure_pricing import (
    PRICING_API_BASE,
    PRICING_API_VERSION,
    _get_with_retry,
)

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchableField,
    SearchFieldDataType,
    SearchIndex,
    SimpleField,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SEARCH_ENDPOINT   = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_API_KEY    = os.environ["AZURE_SEARCH_API_KEY"]
SUBSCRIPTION_ID   = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
SPECS_INDEX       = "vm-skus"
PRICES_INDEX      = "vm-sku-prices"
REGION            = "australiaeast"
HOURS             = 730          # hours per month
BATCH             = 100          # upload batch size
PAGE_LIMIT        = 150          # max pages per bulk fetch (150 × 100 = 15 000 items)


# ── ARM deployability gate ────────────────────────────────────────────────────

def fetch_deployable_arm_skus(region: str) -> tuple[set[str], set[str], set[str]]:
    """
    Return (in_arm_skus, arm64_skus, quota_required_skus) for the given region.

    in_arm_skus:         All VM SKU names present in ARM for this region.
                         If ARM returns a SKU (even with restrictions), the Azure portal
                         shows it as "Size not available — Request quota", meaning it IS
                         deployable once quota is granted.  SKUs absent from ARM entirely
                         are genuinely unavailable and should not appear.
    arm64_skus:          Subset of in_arm_skus with CpuArchitectureType == Arm64.
                         These are Linux-only — Windows images are x86-64.
    quota_required_skus: Subset of in_arm_skus with a LOCATION-level restriction
                         (reasonCode = NOT_AVAILABLE_FOR_SUBSCRIPTION, zones=[]).
                         Index these docs with quota_required=True so the grid can badge
                         them.  Zone-only restrictions are NOT quota-required — those SKUs
                         are fully deployable in other zones.

    Key distinction (both use the same ARM reasonCode NOT_AVAILABLE_FOR_SUBSCRIPTION):
      - LOCATION restriction, zones=[] → quota-required (still in ARM, portal shows it)
      - SKU completely absent from ARM  → hard unavailable (never show)

    Uses AzureCliCredential (works locally with 'az login').
    Falls back to ClientSecretCredential if CLI is not available.
    Returns (set(), set(), set()) on failure; callers skip filters in that case.
    """
    if not SUBSCRIPTION_ID:
        log.warning("AZURE_SUBSCRIPTION_ID not set — skipping ARM deployability filter")
        return set(), set(), set()

    from azure.mgmt.compute import ComputeManagementClient

    credential = None
    try:
        from azure.identity import AzureCliCredential
        credential = AzureCliCredential()
    except Exception:
        pass

    if credential is None:
        tenant = os.environ.get("AZURE_TENANT_ID", "")
        client = os.environ.get("AZURE_CLIENT_ID", "")
        secret = os.environ.get("AZURE_CLIENT_SECRET", "")
        if tenant and client and secret:
            from azure.identity import ClientSecretCredential
            credential = ClientSecretCredential(tenant, client, secret)

    if credential is None:
        log.warning("No ARM credential available — skipping deployability filter")
        return set(), set(), set()

    try:
        compute = ComputeManagementClient(credential, SUBSCRIPTION_ID)
        all_skus = list(compute.resource_skus.list(filter=f"location eq '{region}'"))

        def _has_location_restriction(sku) -> bool:
            # True if the SKU has a LOCATION-level restriction with empty zones list.
            # This means quota must be requested before deployment.
            # Zone-only restrictions (zones=['1']) = deployable in other zones — not quota-required.
            for r in (sku.restrictions or []):
                zones = (r.restriction_info.zones if r.restriction_info else []) or []
                if str(r.type).upper().endswith("LOCATION") and not zones:
                    return True
            return False

        in_arm:         set[str] = set()
        arm64:          set[str] = set()
        quota_required: set[str] = set()

        for s in all_skus:
            if s.resource_type != "virtualMachines":
                continue
            in_arm.add(s.name)
            caps = {c.name: c.value for c in (s.capabilities or [])}
            if caps.get("CpuArchitectureType", "").lower() == "arm64":
                arm64.add(s.name)
            if _has_location_restriction(s):
                quota_required.add(s.name)

        deployable = in_arm - quota_required
        log.info("ARM SKUs for %s: %d total — %d deployable, %d quota-required, %d Arm64",
                 region, len(in_arm), len(deployable), len(quota_required), len(arm64))
        if quota_required:
            sample = sorted(quota_required)[:8]
            log.info("  Quota-required sample: %s", sample)
        return in_arm, arm64, quota_required
    except Exception as e:
        log.warning("ARM SKU fetch failed — skipping deployability filter: %s", e)
        return set(), set(), set()


def cleanup_arm64_windows_docs(arm64_skus: set[str], region: str) -> int:
    """
    Delete Windows index docs for Arm64 SKUs.
    Arm64 SKUs are deployable but Linux-only; no Windows images exist for them.
    The normal phantom cleanup won't catch these because the SKUs ARE in the
    deployable set — they just must not have Windows docs.
    """
    if not arm64_skus:
        return 0
    client = SearchClient(SEARCH_ENDPOINT, PRICES_INDEX, AzureKeyCredential(SEARCH_API_KEY))
    results = client.search(
        search_text="*",
        filter=f"region eq '{region}' and os eq 'Windows'",
        select=["id", "sku_name"],
        top=5000,
    )
    rows = list(results)
    to_delete = [{"id": r["id"]} for r in rows if r["sku_name"] in arm64_skus]
    if not to_delete:
        log.info("Arm64 Windows cleanup: no stale docs found — index is clean")
        return 0
    skus_found = sorted({r["sku_name"] for r in rows if r["sku_name"] in arm64_skus})
    log.info("Arm64 Windows cleanup: deleting %d stale Windows docs: %s",
             len(to_delete), skus_found)
    for i in range(0, len(to_delete), BATCH):
        client.delete_documents(documents=to_delete[i : i + BATCH])
    log.info("Arm64 Windows cleanup: done")
    return len(to_delete)


def cleanup_phantom_docs(deployable_skus: set[str], region: str) -> int:
    """
    Delete documents from the price index whose sku_name is NOT in deployable_skus.
    These are restricted or phantom SKUs that should not appear in the comparison grid.
    Returns number of documents deleted.
    """
    client = SearchClient(SEARCH_ENDPOINT, PRICES_INDEX, AzureKeyCredential(SEARCH_API_KEY))
    results = client.search(
        search_text="*",
        filter=f"region eq '{region}'",
        select=["id", "sku_name"],
        top=5000,
    )
    phantoms = [dict(r) for r in results if r["sku_name"] not in deployable_skus]
    if not phantoms:
        log.info("Cleanup: no phantom docs found — index is clean")
        return 0
    log.info("Cleanup: deleting %d phantom docs (restricted/non-deployable SKUs)...", len(phantoms))
    sample = sorted({d["sku_name"] for d in phantoms})[:10]
    log.info("  Sample phantom SKUs: %s", sample)
    delete_keys = [{"id": d["id"]} for d in phantoms]
    for i in range(0, len(delete_keys), BATCH):
        client.delete_documents(documents=delete_keys[i : i + BATCH])
    log.info("Cleanup: deleted %d documents", len(phantoms))
    return len(phantoms)


# ── Index definition ──────────────────────────────────────────────────────────

def _index_def() -> SearchIndex:
    F = SearchFieldDataType

    def simple(name, typ, *, filterable=True, sortable=False, facetable=False):
        return SimpleField(
            name=name, type=typ,
            filterable=filterable, sortable=sortable, facetable=facetable,
        )

    return SearchIndex(
        name=PRICES_INDEX,
        fields=[
            SimpleField(name="id", type=F.String, key=True, filterable=False),
            SearchableField(
                name="sku_name", type=F.String,
                filterable=True, sortable=True, facetable=True,
            ),
            simple("region",          F.String,       facetable=True),
            simple("os",              F.String,       facetable=True),
            simple("vcpus",           F.Int32,        sortable=True),
            simple("ram_gb",          F.Int32,        sortable=True),
            simple("temp_storage_gb", F.Int32,        sortable=True),
            simple("series",          F.String,       facetable=True),
            simple("architecture",     F.String,       facetable=True),
            simple("quota_required",  F.Boolean),
            simple("retired",         F.Boolean),
            simple("payg_hourly",     F.Double,       sortable=True),
            simple("payg_monthly",    F.Double,       sortable=True),
            simple("sp_1yr_monthly",  F.Double,       sortable=True),
            simple("sp_3yr_monthly",  F.Double,       sortable=True),
            simple("ri_1yr_monthly",  F.Double,       sortable=True),
            simple("ri_3yr_monthly",  F.Double,       sortable=True),
            simple("spot_hourly",     F.Double,       sortable=True),
            simple("price_updated_at",F.DateTimeOffset, sortable=True),
        ],
    )


def ensure_index() -> None:
    idx = SearchIndexClient(SEARCH_ENDPOINT, AzureKeyCredential(SEARCH_API_KEY))
    existing = {i.name for i in idx.list_indexes()}
    if PRICES_INDEX not in existing:
        log.info("Creating index '%s' ...", PRICES_INDEX)
        idx.create_index(_index_def())
        log.info("Index created.")
    else:
        log.info("Updating schema for '%s' (adding new fields if any) ...", PRICES_INDEX)
        idx.create_or_update_index(_index_def())
        log.info("Schema up to date — will upsert documents.")


# ── Read active SKU specs from existing vm-skus index ─────────────────────────

def read_sku_specs() -> list[dict]:
    client = SearchClient(SEARCH_ENDPOINT, SPECS_INDEX, AzureKeyCredential(SEARCH_API_KEY))
    results = client.search(
        search_text="*",
        filter="retired eq false",
        select=["sku_name", "vcpus", "ram_gb", "temp_storage_gb", "series"],
    )
    docs = [dict(r) for r in results]
    log.info("Read %d active SKU specs from '%s'", len(docs), SPECS_INDEX)
    return docs


# ── Bulk price fetch ──────────────────────────────────────────────────────────

async def _bulk_fetch(price_type: str) -> list[dict]:
    """
    Fetch ALL VM price items for REGION for the given priceType.
    No OS filter — we split Linux/Windows in Python.
    """
    odata = (
        f"serviceName eq 'Virtual Machines' "
        f"and armRegionName eq '{REGION}' "
        f"and priceType eq '{price_type}'"
    )
    all_items: list[dict] = []
    next_url: str | None = PRICING_API_BASE
    params: dict | None = {"api-version": PRICING_API_VERSION, "$filter": odata}
    pages = 0

    async with httpx.AsyncClient(timeout=60) as client:
        while next_url and pages < PAGE_LIMIT:
            resp = await _get_with_retry(
                client, next_url, params=params,
                headers={"Accept": "application/json"},
            )
            if not resp.is_success:
                log.error("Bulk fetch HTTP %d (priceType=%s page=%d)",
                          resp.status_code, price_type, pages)
                break
            data = resp.json()
            all_items.extend(data.get("Items") or [])
            next_url = data.get("NextPageLink")
            params = None
            pages += 1
            if pages % 10 == 0:
                log.info("  %s: %d pages, %d items ...", price_type, pages, len(all_items))

    log.info("_bulk_fetch priceType=%s: %d pages, %d items", price_type, pages, len(all_items))
    return all_items


async def fetch_all_prices() -> dict[str, dict]:
    """
    Fetch Consumption (PAYG + Spot) and Reservation (RI) items in parallel.
    Returns {armSkuName: price_data_dict}.

    price_data_dict keys:
      linux_payg        — raw item | None
      linux_sp_1yr      — hourly rate | None  (from savingsPlan field)
      linux_sp_3yr      — hourly rate | None
      linux_spot        — raw item | None
      linux_ri_1yr      — monthly USD | None
      linux_ri_3yr      — monthly USD | None
      windows_payg      — raw item | None
      windows_ri_1yr    — monthly USD | None
      windows_ri_3yr    — monthly USD | None
    """
    log.info("Fetching prices for region=%s ...", REGION)
    consumption = await _bulk_fetch("Consumption")
    reservation = await _bulk_fetch("Reservation")

    prices: dict[str, dict] = {}

    def _entry(arm: str) -> dict:
        if arm not in prices:
            prices[arm] = {
                "linux_payg":   None, "linux_sp_1yr": None, "linux_sp_3yr": None,
                "linux_spot":   None,
                "linux_ri_1yr": None, "linux_ri_3yr": None,
                "windows_payg": None,
                "windows_ri_1yr": None, "windows_ri_3yr": None,
            }
        return prices[arm]

    # ── Process Consumption items (PAYG + Spot) ───────────────────────────────
    for it in consumption:
        arm     = it.get("armSkuName", "").strip()
        sku_n   = it.get("skuName", "")
        product = it.get("productName", "")
        retail  = it.get("retailPrice", 0.0)
        if not arm or not retail:
            continue

        is_win  = "Windows" in product
        is_spot = "Spot" in sku_n
        is_low  = "Low Priority" in sku_n

        e = _entry(arm)

        if is_spot and not is_win:
            # Capture lowest Spot price if multiple items appear
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
                    # Savings Plan rates live on the Linux PAYG item
                    for sp in (it.get("savingsPlan") or []):
                        term = sp.get("term", "")
                        r    = sp.get("retailPrice")
                        if r:
                            if "1 Year" in term:
                                e["linux_sp_1yr"] = r
                            elif "3 Year" in term:
                                e["linux_sp_3yr"] = r

    # ── Process Reservation items ─────────────────────────────────────────────
    # NOTE: retailPrice for Reservation items is the TOTAL TERM COMMITMENT COST
    # (e.g., $1297 for a 1-year RI), NOT an hourly rate — despite unitOfMeasure='1 Hour'.
    # This is a known Azure Retail Prices API quirk. Monthly = annual/12 or 3yr/36.
    # Azure does not publish separate Windows RI items; Windows VMs use the Linux RI
    # infrastructure rate + AHUB (Azure Hybrid Benefit) for the OS licence.
    for it in reservation:
        arm    = it.get("armSkuName", "").strip()
        retail = it.get("retailPrice", 0.0)
        if not arm or not retail:
            continue

        # reservationTerm field is authoritative; fall back to skuName parsing
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

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info("Price data parsed for %d unique SKUs", len(prices))
    spot_skus = [k for k, v in prices.items() if v["linux_spot"]]
    sp_skus   = [k for k, v in prices.items() if v["linux_sp_1yr"]]
    ri_skus   = [k for k, v in prices.items() if v["linux_ri_1yr"]]
    log.info("  Linux PAYG:        %d SKUs", sum(1 for v in prices.values() if v["linux_payg"]))
    log.info("  Windows PAYG:      %d SKUs", sum(1 for v in prices.values() if v["windows_payg"]))
    log.info("  Spot prices:       %d SKUs", len(spot_skus))
    log.info("  Savings Plan:      %d SKUs", len(sp_skus))
    log.info("  RI (Linux 1yr):    %d SKUs", len(ri_skus))
    if spot_skus:
        log.info("  Sample Spot SKUs:  %s", spot_skus[:5])

    return prices


# ── Build documents ───────────────────────────────────────────────────────────

def build_docs(
    specs: list[dict],
    prices: dict[str, dict],
    updated_at: str,
    arm64_skus: set[str] = frozenset(),
    quota_required_skus: set[str] = frozenset(),
) -> list[dict]:
    docs:         list[dict] = []
    no_linux      = 0
    no_windows    = 0
    arm64_skipped = 0
    quota_count   = 0

    for s in specs:
        sku      = s["sku_name"]
        is_arm64 = sku in arm64_skus
        is_quota = sku in quota_required_skus
        if is_quota:
            quota_count += 1
        p        = prices.get(sku, {})
        base     = {
            "sku_name":        sku,
            "region":          REGION,
            "vcpus":           s.get("vcpus") or 0,
            "ram_gb":          s.get("ram_gb") or 0,
            "temp_storage_gb": s.get("temp_storage_gb") or 0,
            "series":          s.get("series") or "?",
            "architecture":    "Arm64" if is_arm64 else "x64",
            "quota_required":  is_quota,
            "retired":         False,
            "price_updated_at": updated_at,
        }

        # Linux document
        lp = p.get("linux_payg")
        if lp:
            hr   = lp["retailPrice"]
            sp1  = p.get("linux_sp_1yr")
            sp3  = p.get("linux_sp_3yr")
            spot = p.get("linux_spot")
            docs.append({
                **base,
                "id":            str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{sku}:{REGION}:Linux")),
                "os":            "Linux",
                "payg_hourly":   round(hr, 6),
                "payg_monthly":  round(hr * HOURS, 4),
                "sp_1yr_monthly": round(sp1 * HOURS, 4) if sp1 else None,
                "sp_3yr_monthly": round(sp3 * HOURS, 4) if sp3 else None,
                "ri_1yr_monthly": round(p["linux_ri_1yr"], 4) if p.get("linux_ri_1yr") else None,
                "ri_3yr_monthly": round(p["linux_ri_3yr"], 4) if p.get("linux_ri_3yr") else None,
                "spot_hourly":   round(spot["retailPrice"], 6) if spot else None,
            })
        else:
            no_linux += 1

        # Windows document
        # Arm64 SKUs are Linux-only: no Windows images exist for Ampere/Arm64 VMs
        # in australiaeast. The Retail Prices API returns Windows prices speculatively,
        # but the portal correctly hides these SKUs from the Windows size selector.
        if is_arm64:
            arm64_skipped += 1
            continue

        # RI rates: Azure publishes Linux-only RI items; Windows uses the same
        # infrastructure rate via AHUB. No separate Windows RI items in API.
        wp = p.get("windows_payg")
        if wp:
            hr_w = wp["retailPrice"]
            sp1  = p.get("linux_sp_1yr")   # SP uses Linux hourly rates
            sp3  = p.get("linux_sp_3yr")
            ri1  = p.get("linux_ri_1yr")   # RI = Linux infra commitment (AHUB model)
            ri3  = p.get("linux_ri_3yr")
            docs.append({
                **base,
                "id":            str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{sku}:{REGION}:Windows")),
                "os":            "Windows",
                "payg_hourly":   round(hr_w, 6),
                "payg_monthly":  round(hr_w * HOURS, 4),
                "sp_1yr_monthly": round(sp1 * HOURS, 4) if sp1 else None,
                "sp_3yr_monthly": round(sp3 * HOURS, 4) if sp3 else None,
                "ri_1yr_monthly": round(ri1, 4) if ri1 else None,
                "ri_3yr_monthly": round(ri3, 4) if ri3 else None,
                "spot_hourly":   None,
            })
        else:
            no_windows += 1

    linux_count   = sum(1 for d in docs if d["os"] == "Linux")
    windows_count = sum(1 for d in docs if d["os"] == "Windows")
    log.info("Built %d documents: %d Linux, %d Windows", len(docs), linux_count, windows_count)
    if quota_count:
        log.info("  Quota-required SKUs (badged in grid): %d", quota_count)
    if arm64_skipped:
        log.info("  Arm64 SKUs (Windows doc skipped):     %d", arm64_skipped)
    if no_linux:
        log.info("  SKUs with no Linux PAYG price:        %d", no_linux)
    if no_windows:
        log.info("  SKUs with no Windows PAYG price:      %d", no_windows)
    return docs


# ── Upload ────────────────────────────────────────────────────────────────────

def upload(docs: list[dict]) -> None:
    client = SearchClient(SEARCH_ENDPOINT, PRICES_INDEX, AzureKeyCredential(SEARCH_API_KEY))
    total  = len(docs)
    done   = 0
    for i in range(0, total, BATCH):
        batch  = docs[i : i + BATCH]
        result = client.merge_or_upload_documents(documents=batch)
        ok     = sum(1 for r in result if r.succeeded)
        done  += ok
        log.info(
            "Batch %d/%d — %d/%d ok",
            i // BATCH + 1, -(-total // BATCH), ok, len(batch),
        )
    log.info("Upload done: %d/%d documents", done, total)


# ── Entry point ───────────────────────────────────────────────────────────────

async def _main() -> None:
    t0 = time.monotonic()
    log.info("=" * 60)
    log.info("VM Pricing Indexer — Stage 1")
    log.info("  Region : %s", REGION)
    log.info("  OS     : Linux + Windows")
    log.info("  Index  : %s -> %s", SPECS_INDEX, PRICES_INDEX)
    log.info("=" * 60)

    ensure_index()
    specs = read_sku_specs()
    if not specs:
        log.error("No active SKUs in '%s' — run index_vm_skus.py first", SPECS_INDEX)
        return

    # ARM gate — returns all SKUs known to ARM for this region:
    #   in_arm_skus:         index all of these (includes quota-required)
    #   arm64_skus:          skip Windows doc for these (Linux-only architecture)
    #   quota_required_skus: mark with quota_required=True (portal: "Request quota")
    # SKUs absent from ARM entirely are genuinely unavailable and excluded below.
    in_arm_skus, arm64_skus, quota_required_skus = fetch_deployable_arm_skus(REGION)
    if in_arm_skus:
        before = len(specs)
        specs  = [s for s in specs if s["sku_name"] in in_arm_skus]
        excluded = before - len(specs)
        log.info(
            "ARM filter: %d → %d specs (%d absent from ARM — genuinely unavailable)",
            before, len(specs), excluded,
        )
        log.info("Arm64 SKUs (Linux-only, Windows doc skipped): %d", len(arm64_skus))
        log.info("Quota-required SKUs (deployable after quota request): %d",
                 len(quota_required_skus))
    else:
        log.warning("ARM filter skipped — indexing all specs (may include phantoms)")

    prices     = await fetch_all_prices()
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    docs       = build_docs(specs, prices, updated_at,
                            arm64_skus=arm64_skus,
                            quota_required_skus=quota_required_skus)

    if not docs:
        log.error("No documents built — pricing fetch may have failed")
        return

    upload(docs)

    # Remove docs for SKUs no longer in ARM (genuinely unavailable)
    if in_arm_skus:
        cleanup_phantom_docs(in_arm_skus, REGION)
    # Remove stale Windows docs for Arm64 SKUs (phantom cleanup won't catch these)
    if arm64_skus:
        cleanup_arm64_windows_docs(arm64_skus, REGION)
    log.info("=" * 60)
    log.info("Done in %.1fs", time.monotonic() - t0)
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(_main())
