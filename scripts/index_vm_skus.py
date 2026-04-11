"""
scripts/index_vm_skus.py

Fetches VM SKUs from the Azure ARM API for australiaeast, enriches them with
series metadata (use_cases, description, retired flag), and uploads the result
to the Azure AI Search vm-skus index.

Auth: ClientSecretCredential from .env (falls back to DefaultAzureCredential
      which covers Managed Identity in production).

Run:
    cd ~/azure-presales-ai-bot
    source .venv/bin/activate
    python3 scripts/index_vm_skus.py
"""

import os
import sys
import re
import uuid
import logging
from pathlib import Path
from dotenv import load_dotenv

# ── Load .env from repo root ───────────────────────────────────────────────────
load_dotenv(Path(__file__).parent.parent / ".env")

from azure.identity import ClientSecretCredential, DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

SUBSCRIPTION_ID    = os.environ["AZURE_SUBSCRIPTION_ID"]
SEARCH_ENDPOINT    = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_API_KEY     = os.environ["AZURE_SEARCH_API_KEY"]
SEARCH_INDEX       = "vm-skus"
TARGET_REGION      = "australiaeast"
UPLOAD_BATCH_SIZE  = 100

TENANT_ID     = os.getenv("AZURE_TENANT_ID")
CLIENT_ID     = os.getenv("AZURE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")

# ── Series metadata ────────────────────────────────────────────────────────────

_SERIES_META: dict[str, dict] = {
    "b": {
        "description": "Burstable VMs — cost-effective baseline CPU with burst credits for sporadic workloads.",
        "use_cases":   "Burstable, cost-effective for dev/test, low-traffic web apps, small databases",
    },
    "d": {
        "description": "General-purpose VMs with balanced CPU-to-memory ratio and fast local SSD.",
        "use_cases":   "General purpose, web servers, application servers, small-medium databases",
    },
    "e": {
        "description": "Memory-optimised VMs with high RAM-to-vCPU ratio for in-memory workloads.",
        "use_cases":   "Memory optimised, SQL Server, SAP, in-memory caching, analytics",
    },
    "f": {
        "description": "Compute-optimised VMs with high CPU-to-memory ratio for demanding compute tasks.",
        "use_cases":   "Compute optimised, batch processing, gaming, web servers, high CPU workloads",
    },
    "m": {
        "description": "Largest memory VMs (up to 12 TB RAM) for enterprise-scale in-memory databases.",
        "use_cases":   "Large memory, SAP HANA, large SQL Server, in-memory databases",
    },
    "l": {
        "description": "Storage-optimised VMs with high disk throughput and IOPS for data-intensive workloads.",
        "use_cases":   "Storage optimised, NoSQL databases, data warehousing, large transaction logs",
    },
    "n": {
        "description": "GPU-enabled VMs for parallel compute, ML training, and graphics workloads.",
        "use_cases":   "GPU, machine learning, AI training, graphics rendering, HPC",
    },
    "h": {
        "description": "High-performance compute VMs with InfiniBand networking for tightly-coupled HPC jobs.",
        "use_cases":   "HPC, computational fluid dynamics, finite element analysis, seismic processing",
    },
}

_DEFAULT_META = {
    "description": "Azure virtual machine.",
    "use_cases":   "General compute workloads",
}

# Patterns that mark a SKU as retired (matched against the normalised sku_name)
_RETIRED_PATTERNS = [
    re.compile(r"^Standard_A\d", re.IGNORECASE),        # A-series v1
    re.compile(r"^Basic_A\d",    re.IGNORECASE),        # Basic A-series
    re.compile(r"^Standard_D\d+[^s_v]", re.IGNORECASE), # D-series v1 (no 's' suffix, no _v)
]


def _is_retired(sku_name: str) -> bool:
    return any(p.match(sku_name) for p in _RETIRED_PATTERNS)


def _series_letter(sku_name: str) -> str:
    """Extract the single series letter from a normalised SKU name."""
    m = re.match(r"^(?:Standard_|Basic_)?([A-Za-z])", sku_name, re.IGNORECASE)
    return m.group(1).lower() if m else ""


def _get_credential():
    if TENANT_ID and CLIENT_ID and CLIENT_SECRET:
        log.info("Auth: ClientSecretCredential (service principal)")
        return ClientSecretCredential(TENANT_ID, CLIENT_ID, CLIENT_SECRET)
    log.info("Auth: DefaultAzureCredential (Managed Identity / CLI)")
    return DefaultAzureCredential()


