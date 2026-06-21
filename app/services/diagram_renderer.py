"""
app/services/diagram_renderer.py

Converts an architecture JSON dict to PNG bytes using the `diagrams` library.
Supports two schemas:
  - Zones schema (new):  {"zones": [...], "connections": [...], ...}
  - Flat schema (legacy): {"resources": [...], "connections": [...], "region": ...}
"""

import logging
import os
import tempfile
import threading

logger = logging.getLogger(__name__)

_render_lock = threading.Lock()

# Background colour per zone type
_ZONE_COLORS = {
    "onprem": "#EEEEEE",
    "hub":    "#E3F2FD",
    "spoke":  "#E8F5E9",
    "shared": "#F3E5F5",
    "mgmt":   "#FFF3E0",
}

# ── Hardcoded sample — smoke test ─────────────────────────────────────────────

SAMPLE_ARCHITECTURE: dict = {
    "title": "Three-Tier Web — Australia East",
    "region": "Australia East",
    "resources": [
        {"id": "lb1",  "type": "LoadBalancer",  "name": "Load Balancer"},
        {"id": "web1", "type": "VirtualMachine", "name": "Web VM 1"},
        {"id": "web2", "type": "VirtualMachine", "name": "Web VM 2"},
        {"id": "sql1", "type": "SQLDatabase",    "name": "Azure SQL"},
    ],
    "connections": [
        {"from": "lb1",  "to": "web1"},
        {"from": "lb1",  "to": "web2"},
        {"from": "web1", "to": "sql1"},
        {"from": "web2", "to": "sql1"},
    ],
}

# ── Type map ──────────────────────────────────────────────────────────────────

def _get_type_map() -> dict:
    """Return resource-type → diagrams node class mapping.

    Each import is wrapped so a missing class silently falls back to VM.
    """
    from diagrams.azure.compute import VM as _VM

    def _try(module: str, cls: str, default=None):
        try:
            mod = __import__(module, fromlist=[cls])
            return getattr(mod, cls)
        except (ImportError, AttributeError):
            return default if default is not None else _VM

    return {
        # ── Compute ───────────────────────────────────────────────────────────
        "VirtualMachine":      _VM,
        "VM":                  _VM,
        "ScaleSet":            _try("diagrams.azure.compute", "VMScaleSet"),
        "AVDHostPool":         _VM,
        "FunctionApp":         _try("diagrams.azure.compute", "FunctionApps"),
        "ContainerApp":        _try("diagrams.azure.compute", "ContainerInstances"),
        "AKSCluster":          _try("diagrams.azure.compute", "KubernetesServices"),
        "AppService":          _try("diagrams.azure.web",     "AppServices"),

        # ── Networking ────────────────────────────────────────────────────────
        "LoadBalancer":        _try("diagrams.azure.network", "LoadBalancers"),
        "ApplicationGateway":  _try("diagrams.azure.network", "ApplicationGateway"),
        "AzureFirewall":       _try("diagrams.azure.network", "Firewall"),
        "BastionHost":         _try("diagrams.azure.network", "BastionHost",
                                    _try("diagrams.azure.network", "LoadBalancers")),
        "VPNGateway":          _try("diagrams.azure.network", "VPNGateways",
                                    _try("diagrams.azure.network", "VirtualNetworkGateways")),
        "ExpressRouteGateway": _try("diagrams.azure.network", "ExpressRouteCircuits"),
        "VirtualNetwork":      _try("diagrams.azure.network", "VirtualNetworks"),
        "Subnet":              _try("diagrams.azure.network", "Subnets",
                                    _try("diagrams.azure.network", "VirtualNetworks")),
        "NetworkSecurityGroup":_try("diagrams.azure.network", "NetworkSecurityGroupsClassic",
                                    _try("diagrams.azure.network", "ApplicationSecurityGroups")),
        "PrivateDNSZone":      _try("diagrams.azure.network", "DNSZones"),
        "PrivateEndpoint":     _try("diagrams.azure.network", "PrivateLinkServices"),
        "NATGateway":          _try("diagrams.azure.network", "LoadBalancers"),
        "RouteTable":          _try("diagrams.azure.network", "RouteTables",
                                    _try("diagrams.azure.network", "VirtualNetworks")),
        "VNetPeering":         _try("diagrams.azure.network", "VirtualNetworks"),

        # ── Data ──────────────────────────────────────────────────────────────
        "SQLDatabase":         _try("diagrams.azure.database", "SQLDatabases"),
        "SQLManagedInstance":  _try("diagrams.azure.database", "SQLManagedInstances"),
        "StorageAccount":      _try("diagrams.azure.storage",  "StorageAccounts"),
        "CosmosDB":            _try("diagrams.azure.database", "CosmosDb"),
        "MySQLDatabase":       _try("diagrams.azure.database", "MySql"),
        "PostgreSQLDatabase":  _try("diagrams.azure.database", "PostgreSQL"),
        "RedisCache":          _try("diagrams.azure.database", "Cache"),
        "DataFactory":         _try("diagrams.azure.analytics", "DataFactories"),

        # ── Identity & Security ───────────────────────────────────────────────
        "EntraID":             _try("diagrams.azure.identity", "ActiveDirectory"),
        "KeyVault":            _try("diagrams.azure.security", "KeyVaults"),
        "DefenderForCloud":    _try("diagrams.azure.security", "SecurityCenter"),
        "AzurePolicy":         _try("diagrams.azure.general",  "Azure"),
        "Sentinel":            _try("diagrams.azure.security", "Sentinel"),
        "ManagedIdentity":     _try("diagrams.azure.identity", "ManagedIdentities",
                                    _try("diagrams.azure.identity", "ActiveDirectory")),

        # ── Management & Operations ───────────────────────────────────────────
        "RecoveryServicesVault":  _try("diagrams.azure.general", "Azure"),
        "LogAnalyticsWorkspace":  _try("diagrams.azure.monitor", "LogAnalytics",
                                       _try("diagrams.azure.general", "Azure")),
        "AzureMonitor":           _try("diagrams.azure.monitor", "Monitor",
                                       _try("diagrams.azure.general", "Azure")),
        "ApplicationInsights":    _try("diagrams.azure.monitor", "ApplicationInsights",
                                       _try("diagrams.azure.general", "Azure")),
        "UpdateManager":          _try("diagrams.azure.general", "Azure"),
        "AutomationAccount":      _try("diagrams.azure.general", "Azure"),
        "CostManagement":         _try("diagrams.azure.general", "Azure"),

        # ── On-Premises ───────────────────────────────────────────────────────
        "OnPremVM":            _try("diagrams.onprem.compute", "Server"),
        "OnPremServer":        _try("diagrams.onprem.compute", "Server"),
        "HyperVHost":          _try("diagrams.onprem.compute", "Server"),
        "OnPremNetwork":       _try("diagrams.onprem.network", "Firewall",
                                    _try("diagrams.onprem.compute", "Server")),
        "OnPremFirewall":      _try("diagrams.onprem.network", "Firewall",
                                    _try("diagrams.onprem.compute", "Server")),

        # ── Generic fallback ──────────────────────────────────────────────────
        "AzureService":        _try("diagrams.azure.general", "Azure"),

        # ── Legacy aliases ────────────────────────────────────────────────────
        "SQL":                 _try("diagrams.azure.database", "SQLDatabases"),
    }


