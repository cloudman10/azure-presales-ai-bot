"""
app/services/diagram_renderer_svg.py

One-page HLD SVG renderer — consulting-grade landscape document.

Layout:
  [Header bar: gradient title + subtitle          ] (full width)
  [Value pillars: Secure | Reliable | Manageable  ] (full width)
  [On-Prem | Hub | Spoke zone columns] [Sidebar   ]
  [Migration Approach band            ] [panels    ]
  [Design Principles | Future Options ] [stacked   ]
  [Legend                             ]
"""

import base64 as _b64
import html as _html
import pathlib as _pl
import re as _re

# ── XML 1.0 prohibited chars ──────────────────────────────────────────────────
_XML_PROHIBITED = _re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")

# ── Color palette (single source of truth — swap all values here for dark theme) ──
_PALETTE: dict = {
    # ── Canvas & surfaces ─────────────────────────────────────────────────────
    "canvas":           "#0d1117",   # page/document background (near-black)
    "surface":          "#161b22",   # resource row fill, migration step card bg, sidebar panel card
    "surface_alt":      "#1c2129",   # sidebar outer background
    "surface_mig":      "#161b22",   # pillar strip, migration band, Azure envelope label bg
    "surface_cp":       "#161b22",   # principles + future options band bg
    "surface_leg":      "#161b22",   # legend band bg
    # ── Borders & dividers ────────────────────────────────────────────────────
    "border":           "#30363d",   # resource row border, sidebar panel card border
    "border_panel":     "#30363d",   # sidebar outer panel border
    "border_mig":       "#30363d",   # pillar strip bottom line, pillar divider, migration band border
    "border_mig_step":  "#3d444d",   # migration step card border
    "border_cp":        "#30363d",   # principles+future band border
    "border_cp_div":    "#3d444d",   # vertical divider inside principles+future band
    "border_leg":       "#30363d",   # legend band border
    # ── Text ─────────────────────────────────────────────────────────────────
    "text_primary":     "#e6edf3",   # resource names, sidebar labels, region primary text
    "text_secondary":   "#adbac7",   # resource roles, migration step descriptions
    "text_muted":       "#768390",   # region paired row, legend "Legend:" label
    "text_faint":       "#545d68",   # "Region not specified" italic
    "text_dark":        "#e6edf3",   # principles/future options body text (light on dark bg)
    "text_badge":       "#ffffff",   # pillar description text, legend item labels
    # ── Accent colors ─────────────────────────────────────────────────────────
    "accent":           "#388bfd",   # primary blue: connections, envelope, circles, arrows
    "accent_teal":      "#39c5cf",   # networking badge color, region sidebar panel header
    # ── Header gradient ───────────────────────────────────────────────────────
    "hdr_start":        "#1f6feb",   # gradient left stop
    "hdr_end":          "#0d419d",   # gradient right stop
    "hdr_subtitle":     "#adbac7",   # subtitle text on header
    # ── Value pillar accent colors (bright hues readable on dark bg) ──────────
    "pillar_secure":    "#f0883e",   # Secure & Zero Trust; also Security sidebar panel header
    "pillar_reliable":  "#3fb950",   # Resilient & Available; also Future Options heading/bullets
    "pillar_efficient": "#bc8cff",   # Operationally Efficient; Mgmt sidebar panel, Principles heading
    # ── Sidebar panel header colors ───────────────────────────────────────────
    "sb_bkp":           "#388bfd",   # Backup & DR panel header (same as accent)
    # (Security panel = pillar_secure; Management panel = pillar_efficient; Region panel = accent_teal)
    # ── Badge fallback colors (letter-badge for unmapped resource types) ───────
    "badge_compute":    "#388bfd",   # compute: VM, AKS, App Service, Container, Function, AVD
    "badge_networking": "#39c5cf",   # networking: Firewall, Bastion, VPN, ER, LB, AGW, VNet, etc.
    "badge_data":       "#bc8cff",   # data/storage: SQL, Cosmos, Storage, MySQL, Postgres, Redis, ADF
    "badge_identity":   "#79c0ff",   # identity: Entra ID, Managed Identity
    "badge_security":   "#f0883e",   # security: Key Vault, Defender, Sentinel
    "badge_mgmt":       "#a371f7",   # management: Monitor, Log Analytics, RSV, Update Manager, etc.
    "badge_onprem":     "#8b949e",   # on-prem VM / Server badge fill
    "badge_onprem_hv":  "#6e7681",   # HyperV host badge fill
    "badge_onprem_net": "#768390",   # on-prem network / firewall badge fill
    # ── Zone style sub-dicts (_ZONE_STYLE is derived from these) ──────────────
    "zone_onprem": {"bg": "#1c2128", "border": "#6e7681", "hdr": "#373e47", "hdr_fg": "#e6edf3"},
    "zone_hub":    {"bg": "#0d2135", "border": "#388bfd", "hdr": "#1256c9", "hdr_fg": "#ffffff"},
    "zone_spoke":  {"bg": "#0d1f18", "border": "#3fb950", "hdr": "#1a4731", "hdr_fg": "#ffffff"},
    "zone_shared": {"bg": "#1e1228", "border": "#bc8cff", "hdr": "#3d246e", "hdr_fg": "#ffffff"},
    "zone_mgmt":   {"bg": "#201a0e", "border": "#d29922", "hdr": "#5c4400", "hdr_fg": "#ffffff"},
    "zone_default":{"bg": "#161b22", "border": "#3d444d", "hdr": "#1c2129", "hdr_fg": "#adbac7"},
}

# ── Icon infrastructure ───────────────────────────────────────────────────────
_ICON_DIR = _pl.Path(__file__).parent.parent.parent / "static" / "azure-icons"

