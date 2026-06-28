# HyperXen Azure Presales AI Bot — Master Reference

> Single source of truth for architecture, status, resources, and deployment.
> Raw URL (once repo is public): https://raw.githubusercontent.com/cloudman10/azure-presales-ai-bot/main/MASTER.md

---

## Current Status (2026-06-28) — v2.1.0

### Last Known-Good State (2026-06-28)
- Commit: `bc27f52` (main) — tag: **compare-prices-1.0**
- Status: dev healthy. Compare Azure Prices tool fully deployed and validated (subscription-accurate SKU list, Arm64 gate, architecture badges, RAM min/max filter, Windows default). Pricing bot and Solution Architecture Designer unchanged and operational.
- Rollback if a future deploy breaks the app:
  ```bash
  git checkout main
  git reset --hard bc27f52
  git push origin main --force
  ```
  (or safer: `git revert <bad-commit> --no-edit && git push origin main`)
- Previous stable baseline (Solution Architecture Designer, pre-compare): tag `v-arch-svg-1.0`, commit `3c41f95`.

| Item | Status |
|------|--------|
| Frontend (Replit UI) | ✅ Live |
| Backend (Azure App Service) | ✅ Live — https://hyperxen-pricing-bot-db5hmngq3woxa.azurewebsites.net |
| Dev App | ✅ Live and healthy — https://hyperxen-pricing-bot-dev.azurewebsites.net |
| LLM — GPT-4o via Azure AI Foundry (all paths) | ✅ Verified working |
| Azure AI Search (vm-skus) | ✅ Indexed (1185 active SKUs, re-indexed 2026-06-20) |
| Azure AI Search (vm-sku-prices) | ✅ Indexed (1,975 docs: 1,036 Linux + 939 Windows, australiaeast) |
| CORS middleware | ✅ Added |
| Git repo | ✅ Public — https://github.com/cloudman10/azure-presales-ai-bot |
| Dev Environment | ✅ Live — https://dev.hyperxen.com |
| CI/CD Pipeline | ✅ GitHub Actions — auto deploy on push to dev and main |

### All systems operational
Test: `curl https://hyperxen-pricing-bot-db5hmngq3woxa.azurewebsites.net/api/welcome`

### Multi-VM Quote Basket (2026-06-14) — COMPLETE

Users can build a multi-VM quote inside the chat UI and export it as Excel or PDF.

**Basket model (numeric, server-side):**
Each line item: `{id, sku, os, region, term, count, vm_unit_cost, disks[], line_total, pricing_text?}`.
`line_total = round((vm_unit_cost + sum(disk.cost)) × count, 4)` — computed server-side on add.
`grand_total = sum(line_total for item in basket)` — always from numeric fields, never text-parsed.

**Backend (`app/routers/basket.py`, `app/state.py`):**
- `POST /api/basket` — add item; returns updated basket list.
- `GET /api/basket` — fetch basket for session.
- `DELETE /api/basket/{item_id}` — remove one line.
- `DELETE /api/basket` — clear basket.
- `GET /api/basket/total` — `{grand_total, item_count}`.
- `POST /api/basket/report/excel` and `/pdf` — structured export (no text-blob parsing).
- Sessions stored in `sessions["{sid}_basket"]` via shared `app/state.py` dict.

**Frontend (`static/index.html`):**
- Header Quote button + live badge (item count).
- Per-card Qty input (default 1) + "Add to Quote" button; captures live dropdown disk state.
- Slide-in Quote Summary drawer: per-line label, VM+storage detail, Remove button, grand total.
- Export Excel / Export PDF buttons in drawer footer (shown when basket non-empty).
- Basket restores on page refresh via `loadBasket()` at init.
- `card._getDiskState()` closure reads live dropdown state at click time — no stale captures.

**Export (`app/agents/report_agent.py`):**
`generate_excel_basket(items, grand_total)` and `generate_pdf_basket(items, grand_total)` — build structured multi-item reports. Per-item: section header `{count}× {sku} | {os} | {region}`, VM cost row, one disk row per disk, line total. GRAND TOTAL at bottom, footnotes. PDF header uses a 2-row Table (both rows `#0078D4`) so title and subtitle are guaranteed stacked with no overlap; subtitle in `#D4E8FF` for readable contrast on blue.

**Alt-region pick fix:**
`_picks` now carries `sku_region_displays[]` (one per option). `fetchPricingForPicks` uses `picks.sku_region_displays[idx]` instead of the global `picks.region_display`, so alt-region fills (e.g. `[Available in Australia East]` options returned for a Melbourne query) are priced in their actual source region — fixes "pricing fetch failed" on those picks.

**Known ASCII-only rule for generated docs:**
Azure/reportlab environment garbles non-ASCII (box-drawing `│` → tofu box; middle-dot `·` → `Â·`). All generated Excel/PDF text must use ASCII separators (`|` or `-`). See section below.

### Advisor deployability gate (2026-06-20) — COMPLETE

Gates advisor recommendations on ARM Compute SKU deployability — prevents recommending or pricing SKUs that exist in the Azure Retail Prices API catalogue but are not actually deployable in the requested region.

**Implementation (`app/services/azure_pricing.py`, `app/agents/sku_advisor_agent.py`):**
- `_get_arm_skus_for_region(region)` — paginated ARM Compute SKUs fetch with 1-hour TTL module-level cache (`_arm_sku_cache`). Shared by `fetch_deployable_skus`, `fetch_temp_storage_gb`, and `vm_supports_premium` — net cost is 1 ARM call per region per hour regardless of how many advisor queries or disk/premium lookups follow.
- `fetch_deployable_skus(region)` — returns `set[str]` of VM SKU names deployable in that region per ARM (`resourceType == 'virtualMachines'`). Returns empty set on failure; callers skip the filter on empty (fail-open, not fail-closed).
- Advisor fires `fetch_deployable_skus` and `fetch_vm_prices_for_region` concurrently via `asyncio.gather` — ARM call overlaps the Prices API pagination, no serialised wait.
- After `_is_standard` filter, advisor filters out any candidate whose `armSkuName` is absent from the deployable set. Logs `excluded=N remaining=M` for observability.

**Root cause fixed:** Azure Retail Prices API publishes "projected" prices for catalogue SKUs with `isPrimaryMeterRegion=false` — these are not deployable in that region, just priced at a projected rate. ARM Compute SKUs is authoritative. Example: `Standard_E4-2as_v6` appeared with a valid `$360.62/mo` price in australiasoutheast but was absent from ARM — the gate now excludes it before scoring.

**Verified (2026-06-20):**
- Melbourne/australiasoutheast, 4 vCPU, 6 GB, Windows → 3 options: D4als_v6, E4as_v6, B4als_v2. All in ARM. No E4-2as_v6. No "?" RAM.
- Australia East, 4 vCPU, 16 GB, Windows → 3 options: D4als_v7, E4-2as_v7, B4pls_v2. All in ARM. No "?" RAM.
- australiasoutheast: 865 deployable SKUs (vs 1185 in australiaeast); ~320 SKUs filtered without over-filtering.

### AI Search index (2026-06-20) — COMPLETE

**Re-indexed:** 1,185 SKUs including Easv6 variants (`Standard_E2as_v6` through `Standard_E96as_v6` and EC variants). Previously missing from index because indexer was only run against australiaeast ARM at initial setup.

**Auth fix (`scripts/index_vm_skus.py`):** Switched from `ClientSecretCredential` (`.env` client secret had expired → `AADSTS90013: Invalid input received from the user`) to `DefaultAzureCredential` — uses `az` CLI credentials locally, Managed Identity in production. No service principal secret needed.

**Important query note:** AI Search full-text search tokenises on underscores — `E4as_v6` tokenises as `E4as` + `v6` and may return 0 hits. Always use a filter query for exact SKU lookups: `$filter=sku_name eq 'Standard_E4as_v6'`. The advisor's search path uses `search.ismatch` / scored text search which is unaffected (token-level match works for recommendation ranking).

**Index staleness:** The index is a point-in-time snapshot of australiaeast ARM SKUs. It drifts as Azure adds/retires SKUs. The ARM deployability gate (`fetch_deployable_skus`) is the correctness gate — a stale index causes "?" in specs but never causes a bad SKU recommendation. Consider scheduling the indexer. [deferred]

### Quote — pricing term selection (2026-06-20) — COMPLETE

Users can select one pricing term per VM card before adding to the quote. The selected term's monthly cost and label feed the basket line.

