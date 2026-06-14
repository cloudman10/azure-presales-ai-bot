# HyperXen Azure Presales AI Bot — Master Reference

> Single source of truth for architecture, status, resources, and deployment.
> Raw URL (once repo is public): https://raw.githubusercontent.com/cloudman10/azure-presales-ai-bot/main/MASTER.md

---

## Current Status (2026-06-14) — v1.4.0

### Last Known-Good State (2026-06-14)
- Commit: fcf35f647859b51b00eee063d453fbd6af347f3d (main)
- Status: dev + prod healthy, §2.2 storage selector verified on both pricing paths.
- Rollback if a future deploy breaks the app:
  ```bash
  git checkout main
  git reset --hard fcf35f647859b51b00eee063d453fbd6af347f3d
  git push origin main --force
  ```
  (or safer: `git revert <bad-commit> --no-edit && git push origin main`)
- Note: failed deploy fcf35f6 recovered; app currently serving correctly. Root cause was unpinned packages pulling a breaking version on cold-start — fixed by pinning all deps in requirements.txt (dev commit f992620).

| Item | Status |
|------|--------|
| Frontend (Replit UI) | ✅ Live |
| Backend (Azure App Service) | ✅ Live — https://hyperxen-pricing-bot-db5hmngq3woxa.azurewebsites.net |
| Dev App | ✅ Live and healthy — https://hyperxen-pricing-bot-dev.azurewebsites.net |
| LLM (GPT-4o via Azure AI Foundry) | ✅ Verified working |
| Azure AI Search | ✅ Indexed (894 active SKUs) |
| CORS middleware | ✅ Added |
| Git repo | ✅ Public — https://github.com/cloudman10/azure-presales-ai-bot |
| Dev Environment | ✅ Live — https://dev.hyperxen.com |
| CI/CD Pipeline | ✅ GitHub Actions — auto deploy on push to dev and main |

### All systems operational
Test: `curl https://hyperxen-pricing-bot-db5hmngq3woxa.azurewebsites.net/api/welcome`

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

### Session Storage — IMPORTANT prod constraint (logged 2026-06-14)
All session state (conversation history, advisor picks, quote basket) is stored in a plain Python `dict` in `app/state.py` — **in-process, per-worker, no persistence**.

**Dev:** `startup.sh` temporarily runs `-w 1` (single gunicorn worker) so the basket and session are always consistent during development.

**⚠ BEFORE promoting basket to prod (4 workers):** migrate session storage to a shared store — Redis is the standard choice for Azure App Service (Azure Cache for Redis). With 4 workers, every request may land on a different process, each with its own empty dict. This is already the root cause of the "session memory cleared" bug on the advisor. The basket will silently appear empty on ~75% of requests if promoted as-is.

**Migration path:** replace `app/state.py` with a Redis-backed adapter (e.g. `aioredis`); key schema stays the same (`{sid}`, `{sid}_advisor_state`, `{sid}_basket`, etc.).

### Known bugs to fix (logged 2026-06-08, not yet addressed)
- Advisor renders literal `**1**`/`**2**`/`**3**` (markdown asterisks not rendering in advisor replies)
- Advisor spec lookup fails for older SKUs (Standard_A6 shows "? GB RAM", wrong vCPU count, no "Best for" line)
- Advisor "session memory cleared" on some option-picks via API/refresh path
- Minor formatter spacing nits ("Microsoft RetailRRP", "Capacity only;per-10K")

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
        ├──► GPT-4o via Azure AI Foundry
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
| Startup command | `uvicorn app.main:app --host 0.0.0.0 --port 8000` |
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
│   ├── routers/
│   │   └── chat.py               ← /api/chat, /api/welcome, /api/report/*
│   ├── services/
│   │   └── azure_pricing.py      ← Azure Retail Prices API + ARM SKU capabilities
│   ├── utils/
│   │   ├── sku_normalizer.py     ← normalize_sku_name(), extract_sku()
│   │   ├── pricing_calculator.py ← PAYG / RI / Savings Plan calculations
│   │   └── region_normalizer.py  ← 60+ city → armRegionName mapping
│   ├── models/
│   │   └── schemas.py
│   └── config/
│       └── settings.py
├── static/
│   └── index.html                ← chat UI with Excel/PDF download buttons
├── scripts/
│   └── index_vm_skus.py          ← VM SKU indexer for Azure AI Search
├── infra/
│   └── main.bicep                ← App Service infrastructure
├── startup.sh                    ← pip install + uvicorn launch
├── requirements.txt
└── MASTER.md                     ← this file
```

---

## What's Built and Working

- FastAPI chat API (`/api/chat`) with Azure OpenAI GPT-4o
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
| 10 | ⏳ Pending | Claude via Foundry — Microsoft quota approval pending |
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
| `ANTHROPIC_API_KEY` | Anthropic API key (for future Claude integration) |
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