_TYPE_ICON: dict[str, str] = {
    "VirtualMachine":       "virtual-machine.svg",
    "HyperVHost":           "virtual-machine.svg",
    "OnPremVM":             "virtual-machine.svg",
    "OnPremServer":         "virtual-machine.svg",
    "ScaleSet":             "scale-set.svg",
    "AVDHostPool":          "host-pool.svg",
    "AppService":           "app-service.svg",
    "FunctionApp":          "function-app.svg",
    "AKSCluster":           "aks.svg",
    "ContainerApp":         "container-instance.svg",
    "AzureFirewall":        "firewall.svg",
    "OnPremFirewall":       "firewall.svg",
    "BastionHost":          "bastion.svg",
    "VPNGateway":           "vpn-gateway.svg",
    "ExpressRouteGateway":  "expressroute.svg",
    "LoadBalancer":         "load-balancer.svg",
    "ApplicationGateway":   "app-gateway.svg",
    "VirtualNetwork":       "virtual-network.svg",
    "OnPremNetwork":        "virtual-network.svg",
    "Subnet":               "subnet.svg",
    "NetworkSecurityGroup": "nsg.svg",
    "PrivateDNSZone":       "dns-zone.svg",
    "PrivateEndpoint":      "private-link.svg",
    "NATGateway":           "nat-gateway.svg",
    "RouteTable":           "route-table.svg",
    "SQLDatabase":          "sql-database.svg",
    "SQLManagedInstance":   "sql-managed-instance.svg",
    "StorageAccount":       "storage-account.svg",
    "CosmosDB":             "cosmos-db.svg",
    "MySQLDatabase":        "mysql.svg",
    "PostgreSQLDatabase":   "postgresql.svg",
    "RedisCache":           "redis-cache.svg",
    "DataFactory":          "data-factory.svg",
    "EntraID":              "entra-id.svg",
    "ManagedIdentity":      "managed-identity.svg",
    "KeyVault":             "key-vault.svg",
    "DefenderForCloud":     "defender.svg",
    "AzurePolicy":          "policy.svg",
    "Sentinel":             "sentinel.svg",
    "RecoveryServicesVault":"recovery-vault.svg",
    "LogAnalyticsWorkspace":"log-analytics.svg",
    "AzureMonitor":         "monitor.svg",
    "ApplicationInsights":  "app-insights.svg",
    "UpdateManager":        "updates.svg",
    "AutomationAccount":    "automation.svg",
}

_icon_cache: dict[str, str | None] = {}

def _icon_uri(rtype: str) -> str | None:
    fn = _TYPE_ICON.get(rtype)
    if not fn:
        return None
    if fn in _icon_cache:
        return _icon_cache[fn]
    p = _ICON_DIR / fn
    uri: str | None = ("data:image/svg+xml;base64," + _b64.b64encode(p.read_bytes()).decode()) if p.exists() else None
    _icon_cache[fn] = uri
    return uri

# ── Abbreviation + colour fallbacks ──────────────────────────────────────────
_ABBREV = {
    "VirtualMachine": "VM",     "ScaleSet": "SS",          "AVDHostPool": "AVD",
    "FunctionApp": "FN",        "ContainerApp": "CA",      "AKSCluster": "AKS",
    "AppService": "App",        "AzureFirewall": "FW",     "BastionHost": "BAS",
    "VPNGateway": "VPN",        "ExpressRouteGateway": "ER","ApplicationGateway": "AGW",
    "LoadBalancer": "LB",       "VirtualNetwork": "VNet",  "Subnet": "Sub",
    "NetworkSecurityGroup": "NSG","PrivateDNSZone": "DNS", "PrivateEndpoint": "PE",
    "NATGateway": "NAT",        "RouteTable": "RT",        "VNetPeering": "Peer",
    "SQLDatabase": "SQL",       "SQLManagedInstance": "MI","StorageAccount": "STG",
    "CosmosDB": "CDB",          "MySQLDatabase": "MySQL",  "PostgreSQLDatabase": "PG",
    "RedisCache": "Redis",      "DataFactory": "ADF",      "EntraID": "Entra",
    "KeyVault": "KV",           "DefenderForCloud": "DfC", "AzurePolicy": "Pol",
    "Sentinel": "SIEM",         "ManagedIdentity": "MI",   "RecoveryServicesVault": "RSV",
    "LogAnalyticsWorkspace": "LA","AzureMonitor": "Mon",   "ApplicationInsights": "AI",
    "UpdateManager": "UM",      "AutomationAccount": "Auto","CostManagement": "Cost",
    "OnPremVM": "VM",           "OnPremServer": "Svr",     "HyperVHost": "HV",
    "OnPremNetwork": "Net",     "OnPremFirewall": "FW",    "AzureService": "SVC",
}

_COLOR = {
    "VirtualMachine": _PALETTE["badge_compute"],   "ScaleSet": _PALETTE["badge_compute"],    "AVDHostPool": _PALETTE["badge_compute"],
    "FunctionApp": _PALETTE["badge_compute"],      "ContainerApp": _PALETTE["badge_compute"], "AKSCluster": _PALETTE["badge_compute"],
    "AppService": _PALETTE["badge_compute"],       "AzureFirewall": _PALETTE["badge_networking"], "BastionHost": _PALETTE["badge_networking"],
    "VPNGateway": _PALETTE["badge_networking"],    "ExpressRouteGateway": _PALETTE["badge_networking"],
    "ApplicationGateway": _PALETTE["badge_networking"], "LoadBalancer": _PALETTE["badge_networking"], "VirtualNetwork": _PALETTE["badge_networking"],
    "Subnet": _PALETTE["badge_networking"],        "NetworkSecurityGroup": _PALETTE["badge_networking"],
    "PrivateDNSZone": _PALETTE["badge_networking"], "PrivateEndpoint": _PALETTE["badge_networking"], "NATGateway": _PALETTE["badge_networking"],
    "RouteTable": _PALETTE["badge_networking"],    "VNetPeering": _PALETTE["badge_networking"],
    "SQLDatabase": _PALETTE["badge_data"],         "SQLManagedInstance": _PALETTE["badge_data"],    "StorageAccount": _PALETTE["badge_data"],
    "CosmosDB": _PALETTE["badge_data"],            "MySQLDatabase": _PALETTE["badge_data"],         "PostgreSQLDatabase": _PALETTE["badge_data"],
    "RedisCache": _PALETTE["badge_data"],          "DataFactory": _PALETTE["badge_data"],
    "EntraID": _PALETTE["badge_identity"],         "KeyVault": _PALETTE["badge_security"],          "DefenderForCloud": _PALETTE["badge_security"],
    "AzurePolicy": _PALETTE["badge_mgmt"],         "Sentinel": _PALETTE["badge_security"],          "ManagedIdentity": _PALETTE["badge_identity"],
    "RecoveryServicesVault": _PALETTE["badge_mgmt"], "LogAnalyticsWorkspace": _PALETTE["badge_mgmt"],
    "AzureMonitor": _PALETTE["badge_mgmt"],        "ApplicationInsights": _PALETTE["badge_mgmt"],
    "UpdateManager": _PALETTE["badge_mgmt"],       "AutomationAccount": _PALETTE["badge_mgmt"],     "CostManagement": _PALETTE["badge_mgmt"],
    "OnPremVM": _PALETTE["badge_onprem"],          "OnPremServer": _PALETTE["badge_onprem"],        "HyperVHost": _PALETTE["badge_onprem_hv"],
    "OnPremNetwork": _PALETTE["badge_onprem_net"], "OnPremFirewall": _PALETTE["badge_onprem_net"],  "AzureService": _PALETTE["badge_compute"],
}

