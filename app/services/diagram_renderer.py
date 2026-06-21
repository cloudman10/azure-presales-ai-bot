"""
app/services/diagram_renderer.py

Phase 1 — render pipeline only (no AI).
Converts an architecture JSON dict → PNG bytes using the `diagrams` library.
Requires system graphviz (`dot` binary); installed in startup.sh.

Phase 2 will wire in Foundry to generate the JSON from a user conversation.
"""

import logging
import os
import tempfile
import threading

logger = logging.getLogger(__name__)

# diagrams uses a global graph context; serialise all renders with a lock.
_render_lock = threading.Lock()

# ── Hardcoded sample — Phase 1 smoke test ────────────────────────────────────

SAMPLE_ARCHITECTURE: dict = {
    "title": "Three-Tier Web — Australia East",
    "region": "Australia East",
    "resources": [
        {"id": "lb1",  "type": "LoadBalancer",   "name": "Load Balancer"},
        {"id": "web1", "type": "VirtualMachine",  "name": "Web VM 1"},
        {"id": "web2", "type": "VirtualMachine",  "name": "Web VM 2"},
        {"id": "sql1", "type": "SQLDatabase",     "name": "Azure SQL"},
    ],
    "connections": [
        {"from": "lb1",  "to": "web1"},
        {"from": "lb1",  "to": "web2"},
        {"from": "web1", "to": "sql1"},
        {"from": "web2", "to": "sql1"},
    ],
}

# ── Type → diagrams Azure node class mapping ─────────────────────────────────

def _get_type_map() -> dict:
    """Import Azure node classes lazily so startup doesn't fail before graphviz installs."""
    from diagrams.azure.compute import VM
    from diagrams.azure.database import SQLDatabases
    from diagrams.azure.network import LoadBalancers
    from diagrams.azure.storage import StorageAccounts
    from diagrams.azure.web import AppServices

    return {
        "VirtualMachine":  VM,
        "VM":              VM,
        "LoadBalancer":    LoadBalancers,
        "SQLDatabase":     SQLDatabases,
        "SQL":             SQLDatabases,
        "StorageAccount":  StorageAccounts,
        "AppService":      AppServices,
    }


# ── Render function ───────────────────────────────────────────────────────────

def render_architecture(arch: dict) -> bytes:
    """Render architecture JSON to PNG bytes.

    Thread-safe: uses _render_lock because the diagrams library stores the
    active graph in a module-level stack (_default_graph).

    Returns PNG bytes on success; raises on any error (graphviz missing, bad
    schema, etc.) — callers should wrap in try/except.
    """
    from diagrams import Cluster, Diagram

    type_map = _get_type_map()
    fallback = list(type_map.values())[0]   # VM as generic fallback

    title       = arch.get("title", "Architecture")
    region      = arch.get("region", "")
    resources   = arch.get("resources", [])
    connections = arch.get("connections", [])

    with _render_lock:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_base = os.path.join(tmp_dir, "diagram")

            graph_attr = {"bgcolor": "white", "pad": "0.75", "fontname": "Helvetica"}
            node_attr  = {"fontname": "Helvetica", "fontsize": "11"}

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

                if region:
                    with Cluster(f"Azure - {region}"):
                        for res in resources:
                            cls = type_map.get(res["type"], fallback)
                            nodes[res["id"]] = cls(res["name"])
                else:
                    for res in resources:
                        cls = type_map.get(res["type"], fallback)
                        nodes[res["id"]] = cls(res["name"])

                for conn in connections:
                    src = nodes.get(conn["from"])
                    dst = nodes.get(conn["to"])
                    if src and dst:
                        src >> dst  # noqa: B015  (diagrams overloads >>)

            png_path = out_base + ".png"
            logger.info("diagram rendered: %s (%d bytes)", png_path, os.path.getsize(png_path))
            with open(png_path, "rb") as fh:
                return fh.read()
