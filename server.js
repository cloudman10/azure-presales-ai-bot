process.env.ANTHROPIC_API_KEY = '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';

const express = require('express');
const cors = require('cors');
const fetch = require('node-fetch');
const app = express();
const PORT = process.env.PORT || 3000;
app.use(cors());
app.use(express.json());

// ── REGION MAP ───────────────────────────────────────────────────────────────
const REGION_MAP = {
  'australia east': 'australiaeast', 'australia southeast': 'australiasoutheast',
  'australia central': 'australiacentral', 'east asia': 'eastasia',
  'southeast asia': 'southeastasia', 'central india': 'centralindia',
  'south india': 'southindia', 'west india': 'westindia',
  'japan east': 'japaneast', 'japan west': 'japanwest',
  'korea central': 'koreacentral', 'korea south': 'koreasouth',
  'new zealand north': 'newzealandnorth', 'malaysia west': 'malaysiawest',
  'malaysia central': 'malaysiacentral', 'indonesia central': 'indonesiacentral',
  'east us': 'eastus', 'east us 2': 'eastus2', 'west us': 'westus',
  'west us 2': 'westus2', 'west us 3': 'westus3', 'central us': 'centralus',
  'north central us': 'northcentralus', 'south central us': 'southcentralus',
  'canada central': 'canadacentral', 'canada east': 'canadaeast',
  'brazil south': 'brazilsouth', 'north europe': 'northeurope',
  'west europe': 'westeurope', 'uk south': 'uksouth', 'uk west': 'ukwest',
  'france central': 'francecentral', 'germany west central': 'germanywestcentral',
  'switzerland north': 'switzerlandnorth', 'norway east': 'norwayeast',
  'sweden central': 'swedencentral', 'uae north': 'uaenorth',
  'south africa north': 'southafricanorth', 'israel central': 'israelcentral',
  'qatar central': 'qatarcentral',
};

// Premium SSD disk tiers (USD/month retail)
const DISK_TIERS = {
  'P4':  { gb: 32,   price: 5.28   }, 'P6':  { gb: 64,   price: 10.21  },
  'P10': { gb: 128,  price: 19.71  }, 'P15': { gb: 256,  price: 38.11  },
  'P20': { gb: 512,  price: 73.22  }, 'P30': { gb: 1024, price: 135.17 },
  'P40': { gb: 2048, price: 261.40 }, 'P50': { gb: 4096, price: 510.71 },
  'P60': { gb: 8192, price: 956.34 },
};

const HOURS_PER_MONTH = 730;

// ── SKU NORMALIZATION ────────────────────────────────────────────────────────
function normalizeSkuName(raw) {
  if (!raw) return null;
  let sku = raw.trim();
  if (/^Standard_/i.test(sku)) sku = sku.replace(/^Standard_/i, '');
  sku = sku.replace(/\s+/g, '');

  // Handle constrained vCPU format: E8-4ads_v5 or E8-4adsv5
  // These have a hyphen with constrained core count
  const constrainedMatch = sku.match(/^([A-Za-z])(\d+-\d+[A-Za-z]*)(_v\d+|v\d+)?$/i);
  if (constrainedMatch) {
    const series  = constrainedMatch[1].toUpperCase();
    const size    = constrainedMatch[2].toLowerCase();
    const version = constrainedMatch[3]
      ? '_v' + constrainedMatch[3].replace(/[^0-9]/g, '')
      : '';
    return 'Standard_' + series + size + version;
  }

  // Standard format: insert _ before version if missing
  sku = sku.replace(/^([A-Za-z]\d+[A-Za-z]*?)(v\d+)$/i, '$1_$2');
  const match = sku.match(/^([A-Za-z])(\d+)([A-Za-z]*)(_v\d+)?$/i);
  if (!match) return null;
  return 'Standard_' + match[1].toUpperCase() + match[2] + match[3].toLowerCase() + (match[4] ? '_v' + match[4].replace(/[^0-9]/g, '') : '');
}