**Term selection (`static/index.html`):**
- Radio button placed inside each pricing term's own header — no separate selection block. PAYG default. Exactly one term selectable per card (single radio group, unique `tc{n}` name per card).
- Available terms: PAYG (hero block), 1-Year Savings Plan, 3-Year Savings Plan, 1-Year Reserved Instance, 3-Year Reserved Instance. Windows-only additions: PAYG + HB, 1-Year RI + HB, 3-Year RI + HB. Linux cards: no +HB rows.
- Clicking a term's radio selects it without expanding the breakdown body (`stopPropagation` prevents header click from firing). Clicking the rest of the header still toggles the accordion. SP/RI: radio + total monthly price in the collapsible header. +HB rows: same collapsible style, each with its own radio.
- `card._getSelectedTerm()` → `{label, monthly}` reads the checked radio; `addToQuote()` sends `vm_unit_cost: monthly, term: label`. Mixed terms across basket lines fully supported.

**+HB = Linux-equivalent rate (compute only, Windows licence removed):**
- +HB values are actual Linux prices fetched from the Azure Retail Prices API — not a percentage approximation. `PAYG + HB = linux_payg['retailPrice'] * 730`; `1/3-Year RI + HB = ri_monthly(find_price(items, 'Linux', 'Reservation', '1/3 Year'))`. No backend change was needed — the pricing agent already fetches Linux rates directly.
- Verified: D4s_v5 Australia East — Windows PAYG $309.52/mo, Linux PAYG $175.20/mo. $309.52 − $134.32 (Windows licence RRP) = $175.20. PAYG + HB matches Linux PAYG exactly.
- Each +HB row expands to: `Compute: $X/month` (the Linux-equivalent rate) + `License: $0.00/month  (Azure Hybrid Benefit)` + `Total: $X/month`. BYO-licence assumption is explicit inline.

**Basket + export:**
- Storage stays undiscounted regardless of term (RI/SP/HB discounts apply to compute only; disk costs added at list price).
- Basket drawer label: `Nx Standard_D4s_v5 (3-Year RI + HB)` — SKU + term in parentheses, monthly line total.
- Excel/PDF: VM row description is `VM - {term}` (e.g. `VM - 3-Year Reserved Instance`).
- +HB footnote appended to export when any basket line carries a `+ HB` term: `"+ HB lines assume customer owns eligible Azure Hybrid Benefit (Windows Server) licenses."`
- Monthly figures only throughout — no annual or committed-cost totals shown.

**Note — SP + HB not offered:** No Azure Retail Prices API data exists for Savings Plan + Hybrid Benefit combined. RI + HB is available and shown. SP + HB is omitted; flag as a possible future addition if a customer asks.

---

### Storage Pricing — Phase 1 §2.2 (2026-06-08) — COMPLETE
Managed disk pricing for VM workloads with interactive selector. Live Azure Retail Prices API, no hardcoded prices.

**Model:** Disks bill by provisioned tier (fixed size → fixed monthly price), NOT per-GB.
Standard HDD (S) / Standard SSD (E) / Premium SSD (P) tier-based via `pick_tier()`; Premium SSD v2 per-GiB linear, capacity-only at free baseline (3000 IOPS / 125 MB/s). Tier→size table static; prices always live-fetched.