# ── Renderer ──────────────────────────────────────────────────────────────────

def render_architecture(arch: dict) -> bytes:
    """Render architecture JSON to PNG bytes.

    Thread-safe: uses _render_lock because the diagrams library stores the
    active graph in a module-level stack.
    """
    from diagrams import Cluster, Diagram

    type_map = _get_type_map()
    fallback = type_map.get("VirtualMachine", list(type_map.values())[0])

    title       = arch.get("title", "Architecture")
    zones       = arch.get("zones")          # new schema
    connections = arch.get("connections", [])

    graph_attr = {
        "bgcolor":  "white",
        "pad":      "1.0",
        "fontname": "Helvetica",
        "compound": "true",
        "nodesep":  "0.5",
        "ranksep":  "0.9",
    }
    node_attr = {"fontname": "Helvetica", "fontsize": "10"}

    with _render_lock:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_base = os.path.join(tmp_dir, "diagram")

            with Diagram(
                title,
                filename=out_base,
                show=False,
                direction="LR",
                outformat="png",
                graph_attr=graph_attr,
                node_attr=node_attr,
            ):
                nodes: dict = {}

                if zones:
                    # ── Zones schema ──────────────────────────────────────────
                    for zone in zones:
                        zone_id    = zone.get("id", "")
                        zone_label = zone.get("label", zone_id)
                        zone_type  = zone.get("type", "")
                        zone_res   = zone.get("resources", [])
                        bgcolor    = _ZONE_COLORS.get(zone_type, "#FAFAFA")

                        first_node = None

                        with Cluster(
                            zone_label,
                            graph_attr={
                                "bgcolor":   bgcolor,
                                "style":     "filled",
                                "fontname":  "Helvetica",
                                "fontsize":  "12",
                                "pencolor":  "#999999",
                                "penwidth":  "1.5",
                            },
                        ):
                            for res in zone_res:
                                cls  = type_map.get(res.get("type", ""), fallback)
                                name = res.get("name", res.get("id", ""))
                                node = cls(name)
                                nodes[res["id"]] = node
                                if first_node is None:
                                    first_node = node

                        # Map zone_id to first resource for zone-level connections
                        if first_node is not None:
                            nodes[zone_id] = first_node

                else:
                    # ── Legacy flat schema ────────────────────────────────────
                    region    = arch.get("region", "")
                    resources = arch.get("resources", [])

                    if region:
                        with Cluster(f"Azure - {region}"):
                            for res in resources:
                                cls = type_map.get(res.get("type", ""), fallback)
                                nodes[res["id"]] = cls(res.get("name", res["id"]))
                    else:
                        for res in resources:
                            cls = type_map.get(res.get("type", ""), fallback)
                            nodes[res["id"]] = cls(res.get("name", res["id"]))

                # ── Connections ───────────────────────────────────────────────
                for conn in connections:
                    src = nodes.get(conn.get("from"))
                    dst = nodes.get(conn.get("to"))
                    if src and dst and src is not dst:
                        src >> dst  # noqa: B015

            png_path = out_base + ".png"
            logger.info("diagram rendered: %s (%d bytes)", png_path, os.path.getsize(png_path))
            with open(png_path, "rb") as fh:
                return fh.read()
