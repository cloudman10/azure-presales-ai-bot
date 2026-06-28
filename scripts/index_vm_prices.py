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

SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_API_KEY  = os.environ["AZURE_SEARCH_API_KEY"]
SPECS_INDEX     = "vm-skus"
PRICES_INDEX    = "vm-sku-prices"
REGION          = "australiaeast"
HOURS           = 730          # hours per month
BATCH           = 100          # upload batch size
PAGE_LIMIT      = 150          # max pages per bulk fetch (150 × 100 = 15 000 items)


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
        log.info("Index '%s' already exists — will upsert documents.", PRICES_INDEX)


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

def build_docs(specs: list[dict], prices: dict[str, dict], updated_at: str) -> list[dict]:
    docs:       list[dict] = []
    no_linux    = 0
    no_windows  = 0

    for s in specs:
        sku  = s["sku_name"]
        p    = prices.get(sku, {})
        base = {
            "sku_name":        sku,
            "region":          REGION,
            "vcpus":           s.get("vcpus") or 0,
            "ram_gb":          s.get("ram_gb") or 0,
            "temp_storage_gb": s.get("temp_storage_gb") or 0,
            "series":          s.get("series") or "?",
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
    if no_linux:
        log.info("  SKUs with no Linux PAYG price:   %d", no_linux)
    if no_windows:
        log.info("  SKUs with no Windows PAYG price: %d", no_windows)
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

    prices     = await fetch_all_prices()
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    docs       = build_docs(specs, prices, updated_at)

    if not docs:
        log.error("No documents built — pricing fetch may have failed")
        return

    upload(docs)
    log.info("=" * 60)
    log.info("Done in %.1fs", time.monotonic() - t0)
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(_main())