# ── Zone colour styles ────────────────────────────────────────────────────────
_ZONE_STYLE = {
    "onprem": _PALETTE["zone_onprem"],
    "hub":    _PALETTE["zone_hub"],
    "spoke":  _PALETTE["zone_spoke"],
    "shared": _PALETTE["zone_shared"],
    "mgmt":   _PALETTE["zone_mgmt"],
}
_DEFAULT_STYLE = _PALETTE["zone_default"]

# ── Region detection ──────────────────────────────────────────────────────────
_KNOWN_REGIONS = [
    "Australia East", "Australia Southeast", "Australia Central",
    "East US 2", "East US", "West US 2", "West US 3", "West US",
    "Central US", "North Central US", "South Central US",
    "North Europe", "West Europe", "UK South", "UK West",
    "Southeast Asia", "East Asia", "Japan East", "Japan West",
    "Canada Central", "Canada East", "Brazil South",
    "South Africa North", "South Africa West",
    "Central India", "South India", "West India",
    "Germany West Central", "Switzerland North",
    "France Central", "Norway East", "Sweden Central",
    "UAE North", "Korea Central", "Korea South",
]

_REGION_PAIRS = {
    "Australia East": "Australia Southeast",
    "Australia Southeast": "Australia East",
    "East US": "West US",           "West US": "East US",
    "East US 2": "Central US",      "Central US": "East US 2",
    "North Europe": "West Europe",  "West Europe": "North Europe",
    "UK South": "UK West",          "UK West": "UK South",
    "Southeast Asia": "East Asia",  "East Asia": "Southeast Asia",
    "Japan East": "Japan West",     "Japan West": "Japan East",
    "Canada Central": "Canada East","Canada East": "Canada Central",
    "South Africa North": "South Africa West",
    "Central India": "South India", "West US 2": "West US 3",
    "Germany West Central": "North Europe",
    "France Central": "North Europe",
    "Norway East": "West Europe",   "Sweden Central": "North Europe",
}

def _find_region(text: str) -> str:
    tl = text.lower()
    for r in sorted(_KNOWN_REGIONS, key=lambda x: -len(x)):
        if r.lower() in tl:
            return r
    return ""

# ── Value pillars ─────────────────────────────────────────────────────────────
_PIL_DEFAULTS = [
    (_PALETTE["pillar_secure"],    "Secure & Zero Trust",
     "Azure Firewall, no public IPs, Bastion access, Entra hybrid identity"),
    (_PALETTE["pillar_reliable"],  "Resilient & Available",
     "Recovery Vault backup, geo-redundant storage, zone-aware deployment"),
    (_PALETTE["pillar_efficient"], "Operationally Efficient",
     "Azure Monitor, Update Manager, Automation for Day-2 operations"),
]
_SECURE_KW  = {"firewall","bastion","public","trust","security","nsg","identity","zero"}
_RELIABLE_KW= {"backup","recovery","availab","dr ","replicate","geo","resilient","redundan","ha "}
_MGMT_KW    = {"monitor","automat","update","patch","operat","log analytics","cost"}

def _make_pillars(principles: list[str]) -> list[tuple]:
    kw_sets = [_SECURE_KW, _RELIABLE_KW, _MGMT_KW]
    descs = [p[2] for p in _PIL_DEFAULTS]
    for p in principles:
        pl = p.lower()
        for i, kws in enumerate(kw_sets):
            if descs[i] == _PIL_DEFAULTS[i][2] and any(k in pl for k in kws):
                descs[i] = _trunc(p, 75)
                break
    return [(_PIL_DEFAULTS[i][0], _PIL_DEFAULTS[i][1], descs[i]) for i in range(3)]

# ── Helpers ───────────────────────────────────────────────────────────────────
def _e(s: str) -> str:
    return _html.escape(_XML_PROHIBITED.sub("", str(s)))

def _trunc(s: str, n: int = 24) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"

# ── Layout constants ──────────────────────────────────────────────────────────
_M       = 18    # canvas margin
_HDR_H   = 66    # header bar
_PIL_H   = 50    # value pillars row
_PIL_GAP = 10    # gap between pillar row and arch content

_PANEL_W = 218   # zone panel width
_COL_GAP = 36    # gap between arch columns
_SB_GAP  = 18    # gap: arch area <-> sidebar
_SB_W    = 200   # sidebar width
_AZP     = 10    # Azure envelope padding

_ZP      = 8     # zone inner padding
_ZTH     = 25    # zone title bar height
_RES_H   = 30    # resource row height
_RES_P   = 3     # resource row gap
_ROW_GAP = 10    # gap between stacked zones in a column

_SB_TH   = 22    # sidebar panel title height
_SB_IH   = 22    # sidebar item height
_SB_PAD  = 6     # sidebar inner y-padding (top + bottom each)
_SB_PG   = 10    # gap between sidebar panels

_MBAND_H = 70    # migration band height
_LEG_H   = 36    # legend height
_BAND_GAP= 10    # gap between bands

# Derived widths
_ARCH_W    = 3 * _PANEL_W + 2 * _COL_GAP   # = 726
_CONTENT_W = _ARCH_W + _SB_GAP + _SB_W     # = 944
W          = _M + _CONTENT_W + _M           # = 980


def _zone_h(zone: dict) -> int:
    n = len(zone.get("resources", []))
    res = max(0, n * (_RES_H + _RES_P) - _RES_P) if n else 0
    return _ZP + _ZTH + res + _ZP


def _col_h(zones: list) -> int:
    if not zones:
        return 0
    return sum(_zone_h(z) for z in zones) + _ROW_GAP * (len(zones) - 1)


def _sb_panel_h(n_items: int) -> int:
    return _SB_TH + _SB_PAD + n_items * _SB_IH + _SB_PAD


# ── Main renderer ─────────────────────────────────────────────────────────────

