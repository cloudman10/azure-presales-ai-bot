import json
import logging
import os
import re

from app.services.azure_pricing import fetch_prices, fetch_temp_storage_gb
from app.utils.pricing_calculator import HOURS_PER_MONTH, detect_item_os, find_price, ri_monthly
from app.utils.region_normalizer import display_region

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an Azure VM pricing assistant. Collect three required fields:
1. VM SKU name (e.g. D4s_v5, E8s_v3, E8-4ads_v7, D2_v3)
2. Azure Region (e.g. Australia East, East US, Southeast Asia)
3. OS type: Windows or Linux only

RULES:
- Ask ONE question at a time in order: SKU first, then Region, then OS
- If user provides some fields upfront, only ask for what is missing
- Map city names to regions: Sydney=Australia East, Melbourne=Australia Southeast,
  Singapore=Southeast Asia, Tokyo=Japan East
- CRITICAL: NEVER tell the user a VM SKU does not exist. Accept any SKU including
  v6, v7, constrained vCPU variants like E8-4ads_v7. Never reject a SKU.
- Accept quantity (e.g. 5x), storage (e.g. 1TB), RI preference (1-year/3-year)
  but only ask about these AFTER the 3 required fields are confirmed
- Once SKU, Region and OS are confirmed ask:
  "Got it! Would you like to include quantity, storage, or Reserved Instance
   options - or shall I fetch the pricing now?"
- When user says fetch/yes/go/now/one/just 1 or similar, respond ONLY with
  this exact JSON and nothing else:
  FETCH_PRICING:{"sku":"<normalized>","region":"<armRegionName>","os":"<Windows|Linux>","qty":<n>,"storage_gb":null,"wants_hb":false,"wants_ri":null}