function extractSku(msg) {
  const pats = [
    /Standard_[A-Za-z]\d+[A-Za-z]*_v\d+/i, /Standard_[A-Za-z]\d+[A-Za-z]*/i,
    /[A-Za-z]\d+[a-z]*_v\d+/, /[A-Za-z]\d+[a-z]*v\d+/, /[A-Za-z]\d+[a-z]*\s+v\d+/,
  ];
  for (const p of pats) { const m = msg.match(p); if (m && !/^\d/.test(m[0])) return m[0]; }
  return null;
}

// ── REGION HELPERS ───────────────────────────────────────────────────────────
function extractRegion(msg) {
  const lower = msg.toLowerCase();
  for (const key of Object.keys(REGION_MAP).sort((a, b) => b.length - a.length)) {
    if (lower.includes(key)) return { displayName: key, armName: REGION_MAP[key] };
  }
  for (const [key, val] of Object.entries(REGION_MAP).sort((a, b) => b[1].length - a[1].length)) {
    if (lower.includes(val)) return { displayName: key, armName: val };
  }
  return null;
}

function displayRegion(arm) {
  const e = Object.entries(REGION_MAP).find(([, v]) => v === arm);
  return e ? e[0].replace(/\b\w/g, c => c.toUpperCase()) : arm;
}

// ── OS EXTRACTION ────────────────────────────────────────────────────────────
function extractOS(msg) {
  if (/\bwindows\b/i.test(msg)) return 'Windows';
  if (/\blinux\b|\bubuntu\b|\bdebian\b|\brhel\b|\bcentos\b|\bsuse\b/i.test(msg)) return 'Linux';
  return null;
}

// ── QUANTITY / STORAGE ───────────────────────────────────────────────────────
function extractQuantity(msg) {
  const words = { one:1,two:2,three:3,four:4,five:5,six:6,seven:7,eight:8,nine:9,ten:10 };
  const lower = msg.toLowerCase();
  for (const [w, n] of Object.entries(words)) { if (new RegExp('\\b' + w + '\\b').test(lower)) return n; }
  const pats = [/(\d+)\s*[x×]\s*(?:vm|vms|instance|server)/i, /[x×]\s*(\d+)/i, /(\d+)\s*[x×]/i, /(\d+)\s+(?:vms?|instances?|servers?)/i];
  for (const p of pats) { const m = msg.match(p); if (m) return parseInt(m[1], 10); }
  return 1;
}

function bestDiskTier(totalGb) {
  const sorted = Object.entries(DISK_TIERS).sort((a, b) => a[1].gb - b[1].gb);
  for (const [tier, info] of sorted) { if (info.gb >= totalGb) return [{ tier, ...info, count: 1 }]; }
  const [lt, li] = sorted[sorted.length - 1];
  return [{ tier: lt, ...li, count: Math.ceil(totalGb / li.gb) }];
}

function extractStorage(msg) {
  const hasSSD = /premium\s*ssd/i.test(msg);
  const tb = msg.match(/(\d+(?:\.\d+)?)\s*tb/i);
  if (tb) { const gb = parseFloat(tb[1]) * 1024; return { gb, label: hasSSD ? 'Premium SSD' : 'Managed Disk', disks: bestDiskTier(gb) }; }
  const gb = msg.match(/(\d+(?:\.\d+)?)\s*gb(?!\s*ram)/i);
  if (gb && parseFloat(gb[1]) >= 32) { const g = parseFloat(gb[1]); return { gb: g, label: hasSSD ? 'Premium SSD' : 'Managed Disk', disks: bestDiskTier(g) }; }
  return null;
}

function extractHB(msg)  { return /hybrid\s*benefit|ahb|azure\s*hybrid/i.test(msg); }
function extractRI(msg)  {
  if (/3.?year|3yr|three.?year/i.test(msg)) return '3 Years';
  if (/1.?year|1yr|one.?year/i.test(msg)) return '1 Year';
  if (/reserved|reservation|\bri\b/i.test(msg)) return '1 Year';
  return null;
}

