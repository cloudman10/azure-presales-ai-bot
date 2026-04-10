## Azure Resources

- **Subscription ID:** dd5a4d29-50b0-4330-b83a-37094699272c

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
