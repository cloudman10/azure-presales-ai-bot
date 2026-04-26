# HyperXen Azure Presales AI Bot — Master Reference

> Single source of truth for architecture, status, resources, and deployment.
> Raw URL (once repo is public): https://raw.githubusercontent.com/cloudman10/azure-presales-ai-bot/main/MASTER.md

---

## Current Status (2026-04-26)

| Item | Status |
|------|--------|
| Frontend (Replit UI) | ✅ Live |
| Backend (Azure App Service) | ✅ Live — https://hyperxen-pricing-bot-db5hmngq3woxa.azurewebsites.net |
| LLM (GPT-4o via Azure AI Foundry) | ✅ Verified working |
| Azure AI Search | ✅ Indexed (894 active SKUs) |
| CORS middleware | ✅ Added |
| Git repo | ✅ Public — https://github.com/cloudman10/azure-presales-ai-bot |

### All systems operational
Test: `curl https://hyperxen-pricing-bot-db5hmngq3woxa.azurewebsites.net/api/welcome`

---

## Architecture

```
Replit Frontend (HyperXen.ai)
  https://replit.com/@ericbluesky/Hyperxen-UI
        │
        │  POST /api/chat
        │  GET  /api/welcome
        ▼
Azure App Service (Australia East)
  https://hyperxen-pricing-bot-db5hmngq3woxa.azurewebsites.net
  Python 3.11 · FastAPI · uvicorn · B1 Linux
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
| App Service | hyperxen-pricing-bot-db5hmngq3woxa | rg-hyperxen-app-dev | Australia East |
| App Service Plan | hyperxen-pricing-bot-plan (B1 Linux) | rg-hyperxen-app-dev | Australia East |
| Azure AI Foundry | hyperxen-foundry-presales1 | rg-hyperxen-dev1 | East US 2 |
| Azure AI Search | hyperxen-search (Free tier) | rg-hyperxen-app-dev | Australia East |
| Managed Identity | Enabled on App Service | — | Principal ID: 1781559f-16d2-4fbc-9140-87489df58699 |
| Service Principal | hyperxen-app-sp | — | Reader role on subscription |

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
- SKU Advisor Agent — scenario-based VM recommendations via Azure AI Search; top 3 matches; live pricing fetch
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

```bash
az deployment group create --resource-group rg-hyperxen-app-dev --template-file infra/main.bicep
```

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
| `PYTHONPATH` | `/home/site/wwwroot` (set as App Service app setting) |
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