// ── AZURE RETAIL PRICES API ──────────────────────────────────────────────────
// Convert Standard_D2_v3 → "D2 v3" (meterName format used for older series)
function skuToMeterName(sku) {
  return sku
    .replace(/^Standard_/i, '')
    .replace(/_v(\d+)$/i, ' v$1')
    .replace(/_/g, ' ');
}

async function fetchPrices(region, sku) {
  const baseUrl = 'https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview&$filter=';

  // Pass 1: query by armSkuName — works for most modern series (v4, v5, B, etc.)
  const f1 = "serviceName eq 'Virtual Machines' and armRegionName eq '" + region + "' and armSkuName eq '" + sku + "'";
  const r1 = await fetch(baseUrl + encodeURIComponent(f1), { headers: { Accept: 'application/json' }, timeout: 12000 });
  if (!r1.ok) throw new Error('Azure API HTTP ' + r1.status);
  const items1 = (await r1.json()).Items || [];
  if (items1.length > 0) return items1;

  // Pass 2: fallback by meterName — handles older series (Dv2, Dv3, DSv2, F, FS, etc.)
  // These series have known gaps in armSkuName indexing in the Azure Retail Prices API
  const meterName = skuToMeterName(sku);
  const f2 = "serviceName eq 'Virtual Machines' and armRegionName eq '" + region + "' and meterName eq '" + meterName + "'";
  const r2 = await fetch(baseUrl + encodeURIComponent(f2), { headers: { Accept: 'application/json' }, timeout: 12000 });
  if (!r2.ok) throw new Error('Azure API HTTP ' + r2.status);
  return (await r2.json()).Items || [];
}

// ── OS DETECTION FROM API ITEM ────────────────────────────────────────────────
function detectItemOS(item) {
  const product = (item.productName || '').toLowerCase();
  const sku     = (item.skuName || '').toLowerCase();
  if (product.includes('windows') || sku.includes('windows')) return 'Windows';
  return 'Linux';
}

function getItemPriceType(item) {
  // Azure API returns 'type' in some responses and 'priceType' in others
  return item.priceType || item.type || '';
}

function findPrice(items, os, priceType, term) {
  const clean = items.filter(item => {
    const s = (item.skuName || '').toLowerCase();
    return !s.includes('spot') && !s.includes('low priority');
  });

  const isHourly = (item) => (item.unitOfMeasure || '').includes('Hour');

  const matchesPriceType = (item) => {
    const pt = getItemPriceType(item);
    if (priceType === 'Consumption') return pt === 'Consumption' && isHourly(item);
    if (priceType === 'Reservation') return pt === 'Reservation' && item.reservationTerm === term && isHourly(item);
    return false;
  };

  // For Consumption: filter by OS first, then price type
  if (priceType === 'Consumption') {
    const byOS = clean.filter(item => detectItemOS(item) === os && matchesPriceType(item));
    if (byOS.length > 0) return byOS[0];
    // Fallback: Windows=highest, Linux=lowest priced hourly consumption item
    const allConsumption = clean.filter(item => matchesPriceType(item));
    if (!allConsumption.length) return null;
    if (os === 'Windows') return allConsumption.sort((a, b) => b.retailPrice - a.retailPrice)[0];
    return allConsumption.sort((a, b) => a.retailPrice - b.retailPrice)[0];
  }

  // For Reservations: Azure often does not put Windows in productName for RI items.
  // Only use hourly-rate RI items (unitOfMeasure = 1 Hour), not annual lump sums.
  if (priceType === 'Reservation') {
    const byOS = clean.filter(item => detectItemOS(item) === os && matchesPriceType(item));
    if (byOS.length > 0) return byOS[0];
    // Fallback: all hourly RI items for this term — Windows=highest, Linux=lowest
    const allRI = clean.filter(item => matchesPriceType(item));
    if (!allRI.length) return null;
    if (os === 'Windows') return allRI.sort((a, b) => b.retailPrice - a.retailPrice)[0];
    return allRI.sort((a, b) => a.retailPrice - b.retailPrice)[0];
  }

  return null;
}

