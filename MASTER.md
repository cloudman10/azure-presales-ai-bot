## Azure Resources

- **Subscription ID:** dd5a4d29-50b0-4330-b83a-37094699272c
- **Azure Tenant ID:** ceba3126-eb69-4216-9b6f-623fdd3f19de
- **App Service URL:** https://hyperxen-pricing-bot-db5hmngq3woxa.azurewebsites.net
- **App Service Resource Group:** rg-hyperxen-app-dev (Australia East)
- **App Service Plan:** hyperxen-pricing-bot-plan (B1, Linux)
- **Managed Identity:** Enabled (Principal ID: 1781559f-16d2-4fbc-9140-87489df58699)
- **Service Principal:** hyperxen-app-sp (Reader role on subscription)
- **Azure AI Search:** hyperxen-search (Free tier, Australia East)
- **Search Endpoint:** https://hyperxen-search.search.windows.net
- **Search Index:** vm-skus (894 active SKUs indexed, 291 retired flagged)

## Project Structure

```
app/
  agents/
    orchestrator.py          ← routes user messages to the right agent
    pricing_agent.py         ← LLM conversation loop + pricing output formatter
    sku_agent.py             ← SKU Normalizer Agent (rule-based, no LLM)
    sku_advisor_agent.py     ← SKU Advisor (scenario-based, Azure AI Search)
    report_agent.py          ← Report Agent (Excel/PDF generation)
  routers/
    chat.py                  ← FastAPI /api/chat, /api/report/excel, /api/report/pdf
  services/
    azure_pricing.py         ← Azure Retail Prices API + ARM SKU capabilities
  utils/
    sku_normalizer.py        ← normalize_sku_name(), extract_sku()
    pricing_calculator.py    ← PAYG / RI / Savings Plan calculations
    region_normalizer.py     ← 60+ city → armRegionName mapping
  models/
    schemas.py
  config/
    settings.py
  main.py
scripts/
  index_vm_skus.py           ← VM SKU indexer for Azure AI Search
static/
  index.html                 ← chat UI with Excel/PDF download buttons
```

## What's Built and Working

- FastAPI chat API (`/api/chat`) with Azure OpenAI (GPT-4o) as the LLM
- Multi-turn conversation: collects SKU, region, OS before fetching pricing
- PAYG, 1-Year/3-Year RI, Savings Plan, and Azure Hybrid Benefit pricing output
- SKU Normalizer Agent (`app/agents/sku_agent.py`) — handles constrained vCPU normalization (e42adsv5 → Standard_E4-2ads_v5)
- Python SKU normalization always overrides LLM output to prevent hallucinated SKU names
- Temp storage display via Azure ARM API + Managed Identity (only shown when present)
- Deployed to Azure App Service (Australia East) — live and serving traffic
- **SKU Advisor Agent** — scenario-based VM recommendations via 5-state pure-Python conversation flow; queries Azure AI Search vm-skus index for top 3 matches; fetches live pricing; user selects 1/2/3/all for full pricing breakdown
- **Report Agent** — Excel (.xlsx) and PDF download from any pricing result; HyperXen.ai branding; section-aware formatting; download buttons appear inline after every pricing response
- **60+ city-to-region mapping** — covers Australia, Asia Pacific, Middle East, Europe, Americas, Africa
- **Modern SKU generation preference** — v4/v5/v6 ranked above v1/v2 in advisor recommendations; Promo and Basic variants excluded

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
| 10 | ⏳ Blocked | Waiting for Claude Foundry quota (currently using GPT-4o via Azure AI Foundry) |
| 11 | ✅ Done | SKU Advisor Agent with Azure AI Search, scenario-based recommendations, 5-state conversation flow |
| 12 | ✅ Done | Report Agent, Excel and PDF download, download buttons in UI |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `AZURE_OPENAI_ENDPOINT` | Azure AI Foundry endpoint |
| `AZURE_OPENAI_KEY` | Azure OpenAI API key |
| `AZURE_OPENAI_DEPLOYMENT` | Deployment name (e.g. `gpt-4o`) |
| `AZURE_SUBSCRIPTION_ID` | `dd5a4d29-50b0-4330-b83a-37094699272c` |
| `AZURE_TENANT_ID` | `ceba3126-eb69-4216-9b6f-623fdd3f19de` |
| `AZURE_CLIENT_ID` | Service principal client ID (hyperxen-app-sp) |
| `AZURE_CLIENT_SECRET` | Service principal secret |
| `AZURE_SEARCH_ENDPOINT` | `https://hyperxen-search.search.windows.net` |
| `AZURE_SEARCH_API_KEY` | Azure AI Search admin key |
| `ENVIRONMENT` | `dev` / `prod` |
| `PORT` | `8000` |

On App Service, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and `AZURE_CLIENT_SECRET` are set via app settings (not `.env`). Managed Identity is also enabled as a fallback when SP vars are absent.

## Deployment

### Azure App Service (Production)
- Resource Group: rg-hyperxen-app-dev (Australia East)
- URL: https://hyperxen-pricing-bot-db5hmngq3woxa.azurewebsites.net
- Deploy command:
```bash
cd ~/azure-presales-ai-bot && zip -r deploy.zip . --exclude ".git/*" --exclude ".venv/*" --exclude "__pycache__/*" --exclude "*.pyc" --exclude ".env" 2>/dev/null && az webapp deployment source config-zip --resource-group rg-hyperxen-app-dev --name hyperxen-pricing-bot-db5hmngq3woxa --src deploy.zip
```

### Bicep Template
- Location: infra/main.bicep
- Redeploy infrastructure:
```bash
az deployment group create --resource-group rg-hyperxen-app-dev --template-file ~/azure-presales-ai-bot/infra/main.bicep
```

## Key Pricing Logic

<!-- Pricing logic documentation goes here -->

## Known API Limitations

### Reserved Instance Availability
The Azure Retail Prices API does not publish RI items for all VM series. Three distinct cases:

**Case 1 - No items returned at all:**
- VM is retired or not available in that region
- Bot shows: "VM not found — may be retired or unavailable in this region"

**Case 2 - PAYG items exist but no RI items:**
- Azure calculator may still show RI pricing (sourced from internal Microsoft data)
- Affects: HPC series (HC44rs, HB-series, HBv2, HBv3), some specialty SKUs
- Bot shows: "RI not available via public API for this SKU — verify at azure.com/calculator"

**Case 3 - RI items exist:**
- Standard VM series: D, E, F, B, M series v3/v4/v5+
- Bot shows correct RI pricing

### Windows RI License Pricing
Azure does not publish Windows-specific RI items for older VM series (DSv2, Dv2, FSv2) in the public API. Windows license shown at PAYG RRP rate for these series. Newer series (v4, v5+) publish Windows RI items correctly.

### Savings Plan Data
The savingsPlan field is only present on Linux Consumption items. Windows items never carry savingsPlan data. Always fetch Linux item to get savings plan rates.

### VM Retirement
Microsoft publishes retirement notices at: https://learn.microsoft.com/en-us/azure/virtual-machines/retirement-announcements
When the API returns no items for a SKU, it may be retired. Bot handles this with "VM not found" message.