**Default OS disk:** Injected in code via shared `resolve_disks()` (used by both `pricing_agent.run()` and `sku_advisor_agent._show_full_pricing()` — unified so paths can't drift). Premium SSD P10 (128 GiB) when premium-capable, else Standard SSD E10.

**Interactive selector (commit 7d6b537):** Card renders type + size dropdowns on each disk row + "Add data disk" button. Backend emits `STORAGE_DATA` JSON (all eligible tier prices for the VM/region, premium-gated). Dropdowns re-price client-side instantly, no server round-trip. Verified: P10→P20 live update on size change.

**Premium gating:** `vm_supports_premium()` reads `PremiumIO` from ARM Compute SKU capabilities. Premium types excluded from `STORAGE_DATA` on non-premium VMs, so dropdown can't offer them; code also downgrades. Applies to defaults and user picks.

**Output:** `=== Storage ===` block in card + Excel + PDF. Footnotes: `*` Standard SSD/HDD capacity-only (transactions excluded); `**` v2 baseline caveat; Premium SSD none.

**Pricing verified correct:** Matched live API to the cent for E4-2as_v7 Windows AU East. Earlier calc discrepancy was a SKU mismatch (as_v7 vs ads_v7), not an error.

**Caching fix (commit c7e1486):** `Cache-Control: no-cache, no-store, must-revalidate` on `/` route — inline JS in `index.html` was being served stale (cost significant debug time; diagnose cache early next time via InPrivate window).

**Deferred to Phase 2:** Standard SSD/HDD transaction costs, full v2 IOPS/throughput, snapshots/backup, blob, Files premium.

### Document generation — ASCII only (2026-06-14)
Azure App Service + reportlab environment garbles non-ASCII characters in generated Excel/PDF files:
- Middle-dot `·` (U+00B7) → `Â·` (UTF-8 bytes misread as Latin-1)
- Box-drawing `│` (U+2502) → tofu box (not in reportlab built-in font's Latin-1 glyph set)
- Em-dash `—` (U+2014) → similar garbling risk

**Rule: use only ASCII in all text written to Excel cells or PDF paragraphs/tables.** Use `|` or ` - ` as separators, not `│`, `·`, or `—`. This applies to `report_agent.py` and any future document-generation code. Recurring issue — bake into every code review of doc generation.

### Known limitations / before-prod (logged 2026-06-14)

**Single-worker dev constraint:**
`startup.sh` runs `-w 1` (single gunicorn worker) so the in-memory basket and session dict are always consistent during development. This is a dev-only workaround.

**⚠ Redis required before basket goes to prod (4 workers):**
All session state (conversation history, advisor picks, quote basket) lives in a plain Python `dict` in `app/state.py` — in-process, per-worker, no persistence. With 4 workers, each request may land on a different process with its own empty dict. The basket silently appears empty on ~75% of prod requests if promoted as-is. This is also the root cause of the "session memory cleared" advisor bug.

**Migration path:** replace `app/state.py` with a Redis-backed adapter (e.g. `aioredis`); key schema stays the same (`{sid}`, `{sid}_advisor_state`, `{sid}_basket`, etc.). Azure Cache for Redis is the standard choice for App Service.

### Known bugs to fix (logged 2026-06-08, not yet addressed)
- Advisor renders literal `**1**`/`**2**`/`**3**` (markdown asterisks not rendering in advisor replies)
- Advisor spec lookup fails for older SKUs (Standard_A6 shows "? GB RAM", wrong vCPU count, no "Best for" line)
- Advisor "session memory cleared" on some option-picks via API/refresh path (root cause: multi-worker — fixed by Redis migration)
- Standard HDD size dropdown can render empty → `$0.00` disk cost shown on card
- Minor formatter spacing nits ("Microsoft RetailRRP", "Capacity only;per-10K")

### Next / deferred
- **SP + HB combination** — no Azure Retail Prices API data for Savings Plan + Hybrid Benefit combined; RI + HB is available and shown. Flag as a possible future addition if a customer specifically asks.
- **Quote history / save** — deferred. Options: (a) new-conversation confirm guard; (b) client-side save/restore to JSON file; (c) named server-side saved quotes (requires Redis + auth). Redis migration gates all server-side persistence options.
- **Session/basket persistence migration** — Redis (Azure Cache for Redis) required before prod basket + saved quotes. Unblocks basket on prod and fixes the multi-worker advisor "session cleared" bug.
- **Phase 2 storage features** — Standard SSD/HDD transaction costs, full v2 IOPS/throughput, snapshots/backup, blob storage, Azure Files premium.
- **AI Search index scheduled re-run** — index is a manual snapshot; drifts as Azure adds SKUs. ARM gate prevents wrong recommendations from stale index (worst case = "?" specs). Schedule weekly/monthly re-index via Azure Function or GitHub Actions cron. [deferred]

### v1.3.0 Changes (2026-06-07)
- **hyperxen.ai live:** Azure managed SSL cert bound (`AA7A318E...`, expires 2026-12-06); `https://hyperxen.ai` returns HTTP 200
- **429 retry fix:** Azure Retail Prices API rate limit handling upgraded to 8 retries with exponential backoff (2s → 4s → 8s → 16s → 32s cap) and 0–1s random jitter to avoid thundering herd
- **Session loss handling:** when a worker restart wipes in-memory session state and user selects an option number (e.g. "3") with no prior context, app returns a clear "session expired, please repeat your requirements" message instead of a generic error
- **Dynamic SKU search:** always returns 3 options for any region by querying Azure Retail Prices API directly — no hardcoded series lists
- **Alt-region label:** `[Available in Australia East]` label only appears on options sourced from the fallback region, not on options from the requested region

### v1.2.1 Fixes (2026-06-06)
- Linux/Windows OS reply after advisor picks now stays in advisor flow instead of routing to `pricing_agent` — re-runs STATE 4 with the new OS, same region and specs
- Azure Retail Prices API Linux filter fixed: `not contains(productName, 'Windows')` is unsupported by the API; now fetches all and filters by OS in Python

### v1.2.0 Changes (2026-06-03)
- **SKU advisor fully dynamic:** queries Azure Retail Prices API directly — no hardcoded series names or SKU lists; works for any region, any VM family, any core count
- **Full region coverage:** returns 3 options even for limited regions like Australia Southeast (was returning only 2 due to Azure AI Search index gaps)
- **Speed:** concurrent metadata lookups via `asyncio.gather()`; response time under 20 seconds (was up to 2 minutes with sequential per-SKU API calls)
- **Alt-region fill:** when fewer than 3 options exist in the requested region, fills remaining slots from Australia East clearly labelled `[Available in Australia East]`
- INFO-level logging enabled globally (`logging.basicConfig(level=INFO)`) — SKU advisor debug output now visible in App Service logs
- Pricing verification now uses `asyncio.gather()` throughout

### v1.1.0 Fixes (2026-05-11)
- Accordion pricing now loads full data when selecting a SKU from advisor results
- Bare numbers (e.g. "1") no longer reset conversation context
- Spec-based queries (e.g. "6 cores 8GB RAM") now route to SKU advisor
- Removed 10,457 stale log/zip files from git tracking
- Added `.gitignore` entries for `app_logs*/`, `*.zip`, `dev_logs*/`, `debug.log`

### Known Issues
- See "Known bugs to fix" under Storage Pricing §2.2 above

---

## DNS & Domain Status (2026-06-07)

| Domain | Status |
|--------|--------|
| hyperxen.ai | ✅ Live — A record → `20.211.64.31`, Azure managed cert bound (thumbprint `AA7A318E`, expires 2026-12-06), `httpsOnly: true` |
| www.hyperxen.ai | CNAME → `hyperxen-pricing-bot-db5hmngq3woxa.azurewebsites.net` |
| hyperxen.com | Unchanged, still pointing to prod app |
| dev.hyperxen.com | ✅ Live on self-signed cert by design (thumbprint `66CB417763C7…`, expires 2027-05-02). Azure managed cert abandoned — see note below. |

### Dev SSL — Resolved by Decision (2026-06-07)
Dev stays on self-signed by design. Azure managed cert attempted 2026-06-07 — `az webapp config ssl create` returned exit 0 but the cert never materialised after 20+ min (silent provisioning failure, known intermittent Azure bug; `ssl list` and ARM both showed no cert). Abandoned to avoid the pending-operation lock and 429 throttling this caused previously. Existing self-signed cert (thumbprint `66CB417763C7…`, expires 2027-05-02) remains bound and working. If a real cert is ever needed for dev, use Cloudflare (DNS-only, no proxy) — not Azure managed cert.

---

## Runtime Environment (audited 2026-06-21)

### LLM Engine — Provider-Switchable via Config

Active provider: **Azure AI Foundry / GPT-4o** (`LLM_PROVIDER=foundry` on both dev and prod).
Anthropic Claude is wired in code but dormant — no key set, no live calls. Switch via config when Anthropic becomes billable on the Azure subscription.

**Constraint:** All LLM usage must be Azure-billable via Foundry while Azure startup credits are in use. Anthropic is not covered by current credits. Do not set `LLM_PROVIDER=anthropic` in prod until Anthropic billing is confirmed on the subscription.

| Agent / path | Active provider | Notes |
|---|---|---|
| `pricing_agent.py` — VM pricing flow | Azure OpenAI / GPT-4o (Foundry) | Always Foundry; no provider flag |
| `orchestrator._call_llm()` — general conversation fallback | Routed by `LLM_PROVIDER` — currently `_call_foundry()` | Both Foundry and Anthropic paths compiled and present |
| `sku_advisor_agent.py` — scenario advisor | No LLM | Azure AI Search + rule-based only |

**`LLM_PROVIDER` flag** (controls `orchestrator._call_llm` only):
- `"foundry"` (default, **active on dev + prod**) → `_call_foundry()` — Azure AI Foundry / GPT-4o via httpx. No Anthropic call, no Anthropic key needed.
- `"anthropic"` (**dormant**) → `_call_anthropic()` — Anthropic Claude Sonnet `claude-sonnet-4-20250514`. Raises clearly if `ANTHROPIC_API_KEY` absent (no silent fail).

**To switch to Anthropic** when it's billable: add two App Service settings — no rebuild, no code change:
```
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=<key>
```

**Current key state (both dev + prod):** `ANTHROPIC_API_KEY` — **not set** (removed 2026-06-21). `LLM_PROVIDER=foundry` — **set explicitly**. Verified on prod: fallback replies correctly after key removal.

### Hosting

- **Platform:** Azure App Service Linux B1 (`rg-hyperxen-app-dev`)
- **Runtime:** Python 3.11
- **Process manager:** `gunicorn -w 1 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 app.main:app` (from `startup.sh`)
- **Worker count:** `-w 1` — **intentional, required** while session state is in-memory (`app/state.py`). Multi-worker breaks basket/session consistency. Redis required before increasing workers.

### Auth / Identity Model

| Service | Auth method | Env vars involved |
|---|---|---|
| Azure OpenAI — all active LLM paths | API key | `AZURE_OPENAI_KEY` |
| Anthropic Claude — dormant (`LLM_PROVIDER=anthropic`) | API key | `ANTHROPIC_API_KEY` (not set; add when switching) |
| Azure AI Search (advisor + indexer) | Admin key (`AzureKeyCredential`) | `AZURE_SEARCH_API_KEY` |
| ARM Compute SKU API (azure_pricing.py) | MSI **or** service principal (SP wins if all 3 vars set) | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` |
| ARM (indexer script, local only) | `DefaultAzureCredential` (CLI locally / MSI in prod) | `AZURE_SUBSCRIPTION_ID` |
| Application Insights | Connection string | `APPLICATIONINSIGHTS_CONNECTION_STRING` |
| GitHub Actions CI/CD | Service principal `hyperxen-github-actions` | `AZURE_CREDENTIALS` (GitHub Secret) |

**ARM auth fallback:** If `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` are all set → service principal path. If any is missing → MSI via `http://169.254.169.254/metadata`. On App Service the MSI path works; the SP path is the **expiry risk**.

### All Environment Variables (runtime, grouped)

**LLM — Azure OpenAI (`pricing_agent.py`):**
- `AZURE_OPENAI_ENDPOINT` — `https://hyperxen-foundry-presales1.services.ai.azure.com`
- `AZURE_OPENAI_KEY` — API key for Azure AI Foundry
- `AZURE_OPENAI_DEPLOYMENT` — deployment name (default: `gpt-4o`)

**LLM provider flag:**
- `LLM_PROVIDER` — `"foundry"` (default, active) or `"anthropic"` (dormant). Set in App Service settings.

**LLM — `orchestrator._call_llm()` (general conversation fallback):**
- When `LLM_PROVIDER=foundry`: uses the same `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_KEY` / `AZURE_OPENAI_DEPLOYMENT`. No separate env vars.
- When `LLM_PROVIDER=anthropic`: requires `ANTHROPIC_API_KEY`. Key is not set in App Service today.

**Azure AI Search (`sku_advisor_agent.py`):**
- `AZURE_SEARCH_ENDPOINT` — `https://hyperxen-search.search.windows.net`
- `AZURE_SEARCH_API_KEY` — admin key (also used by `scripts/index_vm_skus.py`)

**ARM Compute SKU API (`azure_pricing.py`):**
- `AZURE_SUBSCRIPTION_ID` — subscription for ARM SKU queries and indexer
- `AZURE_TENANT_ID` — optional; only needed for SP path (else MSI)
- `AZURE_CLIENT_ID` — optional; only needed for SP path
- `AZURE_CLIENT_SECRET` — optional; only needed for SP path — **⚠ EXPIRY RISK**

**Observability:**
- `APPLICATIONINSIGHTS_CONNECTION_STRING` — Azure Monitor SDK (`configure_azure_monitor()` in `main.py`)

**Runtime / framework:**
- `ENVIRONMENT` — `dev` or `prod` (read by `settings.py` / `pydantic-settings`)
- `PORT` — `8000` (read by `settings.py`)

### Expiry-Prone Secrets — Watch List

| Secret | Location | Risk | Rotation action |
|---|---|---|---|
| `AZURE_CLIENT_SECRET` | App Service app settings | **HIGH** — SP secret, typically 1–2 yr expiry. Expiry silently falls back to MSI; if MSI also fails ARM SKU lookups degrade (temp storage, premium gate, deployability filter return empty). | `az ad sp credential reset --id <client-id>`; update App Service setting |
| `AZURE_CREDENTIALS` (GitHub Secret) | GitHub Actions | **MEDIUM** — expires ~May 2027 (service principal `hyperxen-github-actions`, clientId `51c2f18d-444d-4af8-8129-8ec4b317fb0f`). Expiry breaks CI/CD deploys. | `az ad sp credential reset --id 51c2f18d-444d-4af8-8129-8ec4b317fb0f`; update GitHub Secret |
| Dev SSL cert | App Service TLS binding | **LOW** — self-signed, expires 2027-05-02. Only affects `dev.hyperxen.com`. | Regenerate PFX with openssl, re-upload and rebind |
| `AZURE_OPENAI_KEY` | App Service app settings | **LOW** — no automatic expiry; rotate if compromised | Azure AI Foundry portal → Keys |
| `AZURE_SEARCH_API_KEY` | App Service app settings | **LOW** — no automatic expiry; rotate if compromised | Azure AI Search portal → Keys |

---

## Architecture

```
Replit Frontend (HyperXen.ai)
  https://replit.com/@ericbluesky/Hyperxen-UI
        │
        │  POST /api/chat
        │  GET  /api/welcome
        ▼
┌─────────────────────────────────────────────────────────┐
│  GitHub Actions CI/CD                                   │
│  push to dev  → hyperxen-pricing-bot-dev  (dev env)    │
│  push to main → hyperxen-pricing-bot-prod (production) │
└─────────────────────────────────────────────────────────┘
        │                          │
        ▼                          ▼
Azure App Service (dev)     Azure App Service (prod)
  https://dev.hyperxen.com    https://hyperxen-pricing-bot-db5hmngq3woxa.azurewebsites.net
  Python 3.11 · FastAPI        Python 3.11 · FastAPI
  B1 Linux                     B1 Linux
        │                          │
        └──────────┬───────────────┘
                   │
        ├──► GPT-4o via Azure AI Foundry  (ALL LLM paths — pricing + conversation fallback)
        │    https://hyperxen-foundry-presales1.services.ai.azure.com
        │    Deployment: gpt-4o
        │
        ├──► Azure AI Search
        │    https://hyperxen-search.search.windows.net
        │    Index: vm-skus (894 active SKUs, 291 retired flagged)
        │
        └──► Azure Retail Prices API (public, no auth)
             https://prices.azure.com/api/retail/prices
```

---

## Azure Resources

| Resource | Name | Resource Group | Region |
|----------|------|---------------|--------|
| App Service (prod) | hyperxen-pricing-bot-db5hmngq3woxa | rg-hyperxen-app-dev | Australia East |
| App Service Plan (prod) | hyperxen-pricing-bot-plan (B1 Linux) | rg-hyperxen-app-dev | Australia East |
| App Service (dev) | hyperxen-pricing-bot-dev | rg-hyperxen-app-dev | Australia East |
| App Service Plan (dev) | hyperxen-pricing-bot-plan-dev (B1 Linux) | rg-hyperxen-app-dev | Australia East |
| Azure AI Foundry | hyperxen-foundry-presales1 | rg-hyperxen-dev1 | East US 2 |
| Azure AI Search | hyperxen-search (Free tier) | rg-hyperxen-app-dev | Australia East |
| Managed Identity | Enabled on App Service | — | Principal ID: 1781559f-16d2-4fbc-9140-87489df58699 |
| Service Principal | hyperxen-app-sp | — | Reader role on subscription |
| Service Principal | hyperxen-github-actions | — | Contributor on rg-hyperxen-app-dev |

---

## Environments

| Environment | App Service | URL | Branch |
|------------|-------------|-----|--------|
| Production | hyperxen-pricing-bot-db5hmngq3woxa | https://hyperxen.com | main |
| Dev | hyperxen-pricing-bot-dev | https://dev.hyperxen.com | dev |

## Dev SSL Certificate
- **Type:** Self-signed (Azure managed cert kept failing due to duplicate pending operations)
- **Thumbprint:** `66CB417763C7318ABD21763171CC5ABE2D447C6B`
- **Expires:** 2027-05-02
- **To rotate:** regenerate `dev-hyperxen.pfx` with openssl, upload via `az webapp config ssl upload`, rebind with `az webapp config ssl bind`
- **Password:** stored securely (do not commit to repo)

### Why self-signed instead of Azure managed cert
Azure managed cert (Let's Encrypt via App Service) failed repeatedly due to:
1. Excessive CLI polling created too many pending operations on the subscription
2. Each failed attempt created a 2-hour lock that blocked the next attempt
3. Azure throttled the subscription with 429 Too Many Requests after repeated retries

Solution: Upload a self-signed PFX cert directly — bypasses Azure's provisioning entirely.
For future dev environments, skip managed cert and go straight to self-signed.

## Replit Frontend Environment Variables

| Variable | Value | Purpose |
|----------|-------|---------|
| BACKEND_URL | https://hyperxen-pricing-bot-db5hmngq3woxa.azurewebsites.net | Production backend |
| BACKEND_URL_DEV | https://dev.hyperxen.com | Dev backend |

Backend URL is read from `process.env.BACKEND_URL` in `server/routes.ts` line 8.
To switch to dev: change `BACKEND_URL` value to `https://dev.hyperxen.com` in Replit Secrets.
To switch back to prod: change `BACKEND_URL` value back to `https://hyperxen-pricing-bot-db5hmngq3woxa.azurewebsites.net`

---

## DNS (HostPapa)

| Record | Type | Value |
|--------|------|-------|
| dev | CNAME | hyperxen-pricing-bot-dev.azurewebsites.net |
| asuid.dev | TXT | ED0F428CFF97A626A727B50EAF889D67CBF0603A47C6F2DA6F104CB5E278BC52 |

---

- **Subscription ID:** `dd5a4d29-50b0-4330-b83a-37094699272c`
- **Tenant ID:** `ceba3126-eb69-4216-9b6f-623fdd3f19de`
- **App Service URL:** https://hyperxen-pricing-bot-db5hmngq3woxa.azurewebsites.net
- **Foundry Endpoint:** https://hyperxen-foundry-presales1.services.ai.azure.com
- **Search Endpoint:** https://hyperxen-search.search.windows.net
- **Frontend:** https://replit.com/@ericbluesky/Hyperxen-UI
- **Repo:** https://github.com/cloudman10/azure-presales-ai-bot

---

## App Service Configuration

| Setting | Value |
|---------|-------|
| Runtime | PYTHON\|3.11 |
| Startup command | `startup.sh` → `gunicorn -w 1 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 app.main:app` |
| Health check path | `/` |
| `PYTHONPATH` | `/home/site/wwwroot` |
| `AZURE_OPENAI_ENDPOINT` | `https://hyperxen-foundry-presales1.services.ai.azure.com` |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-4o` |
| `AZURE_OPENAI_KEY` | (set in app settings) |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` (Oryx builds on deploy) |
| `GUNICORN_CMD_ARGS` | `--worker-class=uvicorn.workers.UvicornWorker --bind=0.0.0.0:8000` |

> **Critical:** GUNICORN_CMD_ARGS forces uvicorn workers regardless of gunicorn.conf.py CWD — without this, Oryx auto-detect launches sync workers which fail with FastAPI (TypeError: FastAPI.__call__() missing 1 required positional argument: 'send').

---

## Project Structure

```
azure-presales-ai-bot/
├── app/
│   ├── main.py                   ← FastAPI app, CORS middleware, routes
│   ├── agents/
│   │   ├── orchestrator.py       ← routes user messages to the right agent
│   │   ├── pricing_agent.py      ← LLM conversation loop + pricing formatter
│   │   ├── sku_agent.py          ← SKU Normalizer Agent (rule-based, no LLM)
│   │   ├── sku_advisor_agent.py  ← SKU Advisor (scenario-based, Azure AI Search)
│   │   └── report_agent.py       ← Report Agent (Excel/PDF generation)
│   ├── state.py                  ← shared in-memory session dict (basket, history, advisor)
│   ├── routers/
│   │   ├── chat.py               ← /api/chat, /api/welcome, /api/report/*
│   │   ├── basket.py             ← /api/basket CRUD + /api/basket/report/excel|pdf
│   │   └── diagram.py            ← /api/diagram/* (chat, render, svg-test, health, sample)
│   ├── services/
│   │   ├── azure_pricing.py      ← Azure Retail Prices API + ARM SKU capabilities
│   │   ├── diagram_architect.py  ← multi-turn GPT-4o HLD discovery agent
│   │   ├── diagram_renderer.py   ← PNG renderer (graphviz, fallback)
│   │   └── diagram_renderer_svg.py ← primary SVG renderer (980px landscape HLD)
│   ├── utils/
│   │   ├── sku_normalizer.py     ← normalize_sku_name(), extract_sku()
│   │   ├── pricing_calculator.py ← PAYG / RI / Savings Plan calculations
│   │   └── region_normalizer.py  ← 60+ city → armRegionName mapping
│   ├── models/
│   │   └── schemas.py
│   └── config/
│       └── settings.py
├── static/
│   ├── index.html                ← VM pricing chat UI with basket/export
│   ├── architect.html            ← Solution Architecture Designer UI (/architect)
│   └── azure-icons/              ← 40 official Azure Architecture Icons V23 SVGs
├── scripts/
│   └── index_vm_skus.py          ← VM SKU indexer for Azure AI Search
├── infra/
│   └── main.bicep                ← App Service infrastructure
├── startup.sh                    ← graphviz install + gunicorn launch
├── requirements.txt
└── MASTER.md                     ← this file
```

---

## What's Built and Working

- FastAPI chat API (`/api/chat`) with Azure OpenAI GPT-4o (all LLM paths — pricing flow + conversation fallback both route through Azure AI Foundry)
- Multi-turn conversation: collects SKU, region, OS before fetching pricing
- PAYG, 1-Year/3-Year RI, Savings Plan, and Azure Hybrid Benefit pricing
- SKU Normalizer Agent — handles constrained vCPU normalization (e.g. `e42adsv5` → `Standard_E4-2ads_v5`)
- Python SKU normalization always overrides LLM output to prevent hallucinated SKU names
- Temp storage display via Azure ARM API + Managed Identity
- SKU Advisor Agent — scenario-based VM recommendations; queries Azure Retail Prices API directly (no hardcoded series); top 3 picks from general/memory/cost categories; works for any region
- SKU Advisor: full region coverage — `fetch_vm_prices_for_region()` pages through all PAYG VMs in a region; Python filters by vCPU count; no Azure AI Search dependency for candidate discovery
- SKU Advisor: alt-region fill — when fewer than 3 options exist, fills remaining slots from the nearest alternative region (e.g. Australia East) labelled `[Available in Australia East]`
- SKU Advisor: concurrent lookups — `asyncio.gather()` for metadata enrichment and pricing verification; response under 20 seconds
- SKU Advisor: region and OS carry-over from full conversation history — info stated before advisor started is captured without re-asking
- SKU Advisor: typo-tolerant OS detection (`windwos`, `windoes`, `widnows`, `win` all map to Windows; `lin` prefix maps to Linux)
- SKU Advisor: direct option 1/2/3 selection without confirmation loops — "option 2", "2", "what about option 2 pricing", standalone "yes"/"ok" all trigger immediate pricing fetch
- SKU Advisor: follow-up pricing requests after full output (different region, different OS, more cores) handled by pricing_agent with full history context — no fallback to generic response
- Uncertainty routing: "dont know", "don't know", "not sure", "recommend", "which vm" and similar phrases route to SKU Advisor instead of failing in the pricing flow
- SKU Advisor: generation scoring v7=70/v6=60/v5=50/v4=40/v3=30/v2=20 — always recommends newest generation first
- SKU Advisor: vCPUs and RAM now shown in `=== Azure VM Pricing Estimate ===` output
- SKU Advisor: OS detection false positives fixed (words like "handling" no longer trigger Linux match)
- SKU Advisor: OS always asked if not already known, regardless of how advisor was triggered
- Report Agent — Excel (.xlsx) and PDF download from any pricing result; HyperXen.ai branding; download buttons appear inline
- Multi-VM Quote Basket — add any VM+storage combo to a running quote; per-card Qty + "Add to Quote"; slide-in drawer with per-line remove, grand total, Export Excel/PDF buttons; basket restores on refresh
- Basket export — `generate_excel_basket` / `generate_pdf_basket` build from structured numeric model; per-item VM + disk breakdown, line totals, GRAND TOTAL; no text-blob parsing
- Alt-region advisor pick fix — `_picks.sku_region_displays[]` per option; frontend prices each option in its own source region (fixes "pricing fetch failed" on `[Available in Australia East]` alt-region fills)
- Advisor deployability gate — `fetch_deployable_skus()` cross-checks ARM Compute SKUs; candidates absent from ARM filtered before scoring; ARM data cached 1 hour and shared with `fetch_temp_storage_gb` / `vm_supports_premium`; ARM + Prices API fetched concurrently via `asyncio.gather`
- AI Search index refreshed (2026-06-20) — 1,185 SKUs incl. Easv6 variants; indexer switched to `DefaultAzureCredential` (CLI locally, Managed Identity in prod)
- VM Price Compare (`/compare`) — filterable/sortable table of all deployable australiaeast SKUs; region, OS, vCPU range, RAM range filters; all pricing tiers (PAYG, Spot, SP 1/3yr, RI 1/3yr); default OS=Windows; result count + price freshness; purple "Arm64" badge on Linux Arm64 SKUs
- Compare deployability gate — `index_vm_prices.py` applies ARM LOCATION-restriction filter (141 location-restricted SKUs excluded) plus Arm64/OS compatibility gate (Arm64 SKUs are Linux-only in the grid; Windows images are x86-64; 9 phantom Windows Arm64 docs removed); index: 1,975 docs (1,036 Linux + 939 Windows); `architecture` field (Arm64/x64) on every doc; portal-accurate results
- Per-VM pricing term selection — radio in each term's own header (PAYG hero, 1/3-Yr SP, 1/3-Yr RI, PAYG+HB, 1/3-Yr RI+HB for Windows); PAYG default; radio click selects without expanding breakdown; SP/RI show total monthly in header; +HB rows collapsible with Compute + License $0 (AHB) + Total; basket and export show term label per line; +HB footnote in export; storage undiscounted regardless of term
- 60+ city-to-region mapping (Australia, Asia Pacific, Middle East, Europe, Americas, Africa)
- Modern SKU preference — v4/v5/v6 ranked above v1/v2; Promo/Basic excluded
- CORS middleware (`allow_origins=["*"]`) for Replit frontend access
- Replit frontend connected to Azure App Service backend (full end-to-end)

---

## Deployment

### Standard Deploy (Windows)

```bash
cd "C:/Users/Admin/azure-presales-ai-bot"
# Build zip using Python to ensure forward-slash paths (required for Linux extraction)
python3 -c "
import zipfile, os
include = ['app', 'static', 'requirements.txt', 'startup.sh']
exclude_dirs = {'.git', '.venv', '__pycache__', 'antenv'}
with zipfile.ZipFile('deploy.zip', 'w', zipfile.ZIP_DEFLATED) as zf:
    for item in include:
        if os.path.isfile(item):
            zf.write(item, item)
        else:
            for d, dirs, files in os.walk(item):
                dirs[:] = [x for x in dirs if x not in exclude_dirs]
                for f in files:
                    if f.endswith('.pyc'): continue
                    p = os.path.join(d, f)
                    zf.write(p, p.replace(os.sep, '/'))
"
az webapp deployment source config-zip --resource-group rg-hyperxen-app-dev --name hyperxen-pricing-bot-db5hmngq3woxa --src deploy.zip
```

> **Critical:** Always use Python's `zipfile` to build the zip on Windows — NOT `Compress-Archive`.
> `Compress-Archive` uses backslash path separators (`app\main.py`). On Linux, those extract as
> literal filenames with backslashes, not directory structure. Python's `zipfile` always uses `/`.

### Check Deploy Logs (if app crashes)

```powershell
# Get publishing creds then fetch docker log via Kudu API
$creds = az webapp deployment list-publishing-credentials --resource-group rg-hyperxen-app-dev --name hyperxen-pricing-bot-db5hmngq3woxa --query "{user:publishingUserName, pass:publishingPassword}" | ConvertFrom-Json
$encoded = [System.Convert]::ToBase64String([System.Text.Encoding]::ASCII.GetBytes("$($creds.user):$($creds.pass)"))
$logList = Invoke-RestMethod -Uri "https://hyperxen-pricing-bot-db5hmngq3woxa.scm.azurewebsites.net/api/logs/docker" -Headers @{Authorization="Basic $encoded"}
$defaultLog = $logList | Where-Object { $_.machineName -like "*_default" -and $_.machineName -notlike "*scm*" } | Select-Object -First 1
$content = Invoke-RestMethod -Uri $defaultLog.href -Headers @{Authorization="Basic $encoded"}
($content -split "`n") | Select-Object -Last 50
```

### Deployment Gotchas

**`startup.sh` does NOT update via the normal Oryx/Kudu deploy.**
`startup.sh` lives at `/home/site/startup.sh` on the persistent `/home` volume. Oryx-based deploys (zip → Kudu → `output.tar.zst`) never touch this file. Before 2026-06-21, deploys would silently leave the old version running indefinitely — this is why the graphviz install block "had no effect" even after code was updated.

Fix in place: both GH Actions workflows now include a `curl -X PUT` step that writes `startup.sh` directly to `/home/site/startup.sh` via the Kudu VFS API after each successful deploy. If `startup.sh` changes ever seem to "not take effect," check:
1. Whether the GH Actions "Sync startup.sh" step ran successfully (check workflow logs).
2. Whether the container has restarted since the sync (new file is only read on next container start).

**Graphviz installed at container start, not build time.**
The `diagrams` library (`/api/diagram/*`) requires the `dot` binary, which is not pre-installed in the Azure App Service Python 3.11 container. `startup.sh` installs it via `apt-get install -y graphviz` on each cold-container start (~10s overhead). Confirmed working as of 2026-06-21: `dot - graphviz version 2.43.0` at `/usr/bin/dot`.

Future hardening option: a custom Docker image with Graphviz baked in (`FROM mcr.microsoft.com/appsvc/python:3.11 && RUN apt-get install -y graphviz`) is the robust long-term alternative if the startup-install ever proves flaky (slow cold starts, apt unavailable, Azure removes root access). Requires switching App Service to Custom Container mode with ACR. Filed as a known future hardening item — current approach is stable.

**Self-healing fallback for `az webapp restart`.**
`az webapp restart` kills the container process and clears ephemeral `/tmp/`. The Oryx-built `antenv` (normally in `/tmp/<hash>/antenv/`) is gone, so the standard gunicorn launch path breaks. `startup.sh` detects this and falls back to re-extracting `/home/site/wwwroot/output.tar.zst` into persistent `/home/site/oryx-build/`. Re-extraction only runs when `output.tar.zst` is newer than the last extract (~2–4 min for 242 MB uncompressed); subsequent restarts reuse the existing `/home/site/oryx-build/antenv` directly (fast path, <1s).

---

### Bicep Infrastructure

> Always run `az deployment group what-if --resource-group rg-hyperxen-app-dev --template-file infra/main.bicep` before deploying Bicep to preview changes.

```bash
az deployment group create --resource-group rg-hyperxen-app-dev --template-file infra/main.bicep
```

---

## Post-Bicep Deploy Checklist

After any fresh Bicep deploy, these 3 secrets must be set manually (they are redacted in main.bicep for security):

```powershell
az webapp config appsettings set --resource-group rg-hyperxen-app-dev --name hyperxen-pricing-bot-db5hmngq3woxa --settings ANTHROPIC_API_KEY="REPLACE_WITH_REAL_KEY" AZURE_CLIENT_SECRET="REPLACE_WITH_REAL_SECRET" AZURE_SEARCH_API_KEY="REPLACE_WITH_REAL_KEY"
```

> These values are not stored in the repo. Keep them in a secure password manager.

---

## CI/CD Pipeline (GitHub Actions)

| Branch | Deploys To | URL |
|--------|-----------|-----|
| dev | hyperxen-pricing-bot-dev | https://dev.hyperxen.com |
| main | hyperxen-pricing-bot-db5hmngq3woxa (production) | https://hyperxen.com |

Workflow file: `.github/workflows/deploy.yml`
GitHub Secret: `AZURE_CREDENTIALS` (service principal: `hyperxen-github-actions`, clientId: `51c2f18d-444d-4af8-8129-8ec4b317fb0f`)
Secret expiry: ~May 2027 — rotate with: `az ad sp credential reset --id 51c2f18d-444d-4af8-8129-8ec4b317fb0f`

### Developer Workflow
1. Make changes locally on `dev` branch
2. `git push origin dev` → auto deploys to https://dev.hyperxen.com
3. Test at https://dev.hyperxen.com
4. When happy → `git checkout main && git merge dev && git push origin main` → auto deploys to production

---

## Monitoring (Application Insights)

| Resource | Name | Resource Group |
|----------|------|----------------|
| Application Insights | hyperxen-insights | rg-hyperxen-app-dev |

- **SDK:** `azure-monitor-opentelemetry==1.6.4`
- **Configured in:** `app/main.py` via `configure_azure_monitor()`
- **Connection string:** stored as `APPLICATIONINSIGHTS_CONNECTION_STRING` app setting on both prod and dev
- **Data visible at:** https://portal.azure.com → hyperxen-insights → Search / Failures / Performance
- **Tracks:** all HTTP requests, response times, failures, dependencies (Azure AI Search, Prices API calls)
- **Note:** Live Metrics not supported with OpenTelemetry SDK — use Search and Performance tabs instead
- **Cold start warning:** B1 instance takes 75–211s on cold start due to OpenTelemetry outbound connections. Upgrade to S1 for Always On if needed.

---

---

## Compare Azure Prices

Added in v2.1 (2026-06-28, tag `compare-prices-1.0`). A filterable, sortable VM price comparison grid — the "Holori-style" table that lets users compare every Azure VM SKU across all pricing tiers for a region in one view.

### Purpose

Route `/compare`. Sibling tool to the VM pricing engine (chat advisor). Where the advisor recommends 3 VMs for a scenario, the Compare tool lets users browse and filter the full catalogue — useful for cost benchmarking, pre-qualification, and quote building. All price columns visible simultaneously: PAYG, Spot, SP 1/3yr, RI 1/3yr.

### Data Layer

Separate Azure AI Search index **`vm-sku-prices`** — distinct from the advisor's `vm-skus` index. Populated by `scripts/index_vm_prices.py`, which:
1. Reads active SKU specs from `vm-skus` (vcpus, ram_gb, series)
2. Applies ARM deployability gate (see Accuracy section below)
3. Bulk-fetches all PAYG+Spot and Reservation prices from the Azure Retail Prices API for the region
4. Builds one Linux doc + one Windows doc per deployable x64 SKU (Arm64 SKUs: Linux only)
5. Uploads via `merge_or_upload_documents`; runs cleanup to remove any stale docs

**Current index:** ~1,975 docs — 1,036 Linux + 939 Windows, australiaeast only. Run is manual; no scheduled refresh yet.

### Read API

```
GET /api/vm-prices/search?region=australiaeast&os=Windows&vcpus_min=4&vcpus_max=8&ram_min=0&ram_max=32&sort_by=payg_monthly&top=200
GET /api/vm-prices/sku/{sku_name}   — all pricing tiers for a single SKU (both OS)
```

Filter params: `region`, `os` (Linux/Windows), `vcpus_min/max` (1–512), `ram_min/max` (0–12288 GiB), `sort_by`, `top` (max 200).

Returns: `sku_name`, `vcpus`, `ram_gb`, `series`, `architecture`, `payg_hourly`, `payg_monthly`, `spot_hourly`, `sp_1yr_monthly`, `sp_3yr_monthly`, `ri_1yr_monthly`, `ri_3yr_monthly`, `price_updated_at`.

### Accuracy Disciplines (hard-won)

**1 — Deployability gate (ARM LOCATION restriction filter)**
Only SKUs that are ARM-deployable for this subscription in the region are indexed. The ARM `resource_skus.list()` API is called during every index run; SKUs with `type=Location, zones=[]` restriction (not available at all) are excluded. This removes SKUs the global Azure Calculator shows but that cannot actually be deployed — e.g. the original B-series (B2ms, B4ms, B8ms, B20ms) are LOCATION-restricted for this subscription in australiaeast. Zone-only restrictions (`type=Zone, zones=['1']`) are kept — the SKU is deployable in other zones.

**2 — Architecture-aware (Arm64 / Windows)**
Arm64 SKUs (naming convention: `pls`, `ps`, `pds`, `plds`, `pts` sub-series — e.g. Bpsv2, Dps_v5/v6, Eps_v5/v6 families) are Linux-only. Standard Windows images are x86-64. The Retail Prices API returns Windows prices for Arm64 SKUs speculatively, but no Windows ARM64 images exist in australiaeast — the portal correctly hides them. The indexer skips the Windows doc for all Arm64 SKUs (`CpuArchitectureType=Arm64` capability). The `/compare` UI shows a purple **Arm64** badge next to Arm64 SKUs in the Linux grid so users know they need an ARM64 image (e.g. Ubuntu Arm64), not a standard x86 image.

**3 — RI price formula**
The Retail Prices API `retailPrice` for Reservation items is the **total term commitment** (e.g. $1,297 for a 1-year RI) — NOT an hourly rate, despite `unitOfMeasure = "1 Hour"`. Monthly RI = `retail / 12` (1yr) or `retail / 36` (3yr).

**4 — Spot and SP**
Spot prices: Linux only (no Windows Spot market). Savings Plan rates: embedded in the Linux PAYG item's `savingsPlan[]` field (1 Year / 3 Year). Windows uses the same SP/RI infrastructure rates as Linux via AHB (Azure Hybrid Benefit). Nulls rendered as `—` in the grid.

### Subscription-Specificity Note

**Deployability is subscription-specific.** The gate reflects which SKUs *this subscription* can deploy in the region. A different subscription (e.g. MSDN, EA, Pay-As-You-Go) may have different access. The grid is more accurate than the generic Azure Calculator for this subscription, but "deployable" is relative to the subscription used to build the index. The index must be re-run if the subscription or region quota changes.

### Known TODOs (parked)

- Scheduled daily refresh (currently manual: `python scripts/index_vm_prices.py`)
- Additional regions beyond australiaeast (REGION constant in the script)
- Add-to-Basket from the grid (wire into the existing quote basket)
- CSV export
- Architecture filter in the UI (e.g. show Arm64 / x64 toggle)

---

---

## Solution Architecture Designer

Added in v2.0 (2026-06-24, tag `v-arch-svg-1.0`). A multi-turn LLM-powered agent that generates professional one-page High-Level Design (HLD) diagrams for Azure architectures — consultant-quality SVG output, suitable for presales decks and customer workshops.

### Purpose

A pre-sales conversation tool: the user describes a customer scenario (e.g., "5 Hyper-V VMs running an ERP workload, migrate to Azure with DR"), the agent asks clarifying questions, and produces a ready-to-share HLD. Accessed at `/architect`.

### Flow

```
User message → POST /api/diagram/chat
  → diagram_architect.py  (multi-turn GPT-4o discovery)
  → emits ARCHITECTURE_JSON: {...}
  → diagram_renderer_svg.py  (980px landscape SVG)
  → svg_b64 in JSON response
  → architect.html  (inline <img src="data:image/svg+xml;base64,...">)
```

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/architect` | UI page (`static/architect.html`) |
| `POST` | `/api/diagram/chat` | Main chat; returns `{type, reply, json, svg_b64, png_base64}` |
| `GET` | `/api/diagram/svg-test` | Smoke-test with minimal hardcoded payload; returns raw SVG |
| `GET` | `/api/diagram/health` | Health check |
| `GET` | `/api/diagram/sample` | Returns sample architecture JSON |
| `POST` | `/api/diagram/render` | Render arbitrary architecture JSON → SVG |

### SVG Renderer (`app/services/diagram_renderer_svg.py`)

980px × auto-height canvas, 5 visual bands top-to-bottom:

1. **Header bar** — gradient `#0F2D57 → #1565C0`, large title, subtitle, 3 value-pillar callouts derived from `design_principles` by keyword matching. Pillar titles: "Secure & Zero Trust" / "Resilient & Available" / "Operationally Efficient".

2. **Architecture area (center + right sidebar)**
   - *Left 3 columns inside Azure envelope*: `onprem` (col 0), `hub` (col 1), `spoke`+unknown (col 2). Each zone: colored header bar, resource rows with 28×28px official Azure icon + name + role.
   - *Right sidebar (200px)*: 4 stacked panels for cross-cutting concerns — Security & Identity, Management & Monitoring, Backup & DR, Region (with paired region). Populated from `shared`/`mgmt` zone types.

3. **Migration Approach band** — numbered circles + step name + truncated description, horizontal flow with SVG `<path>` arrows.

4. **Bottom band** — Key Design Principles (2-column checklist from `design_principles[]`) + Future Options (`future_options[]`).

5. **Legend** — zone-type colour key.

**Canvas width breakdown:**
```
W = 18 (left margin) + 726 (3×218px cols + 2×36px gaps) + 18 (gap) + 200 (sidebar) + 18 (right margin) = 980px
```

### Architecture JSON Schema

```json
{
  "title": "string",
  "subtitle": "string",
  "zones": [
    {
      "id": "string",
      "label": "string",
      "type": "onprem|hub|spoke|shared|mgmt",
      "resources": [
        {"id": "string", "type": "ResourceType", "name": "string", "role": "string"}
      ]
    }
  ],
  "connections": [{"from": "string", "to": "string", "label": "string"}],
  "shared_services": [{"type": "string", "name": "string", "purpose": "string"}],
  "migration_approach": [{"step": "string", "description": "string"}],
  "design_principles": ["string"],
  "future_options": ["string"]
}
```

**Zone type routing:**
- `onprem` → center column 0
- `hub` → center column 1
- `spoke` + any unknown type → center column 2
- `shared` → sidebar Security & Identity + Mgmt panels (by resource type)
- `mgmt` → sidebar Management & Monitoring panel

### Azure Icon Mapping

Official Microsoft Azure Architecture Icons **V23** (707-entry pack). 40 SVG files in `static/azure-icons/`, mapped in `_TYPE_ICON` dict in `diagram_renderer_svg.py`. Unmapped types fall back to a colored letter-badge.

| Category | Mapped types |
|----------|-------------|
| Compute | VirtualMachine, HyperVHost, OnPremVM, OnPremServer, ScaleSet, AVDHostPool, AppService, FunctionApp, AKSCluster, ContainerApp |
| Network | AzureFirewall, OnPremFirewall, BastionHost, VPNGateway, ExpressRouteGateway, LoadBalancer, ApplicationGateway, VirtualNetwork, OnPremNetwork, Subnet, NetworkSecurityGroup, PrivateDNSZone, PrivateEndpoint, NATGateway, RouteTable |
| Data | SQLDatabase, SQLManagedInstance, StorageAccount, CosmosDB, MySQLDatabase, PostgreSQLDatabase, RedisCache, DataFactory |
| Security | EntraID*, ManagedIdentity, KeyVault, DefenderForCloud, AzurePolicy, Sentinel |
| Operations | RecoveryServicesVault, LogAnalyticsWorkspace, AzureMonitor, ApplicationInsights, UpdateManager, AutomationAccount |

*EntraID uses `enterprise-applications.svg` proxy — V23 has no standalone Entra ID service icon.

### XML Encoding Rule (important — recurring issue)

LLM-generated text can contain C0/C1 control characters (`\x00-\x08`, `\x0b`, `\x0c`, `\x0e-\x1f`, `\x7f`, `\x80-\x9f`) and smart Unicode quotes that are illegal in XML 1.0. The renderer:
1. Strips prohibited chars via `_XML_PROHIBITED` regex before writing any text node
2. Converts smart quotes / em-dashes to ASCII equivalents in `diagram_architect._sanitize()`
3. Validates the final SVG with `xml.etree.ElementTree.fromstring()` — raises immediately on any violation

Any future LLM text injection into SVG **must** go through `_xt()` (the renderer's text-escape + prohibited-char strip function).

### Known Limitations

- **No connections rendered** — `connections[]` is parsed but SVG arrows between zones are not drawn (deferred). Zone-to-zone flow is implied by column order (left→right).
- **No draw.io export** — SVG only; draw.io evaluated and rejected 2026-06-24 (see decision log).
- **LLM grounding** — architect agent uses GPT-4o with no RAG retrieval. New or unusual Azure service types may produce unmapped `type` values that fall back to letter-badge.
- **Auto-height** — canvas grows with zone count; very tall zone stacks may exceed typical slide height.
- **Single-step architect** — no memory of prior architecture conversations (each `/architect` session is independent). Session ID scoped to page load.

---

### Next Directions (parked)

#### Priority 1 — RAG grounding for the architect agent

**Why:** The top presales differentiator would be grounding the agent against customer-specific data — Hyper-V inventory exports, Azure Migrate assessment CSVs, or a product catalogue. Without grounding, every scenario is a blank-slate LLM inference; with grounding, the agent could import a real migration assessment and produce a diagram directly from the customer's actual workload data.

**Implementation path:**
1. Ingest assessment CSV → Azure AI Search index (same infra as the VM SKU index, `rg-hyperxen-app-dev`)
2. Add a RAG retrieval step in `diagram_architect.py` system context before the first turn
3. Agent uses retrieved workload facts as grounding; still asks clarifying questions for gaps

**Why this is first:** Grounds the tool in real data, differentiates from generic AI diagram generators, directly addresses the "how many VMs / what workloads?" question that every pre-sales conversation starts with.

#### Track 2 — draw.io rendering experiment — **EVALUATED AND REJECTED (2026-06-24)**

**Branch:** `feat/drawio-spike` (kept for reference, never merged to main)

**What was built:** `app/services/diagram_renderer_drawio.py` produced mxGraphModel XML from the same architecture JSON. New `/architect1` route deployed to dev alongside `/architect` for side-by-side comparison. 50 Azure mscae stencil shape mappings. Inline browser rendering via `viewer.diagrams.net/js/viewer-static.min.js`.

**Findings — why it was rejected:**

1. **Fallback rectangles, not real Azure icons.** The draw.io static viewer (`viewer-static.min.js`) does not bundle the `mscae` Azure stencil library. All Azure-typed nodes render as plain `#dae8fc` rectangles instead of the Microsoft Azure icons. Getting real icons would require the server-side draw.io CLI (`drawio --export`), which is a heavyweight Linux dependency (Electron + Chromium, ~300 MB) — the opposite of our startup.sh philosophy.

2. **Output far less complete than the SVG renderer.** The draw.io XML only has zones, resources, and connection edges. Missing entirely: gradient header + title, value-pillar callouts (Secure/Resilient/Efficient), migration approach band (numbered steps + arrows), Key Design Principles checklist, Future Options, Legend. The SVG renderer produces a complete consulting one-pager; the draw.io version is a bare swimlane diagram.

3. **Messy connection labels.** draw.io's orthogonal edge routing overlaps label text with container borders when source and target are in different swimlane hierarchies (e.g., on-prem resource → Azure envelope resource). The SVG renderer doesn't draw connections at all (deferred) — a cleaner tradeoff than drawing them badly.

4. **No interactive benefit for the spike goal.** The inline viewer renders a static canvas (same visual result as a PNG); "interactive" (zoom/pan) is a minor UX improvement that doesn't offset the above deficits.

**Only residual value:** A `.drawio` download button so a customer's architect can open the diagram in draw.io and edit it. Not worth the complexity delta right now.

**Canonical renderer:** SVG renderer (`diagram_renderer_svg.py`, tag `v-arch-svg-1.0`) remains the sole renderer. `/architect` (SVG) is the default and production path. `/architect1` was left on `dev` as a dead-end reference; it will not be promoted to `main`.

#### Decision Log

| Decision | Rationale |
|----------|-----------|
| SVG over draw.io first | SVG renders inline in the browser with zero client dependency; for a presales demo, instant inline visual wins over "open in another app" |
| draw.io evaluated and rejected (2026-06-24) | Inline viewer shows fallback rectangles (no real icons without Electron/Chromium dep); output missing migration/principles/pillars/sidebars; connection labels messy. SVG renderer produces a better one-pager. |
| SVG over PNG | PNG pixelates on retina/slides; SVG scales perfectly and is ~4× smaller (82 KB vs ~340 KB equivalent PNG) |
| Removed `.arch-head` card title | HTML card showed title in plain text above SVG *and* SVG gradient bar also showed it — two identical titles looked like a bug. SVG header bar is now the single title display. |
| shared/mgmt zones → sidebar only | Cross-cutting concerns (Entra ID, Monitor, Defender) belong in a dedicated right panel, not mixed into the on-prem→hub→spoke traffic flow. Matches Microsoft reference architecture layout standard. |
| EntraID proxy icon | V23 has no standalone "Microsoft Entra ID" service icon. `enterprise-applications.svg` is the closest visual proxy (blue people icon). Will update if V24 adds one. |

---

## Roadmap

| Step | Status | Description |
|------|--------|-------------|
| 1 | ✅ Done | Azure Retail Prices API integration |
| 2 | ✅ Done | PAYG pricing output |
| 3 | ✅ Done | Reserved Instance pricing |
| 4 | ✅ Done | Savings Plan pricing |
| 5 | ✅ Done | Azure Hybrid Benefit |
| 6 | ✅ Done | Multi-turn LLM conversation loop |
| 7 | ✅ Done | SKU normalization (including constrained vCPU) |
| 8 | ✅ Done | Temp storage via ARM SKU capabilities API |
| 9 | ✅ Done | Deployed to Azure App Service (Bicep), URL live |
| 10 | ✅ Done | Claude Sonnet live as general conversation fallback (`orchestrator._call_claude`) — Anthropic direct API |
| 11 | ✅ Done | SKU Advisor Agent with Azure AI Search |
| 12 | ✅ Done | Report Agent — Excel/PDF download with HyperXen branding |
| 13 | ✅ Done | Replit Frontend — HyperXen.ai connected to backend |
| 14 | ✅ Done | Fix Oryx/zip deploy — Python zipfile (forward slashes), LF line endings, startup command |

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `AZURE_OPENAI_ENDPOINT` | `https://hyperxen-foundry-presales1.services.ai.azure.com` |
| `AZURE_OPENAI_KEY` | Azure AI Foundry API key |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-4o` |
| `AZURE_SUBSCRIPTION_ID` | `dd5a4d29-50b0-4330-b83a-37094699272c` |
| `AZURE_TENANT_ID` | `ceba3126-eb69-4216-9b6f-623fdd3f19de` |
| `AZURE_CLIENT_ID` | Service principal client ID (hyperxen-app-sp) |
| `AZURE_CLIENT_SECRET` | Service principal secret |
| `AZURE_SEARCH_ENDPOINT` | `https://hyperxen-search.search.windows.net` |
| `AZURE_SEARCH_API_KEY` | Azure AI Search admin key |
| `ANTHROPIC_API_KEY` | Anthropic API key — **not set in App Service** (Anthropic path dormant). Add when switching `LLM_PROVIDER=anthropic`. |
| `ENVIRONMENT` | `dev` / `prod` |
| `PORT` | `8000` |

---

## Known API Limitations

### Reserved Instance Availability
The Azure Retail Prices API does not publish RI items for all VM series:

| Case | Condition | Bot behaviour |
|------|-----------|---------------|
| 1 | No items returned at all | "VM not found — may be retired or unavailable in this region" |
| 2 | PAYG exists, no RI items | "RI not available via public API — verify at azure.com/calculator" |
| 3 | RI items exist | Shows correct RI pricing |

Affected series for Case 2: HPC series (HC44rs, HB-series, HBv2, HBv3), some specialty SKUs.

### Windows RI License Pricing
Azure does not publish Windows-specific RI items for older VM series (DSv2, Dv2, FSv2). Windows licence shown at PAYG RRP rate. Newer series (v4, v5+) publish Windows RI items correctly.

### Savings Plan Data
`savingsPlan` field is only present on Linux Consumption items. Always fetch Linux item to get Savings Plan rates; Windows items never carry this field.

### VM Retirement
Retirement notices: https://learn.microsoft.com/en-us/azure/virtual-machines/retirement-announcements
When API returns no items for a SKU it may be retired — bot surfaces "VM not found" message.