// ── FORMAT PRICING OUTPUT ─────────────────────────────────────────────────────
async function getPricingResult(collected) {
  const { sku, region, os, qty = 1, storage, wantsHB } = collected;

  let items;
  try { items = await fetchPrices(region, sku); }
  catch (e) { return 'Error reaching Azure Pricing API: ' + e.message; }

  console.log('[DEBUG] Total:', items.length, 'types:', [...new Set(items.map(i => (i.priceType||i.type) + '/' + (i.reservationTerm||'')))].join(', '));
  // DEBUG: log all returned items for troubleshooting
  console.log('[DEBUG] ' + items.length + ' items for ' + sku + ':');
  items.forEach(i => console.log('  ' + (i.priceType||i.type) + ' | ' + (i.reservationTerm||'PAYG') + ' | ' + i.unitOfMeasure + ' | ' + i.retailPrice + ' | ' + i.productName));
  if (!items.length) return 'No pricing data found for ' + sku + ' in ' + displayRegion(region) + '.\n\nThe VM may not be available in this region. Please check the SKU and region.';

  const paygItem = findPrice(items, os, 'Consumption');
  if (!paygItem) return 'Found Azure data but no PAYG ' + os + ' price for ' + sku + '. Please verify the OS type.';

  const currency = paygItem.currencyCode || 'USD';
  const pH = paygItem.retailPrice;
  const pM = pH * HOURS_PER_MONTH;

  const ri1 = findPrice(items, os, 'Reservation', '1 Year');
  const ri3 = findPrice(items, os, 'Reservation', '3 Years');

  // Helper: convert RI retailPrice to monthly cost.
  // Azure Retail Prices API stores RI prices inconsistently:
  //   - Modern series (v4/v5+): stored as TRUE hourly rate (e.g. 0.25/hr) with unitOfMeasure=1 Hour
  //   - Some series: stored as ANNUAL lump sum (e.g. 3767 for 1yr) with unitOfMeasure=1 Hour
  // We detect lump sums by checking if price > 50 (no VM costs 0+/hr on RI).
  // Annual lump sum: divide by 8760hrs/yr to get hourly, then x730 for monthly.
  // 3-Year lump sum: divide by 26280hrs/3yr to get hourly, then x730 for monthly.
  const riMonthly = (item) => {
    const price = item.retailPrice;
    const uom   = (item.unitOfMeasure || '').toLowerCase();
    const term  = item.reservationTerm || '1 Year';

    // Annual/multi-year lump sum stored with non-hour unit
    if (uom.includes('year')) {
      if (term === '3 Years') return price / 36;
      return price / 12;
    }

    // Stored as hourly rate — check if it is a true hourly or a lump sum mislabelled as hourly
    // Heuristic: real VM hourly RI rates are always under 0/hr. Above that = annual lump sum.
    if (price > 50) {
      // Annual lump sum mislabelled as 1 Hour
      if (term === '3 Years') return (price / 26280) * HOURS_PER_MONTH;
      return (price / 8760) * HOURS_PER_MONTH;
    }

    // True hourly rate
    return price * HOURS_PER_MONTH;
  };

  const hbPayg = (wantsHB && os === 'Windows') ? findPrice(items, 'Linux', 'Consumption') : null;
  const hbRi1  = (wantsHB && os === 'Windows') ? findPrice(items, 'Linux', 'Reservation', '1 Year') : null;
  const hbRi3  = (wantsHB && os === 'Windows') ? findPrice(items, 'Linux', 'Reservation', '3 Years') : null;

  const diskM = storage ? storage.disks.reduce((s, d) => s + d.price * d.count, 0) : 0;

  const f2 = n => n.toFixed(2);
  const f4 = n => n.toFixed(4);
  const pct = (a, b) => ((a - b) / a * 100).toFixed(0);
  const c = n => currency + ' ' + f2(n);
  const reg = displayRegion(region);
  const qL = qty > 1 ? qty + 'x ' : '';

  let out = '';
  out += '=== Azure VM Pricing Estimate ===\n';
  out += 'VM:       ' + qL + sku + '\n';
  out += 'OS:       ' + os + (wantsHB && os === 'Windows' ? ' + Azure Hybrid Benefit' : '') + '\n';
  out += 'Region:   ' + reg + '\n';
  if (qty > 1) out += 'Quantity: ' + qty + ' VMs\n';
  if (storage) out += 'Storage:  ' + storage.label + ' ' + storage.gb + 'GB\n';
  out += '\n';

  out += '--- PAYG (Pay-as-you-go) ---\n';
  out += 'Per VM:  ' + currency + ' ' + f4(pH) + '/hr  |  ' + c(pM) + '/month\n';
  if (qty > 1) out += 'Total:   ' + c(pM * qty) + '/month\n';
  if (diskM > 0) {
    out += 'Disk/VM: USD ' + f2(diskM) + '/month\n';
    if (qty > 1) out += 'Disk total: USD ' + f2(diskM * qty) + '/month\n';
  }

  // Windows RI total = compute RI cost + Windows license cost
  // License cost = (Windows PAYG - Linux PAYG) * 730 — stays constant across RI terms
  const linuxPaygItem = os === 'Windows' ? findPrice(items, 'Linux', 'Consumption') : null;
  const winLicMonthly = (os === 'Windows' && linuxPaygItem)
    ? Math.max(0, (pH - linuxPaygItem.retailPrice) * HOURS_PER_MONTH)
    : 0;

  out += '\n--- Reserved Instances (vs PAYG) ---\n';
  if (ri1) {
    const r1Compute = riMonthly(ri1);
    const r1M = r1Compute + winLicMonthly;
    out += '1-Year RI  (save ' + pct(pM, r1M) + '%):\n';
    out += '  Per VM:  ' + c(r1M) + '/month';
    if (os === 'Windows' && winLicMonthly > 0) {
      out += '  (' + c(r1Compute) + ' compute + ' + c(winLicMonthly) + ' Win license)';
    }
    out += '\n';
    if (qty > 1) out += '  Total:   ' + c(r1M * qty) + '/month\n';
  } else { out += '1-Year RI: not available in this region\n'; }

  if (ri3) {
    const r3Compute = riMonthly(ri3);
    const r3M = r3Compute + winLicMonthly;
    out += '3-Year RI  (save ' + pct(pM, r3M) + '%):\n';
    out += '  Per VM:  ' + c(r3M) + '/month';
    if (os === 'Windows' && winLicMonthly > 0) {
      out += '  (' + c(r3Compute) + ' compute + ' + c(winLicMonthly) + ' Win license)';
    }
    out += '\n';
    if (qty > 1) out += '  Total:   ' + c(r3M * qty) + '/month\n';
  } else { out += '3-Year RI: not available in this region\n'; }

  // Always show HB section for Windows VMs
  if (os === 'Windows') {
    const hbP  = findPrice(items, 'Linux', 'Consumption');
    const hbR1 = findPrice(items, 'Linux', 'Reservation', '1 Year');
    const hbR3 = findPrice(items, 'Linux', 'Reservation', '3 Years');

    out += '\n--- Azure Hybrid Benefit (compute rate only, no Windows license) ---\n';
    out += '(Requires existing Windows Server licenses with Software Assurance)\n';

    if (hbP) {
      const hM = hbP.retailPrice * HOURS_PER_MONTH;
      out += 'PAYG + HB:      ' + c(hM) + '/month  (save ' + pct(pM, hM) + '% vs Windows PAYG)\n';
    }
    if (hbR1) {
      const h1M = riMonthly(hbR1);
      out += '1-Year RI + HB: ' + c(h1M) + '/month  (save ' + pct(pM, h1M) + '% vs Windows PAYG)\n';
    }
    if (hbR3) {
      const h3M = riMonthly(hbR3);
      out += '3-Year RI + HB: ' + c(h3M) + '/month  (save ' + pct(pM, h3M) + '% vs Windows PAYG)\n';
    }
  }
  out += '\nMonthly estimates based on ' + HOURS_PER_MONTH + ' hours. Prices are retail list rates.';
  if (diskM > 0) out += ' Disk prices in USD (global).';
  return out;
}

