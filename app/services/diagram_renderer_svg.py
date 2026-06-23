"""
app/services/diagram_renderer_svg.py

Landscape SVG renderer for the Solution Architecture Designer.
Produces a readable landscape layout — zones as side-by-side panels.
No Graphviz or third-party renderer; pure Python SVG generation.
"""

import html as _html

# ── Layout constants ───────────────────────────────────────────────────────────
MARGIN        = 20
HEADER_H      = 60     # title + subtitle area
PANEL_W       = 220    # width of each zone panel
COL_GAP       = 44     # horizontal gap between columns (room for connection arrows)
ROW_GAP       = 14     # vertical gap between stacked zones in one column
ZONE_PAD      = 10     # inner padding inside a zone panel
ZONE_TITLE_H  = 28     # zone header bar height
RES_H         = 34     # height of each resource row
RES_PAD       = 5      # gap between resource rows
AZ_ENV_PAD    = 10     # padding around the Azure dashed-border envelope

# ── Zone styling ───────────────────────────────────────────────────────────────
_ZONE_STYLE = {
    "onprem": {"bg": "#F0F0F0", "border": "#AAAAAA", "hdr": "#757575", "hdr_fg": "#FFFFFF"},
    "hub":    {"bg": "#E3F2FD", "border": "#90CAF9", "hdr": "#1565C0", "hdr_fg": "#FFFFFF"},
    "spoke":  {"bg": "#E8F5E9", "border": "#A5D6A7", "hdr": "#2E7D32", "hdr_fg": "#FFFFFF"},
    "shared": {"bg": "#F3E5F5", "border": "#CE93D8", "hdr": "#6A1B9A", "hdr_fg": "#FFFFFF"},
    "mgmt":   {"bg": "#FFF3E0", "border": "#FFCC80", "hdr": "#E65100", "hdr_fg": "#FFFFFF"},
}
_DEFAULT_STYLE = {"bg": "#F5F5F5", "border": "#CCCCCC", "hdr": "#999999", "hdr_fg": "#FFFFFF"}

# ── Resource type → badge abbreviation ────────────────────────────────────────
_ABBREV = {
    "VirtualMachine": "VM",    "ScaleSet": "SS",         "AVDHostPool": "AVD",
    "FunctionApp": "FN",       "ContainerApp": "CA",     "AKSCluster": "AKS",
    "AppService": "App",
    "AzureFirewall": "FW",     "BastionHost": "BAS",     "VPNGateway": "VPN",
    "ExpressRouteGateway": "ER","ApplicationGateway": "AGW","LoadBalancer": "LB",
    "VirtualNetwork": "VNet",  "Subnet": "Sub",          "NetworkSecurityGroup": "NSG",
    "PrivateDNSZone": "DNS",   "PrivateEndpoint": "PE",  "NATGateway": "NAT",
    "RouteTable": "RT",        "VNetPeering": "Peer",
    "SQLDatabase": "SQL",      "SQLManagedInstance": "MI","StorageAccount": "STG",
    "CosmosDB": "CDB",         "MySQLDatabase": "MySQL", "PostgreSQLDatabase": "PG",
    "RedisCache": "Redis",     "DataFactory": "ADF",
    "EntraID": "Entra",        "KeyVault": "KV",         "DefenderForCloud": "DfC",
    "AzurePolicy": "Pol",      "Sentinel": "SIEM",       "ManagedIdentity": "MI",
    "RecoveryServicesVault": "RSV","LogAnalyticsWorkspace": "LA","AzureMonitor": "Mon",
    "ApplicationInsights": "AI","UpdateManager": "UM",   "AutomationAccount": "Auto",
    "CostManagement": "Cost",
    "OnPremVM": "VM",          "OnPremServer": "Svr",    "HyperVHost": "HV",
    "OnPremNetwork": "Net",    "OnPremFirewall": "FW",   "AzureService": "SVC",
}

