import json
import logging
import os
import re

from app.services.azure_pricing import (
    fetch_disk_tier_prices, fetch_prices, fetch_temp_storage_gb,
    fetch_v2_capacity_rate, vm_supports_premium,
)
from app.utils.pricing_calculator import (
    HOURS_PER_MONTH, detect_item_os, find_price, pick_tier, ri_monthly, v2_monthly_cost,
)
from app.utils.region_normalizer import display_region, extract_region

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
  if the user mentions them; otherwise use defaults (qty=1, no storage, no RI)
- Once SKU, Region and OS are confirmed, respond ONLY with this exact JSON and nothing else
  (do NOT ask any follow-up confirmation question):
  FETCH_PRICING:{"sku":"<normalized>","region":"<armRegionName>","os":"<Windows|Linux>","qty":<n>,"storage_gb":null,"wants_hb":false,"wants_ri":null}
- Normalize SKU to Standard_ format: Standard_D4s_v5, Standard_E8-4ads_v7
- CRITICAL: Constrained vCPU SKUs MUST keep the hyphen. The format is Standard_X{size}-{vcpu}{suffix}_vN
  - e42adsv5 → Standard_E4-2ads_v5 (NOT Standard_E42ads_v5)
  - e84adsv5 → Standard_E8-4ads_v5 (NOT Standard_E84ads_v5)
  - e164adsv5 → Standard_E16-4ads_v5 (NOT Standard_E164ads_v5)
  - e328adsv5 → Standard_E32-8ads_v5 (NOT Standard_E328ads_v5)
  - Rule: if digits run together without hyphen, split where second number is 2, 4, 8, or 16
- Normalize region to armRegionName: australiaeast, southeastasia, eastus
- Never make up prices

STORAGE PRICING (always included — server injects default if not specified):
A default OS disk (128 GiB, premium_ssd on premium-capable VMs, else standard_ssd) is
always shown in the pricing output. The server handles this — you do NOT need to add a
disks array for a plain VM price query. Only add a disks array when the user explicitly
asks to change the OS disk tier/size or add data disks.

Disk types: standard_hdd, standard_ssd, premium_ssd, premium_ssd_v2.
- premium_ssd / premium_ssd_v2 require premium storage support — server enforces this.
- Accept sizes in GiB or GB; accept any disk type the user names.
- Data disks are opt-in — only include when user asks.

When the user specifies custom disks, include them in the FETCH_PRICING marker:
  FETCH_PRICING:{"sku":"...","region":"...","os":"...","qty":1,"storage_gb":null,"wants_hb":false,"wants_ri":null,"disks":[{"role":"os","type":"premium_ssd","size_gb":256},{"role":"data","type":"standard_ssd","size_gb":512}]}