// ── CLAUDE SYSTEM PROMPT ──────────────────────────────────────────────────────
const SYSTEM_PROMPT = `You are an Azure VM pricing assistant. Your job is to collect three required fields to look up VM pricing, then confirm you have everything needed.

The three required fields are, in this order:
1. VM SKU name (e.g. D4s_v5, E8s_v3, Standard_F16s_v2, D2_v3, B2ms)
2. Azure Region (e.g. Australia East, Southeast Asia, East US)
3. OS type: Windows or Linux only

RULES:
- If the user provides some or all fields upfront, acknowledge what you have and only ask for what is missing in order.
- Ask ONE question at a time. Never ask for two fields in the same message.
- Be friendly and concise. Keep responses short.
- If the user types a city or location (e.g. "Sydney", "Melbourne", "Singapore"), map it to the closest Azure region and confirm.
- CRITICAL: NEVER tell the user a VM SKU does not exist. You do not have up-to-date knowledge of Azure VM availability. Azure regularly releases new VM series including v6, v7, and beyond. If the user says E8-4ads_v7 — that is the SKU, accept it and use it. Your only job is to collect the SKU exactly as the user provides it, normalize it to Standard_ format, and trigger the API lookup. The API will determine if the SKU is valid.
- You may also optionally collect: quantity, storage, Reserved Instance preference (1-year or 3-year), Azure Hybrid Benefit (Windows only). Ask about these ONLY after the three required fields are confirmed, and only if the user has not already mentioned them.
- Once you have SKU, Region, and OS confirmed, ask: "Got it! Would you like to include quantity, storage, or Reserved Instance options - or shall I fetch the pricing now?"
- If the user says "fetch", "go ahead", "get pricing", "yes", "now", "just 1", or similar, respond ONLY with this exact JSON and nothing else:
  FETCH_PRICING:{"sku":"<normalized_sku>","region":"<armRegionName>","os":"<Windows|Linux>","qty":<number>,"storageGb":null,"wantsHB":<true|false>,"wantsRI":<null|"1 Year"|"3 Years">}
- If the user provides quantity, storage or RI details, incorporate them into the FETCH_PRICING JSON.
- Normalize SKU to Azure format: Standard_D4s_v5, Standard_D2_v3, Standard_B2ms etc.
- Normalize region to armRegionName: australiaeast, southeastasia, eastus etc.
- Never make up or guess prices. Your only job is to collect fields and trigger the API call.
- If the user asks something unrelated to Azure VM pricing, politely redirect them.`;

