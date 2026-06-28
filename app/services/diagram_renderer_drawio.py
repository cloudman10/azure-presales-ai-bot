"""
Convert architecture JSON (same schema as diagram_renderer_svg.py) to
draw.io mxGraphModel XML for display in the draw.io browser viewer.

Layout mirrors the SVG renderer:
  Left column   — onprem zones
  Center        — hub + spoke zones inside an "Microsoft Azure" swimlane
  Right column  — shared / mgmt zones (sidebar)
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# ── Zone routing (mirrors SVG renderer) ───────────────────────────────────────
_SIDEBAR_TYPES: set[str] = {"shared", "mgmt"}

# ── Azure draw.io stencil shape IDs (mscae library) ──────────────────────────
# If a stencil is not bundled in the static viewer it falls back to a rectangle.
_SHAPE: dict[str, str] = {
    "VirtualMachine":        "shape=mscae.compute.virtual_machine;",
    "HyperVHost":            "shape=mscae.compute.virtual_machine;",
    "OnPremVM":              "shape=mscae.compute.virtual_machine;",
    "OnPremServer":          "shape=mscae.compute.virtual_machine;",
    "ScaleSet":              "shape=mscae.compute.vm_scale_sets;",
    "AVDHostPool":           "shape=mscae.compute.azure_virtual_desktop;",
    "AppService":            "shape=mscae.compute.app_services;",
    "FunctionApp":           "shape=mscae.compute.function_apps;",
    "AKSCluster":            "shape=mscae.compute.kubernetes_services;",
    "ContainerApp":          "shape=mscae.compute.container_instances;",
    "AzureFirewall":         "shape=mscae.networking.firewall;",
    "OnPremFirewall":        "shape=mscae.networking.firewall;",
    "BastionHost":           "shape=mscae.networking.bastion;",
    "VPNGateway":            "shape=mscae.networking.vpn_gateway;",
    "ExpressRouteGateway":   "shape=mscae.networking.expressroute_circuit;",
    "LoadBalancer":          "shape=mscae.networking.load_balancer;",
    "ApplicationGateway":    "shape=mscae.networking.application_gateway;",
    "VirtualNetwork":        "shape=mscae.networking.virtual_network;",
    "OnPremNetwork":         "shape=mscae.networking.virtual_network;",
    "Subnet":                "shape=mscae.networking.subnet;",
    "NetworkSecurityGroup":  "shape=mscae.networking.network_security_group;",
    "PrivateDNSZone":        "shape=mscae.networking.dns_zone;",
    "PrivateEndpoint":       "shape=mscae.networking.private_link;",
    "NATGateway":            "shape=mscae.networking.nat;",
    "RouteTable":            "shape=mscae.networking.route_table;",
    "SQLDatabase":           "shape=mscae.databases.sql_database;",
    "SQLManagedInstance":    "shape=mscae.databases.sql_managed_instance;",
    "StorageAccount":        "shape=mscae.storage.storage_account;",
    "CosmosDB":              "shape=mscae.databases.cosmos_db;",
    "MySQLDatabase":         "shape=mscae.databases.azure_database_for_mysql;",
    "PostgreSQLDatabase":    "shape=mscae.databases.azure_database_for_postgresql;",
    "RedisCache":            "shape=mscae.databases.cache_for_redis;",
    "DataFactory":           "shape=mscae.analytics.data_factory;",
    "EntraID":               "shape=mscae.identity.microsoft_entra_id;",
    "ManagedIdentity":       "shape=mscae.identity.managed_identities;",
    "KeyVault":              "shape=mscae.security.key_vault;",
    "DefenderForCloud":      "shape=mscae.security.microsoft_defender_for_cloud;",
    "AzurePolicy":           "shape=mscae.management_governance.policy;",
    "Sentinel":              "shape=mscae.security.microsoft_sentinel;",
    "RecoveryServicesVault": "shape=mscae.management_governance.recovery_services_vault;",
    "LogAnalyticsWorkspace": "shape=mscae.management_governance.log_analytics_workspace;",
    "AzureMonitor":          "shape=mscae.management_governance.monitor;",
    "ApplicationInsights":   "shape=mscae.devtools.application_insights;",
    "UpdateManager":         "shape=mscae.management_governance.update_management;",
    "AutomationAccount":     "shape=mscae.management_governance.automation_accounts;",
    "CostManagement":        "shape=mscae.management_governance.cost_management;",
}

# Zone colours match SVG renderer
_FILL   = {"onprem": "#E8EAF6", "hub": "#E3F2FD", "spoke": "#E8F5E9",
           "shared": "#FFF3E0", "mgmt": "#F3E5F5"}
_STROKE = {"onprem": "#3F51B5", "hub": "#1565C0", "spoke": "#2E7D32",
           "shared": "#E65100", "mgmt": "#6A1B9A"}
_FONT   = {"onprem": "#283593", "hub": "#0D47A1", "spoke": "#1B5E20",
           "shared": "#BF360C", "mgmt": "#4A148C"}

# ── Layout constants ───────────────────────────────────────────────────────────
_HDR   = 28   # swimlane header height
_PAD   = 10   # inner top/bottom padding
_NW    = 190  # resource node width
_NH    = 42   # resource node height
_NG    = 8    # gap between nodes
_OW    = 220  # on-prem column width
_HW    = 220  # hub column width
_PW    = 220  # spoke column width
_SBW   = 220  # sidebar column width
_GAP   = 18   # column gap
_AZP   = 16   # azure envelope inner padding


# ── Helpers ────────────────────────────────────────────────────────────────────

_PROHIBITED = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]')


def _clean(s: str) -> str:
    s = _PROHIBITED.sub('', str(s or ''))
    for a, b in [('‘', "'"), ('’', "'"), ('“', '"'),
                 ('”', '"'), ('–', '-'), ('—', '-')]:
        s = s.replace(a, b)
    return s


def _zone_h(zone: dict) -> int:
    n = max(1, len(zone.get("resources", [])))
    return _HDR + _PAD + n * (_NH + _NG) - _NG + _PAD


def _col_h(zones: list[dict]) -> int:
    if not zones:
        return 220
    return sum(_zone_h(z) for z in zones) + max(0, len(zones) - 1) * 14


# ── Main renderer ──────────────────────────────────────────────────────────────

def render_drawio(arch: dict) -> str:
    """Return mxfile XML string for the given architecture dict."""
    zones       = arch.get("zones", [])
    connections = arch.get("connections", [])
    title       = _clean(arch.get("title", "Architecture"))[:60]

    col0    = [z for z in zones if z.get("type") == "onprem"]
    col1    = [z for z in zones if z.get("type") == "hub"]
    col2    = [z for z in zones if z.get("type") == "spoke"]
    col2   += [z for z in zones if z.get("type") not in {"onprem", "hub", "spoke"} | _SIDEBAR_TYPES]
    sidebar = [z for z in zones if z.get("type") in _SIDEBAR_TYPES]

    # Heights
    h0 = _col_h(col0)
    h1 = _col_h(col1)
    h2 = _col_h(col2)
    hs = _col_h(sidebar)
    az_inner_h = max(h1, h2, 220)
    az_h       = _HDR + _AZP + az_inner_h + _AZP

    top_y  = 50
    page_h = top_y + max(h0, az_h, hs, 300) + 60

    # X positions
    x0   = 30
    x_az = x0 + _OW + _GAP
    az_w = _AZP + _HW + _GAP + _PW + _AZP
    x_sb = x_az + az_w + _GAP
    page_w = x_sb + _SBW + 30

    # ── ID counter (0 and 1 reserved) ─────────────────────────────────────────
    _ctr: list[int] = [1]

    def nid() -> str:
        _ctr[0] += 1
        return str(_ctr[0])

    cells: list[ET.Element] = []
    res_cell: dict[str, str] = {}  # resource id/name → mxCell id

    def mk(cid: str, value: str, parent: str, style: str,
           x: int, y: int, w: int, h: int,
           vertex: bool = True, edge: bool = False,
           src: str | None = None, tgt: str | None = None) -> ET.Element:
        attrs: dict[str, str] = {
            "id": cid, "value": _clean(value), "style": style, "parent": parent,
        }
        if vertex:
            attrs["vertex"] = "1"
        if edge:
            attrs["edge"] = "1"
            if src:
                attrs["source"] = src
            if tgt:
                attrs["target"] = tgt
        el = ET.Element("mxCell", attrs)
        geo = ET.SubElement(el, "mxGeometry",
                            {"x": str(x), "y": str(y),
                             "width": str(w), "height": str(h),
                             "as": "geometry"})
        if edge:
            geo.set("relative", "1")
        return el

    def render_col(zone_list: list[dict], cx: int, cy: int,
                   col_w: int, parent: str = "1") -> None:
        y = cy
        for zone in zone_list:
            ztype  = zone.get("type", "spoke")
            resources = zone.get("resources", [])
            zh     = _zone_h(zone)
            fill   = _FILL.get(ztype,   "#F5F5F5")
            stroke = _STROKE.get(ztype, "#888888")
            fcolor = _FONT.get(ztype,   "#333333")
            zid    = nid()
            cells.append(mk(
                zid, zone.get("label", "Zone"), parent,
                f"swimlane;startSize={_HDR};fillColor={fill};strokeColor={stroke};"
                f"fontStyle=1;fontSize=11;fontColor={fcolor};"
                f"align=left;spacingLeft=8;swimlaneLine=1;rounded=1;arcSize=4;",
                cx, y, col_w, zh,
            ))
            ry = _HDR + _PAD
            for res in resources:
                rid    = nid()
                rtype  = res.get("type", "")
                shape_s = _SHAPE.get(rtype, "")
                if shape_s:
                    sty = (
                        f"{shape_s}fillColor=#dae8fc;strokeColor=#6c8ebf;"
                        "fontStyle=0;fontSize=10;align=center;"
                    )
                else:
                    sty = (
                        "rounded=1;arcSize=10;"
                        "fillColor=#f5f5f5;strokeColor=#666666;"
                        "fontColor=#333333;fontStyle=0;fontSize=10;"
                        "align=left;spacingLeft=8;"
                    )
                label = _clean(res.get("name", rtype))
                if res.get("role"):
                    label += f"\n({_clean(res['role'])})"
                cells.append(mk(rid, label, zid, sty, _PAD, ry, _NW, _NH))
                for k in (res.get("id", ""), res.get("name", "")):
                    if k:
                        res_cell[k] = rid
                ry += _NH + _NG
            y += zh + 14

    # Render all sections
    render_col(col0, x0, top_y, _OW)

    az_id = nid()
    cells.append(mk(
        az_id, "Microsoft Azure", "1",
        f"swimlane;startSize={_HDR};fillColor=#E3F2FD;strokeColor=#1565C0;"
        "strokeWidth=2;fontStyle=1;fontSize=12;fontColor=#1565C0;"
        "align=center;rounded=1;arcSize=3;",
        x_az, top_y - 10, az_w, az_h + 10,
    ))
    render_col(col1, _AZP, _HDR + _AZP, _HW, parent=az_id)
    render_col(col2, _AZP + _HW + _GAP, _HDR + _AZP, _PW, parent=az_id)

    render_col(sidebar, x_sb, top_y, _SBW)

    # Edges
    for conn in connections:
        src_c = res_cell.get(conn.get("from", ""))
        tgt_c = res_cell.get(conn.get("to", ""))
        if src_c and tgt_c:
            eid = nid()
            el = ET.Element("mxCell", {
                "id": eid,
                "value": _clean(conn.get("label", "")),
                "style": (
                    "edgeStyle=orthogonalEdgeStyle;rounded=1;"
                    "orthogonalLoop=1;jettySize=auto;"
                    "exitX=1;exitY=0.5;exitDx=0;exitDy=0;"
                ),
                "edge": "1",
                "source": src_c,
                "target": tgt_c,
                "parent": "1",
            })
            ET.SubElement(el, "mxGeometry", {"relative": "1", "as": "geometry"})
            cells.append(el)

    # ── Assemble XML tree ──────────────────────────────────────────────────────
    mxfile  = ET.Element("mxfile")
    diagram = ET.SubElement(mxfile, "diagram", {"name": title})
    model   = ET.SubElement(diagram, "mxGraphModel", {
        "dx": "1422", "dy": "762",
        "grid": "1", "gridSize": "10",
        "guides": "1", "tooltips": "1",
        "connect": "1", "arrows": "1",
        "fold": "1", "page": "1", "pageScale": "1",
        "pageWidth": str(int(page_w)),
        "pageHeight": str(int(page_h)),
        "math": "0", "shadow": "0",
    })
    root_el = ET.SubElement(model, "root")
    ET.SubElement(root_el, "mxCell", {"id": "0"})
    ET.SubElement(root_el, "mxCell", {"id": "1", "parent": "0"})
    for c in cells:
        root_el.append(c)

    xml_str = ET.tostring(mxfile, encoding="unicode", xml_declaration=False)
    ET.fromstring(xml_str)  # validate — raises if any prohibited char survived
    return xml_str