An explicit OS disk in the array replaces the server default. Omit the disks field entirely
for plain VM queries — the server will inject the default OS disk automatically."""


_UNCERTAINTY_PHRASES = [
    "i don't know", "i dont know", "don't know", "dont know",
    "not sure", "no idea",
    "you choose", "you pick", "recommend", "suggest",
    "which vm", "which one", "help me choose", "help me pick",
]

# Matches resource specs like "6 cores", "minimum 4 vcpus", "8gb", "16 GB RAM"
_SPEC_RE = re.compile(
    r'\b(?:minimum\s+)?\d+\s*(?:v?cpu|vcore|core)s?\b'
    r'|\b\d+\s*(?:gb|gib)\b',
    re.IGNORECASE,
)

# Matches recognisable VM SKU names like D4s_v5, E8-4ads_v7, Standard_B2ms
_SKU_PAT = re.compile(
    r'\b(?:Standard_)?[A-Za-z]\d+[A-Za-z-]*(?:_v\d+)?\b',
    re.IGNORECASE,
)


def _user_is_uncertain_about_sku(message: str) -> bool:
    lower = message.lower()
    if any(phrase in lower for phrase in _UNCERTAINTY_PHRASES):
        return True
    # If the message describes resource requirements (cores/RAM) but contains
    # no SKU name, the user is expressing what they *need* rather than what to
    # price — treat as uncertainty and hand off to the advisor.
    if _SPEC_RE.search(message) and not _SKU_PAT.search(message):
        return True
    return False


def _extract_known_context(messages: list[dict]) -> dict:
    """Scan conversation history for region, OS, and sizing already established."""
    from app.agents.sku_advisor_agent import parse_requirements
    found: dict = {"region": None, "os": None, "vcpus": None, "ram_gb": None}
    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = msg["content"]
        r = parse_requirements(text)
        if r["vcpus"] and not found["vcpus"]:
            found["vcpus"] = r["vcpus"]
        if r["ram_gb"] and not found["ram_gb"]:
            found["ram_gb"] = r["ram_gb"]
        if r["os"] and not found["os"]:
            found["os"] = r["os"]
        if not found["region"]:
            rm = extract_region(text)
            if rm:
                found["region"] = rm["arm_name"]
            elif r["region"]:
                found["region"] = r["region"]
    return found


def _parse_fetch_marker(text: str) -> dict | None:
    """Extract and parse the FETCH_PRICING JSON marker.
    Uses balanced-brace counting so nested objects (e.g. disks array) parse correctly.
    """
    start = text.find("FETCH_PRICING:")
    if start == -1:
        return None
    brace_start = text.find("{", start)
    if brace_start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[brace_start:], brace_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[brace_start : i + 1])
                except json.JSONDecodeError:
                    return None
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


_DISK_TYPE_LABELS = {
    "standard_hdd":   "Standard HDD",
    "standard_ssd":   "Standard SSD",
    "premium_ssd":    "Premium SSD",
    "premium_ssd_v2": "Premium SSD v2",
}


def _format_pricing(
    params: dict,
    items: list[dict],
    temp_storage_gb: int | None = None,
    disks: list[dict] | None = None,
) -> str:
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

    vcpus_val  = params.get('vcpus')
    ram_gb_val = params.get('ram_gb')

    out = "=== Azure VM Pricing Estimate ===\n"
    out += f"VM:       {q_label}{sku}\n"
    if vcpus_val:
        out += f"vCPUs:    {vcpus_val}\n"
    if ram_gb_val:
        out += f"RAM:      {ram_gb_val} GB\n"
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

    if disks:
        out += "\n=== Storage ===\n"
        has_std_var = False
        has_v2      = False
        subtotal    = 0.0
        for d in disks:
            label    = _DISK_TYPE_LABELS.get(d["type"], d["type"])
            role_str = "OS disk  " if d["role"] == "os" else "Data disk"
            tier_str = f" {d['tier']}" if d.get("tier") else ""
            fn       = ""
            if d.get("is_standard_variable"):
                fn = "*";  has_std_var = True
            elif d.get("is_v2_baseline"):
                fn = "**"; has_v2 = True
            dg_note = (
                "  ← downgraded: VM does not support premium storage\n"
                if d.get("was_downgraded") else ""
            )
            out += (
                f"{role_str}: {label}{tier_str} ({d['size_gb']} GiB){fn}"
                f" — {c(d['monthly_cost'])}/month\n"
                f"{dg_note}"
            )
            subtotal += d["monthly_cost"]
        out += f"Storage subtotal: {c(subtotal)}/month\n"
        if has_std_var:
            out += (
                "* Capacity only; per-10K-operation transaction charges vary "
                "with workload I/O and are not included.\n"
            )
        if has_v2:
            out += (
                "** v2 capacity at free baseline (3,000 IOPS / 125 MB/s); "
                "provisioned IOPS/throughput above baseline add cost.\n"
            )
        out += "Default OS disk shown — ask to change the tier/size or add data disks.\n"

    out += f"\nAll prices are Microsoft Retail RRP. CSP pricing would be lower.\n"
    out += f"Monthly estimates based on {HOURS_PER_MONTH} hours."
    return out


async def run(messages: list[dict]) -> dict:
    import httpx

    # If user expresses SKU uncertainty and no SKU has been established yet,
    # hand off to sku_advisor_agent rather than looping the LLM.
    user_message = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
    )
    if _user_is_uncertain_about_sku(user_message):
        ctx = _extract_known_context(messages)
        return {
            "reply":        "No problem! Let me switch to recommendation mode — I'll find the best VM for you based on your requirements.",
            "handoff":      "sku_advisor",
            "known_region": ctx["region"],
            "known_os":     ctx["os"],
            "known_vcpus":  ctx["vcpus"],
            "known_ram_gb": ctx["ram_gb"],
        }

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
        from app.utils.sku_normalizer import normalize_sku_name
        raw_sku = fetch_params.get('sku', '')
        normalized = normalize_sku_name(raw_sku)
        if normalized:
            fetch_params['sku'] = normalized

    if fetch_params:
        sku        = fetch_params["sku"]
        region     = fetch_params["region"]
        disks_spec = fetch_params.get("disks") or []
        logger.debug("FETCH_PRICING triggered: sku=%s region=%s os=%s disks=%d",
                     sku, region, fetch_params.get("os"), len(disks_spec))
        try:
            items = await fetch_prices(region, sku)
        except Exception as e:
            return {"reply": f"Error reaching Azure Pricing API: {e}", "type": "pricing"}

        temp_storage_gb = await fetch_temp_storage_gb(sku, region)

        # Always resolve disk pricing. premium_ok is fetched once regardless.
        premium_ok = await vm_supports_premium(sku, region)

        # Inject a default 128 GiB OS disk when the marker didn't include one.
        # This ensures storage always renders — even for bare "price D4s_v5 ..." queries.
        if not any(d.get("role") == "os" for d in disks_spec):
            default_type = "premium_ssd" if premium_ok else "standard_ssd"
            disks_spec = [{"role": "os", "type": default_type, "size_gb": 128}] + list(disks_spec)

        tier_price_cache: dict[str, dict[str, float]] = {}
        v2_rate: float | None = None
        resolved_disks: list[dict] = []

        for disk in disks_spec:
            dtype   = disk.get("type", "standard_ssd")
            size_gb = int(disk.get("size_gb", 128))
            role    = disk.get("role", "data")

            was_downgraded = False
            if dtype in ("premium_ssd", "premium_ssd_v2") and not premium_ok:
                dtype          = "standard_ssd"
                was_downgraded = True

            if dtype == "premium_ssd_v2":
                if v2_rate is None:
                    v2_rate = await fetch_v2_capacity_rate(region)
                monthly_cost = v2_monthly_cost(size_gb, v2_rate) if v2_rate else 0.0
                resolved_disks.append({
                    "role": role, "type": dtype, "tier": None,
                    "size_gb": size_gb, "monthly_cost": monthly_cost,
                    "is_standard_variable": False, "is_v2_baseline": True,
                    "was_downgraded": was_downgraded,
                })
            else:
                if dtype not in tier_price_cache:
                    try:
                        tier_price_cache[dtype] = await fetch_disk_tier_prices(region, dtype)
                    except Exception as e:
                        logger.warning("disk tier fetch failed for %s: %s", dtype, e)
                        tier_price_cache[dtype] = {}
                tier         = pick_tier(size_gb, dtype)
                monthly_cost = tier_price_cache[dtype].get(tier, 0.0)
                resolved_disks.append({
                    "role": role, "type": dtype, "tier": tier,
                    "size_gb": size_gb, "monthly_cost": monthly_cost,
                    "is_standard_variable": dtype in ("standard_hdd", "standard_ssd"),
                    "is_v2_baseline": False,
                    "was_downgraded": was_downgraded,
                })

        pricing_text = _format_pricing(fetch_params, items, temp_storage_gb, resolved_disks)
        return {"reply": pricing_text, "type": "pricing"}

    return {"reply": claude_text, "type": "conversation"}