// ── CALL CLAUDE ───────────────────────────────────────────────────────────────
async function callClaude(messages) {
  const response = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': process.env.ANTHROPIC_API_KEY,
      'anthropic-version': '2023-06-01',
    },
    body: JSON.stringify({
      model: 'claude-sonnet-4-20250514',
      max_tokens: 1024,
      system: SYSTEM_PROMPT,
      messages: messages,
    }),
  });

  if (!response.ok) {
    const err = await response.text();
    throw new Error('Claude API error: ' + response.status + ' ' + err);
  }

  const data = await response.json();
  return data.content && data.content[0] ? data.content[0].text : '';
}

function parseFetchMarker(text) {
  const match = text.match(/FETCH_PRICING:(\{[\s\S]+?\})/);
  if (!match) return null;
  try { return JSON.parse(match[1]); } catch (e) { return null; }
}

// ── API ROUTES ────────────────────────────────────────────────────────────────
app.post('/api/chat', async (req, res) => {
  const { messages } = req.body;
  if (!messages || !Array.isArray(messages) || messages.length === 0) {
    return res.status(400).json({ error: 'messages array required.' });
  }

  try {
    const claudeReply = await callClaude(messages);
    const fetchParams = parseFetchMarker(claudeReply);

    if (fetchParams) {
      const storage = fetchParams.storageGb
        ? { gb: fetchParams.storageGb, label: 'Premium SSD', disks: bestDiskTier(fetchParams.storageGb) }
        : null;

      const pricingResult = await getPricingResult({
        sku:     fetchParams.sku,
        region:  fetchParams.region,
        os:      fetchParams.os,
        qty:     fetchParams.qty || 1,
        storage: storage,
        wantsHB: fetchParams.wantsHB || false,
        wantsRI: fetchParams.wantsRI || null,
      });

      return res.json({ reply: pricingResult, type: 'pricing' });
    }

    return res.json({ reply: claudeReply, type: 'conversation' });

  } catch (err) {
    console.error('[/api/chat]', err.message);
    return res.status(500).json({ error: 'Server error: ' + err.message });
  }
});