def render_architecture_svg(arch: dict) -> bytes:
    """Render architecture JSON → one-page HLD SVG document."""
    zones = arch.get("zones", [])
    if not zones:
        raise ValueError("no zones in arch dict — use legacy renderer")

    connections     = arch.get("connections", [])
    title           = arch.get("title", "Architecture")
    subtitle        = arch.get("subtitle", "")
    shared_services = arch.get("shared_services", [])
    mig_steps       = arch.get("migration_approach", [])
    principles      = arch.get("design_principles", [])
    future_opts     = arch.get("future_options", [])

    # ── Classify zones ────────────────────────────────────────────────────────
    _SIDEBAR_TYPES = {"shared", "mgmt"}
    col0 = [z for z in zones if z.get("type") == "onprem"]
    col1 = [z for z in zones if z.get("type") == "hub"]
    col2 = [z for z in zones if z.get("type") == "spoke"]
    col2 += [z for z in zones if z.get("type") not in {"onprem","hub","spoke"} | _SIDEBAR_TYPES]

    # ── Collect sidebar resources ─────────────────────────────────────────────
    _SEC  = {"EntraID","ManagedIdentity","KeyVault","DefenderForCloud","AzurePolicy","Sentinel"}
    _MGMT = {"LogAnalyticsWorkspace","AzureMonitor","ApplicationInsights",
             "UpdateManager","AutomationAccount","CostManagement"}
    _BKP  = {"RecoveryServicesVault"}

    sec_items, mgmt_items, bkp_items = [], [], []
    sec_seen, mgmt_seen, bkp_seen = set(), set(), set()

    def _try(lst, seen, t, name, purpose):
        if t not in seen:
            lst.append((t, name, purpose)); seen.add(t)

    for zone in zones:
        for res in zone.get("resources", []):
            t, nm, rl = res.get("type",""), res.get("name",""), res.get("role","")
            if   t in _SEC:  _try(sec_items,  sec_seen,  t, nm, rl)
            elif t in _MGMT: _try(mgmt_items, mgmt_seen, t, nm, rl)
            elif t in _BKP:  _try(bkp_items,  bkp_seen,  t, nm, rl)

    for ss in shared_services:
        t, nm, pr = ss.get("type",""), ss.get("name",""), ss.get("purpose","")
        if   t in _SEC:  _try(sec_items,  sec_seen,  t, nm, pr)
        elif t in _MGMT: _try(mgmt_items, mgmt_seen, t, nm, pr)
        elif t in _BKP:  _try(bkp_items,  bkp_seen,  t, nm, pr)

    # ── Region ────────────────────────────────────────────────────────────────
    region_text    = title + " " + subtitle + " " + " ".join(z.get("label","") for z in zones)
    primary_region = _find_region(region_text)
    paired_region  = _REGION_PAIRS.get(primary_region, "")

    # ── Sidebar panels list ───────────────────────────────────────────────────
    sb_panels: list[tuple] = []   # (label, color, items_or_None)
    if sec_items:  sb_panels.append(("Security & Identity",     _PALETTE["pillar_secure"],    sec_items))
    if mgmt_items: sb_panels.append(("Management & Monitoring", _PALETTE["pillar_efficient"], mgmt_items))
    if bkp_items:  sb_panels.append(("Backup & DR",             _PALETTE["sb_bkp"],           bkp_items))
    # Region panel always shown
    region_items_count = (2 if primary_region and paired_region else
                          1 if primary_region else 1)
    sb_panels.append(("Region", _PALETTE["accent_teal"], None))   # None = special region rendering

    sb_total_h = sum(
        _sb_panel_h(len(p[2])) if p[2] is not None else _sb_panel_h(region_items_count)
        for p in sb_panels
    ) + _SB_PG * (len(sb_panels) - 1)

    # ── Architecture area height ──────────────────────────────────────────────
    arch_h = max(_col_h(col0), _col_h(col1), _col_h(col2), sb_total_h, 260)

    # ── Bottom-band heights (dynamic) ────────────────────────────────────────
    has_mig  = bool(mig_steps)
    n_princ  = min(len(principles), 8)
    n_future = min(len(future_opts), 6)
    has_cp   = bool(n_princ or n_future)

    # Combined principles+future band: header 20px + rows
    princ_rows  = -(-n_princ  // 2)   # ceil div
    future_rows = n_future
    cp_h = max(
        (20 + princ_rows  * 14 + 8) if n_princ  else 0,
        (20 + future_rows * 12 + 8) if n_future else 0,
        52
    )

    # ── Vertical layout ───────────────────────────────────────────────────────
    arch_y   = _M + _HDR_H + _PIL_H + _PIL_GAP
    bottom_y = arch_y + arch_h + 14

    total_bottom = 0
    if has_mig: total_bottom += _MBAND_H + _BAND_GAP
    if has_cp:  total_bottom += cp_h     + _BAND_GAP
    total_bottom += _LEG_H

    H = bottom_y + total_bottom + _M

    # ── Column x-positions ────────────────────────────────────────────────────
    cx0  = _M
    cx1  = _M + _PANEL_W + _COL_GAP
    cx2  = _M + 2 * (_PANEL_W + _COL_GAP)
    sb_x = _M + _ARCH_W + _SB_GAP

    # Azure envelope around col1 + col2 (whichever exist)
    az_has = col1 or col2
    if col1 and col2:
        az_x = cx1 - _AZP; az_w = 2 * _PANEL_W + _COL_GAP + 2 * _AZP
    elif col1:
        az_x = cx1 - _AZP; az_w = _PANEL_W + 2 * _AZP
    else:
        az_x = cx2 - _AZP; az_w = _PANEL_W + 2 * _AZP
    az_y = arch_y - _AZP
    az_h = arch_h + 2 * _AZP

    # ── Build SVG ─────────────────────────────────────────────────────────────
    out: list[str] = []
    zone_svgs: list[str] = []
    conn_svgs: list[str] = []
    zone_bounds: dict[str, tuple] = {}   # zone_id → (x, y, w, h)
    res_to_zone: dict[str, str]  = {}   # resource_id → zone_id

    # SVG root + defs
    out += [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<defs>',
        '  <linearGradient id="hdrG" x1="0%" y1="0%" x2="100%" y2="0%">',
        f'    <stop offset="0%"   stop-color="{_PALETTE["hdr_start"]}"/>',
        f'    <stop offset="100%" stop-color="{_PALETTE["hdr_end"]}"/>',
        '  </linearGradient>',
        '  <marker id="arr" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">',
        f'    <polygon points="0 0, 8 3, 0 6" fill="{_PALETTE["accent"]}" opacity="0.8"/>',
        '  </marker>',
        '</defs>',
        # Canvas background
        f'<rect width="{W}" height="{H}" fill="{_PALETTE["canvas"]}"/>',
    ]

    # ── Header bar ────────────────────────────────────────────────────────────
    out += [
        f'<rect x="0" y="0" width="{W}" height="{_HDR_H}" fill="url(#hdrG)"/>',
        # Decorative cloud circle
        f'<circle cx="{_M + 22}" cy="{_HDR_H // 2}" r="19" fill="white" opacity="0.10"/>',
        f'<text x="{_M + 22}" y="{_HDR_H // 2 + 1}" text-anchor="middle" dominant-baseline="middle"'
        f' font-size="18" fill="white" opacity="0.7" font-family="Arial,sans-serif">&#9729;</text>',
        # Title (one authoritative rendering — no other title in the SVG)
        f'<text x="{_M + 50}" y="{_HDR_H // 2 - (9 if subtitle else 0)}" dominant-baseline="middle"'
        f' font-size="18" font-weight="700" fill="white"'
        f' font-family="system-ui,Arial,sans-serif">{_e(_trunc(title, 85))}</text>',
    ]
    if subtitle:
        out.append(
            f'<text x="{_M + 50}" y="{_HDR_H // 2 + 12}" dominant-baseline="middle"'
            f' font-size="10" fill="{_PALETTE["hdr_subtitle"]}"'
            f' font-family="system-ui,Arial,sans-serif">{_e(_trunc(subtitle, 110))}</text>'
        )
    out.append(
        f'<text x="{W - _M}" y="{_HDR_H // 2 + 1}" text-anchor="end" dominant-baseline="middle"'
        f' font-size="9" fill="white" opacity="0.45"'
        f' font-family="system-ui,Arial,sans-serif">High-Level Design</text>'
    )

    # ── Value pillars row ─────────────────────────────────────────────────────
    pil_y = _HDR_H
    pil_w = W // 3
    pillars = _make_pillars(principles)
    out.append(f'<rect x="0" y="{pil_y}" width="{W}" height="{_PIL_H}" fill="{_PALETTE["surface_mig"]}"/>')
    # Thin separator line at bottom of pillar row
    out.append(
        f'<line x1="0" y1="{pil_y + _PIL_H - 1}" x2="{W}" y2="{pil_y + _PIL_H - 1}"'
        f' stroke="{_PALETTE["border_mig"]}" stroke-width="1"/>'
    )

    for i, (pcolor, ptitle, pdesc) in enumerate(pillars):
        px = i * pil_w
        py_mid = pil_y + _PIL_H // 2
        # Colored accent bar at pillar top
        out += [
            f'<rect x="{px + 2}" y="{pil_y}" width="{pil_w - 4}" height="3" fill="{pcolor}" opacity="0.75"/>',
            # Colored dot
            f'<circle cx="{px + _M}" cy="{py_mid - 6}" r="6" fill="{pcolor}"/>',
            # Title
            f'<text x="{px + _M + 14}" y="{py_mid - 6}" dominant-baseline="middle"'
            f' font-size="11" font-weight="700" fill="{pcolor}"'
            f' font-family="system-ui,Arial,sans-serif">{_e(ptitle)}</text>',
            # Description
            f'<text x="{px + _M}" y="{py_mid + 10}" dominant-baseline="middle"'
            f' font-size="8" fill="{_PALETTE["text_badge"]}"'
            f' font-family="system-ui,Arial,sans-serif">{_e(_trunc(pdesc, 78))}</text>',
        ]
        # Vertical divider (not after last pillar)
        if i < 2:
            out.append(
                f'<line x1="{px + pil_w}" y1="{pil_y + 5}" x2="{px + pil_w}" y2="{pil_y + _PIL_H - 5}"'
                f' stroke="{_PALETTE["border_mig"]}" stroke-width="1"/>'
            )

    # ── Azure envelope ────────────────────────────────────────────────────────
    if az_has:
        lbl_w = 130
        out += [
            f'<rect x="{az_x}" y="{az_y}" width="{az_w}" height="{az_h}"'
            f' rx="8" fill="none" stroke="{_PALETTE["accent"]}" stroke-width="1.5" stroke-dasharray="6,3"/>',
            f'<rect x="{az_x + 8}" y="{az_y - 12}" width="{lbl_w}" height="20" rx="4" fill="{_PALETTE["surface_mig"]}"/>',
            f'<text x="{az_x + 8 + lbl_w // 2}" y="{az_y - 2}"'
            f' text-anchor="middle" font-size="10" font-weight="600" fill="{_PALETTE["accent"]}"'
            f' font-family="system-ui,Arial,sans-serif">Microsoft Azure</text>',
        ]

    # ── Zone panel renderer ───────────────────────────────────────────────────
    def _place_column(col_zones: list, col_x: int) -> None:
        ry = float(arch_y)
        for zone in col_zones:
            zh    = _zone_h(zone)
            zid   = zone.get("id", "")
            style = _ZONE_STYLE.get(zone.get("type",""), _DEFAULT_STYLE)
            label = _e(_trunc(zone.get("label", zid), 32))
            ress  = zone.get("resources", [])

            zone_bounds[zid] = (col_x, ry, _PANEL_W, zh)

            p: list[str] = [
                f'<g transform="translate({col_x},{ry:.0f})">',
                f'<rect width="{_PANEL_W}" height="{zh}" rx="6"'
                f' fill="{style["bg"]}" stroke="{style["border"]}" stroke-width="1.5"/>',
                f'<rect width="{_PANEL_W}" height="{_ZTH}" rx="6" fill="{style["hdr"]}"/>',
                f'<rect y="{_ZTH - 6}" width="{_PANEL_W}" height="6" fill="{style["hdr"]}"/>',
                f'<text x="{_PANEL_W // 2}" y="{_ZTH // 2 + 1}"'
                f' text-anchor="middle" dominant-baseline="middle"'
                f' font-size="11" font-weight="600" fill="{style["hdr_fg"]}"'
                f' font-family="system-ui,Arial,sans-serif">{label}</text>',
            ]

            res_y = float(_ZP + _ZTH)
            for res in ress:
                rid   = res.get("id", "")
                rtype = res.get("type", "AzureService")
                rname = _e(_trunc(res.get("name", rid)))
                rrole = _e(_trunc(res.get("role", ""), 28))
                rw    = _PANEL_W - _ZP * 2
                ISIZ  = 26

                res_to_zone[rid] = zid
                icon = _icon_uri(rtype)

                p += [
                    f'<g transform="translate({_ZP},{res_y:.0f})">',
                    f'<rect width="{rw}" height="{_RES_H}" rx="4"'
                    f' fill="{_PALETTE["surface"]}" stroke="{_PALETTE["border"]}" stroke-width="0.8"/>',
                ]

                if icon:
                    iy = (_RES_H - ISIZ) // 2
                    tx = 3 + ISIZ + 5
                    p.append(f'<image href="{icon}" x="3" y="{iy}" width="{ISIZ}" height="{ISIZ}"/>')
                else:
                    abbr  = _ABBREV.get(rtype, rtype[:3].upper())
                    color = _COLOR.get(rtype, _PALETTE["accent"])
                    bw    = max(26, len(abbr) * 6 + 8)
                    tx    = bw + 9
                    p += [
                        f'<rect x="3" y="3" width="{bw}" height="{_RES_H - 6}" rx="3" fill="{color}"/>',
                        f'<text x="{3 + bw / 2:.1f}" y="{_RES_H // 2 + 1}"'
                        f' text-anchor="middle" dominant-baseline="middle"'
                        f' font-size="8" font-weight="700" fill="#FFFFFF"'
                        f' font-family="system-ui,Arial,sans-serif">{_e(abbr)}</text>',
                    ]

                if rrole:
                    p += [
                        f'<text x="{tx}" y="{_RES_H // 2 - 5}" dominant-baseline="middle"'
                        f' font-size="10" font-weight="500" fill="{_PALETTE["text_primary"]}"'
                        f' font-family="system-ui,Arial,sans-serif">{rname}</text>',
                        f'<text x="{tx}" y="{_RES_H // 2 + 7}" dominant-baseline="middle"'
                        f' font-size="8" fill="{_PALETTE["text_secondary"]}"'
                        f' font-family="system-ui,Arial,sans-serif">{rrole}</text>',
                    ]
                else:
                    p.append(
                        f'<text x="{tx}" y="{_RES_H // 2 + 1}" dominant-baseline="middle"'
                        f' font-size="10" font-weight="500" fill="{_PALETTE["text_primary"]}"'
                        f' font-family="system-ui,Arial,sans-serif">{rname}</text>'
                    )
                p.append("</g>")
                res_y += _RES_H + _RES_P

            p.append("</g>")
            zone_svgs.append("\n".join(p))
            ry += zh + _ROW_GAP

    _place_column(col0, cx0)
    _place_column(col1, cx1)
    _place_column(col2, cx2)

    # ── Connection arrows ─────────────────────────────────────────────────────
    drawn_pairs: set = set()
    for conn in connections:
        sid = conn.get("from", "")
        did = conn.get("to",   "")
        lbl = _trunc(conn.get("label", ""), 18)

        sz = res_to_zone.get(sid) or (sid if sid in zone_bounds else None)
        dz = res_to_zone.get(did) or (did if did in zone_bounds else None)
        if not sz or not dz or sz == dz:
            continue
        pair = (sz, dz)
        if pair in drawn_pairs:
            continue
        drawn_pairs.add(pair)

        sb_b = zone_bounds.get(sz)
        db_b = zone_bounds.get(dz)
        if not sb_b or not db_b:
            continue

        sx_c = sb_b[0] + sb_b[2] / 2
        dx_c = db_b[0] + db_b[2] / 2
        sy_c = sb_b[1] + sb_b[3] / 2
        dy_c = db_b[1] + db_b[3] / 2

        if abs(sx_c - dx_c) > 20:
            if sx_c < dx_c:
                px1, py1 = sb_b[0] + sb_b[2], sy_c
                px2, py2 = db_b[0],            dy_c
            else:
                px1, py1 = sb_b[0],            sy_c
                px2, py2 = db_b[0] + db_b[2],  dy_c
            off = min(abs(px2 - px1) * 0.45, 28)
            d_ = (f"M {px1:.0f},{py1:.0f}"
                  f" C {px1+(off if px1<px2 else -off):.0f},{py1:.0f}"
                  f" {px2+(-off if px1<px2 else off):.0f},{py2:.0f}"
                  f" {px2:.0f},{py2:.0f}")
            lx, ly = (px1 + px2) / 2, min(py1, py2) - 5
        else:
            if sy_c < dy_c:
                px1, py1 = sx_c, sb_b[1] + sb_b[3]
                px2, py2 = dx_c, db_b[1]
            else:
                px1, py1 = sx_c, sb_b[1]
                px2, py2 = dx_c, db_b[1] + db_b[3]
            d_ = f"M {px1:.0f},{py1:.0f} L {px2:.0f},{py2:.0f}"
            lx, ly = (px1 + px2) / 2 + 6, (py1 + py2) / 2

        conn_svgs.append(
            f'<path d="{d_}" fill="none" stroke="{_PALETTE["accent"]}"'
            f' stroke-width="1.5" opacity="0.7" marker-end="url(#arr)"/>'
        )
        if lbl:
            tw = len(lbl) * 5 + 6
            conn_svgs += [
                f'<rect x="{lx - 2:.0f}" y="{ly - 10:.0f}" width="{tw}" height="12"'
                f' rx="2" fill="{_PALETTE["canvas"]}" opacity="0.9"/>',
                f'<text x="{lx:.0f}" y="{ly:.0f}" dominant-baseline="middle"'
                f' font-size="8" fill="{_PALETTE["accent"]}" font-style="italic"'
                f' font-family="system-ui,Arial,sans-serif">{_e(lbl)}</text>',
            ]

    # Draw connections first (z-order), then zones on top
    out += conn_svgs
    out += zone_svgs

    # ── Right sidebar ─────────────────────────────────────────────────────────
    # Subtle background panel
    out.append(
        f'<rect x="{sb_x - 6}" y="{arch_y - 6}" width="{_SB_W + 12}" height="{arch_h + 12}"'
        f' rx="8" fill="{_PALETTE["surface_alt"]}" stroke="{_PALETTE["border_panel"]}" stroke-width="1"/>'
    )

    sb_py = arch_y
    for panel_label, panel_color, items in sb_panels:
        is_region = panel_label == "Region"
        n_items = region_items_count if is_region else len(items)
        ph = _sb_panel_h(n_items)

        out += [
            f'<rect x="{sb_x}" y="{sb_py}" width="{_SB_W}" height="{ph}"'
            f' rx="6" fill="{_PALETTE["surface"]}" stroke="{_PALETTE["border"]}" stroke-width="1"/>',
            f'<rect x="{sb_x}" y="{sb_py}" width="{_SB_W}" height="{_SB_TH}" rx="6" fill="{panel_color}"/>',
            f'<rect x="{sb_x}" y="{sb_py + _SB_TH - 6}" width="{_SB_W}" height="6" fill="{panel_color}"/>',
            f'<text x="{sb_x + _SB_W // 2}" y="{sb_py + _SB_TH // 2 + 1}"'
            f' text-anchor="middle" dominant-baseline="middle"'
            f' font-size="10" font-weight="700" fill="white"'
            f' font-family="system-ui,Arial,sans-serif">{_e(panel_label)}</text>',
        ]

        iy = sb_py + _SB_TH + _SB_PAD

        if is_region:
            # Primary region row
            if primary_region:
                out += [
                    f'<circle cx="{sb_x + 12}" cy="{iy + _SB_IH // 2}" r="5" fill="{panel_color}"/>',
                    f'<text x="{sb_x + 22}" y="{iy + _SB_IH // 2 + 1}" dominant-baseline="middle"'
                    f' font-size="9" font-weight="600" fill="{_PALETTE["text_primary"]}"'
                    f' font-family="system-ui,Arial,sans-serif">{_e(_trunc("Primary: " + primary_region, 26))}</text>',
                ]
                iy += _SB_IH
                if paired_region:
                    out += [
                        f'<circle cx="{sb_x + 12}" cy="{iy + _SB_IH // 2}" r="5"'
                        f' fill="none" stroke="{panel_color}" stroke-width="1.5"/>',
                        f'<text x="{sb_x + 22}" y="{iy + _SB_IH // 2 + 1}" dominant-baseline="middle"'
                        f' font-size="9" fill="{_PALETTE["text_muted"]}"'
                        f' font-family="system-ui,Arial,sans-serif">{_e(_trunc("Paired: " + paired_region, 26))}</text>',
                    ]
            else:
                out.append(
                    f'<text x="{sb_x + 10}" y="{iy + _SB_IH // 2 + 1}" dominant-baseline="middle"'
                    f' font-size="9" fill="{_PALETTE["text_faint"]}" font-style="italic"'
                    f' font-family="system-ui,Arial,sans-serif">Region not specified</text>'
                )
        else:
            ISIZ = 18
            for rtype, rname, _ in items:
                icon = _icon_uri(rtype)
                if icon:
                    iiy = iy + (_SB_IH - ISIZ) // 2
                    out.append(f'<image href="{icon}" x="{sb_x + 6}" y="{iiy}" width="{ISIZ}" height="{ISIZ}"/>')
                    tx = sb_x + 6 + ISIZ + 5
                else:
                    color = _COLOR.get(rtype, _PALETTE["accent"])
                    out.append(f'<circle cx="{sb_x + 12}" cy="{iy + _SB_IH // 2}" r="6" fill="{color}"/>')
                    tx = sb_x + 24
                out.append(
                    f'<text x="{tx}" y="{iy + _SB_IH // 2 + 1}" dominant-baseline="middle"'
                    f' font-size="9" fill="{_PALETTE["text_primary"]}"'
                    f' font-family="system-ui,Arial,sans-serif">{_e(_trunc(rname, 23))}</text>'
                )
                iy += _SB_IH

        sb_py += ph + _SB_PG

    # ── Bottom bands ──────────────────────────────────────────────────────────
    bx   = _M
    bw   = _CONTENT_W    # full content width (arch + sidebar)
    cur_y = bottom_y

    # Migration Approach band
    if has_mig:
        out += [
            f'<rect x="{bx}" y="{cur_y}" width="{bw}" height="{_MBAND_H}"'
            f' rx="6" fill="{_PALETTE["surface_mig"]}" stroke="{_PALETTE["border_mig"]}" stroke-width="1"/>',
            f'<text x="{bx + 10}" y="{cur_y + 14}" dominant-baseline="middle"'
            f' font-size="10" font-weight="700" fill="{_PALETTE["accent"]}"'
            f' font-family="system-ui,Arial,sans-serif">Migration Approach</text>',
        ]
        steps = mig_steps[:8]
        n = len(steps)
        if n > 0:
            ARW    = 16    # arrow gap
            avail  = bw - 20
            step_w = max(int((avail - (n - 1) * ARW) / n), 50)
            sy     = cur_y + 22
            sh     = _MBAND_H - 28

            # Strip "N - " prefix from step names
            _STEP_NUM_RE = _re.compile(r"^\d+\s*[-—–]\s*")

            for i, step in enumerate(steps):
                raw_name = step.get("step", "")
                sname    = _STEP_NUM_RE.sub("", raw_name)
                sdesc    = _trunc(step.get("description", ""), 42)
                sx_s     = bx + 10 + i * (step_w + ARW)

                out += [
                    f'<rect x="{sx_s}" y="{sy}" width="{step_w}" height="{sh}"'
                    f' rx="4" fill="{_PALETTE["surface"]}" stroke="{_PALETTE["border_mig_step"]}" stroke-width="1"/>',
                    f'<circle cx="{sx_s + 13}" cy="{sy + sh // 2}" r="10" fill="{_PALETTE["accent"]}"/>',
                    f'<text x="{sx_s + 13}" y="{sy + sh // 2 + 1}"'
                    f' text-anchor="middle" dominant-baseline="middle"'
                    f' font-size="9" font-weight="700" fill="white"'
                    f' font-family="system-ui,Arial,sans-serif">{i+1}</text>',
                    f'<text x="{sx_s + 28}" y="{sy + sh // 2 - (4 if sdesc else 0)}" dominant-baseline="middle"'
                    f' font-size="8.5" font-weight="600" fill="{_PALETTE["text_primary"]}"'
                    f' font-family="system-ui,Arial,sans-serif">{_e(_trunc(sname, 16))}</text>',
                ]
                if sdesc:
                    out.append(
                        f'<text x="{sx_s + 28}" y="{sy + sh // 2 + 9}" dominant-baseline="middle"'
                        f' font-size="7.5" fill="{_PALETTE["text_secondary"]}"'
                        f' font-family="system-ui,Arial,sans-serif">{_e(sdesc)}</text>'
                    )
                # Arrow to next step
                if i < n - 1:
                    ax = sx_s + step_w + 1
                    ay = sy + sh // 2
                    out.append(
                        f'<path d="M {ax},{ay} L {ax + ARW - 3},{ay}"'
                        f' stroke="{_PALETTE["accent"]}" stroke-width="1.5" opacity="0.7" marker-end="url(#arr)"/>'
                    )

        cur_y += _MBAND_H + _BAND_GAP

    # Combined principles + future options band
    if has_cp:
        out.append(
            f'<rect x="{bx}" y="{cur_y}" width="{bw}" height="{cp_h}"'
            f' rx="6" fill="{_PALETTE["surface_cp"]}" stroke="{_PALETTE["border_cp"]}" stroke-width="1"/>'
        )

        split_x = bx + int(bw * 0.58)

        if n_princ:
            out.append(
                f'<text x="{bx + 10}" y="{cur_y + 13}" dominant-baseline="middle"'
                f' font-size="10" font-weight="700" fill="{_PALETTE["pillar_efficient"]}"'
                f' font-family="system-ui,Arial,sans-serif">Key Design Principles</text>'
            )
            col_w = (split_x - bx - 20) // 2
            for idx, ptext in enumerate(principles[:8]):
                px_off = bx + 10 + (idx % 2) * (col_w + 4)
                py_off = cur_y + 22 + (idx // 2) * 14
                out += [
                    f'<circle cx="{px_off + 5}" cy="{py_off + 5}" r="4" fill="{_PALETTE["pillar_efficient"]}" opacity="0.75"/>',
                    f'<text x="{px_off + 14}" y="{py_off + 6}" dominant-baseline="middle"'
                    f' font-size="8.5" fill="{_PALETTE["text_dark"]}"'
                    f' font-family="system-ui,Arial,sans-serif">{_e(_trunc(ptext, 52))}</text>',
                ]

        if n_future:
            fo_x = split_x + 8
            out.append(
                f'<text x="{fo_x}" y="{cur_y + 13}" dominant-baseline="middle"'
                f' font-size="10" font-weight="700" fill="{_PALETTE["pillar_reliable"]}"'
                f' font-family="system-ui,Arial,sans-serif">Future Options</text>'
            )
            for idx, ftext in enumerate(future_opts[:6]):
                fy = cur_y + 22 + idx * 12
                out += [
                    f'<circle cx="{fo_x + 5}" cy="{fy + 4}" r="3" fill="{_PALETTE["pillar_reliable"]}" opacity="0.8"/>',
                    f'<text x="{fo_x + 13}" y="{fy + 5}" dominant-baseline="middle"'
                    f' font-size="8" fill="{_PALETTE["text_dark"]}"'
                    f' font-family="system-ui,Arial,sans-serif">{_e(_trunc(ftext, 46))}</text>',
                ]

        if n_princ and n_future:
            out.append(
                f'<line x1="{split_x}" y1="{cur_y + 6}" x2="{split_x}" y2="{cur_y + cp_h - 6}"'
                f' stroke="{_PALETTE["border_cp_div"]}" stroke-width="1"/>'
            )

        cur_y += cp_h + _BAND_GAP

    # Legend
    out += [
        f'<rect x="{bx}" y="{cur_y}" width="{bw}" height="{_LEG_H}"'
        f' rx="6" fill="{_PALETTE["surface_leg"]}" stroke="{_PALETTE["border_leg"]}" stroke-width="1"/>',
        f'<text x="{bx + 10}" y="{cur_y + _LEG_H // 2 + 1}" dominant-baseline="middle"'
        f' font-size="9" font-weight="600" fill="{_PALETTE["text_muted"]}"'
        f' font-family="system-ui,Arial,sans-serif">Legend:</text>',
        # Primary connection
        f'<line x1="{bx + 68}" y1="{cur_y + _LEG_H // 2}" x2="{bx + 94}" y2="{cur_y + _LEG_H // 2}"'
        f' stroke="{_PALETTE["accent"]}" stroke-width="2" marker-end="url(#arr)"/>',
        f'<text x="{bx + 98}" y="{cur_y + _LEG_H // 2 + 1}" dominant-baseline="middle"'
        f' font-size="8.5" fill="{_PALETTE["text_badge"]}" font-family="system-ui,Arial,sans-serif">Network connection</text>',
        # Azure boundary
        f'<rect x="{bx + 230}" y="{cur_y + _LEG_H // 2 - 7}" width="28" height="13"'
        f' rx="3" fill="none" stroke="{_PALETTE["accent"]}" stroke-width="1.5" stroke-dasharray="4,2"/>',
        f'<text x="{bx + 263}" y="{cur_y + _LEG_H // 2 + 1}" dominant-baseline="middle"'
        f' font-size="8.5" fill="{_PALETTE["text_badge"]}" font-family="system-ui,Arial,sans-serif">Azure boundary</text>',
        # On-premises
        f'<rect x="{bx + 370}" y="{cur_y + _LEG_H // 2 - 7}" width="28" height="13"'
        f' rx="3" fill="{_PALETTE["zone_onprem"]["bg"]}" stroke="{_PALETTE["zone_onprem"]["border"]}" stroke-width="1.5"/>',
        f'<text x="{bx + 403}" y="{cur_y + _LEG_H // 2 + 1}" dominant-baseline="middle"'
        f' font-size="8.5" fill="{_PALETTE["text_badge"]}" font-family="system-ui,Arial,sans-serif">On-premises zone</text>',
        # Sidebar
        f'<rect x="{bx + 510}" y="{cur_y + _LEG_H // 2 - 7}" width="28" height="13"'
        f' rx="3" fill="{_PALETTE["surface_alt"]}" stroke="{_PALETTE["border_panel"]}" stroke-width="1.5"/>',
        f'<text x="{bx + 543}" y="{cur_y + _LEG_H // 2 + 1}" dominant-baseline="middle"'
        f' font-size="8.5" fill="{_PALETTE["text_badge"]}" font-family="system-ui,Arial,sans-serif">Cross-cutting services</text>',
    ]

    out.append("</svg>")

    svg_bytes = "\n".join(out).encode("utf-8")

    import xml.etree.ElementTree as _ET
    _ET.fromstring(svg_bytes)   # raises ParseError on malformed XML

    return svg_bytes