# ── Resource type → badge colour ───────────────────────────────────────────────
_COLOR = {
    "VirtualMachine": "#0078D4",  "ScaleSet": "#0078D4",    "AVDHostPool": "#0078D4",
    "FunctionApp": "#0078D4",     "ContainerApp": "#0078D4","AKSCluster": "#0078D4",
    "AppService": "#0078D4",
    "AzureFirewall": "#0099BC",   "BastionHost": "#0099BC", "VPNGateway": "#0099BC",
    "ExpressRouteGateway": "#0099BC","ApplicationGateway": "#0099BC","LoadBalancer": "#0099BC",
    "VirtualNetwork": "#0099BC",  "Subnet": "#0099BC",      "NetworkSecurityGroup": "#0099BC",
    "PrivateDNSZone": "#0099BC",  "PrivateEndpoint": "#0099BC","NATGateway": "#0099BC",
    "RouteTable": "#0099BC",      "VNetPeering": "#0099BC",
    "SQLDatabase": "#7B2FBE",     "SQLManagedInstance": "#7B2FBE","StorageAccount": "#7B2FBE",
    "CosmosDB": "#7B2FBE",        "MySQLDatabase": "#7B2FBE","PostgreSQLDatabase": "#7B2FBE",
    "RedisCache": "#7B2FBE",      "DataFactory": "#7B2FBE",
    "EntraID": "#005A9E",         "KeyVault": "#D83B01",    "DefenderForCloud": "#D83B01",
    "AzurePolicy": "#5C2D91",     "Sentinel": "#D83B01",    "ManagedIdentity": "#005A9E",
    "RecoveryServicesVault": "#5C2D91","LogAnalyticsWorkspace": "#5C2D91",
    "AzureMonitor": "#5C2D91",    "ApplicationInsights": "#5C2D91",
    "UpdateManager": "#5C2D91",   "AutomationAccount": "#5C2D91","CostManagement": "#5C2D91",
    "OnPremVM": "#555555",        "OnPremServer": "#555555","HyperVHost": "#444444",
    "OnPremNetwork": "#666666",   "OnPremFirewall": "#666666","AzureService": "#0078D4",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _e(s: str) -> str:
    return _html.escape(str(s))

def _trunc(s: str, n: int = 24) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"

def _zone_h(zone: dict) -> int:
    n = len(zone.get("resources", []))
    return ZONE_PAD + ZONE_TITLE_H + n * (RES_H + RES_PAD) + ZONE_PAD - (RES_PAD if n else 0)

def _col_h(zones_list: list) -> int:
    if not zones_list:
        return 0
    return sum(_zone_h(z) for z in zones_list) + ROW_GAP * max(len(zones_list) - 1, 0)


# ── Main renderer ──────────────────────────────────────────────────────────────

def render_architecture_svg(arch: dict) -> bytes:
    """
    Render architecture JSON → landscape SVG bytes.

    Layout:
      Column 0  on-premises zones      (grey)
      Column 1  hub + spoke zones      (blue / green)  — wrapped in Azure envelope
      Column 2  shared + mgmt zones    (purple / amber) — wrapped in Azure envelope

    Raises ValueError when no zones schema present (caller should fall back).
    """
    zones = arch.get("zones")
    if not zones:
        raise ValueError("no zones in arch dict — use legacy renderer")

    connections = arch.get("connections", [])
    title       = arch.get("title", "Architecture")
    subtitle    = arch.get("subtitle", "")

    # ── Assign zones to columns ───────────────────────────────────────────────
    col0 = [z for z in zones if z.get("type") == "onprem"]
    col1 = sorted(
        [z for z in zones if z.get("type") in ("hub", "spoke")],
        key=lambda z: 0 if z.get("type") == "hub" else 1,
    )
    col2 = sorted(
        [z for z in zones if z.get("type") in ("shared", "mgmt")],
        key=lambda z: 0 if z.get("type") == "shared" else 1,
    )
    # Anything unrecognised goes in col2
    known = {"onprem", "hub", "spoke", "shared", "mgmt"}
    col2 += [z for z in zones if z.get("type") not in known]

    max_col_h = max(_col_h(col0), _col_h(col1), _col_h(col2), 120)

    # ── Canvas dimensions ─────────────────────────────────────────────────────
    W = MARGIN + PANEL_W + COL_GAP + PANEL_W + COL_GAP + PANEL_W + MARGIN
    H = MARGIN + HEADER_H + max_col_h + MARGIN

    x0 = MARGIN
    x1 = MARGIN + PANEL_W + COL_GAP
    x2 = MARGIN + PANEL_W * 2 + COL_GAP * 2
    yc = MARGIN + HEADER_H  # top of content area

    # ── Azure envelope (col1 + col2) ──────────────────────────────────────────
    az_x = x1 - AZ_ENV_PAD
    az_y = yc - AZ_ENV_PAD
    az_w = PANEL_W * 2 + COL_GAP + AZ_ENV_PAD * 2
    az_h = max_col_h + AZ_ENV_PAD * 2

    # ── Position zones; collect bounding boxes & resource points ──────────────
    zone_bounds: dict[str, tuple] = {}   # zone_id → (x, y, w, h)
    res_to_zone: dict[str, str]  = {}    # resource_id → zone_id
    zone_svgs:   list[str]       = []

    def _place_column(col_zones: list, col_x: float) -> None:
        ry = float(yc)
        for zone in col_zones:
            zh    = _zone_h(zone)
            zid   = zone.get("id", "")
            style = _ZONE_STYLE.get(zone.get("type", ""), _DEFAULT_STYLE)
            label = _e(_trunc(zone.get("label", zid), 30))
            ress  = zone.get("resources", [])

            zone_bounds[zid] = (col_x, ry, PANEL_W, zh)

            p: list[str] = [
                f'<g transform="translate({col_x:.0f},{ry:.0f})">',
                # Panel bg
                f'<rect width="{PANEL_W}" height="{zh}" rx="6"'
                f' fill="{style["bg"]}" stroke="{style["border"]}" stroke-width="1.5"/>',
                # Header bar
                f'<rect width="{PANEL_W}" height="{ZONE_TITLE_H}" rx="6" fill="{style["hdr"]}"/>',
                # Square off the bottom corners of header
                f'<rect y="{ZONE_TITLE_H - 6}" width="{PANEL_W}" height="6" fill="{style["hdr"]}"/>',
                # Label
                f'<text x="{PANEL_W // 2}" y="{ZONE_TITLE_H // 2 + 1}"'
                f' text-anchor="middle" dominant-baseline="middle"'
                f' font-size="11" font-weight="600" fill="{style["hdr_fg"]}"'
                f' font-family="system-ui,Arial,sans-serif">{label}</text>',
            ]

            res_y = float(ZONE_PAD + ZONE_TITLE_H)
            for res in ress:
                rid   = res.get("id", "")
                rtype = res.get("type", "AzureService")
                rname = _e(_trunc(res.get("name", rid)))
                rrole = _e(_trunc(res.get("role", ""), 30))
                abbr  = _ABBREV.get(rtype, rtype[:3].upper())
                color = _COLOR.get(rtype, "#0078D4")
                bw    = max(28, len(abbr) * 6 + 10)
                rw    = PANEL_W - ZONE_PAD * 2

                res_to_zone[rid] = zid

                tx = bw + 9
                p += [
                    f'<g transform="translate({ZONE_PAD},{res_y:.0f})">',
                    # Row bg
                    f'<rect width="{rw}" height="{RES_H}" rx="4"'
                    f' fill="#FFFFFF" stroke="#E8E8E8" stroke-width="0.8"/>',
                    # Colour badge
                    f'<rect x="3" y="3" width="{bw}" height="{RES_H - 6}" rx="3" fill="{color}"/>',
                    f'<text x="{3 + bw / 2:.1f}" y="{RES_H // 2 + 1}"'
                    f' text-anchor="middle" dominant-baseline="middle"'
                    f' font-size="8" font-weight="700" fill="#FFFFFF"'
                    f' font-family="system-ui,Arial,sans-serif">{_e(abbr)}</text>',
                ]
                if rrole:
                    p += [
                        f'<text x="{tx}" y="{RES_H // 2 - 5}" dominant-baseline="middle"'
                        f' font-size="10" font-weight="500" fill="#1A1A2E"'
                        f' font-family="system-ui,Arial,sans-serif">{rname}</text>',
                        f'<text x="{tx}" y="{RES_H // 2 + 8}" dominant-baseline="middle"'
                        f' font-size="8" fill="#666666"'
                        f' font-family="system-ui,Arial,sans-serif">{rrole}</text>',
                    ]
                else:
                    p.append(
                        f'<text x="{tx}" y="{RES_H // 2 + 1}" dominant-baseline="middle"'
                        f' font-size="10" font-weight="500" fill="#1A1A2E"'
                        f' font-family="system-ui,Arial,sans-serif">{rname}</text>'
                    )
                p.append("</g>")
                res_y += RES_H + RES_PAD

            p.append("</g>")
            zone_svgs.append("\n".join(p))
            ry += zh + ROW_GAP

    _place_column(col0, x0)
    _place_column(col1, x1)
    _place_column(col2, x2)

    # ── Connection arrows (zone-level, cross-zone only) ───────────────────────
    conn_svgs: list[str] = []
    drawn_pairs: set     = set()

    for conn in connections:
        sid = conn.get("from", "")
        did = conn.get("to", "")
        lbl = _trunc(conn.get("label", ""), 18)

        # Resolve IDs to zone IDs
        sz = res_to_zone.get(sid) or (sid if sid in zone_bounds else None)
        dz = res_to_zone.get(did) or (did if did in zone_bounds else None)
        if not sz or not dz or sz == dz:
            continue

        pair = (sz, dz)
        if pair in drawn_pairs:
            continue
        drawn_pairs.add(pair)

        sb = zone_bounds.get(sz)
        db = zone_bounds.get(dz)
        if not sb or not db:
            continue

        sx_c = sb[0] + sb[2] / 2
        dx_c = db[0] + db[2] / 2
        sy_c = sb[1] + sb[3] / 2
        dy_c = db[1] + db[3] / 2

        if abs(sx_c - dx_c) > 20:
            # Cross-column: horizontal bezier, edge-to-edge
            if sx_c < dx_c:
                px1, py1 = sb[0] + sb[2], sy_c
                px2, py2 = db[0],          dy_c
            else:
                px1, py1 = sb[0],          sy_c
                px2, py2 = db[0] + db[2],  dy_c
            gap = abs(px2 - px1)
            off = min(gap * 0.45, 30)
            d_  = (f"M {px1:.0f},{py1:.0f}"
                   f" C {px1 + (off if px1 < px2 else -off):.0f},{py1:.0f}"
                   f" {px2 + (-off if px1 < px2 else off):.0f},{py2:.0f}"
                   f" {px2:.0f},{py2:.0f}")
            lx, ly = (px1 + px2) / 2, min(py1, py2) - 5
        else:
            # Same column: short vertical line
            if sy_c < dy_c:
                px1, py1 = sx_c, sb[1] + sb[3]
                px2, py2 = dx_c, db[1]
            else:
                px1, py1 = sx_c, sb[1]
                px2, py2 = dx_c, db[1] + db[3]
            d_ = f"M {px1:.0f},{py1:.0f} L {px2:.0f},{py2:.0f}"
            lx, ly = (px1 + px2) / 2 + 6, (py1 + py2) / 2

        conn_svgs.append(
            f'<path d="{d_}" fill="none" stroke="#0078D4"'
            f' stroke-width="1.5" opacity="0.7" marker-end="url(#arr)"/>'
        )
        if lbl:
            conn_svgs.append(
                f'<rect x="{lx - 2:.0f}" y="{ly - 10:.0f}"'
                f' width="{len(lbl) * 5 + 6}" height="12" rx="2"'
                f' fill="#F4F6F8" opacity="0.85"/>',
            )
            conn_svgs.append(
                f'<text x="{lx:.0f}" y="{ly:.0f}" dominant-baseline="middle"'
                f' font-size="8" fill="#0078D4" font-style="italic"'
                f' font-family="system-ui,Arial,sans-serif">{_e(lbl)}</text>'
            )

    # ── Assemble final SVG ────────────────────────────────────────────────────
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg"'
        f' width="{W}" height="{H}" viewBox="0 0 {W} {H}">',

        # Arrow marker
        "<defs>",
        '  <marker id="arr" markerWidth="8" markerHeight="6"'
        '   refX="7" refY="3" orient="auto">',
        '    <polygon points="0 0, 8 3, 0 6" fill="#0078D4" opacity="0.8"/>',
        "  </marker>",
        "</defs>",

        # Canvas background
        f'<rect width="{W}" height="{H}" fill="#F4F6F8"/>',

        # Title
        f'<text x="{W // 2}" y="{MARGIN + 22}" text-anchor="middle"'
        f' font-size="15" font-weight="700" fill="#1A1A2E"'
        f' font-family="system-ui,Arial,sans-serif">{_e(title)}</text>',
    ]

    if subtitle:
        out.append(
            f'<text x="{W // 2}" y="{MARGIN + 42}" text-anchor="middle"'
            f' font-size="10" fill="#555555"'
            f' font-family="system-ui,Arial,sans-serif">{_e(subtitle)}</text>'
        )

    # Azure dashed envelope
    if col1 or col2:
        label_w = 120
        out += [
            f'<rect x="{az_x}" y="{az_y}" width="{az_w}" height="{az_h}"'
            f' rx="8" fill="none" stroke="#0078D4"'
            f' stroke-width="1.5" stroke-dasharray="6,3"/>',
            f'<rect x="{az_x + 8}" y="{az_y - 10}" width="{label_w}" height="18"'
            f' rx="4" fill="#F4F6F8"/>',
            f'<text x="{az_x + 8 + label_w // 2}" y="{az_y - 1}"'
            f' text-anchor="middle" font-size="10" font-weight="600" fill="#0078D4"'
            f' font-family="system-ui,Arial,sans-serif">Microsoft Azure</text>',
        ]

    # Connections drawn before zone panels so panels sit on top
    out += conn_svgs
    out += zone_svgs

    out.append("</svg>")

    return "\n".join(out).encode("utf-8")