app.get('/api/welcome', (req, res) => {
  res.json({ reply: 'Hello! I can look up Azure VM pricing for you.\n\nTry:\n  "I need VM pricing"\n  "D4s_v5 Windows in Australia East"\n  "5x E8s_v3 Linux Southeast Asia with 3-year RI"\n\nI will ask for anything that is missing.' });
});

// ── FRONTEND ──────────────────────────────────────────────────────────────────
const HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Azure VM Pricing Bot</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#F4F6F8;height:100vh;display:flex;flex-direction:column;max-width:920px;margin:0 auto}
.header{display:flex;align-items:center;gap:12px;padding:14px 24px;background:#fff;border-bottom:1px solid #E5E9ED;flex-shrink:0}
.header h1{font-size:15px;font-weight:600;color:#1A1A2E}
.header p{font-size:11px;color:#8B95A2;margin-top:2px}
.dot{width:7px;height:7px;border-radius:50%;background:#22C55E;margin-left:auto;flex-shrink:0}
.chat{flex:1;overflow-y:auto;padding:20px 24px;display:flex;flex-direction:column;gap:14px}
.msg{display:flex;gap:10px;align-items:flex-start}
.msg.user{flex-direction:row-reverse}
.av{width:30px;height:30px;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;flex-shrink:0}
.av.bot{background:#E8F4FD;color:#005A9E;border:1px solid #C3DDF5}
.av.user{background:#0078D4;color:#fff}
.bubble{border-radius:12px;padding:12px 16px;font-size:14px;line-height:1.65;max-width:82%}
.bubble.user{background:#0078D4;color:#fff;border-radius:12px 12px 4px 12px}
.bubble.bot{background:#fff;border:1px solid #E5E9ED;border-radius:12px 12px 12px 4px;color:#1A1A2E;white-space:pre-wrap}
.bubble.bot.pricing{font-family:'Consolas','Courier New',monospace;font-size:12.5px;background:#F8FBFF;border-color:#C3DDF5}
.typing{display:flex;align-items:center;gap:4px;padding:4px 0}
.typing span{width:5px;height:5px;border-radius:50%;background:#C3DDF5;animation:blink 1.2s ease-in-out infinite}
.typing span:nth-child(2){animation-delay:.2s}.typing span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{transform:scale(.7);opacity:.5}40%{transform:scale(1);opacity:1}}
.bar{padding:14px 24px;background:#fff;border-top:1px solid #E5E9ED;display:flex;gap:10px;flex-shrink:0}
textarea{flex:1;min-height:44px;max-height:140px;resize:none;border:1px solid #D1D5DB;border-radius:10px;padding:11px 14px;font-family:inherit;font-size:14px;outline:none;line-height:1.5;transition:border-color .15s}
textarea:focus{border-color:#0078D4}
.send{width:44px;height:44px;border-radius:10px;border:none;background:#0078D4;color:#fff;cursor:pointer;font-size:18px;flex-shrink:0;transition:background .15s}
.send:hover{background:#005A9E}.send:disabled{background:#D1D5DB;cursor:not-allowed}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}
.chip{font-size:11.5px;padding:5px 10px;border-radius:6px;background:#F4F6F8;border:1px solid #D1D5DB;color:#005A9E;cursor:pointer;line-height:1.4}
.chip:hover{background:#E8F4FD;border-color:#0078D4}
.chat::-webkit-scrollbar{width:4px}.chat::-webkit-scrollbar-track{background:transparent}.chat::-webkit-scrollbar-thumb{background:#D1D5DB;border-radius:4px}
</style>
</head>
<body>
<div class="header">
  <svg width="30" height="30" viewBox="0 0 32 32" fill="none"><rect width="32" height="32" rx="8" fill="#0078D4"/><path d="M8 22L14 10l4 8-2 1.5L8 22z" fill="white" opacity=".7"/><path d="M13 22h11l-5.5-9L13 22z" fill="white"/></svg>
  <div><h1>Azure VM Pricing Bot</h1><p>Powered by Claude &middot; Azure Retail Prices API</p></div>
  <div class="dot"></div>
</div>
<div class="chat" id="chat"></div>
<div class="bar">
  <textarea id="inp" placeholder="Type your request - e.g. I need VM pricing" rows="1"></textarea>
  <button class="send" id="btn">&#9658;</button>
</div>
<script>
const chat = document.getElementById('chat');
const inp  = document.getElementById('inp');
const btn  = document.getElementById('btn');
let history = [];

inp.addEventListener('input', () => { inp.style.height='auto'; inp.style.height=Math.min(inp.scrollHeight,140)+'px'; });
inp.addEventListener('keydown', e => { if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();} });
btn.addEventListener('click', send);

function addMsg(role, text, type) {
  const w = document.createElement('div'); w.className='msg '+role;
  const av = document.createElement('div'); av.className='av '+role; av.textContent=role==='bot'?'AZ':'You';
  const b = document.createElement('div');
  if (role==='user') { b.className='bubble user'; b.textContent=text; }
  else {
    b.className = 'bubble bot' + (type==='pricing' ? ' pricing' : '');
    b.textContent = text;
  }
  w.appendChild(av); w.appendChild(b);
  chat.appendChild(w); chat.scrollTop=chat.scrollHeight;
  return b;
}

function addTyping() {
  const w=document.createElement('div'); w.className='msg bot'; w.id='typing';
  const av=document.createElement('div'); av.className='av bot'; av.textContent='AZ';
  const b=document.createElement('div'); b.className='bubble bot';
  b.innerHTML='<div class="typing"><span></span><span></span><span></span></div>';
  w.appendChild(av); w.appendChild(b); chat.appendChild(w); chat.scrollTop=chat.scrollHeight;
}

async function send() {
  const text = inp.value.trim(); if (!text) return;
  inp.value=''; inp.style.height='auto'; btn.disabled=true;
  history.push({ role:'user', content:text });
  addMsg('user', text);
  addTyping();

  try {
    const r = await fetch('/api/chat', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({messages:history}) });
    const d = await r.json();
    document.getElementById('typing')?.remove();
    const reply = d.reply || d.error || 'Unexpected response.';
    const type  = d.type || 'conversation';
    if (type==='conversation') { history.push({ role:'assistant', content:reply }); }
    else { history.push({ role:'assistant', content:'Here is the pricing result from Azure.' }); }
    addMsg('bot', reply, type);
  } catch(e) {
    document.getElementById('typing')?.remove();
    addMsg('bot', 'Network error: ' + e.message);
  } finally { btn.disabled=false; inp.focus(); }
}

const SAMPLES = [
  'I need VM pricing',
  'D4s_v5 Windows Australia East',
  '5x E8s_v3 Linux Southeast Asia with 3-year RI',
  'D2_v3 Windows Australia East',
];

fetch('/api/welcome').then(r=>r.json()).then(d=>{
  const b = addMsg('bot', d.reply);
  const chips = document.createElement('div'); chips.className='chips';
  SAMPLES.forEach(s=>{
    const c=document.createElement('button'); c.className='chip'; c.textContent=s;
    c.onclick=()=>{ inp.value=s; inp.focus(); };
    chips.appendChild(c);
  });
  b.appendChild(chips);
}).catch(()=>addMsg('bot','Ready! Ask me about Azure VM pricing.'));
</script>
</body>
</html>`;

app.get('*', (req, res) => res.send(HTML));
app.listen(PORT, () => console.log('Azure VM Pricing Bot running at http://localhost:' + PORT));