- Normalize SKU to Standard_ format: Standard_D4s_v5, Standard_E8-4ads_v7
- Normalize region to armRegionName: australiaeast, southeastasia, eastus
- Never make up prices"""


def _parse_fetch_marker(text: str) -> dict | None:
    match = re.search(r'FETCH_PRICING:(\{[\s\S]+?\})', text)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _get_savings_plan(item: dict) -> dict:
    sp = item.get("savingsPlan") or []
    rates = {}
    for entry in sp:
        term = entry.get("term", "")
        price = entry.get("retailPrice") or entry.get("unitPrice") or 0.0
        if "1" in term:
            rates["1Y"] = price
        elif "3" in term:
            rates["3Y"] = price
    return rates


def _format_pricing(params: dict, items: list[dict], temp_storage_gb: int | None = None) -> str:
    sku = params['sku']
    region = params['region']
    os_type = params['os']
    qty = params.get('qty') or 1
    wants_hb = params.get('wants_hb') or False

    if not items:
        return (
            f"No pricing data found for {sku} in {display_region(region)}.\n\n"
            "The VM may not be available in this region. Please check the SKU and region."
        )

    payg_item = find_price(items, os_type, 'Consumption')
    if not payg_item:
        return (
            f"Found Azure data but no PAYG {os_type} price for {sku}. "
            "Please verify the OS type."
        )

    currency = payg_item.get('currencyCode') or 'USD'
    price_h = payg_item['retailPrice']
    price_m = price_h * HOURS_PER_MONTH

    ri1 = find_price(items, os_type, 'Reservation', '1 Year')
    ri3 = find_price(items, os_type, 'Reservation', '3 Years')

    # Always find Linux item directly by filtering productName
    linux_items = [
        i for i in items
        if 'windows' not in (i.get('productName') or '').lower()
        and (i.get('priceType') or i.get('type')) == 'Consumption'
        and 'spot' not in (i.get('skuName') or '').lower()
        and 'low priority' not in (i.get('skuName') or '').lower()
        and 'Hour' in (i.get('unitOfMeasure') or '')
    ]
    linux_payg_direct = sorted(linux_items, key=lambda x: x['retailPrice'])[0] if linux_items else None
    sp_linux = linux_payg_direct if linux_payg_direct else (payg_item if os_type == 'Linux' else None)
    sp_rates = _get_savings_plan(sp_linux) if sp_linux else {}

    win_lic_payg = (
        max(0.0, (price_h - linux_payg_direct['retailPrice']) * HOURS_PER_MONTH)
        if os_type == 'Windows' and linux_payg_direct
        else 0.0
    )

    def c(n: float) -> str:
        return f"{currency} {n:.2f}"

    def f4(n: float) -> str:
        return f"{n:.4f}"

    def pct(a: float, b: float) -> str:
        return f"{((a - b) / a * 100):.0f}"

    reg = display_region(region)
    q_label = f"{qty}x " if qty > 1 else ""

    out = "=== Azure VM Pricing Estimate ===\n"
    out += f"VM:       {q_label}{sku}\n"
    out += f"OS:       {os_type}"
    if wants_hb and os_type == 'Windows':
        out += " + Azure Hybrid Benefit"
    out += "\n"
    out += f"Region:   {reg}\n"
    if temp_storage_gb:
        out += f"Temp Storage: {temp_storage_gb} GB SSD (included)\n"
    if qty > 1:
        out += f"Quantity: {qty} VMs\n"
    out += "\n"

    out += "--- PAYG (Pay-as-you-go) ---\n"
    out += f"Per VM:  {currency} {f4(price_h)}/hr  |  {c(price_m)}/month\n"
    if qty > 1:
        out += f"Total:   {c(price_m * qty)}/month\n"
    if os_type == 'Windows' and linux_payg_direct:
        ahb_payg_m = linux_payg_direct['retailPrice'] * HOURS_PER_MONTH
        out += f"AHB PAYG: {currency} {ahb_payg_m:.2f}/month  (save {pct(price_m, ahb_payg_m)}% vs Windows PAYG RRP)\n"

    out += "\n--- Savings Plan (flexible, compute discount only) ---\n"
    if sp_rates:
        if os_type == "Windows":
            out += f"License: {c(win_lic_payg)}/month  (at RRP, no discount)\n\n"
        if "1Y" in sp_rates:
            sp1_compute = sp_rates["1Y"] * HOURS_PER_MONTH
            sp1_total = sp1_compute + win_lic_payg
            sp1_compute_base = linux_payg_direct['retailPrice'] * HOURS_PER_MONTH if linux_payg_direct else price_m
            out += f"1-Year Savings Plan  (~{pct(sp1_compute_base, sp1_compute)}% compute discount):\n"
            out += f"  Compute: {c(sp1_compute)}/month  (discounted)\n"
            if os_type == "Windows":
                out += f"  License: {c(win_lic_payg)}/month  (at RRP)\n"
            out += f"  Total:   {c(sp1_total)}/month\n"
            if qty > 1:
                out += f"  {qty} VMs:  {c(sp1_total * qty)}/month\n"
        if "3Y" in sp_rates:
            sp3_compute = sp_rates["3Y"] * HOURS_PER_MONTH
            sp3_total = sp3_compute + win_lic_payg
            out += f"\n3-Year Savings Plan  (~{pct(sp1_compute_base, sp3_compute)}% compute discount):\n"
            out += f"  Compute: {c(sp3_compute)}/month  (discounted)\n"
            if os_type == "Windows":
                out += f"  License: {c(win_lic_payg)}/month  (at RRP)\n"
            out += f"  Total:   {c(sp3_total)}/month\n"
            if qty > 1:
                out += f"  {qty} VMs:  {c(sp3_total * qty)}/month\n"
    else:
        out += "Savings Plan: not available for this VM\n"

    out += "\n--- Reserved Instances (vs PAYG) ---\n"
    if ri1:
        r1_compute = ri_monthly(ri1)
        r1_m = r1_compute + win_lic_payg
        out += f"1-Year RI  (save {pct(price_m, r1_m)}%):\n"
        out += f"  Per VM:  {c(r1_m)}/month"
        if os_type == 'Windows' and win_lic_payg > 0:
            out += f"  ({c(r1_compute)} compute + {c(win_lic_payg)} Win license)"
        out += "\n"
        if qty > 1:
            out += f"  Total:   {c(r1_m * qty)}/month\n"
    else:
        if payg_item:
            out += "1-Year RI: not available via public API for this SKU — verify at azure.com/calculator\n"
        else:
            out += "1-Year RI: not available in this region\n"

    if ri3:
        r3_compute = ri_monthly(ri3)
        r3_m = r3_compute + win_lic_payg
        out += f"3-Year RI  (save {pct(price_m, r3_m)}%):\n"
        out += f"  Per VM:  {c(r3_m)}/month"
        if os_type == 'Windows' and win_lic_payg > 0:
            out += f"  ({c(r3_compute)} compute + {c(win_lic_payg)} Win license)"
        out += "\n"
        if qty > 1:
            out += f"  Total:   {c(r3_m * qty)}/month\n"
    else:
        if payg_item:
            out += "3-Year RI: not available via public API for this SKU — verify at azure.com/calculator\n"
        else:
            out += "3-Year RI: not available in this region\n"

    # Always show HB section for Windows VMs
    if os_type == 'Windows':
        hb_p = linux_payg_direct
        hb_r1 = find_price(items, 'Linux', 'Reservation', '1 Year')
        hb_r3 = find_price(items, 'Linux', 'Reservation', '3 Years')

        out += "\n--- Azure Hybrid Benefit (compute rate only, no Windows license) ---\n"
        out += "(Requires existing Windows Server licenses with Software Assurance)\n"

        if hb_p:
            hb_m = hb_p['retailPrice'] * HOURS_PER_MONTH
            out += f"PAYG + HB:      {c(hb_m)}/month  (save {pct(price_m, hb_m)}% vs Windows PAYG RRP)\n"
        if hb_r1:
            h1_m = ri_monthly(hb_r1)
            out += f"1-Year RI + HB: {c(h1_m)}/month  (save {pct(price_m, h1_m)}% vs Windows PAYG RRP)\n"
        if hb_r3:
            h3_m = ri_monthly(hb_r3)
            out += f"3-Year RI + HB: {c(h3_m)}/month  (save {pct(price_m, h3_m)}% vs Windows PAYG RRP)\n"

    out += f"\nAll prices are Microsoft Retail RRP. CSP pricing will be lower.\n"
    out += f"Monthly estimates based on {HOURS_PER_MONTH} hours."
    return out


async def run(messages: list[dict]) -> dict:
    import httpx
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
    api_key = os.environ["AZURE_OPENAI_KEY"]
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-02-01"

    # Convert Anthropic message format to OpenAI format
    oai_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in messages:
        oai_messages.append({"role": m["role"], "content": m["content"]})

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json={"model": deployment, "messages": oai_messages, "max_tokens": 1024}
        )
        if not response.is_success:
            logger.error("Azure OpenAI error %s: %s", response.status_code, response.text)
        response.raise_for_status()
        data = response.json()

    claude_text = data["choices"][0]["message"]["content"]
    fetch_params = _parse_fetch_marker(claude_text)

    if fetch_params:
        sku = fetch_params["sku"]
        region = fetch_params["region"]
        logger.debug("FETCH_PRICING triggered: sku=%s region=%s os=%s", sku, region, fetch_params.get("os"))
        try:
            items = await fetch_prices(region, sku)
        except Exception as e:
            return {"reply": f"Error reaching Azure Pricing API: {e}", "type": "pricing"}
        temp_storage_gb = await fetch_temp_storage_gb(sku, region)
        pricing_text = _format_pricing(fetch_params, items, temp_storage_gb)
        return {"reply": pricing_text, "type": "pricing"}

    return {"reply": claude_text, "type": "conversation"}