# ── Fetch SKUs from ARM ────────────────────────────────────────────────────────

def fetch_skus(credential) -> list[dict]:
    log.info("Fetching VM SKUs for region '%s' …", TARGET_REGION)
    compute = ComputeManagementClient(credential, SUBSCRIPTION_ID)

    skus = compute.resource_skus.list(filter=f"location eq '{TARGET_REGION}'")

    documents: list[dict] = []
    seen: set[str] = set()

    for sku in skus:
        if sku.resource_type != "virtualMachines":
            continue

        sku_name: str = sku.name
        if sku_name in seen:
            continue
        seen.add(sku_name)

        # Pull capabilities
        caps = {c.name: c.value for c in (sku.capabilities or [])}

        try:
            vcpus           = int(caps.get("vCPUs", 0))
            ram_gb          = int(float(caps.get("MemoryGB", 0)))
            temp_storage_gb = int(float(caps.get("MaxResourceVolumeMB", 0)) / 1024)
        except (ValueError, TypeError):
            vcpus = ram_gb = temp_storage_gb = 0

        # Collect all regions this SKU is available in
        regions = sorted({
            loc.lower().replace(" ", "")
            for loc in (sku.locations or [TARGET_REGION])
        })

        series = _series_letter(sku_name)
        meta   = _SERIES_META.get(series, _DEFAULT_META)

        documents.append({
            "id":              str(uuid.uuid5(uuid.NAMESPACE_DNS, sku_name)),
            "sku_name":        sku_name,
            "series":          series.upper() if series else "?",
            "vcpus":           vcpus,
            "ram_gb":          ram_gb,
            "temp_storage_gb": temp_storage_gb,
            "regions":         regions,
            "description":     meta["description"],
            "use_cases":       meta["use_cases"],
            "retired":         _is_retired(sku_name),
        })

    log.info("Found %d unique VM SKUs in %s", len(documents), TARGET_REGION)
    return documents


# ── Upload to Azure AI Search ──────────────────────────────────────────────────

def upload_to_search(documents: list[dict]) -> None:
    client = SearchClient(
        endpoint=SEARCH_ENDPOINT,
        index_name=SEARCH_INDEX,
        credential=AzureKeyCredential(SEARCH_API_KEY),
    )

    total    = len(documents)
    uploaded = 0
    retired  = sum(1 for d in documents if d["retired"])

    for i in range(0, total, UPLOAD_BATCH_SIZE):
        batch = documents[i : i + UPLOAD_BATCH_SIZE]
        result = client.upload_documents(documents=batch)
        succeeded = sum(1 for r in result if r.succeeded)
        failed    = len(batch) - succeeded
        uploaded += succeeded
        log.info(
            "Batch %d/%d — uploaded %d, failed %d",
            i // UPLOAD_BATCH_SIZE + 1,
            -(-total // UPLOAD_BATCH_SIZE),
            succeeded,
            failed,
        )
        if failed:
            for r in result:
                if not r.succeeded:
                    log.error("  Failed key=%s  error=%s", r.key, r.error_message)

    log.info("─" * 60)
    log.info("Upload complete: %d/%d documents indexed", uploaded, total)
    log.info("  Retired SKUs flagged: %d", retired)
    log.info("  Active SKUs:          %d", total - retired)
    log.info("  Index: %s › %s", SEARCH_ENDPOINT, SEARCH_INDEX)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("VM SKU Indexer — Azure AI Search")
    log.info("  Subscription : %s", SUBSCRIPTION_ID)
    log.info("  Region       : %s", TARGET_REGION)
    log.info("  Search index : %s", SEARCH_INDEX)
    log.info("=" * 60)

    credential = _get_credential()
    documents  = fetch_skus(credential)

    if not documents:
        log.error("No SKUs found — nothing to upload.")
        sys.exit(1)

    # Brief summary before upload
    series_counts: dict[str, int] = {}
    for d in documents:
        series_counts[d["series"]] = series_counts.get(d["series"], 0) + 1
    log.info("Series breakdown: %s", dict(sorted(series_counts.items())))

    upload_to_search(documents)


if __name__ == "__main__":
    main()
