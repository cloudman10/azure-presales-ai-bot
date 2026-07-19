"""
scripts/ingest_arch_center.py

RAG Phase-1 Spike: ingest a small curated set of Azure Architecture Center
reference architectures into the "arch-center-spike" Azure AI Search index.

Uses keyword search only (no embedding model required for the spike).
Run once before setting RAG_ENABLED=true on the dev app.

Usage:
    cd ~/azure-presales-ai-bot
    .venv/Scripts/activate   (Windows)
    python scripts/ingest_arch_center.py
"""

import asyncio
import logging
import os
import re
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchableField,
    SearchFieldDataType,
    SearchIndex,
    SimpleField,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_API_KEY  = os.environ["AZURE_SEARCH_API_KEY"]
INDEX_NAME      = "arch-center-spike"
CHUNK_CHARS     = 1200  # max chars per chunk; sized to keep key paragraphs intact

# ── Curated corpus ─────────────────────────────────────────────────────────────
# 8 reference pages covering the known failure scenarios (AVD+SAP) plus common
# patterns (hub-spoke, hybrid, AKS, web).  Each URL is a stable learn.microsoft
# or architecture-center page with authoritative reference content.
CORPUS = [
    # AVD — the key page: explains reverse-connect transport over HTTPS/443, cloud-only model
    {
        "pattern_name": "Azure Virtual Desktop - Network Connectivity Model",
        "workload_type": "avd",
        "url": "https://learn.microsoft.com/en-us/azure/virtual-desktop/network-connectivity",
    },
    # AVD — CAF landing zone network design (hub connectivity, optional for cloud-only)
    {
        "pattern_name": "Azure Virtual Desktop - CAF Network Topology",
        "workload_type": "avd",
        "url": "https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/scenarios/azure-virtual-desktop/eslz-network-topology-and-connectivity",
    },
    # AVD — Azure Firewall integration guide (when to add egress control)
    {
        "pattern_name": "Azure Virtual Desktop - Azure Firewall Protection",
        "workload_type": "avd",
        "url": "https://learn.microsoft.com/en-us/azure/firewall/protect-azure-virtual-desktop",
    },
    # SAP — planning guide (VM sizing, storage, HA, networking — applicable to all SAP products)
    {
        "pattern_name": "SAP on Azure - VM Planning Guide",
        "workload_type": "sap",
        "url": "https://learn.microsoft.com/en-us/azure/virtual-machines/workloads/sap/planning-guide",
    },
    # SAP — S/4HANA reference architecture (tiers: ASCS, App Server, DB, shared storage)
    {
        "pattern_name": "SAP S/4HANA on Azure - Reference Architecture",
        "workload_type": "sap",
        "url": "https://learn.microsoft.com/en-us/azure/architecture/reference-architectures/sap/sap-s4hana",
    },
    # Networking — hub-spoke topology: when to use VPN Gateway, Firewall, ExpressRoute
    {
        "pattern_name": "Hub-Spoke Network Topology on Azure",
        "workload_type": "networking",
        "url": "https://learn.microsoft.com/en-us/azure/architecture/networking/hub-spoke-vwan-architecture",
    },
    # Networking — hybrid connectivity: VPN vs ExpressRoute decision guide
    {
        "pattern_name": "Hybrid Connectivity - VPN vs ExpressRoute Comparison",
        "workload_type": "hybrid",
        "url": "https://learn.microsoft.com/en-us/azure/architecture/reference-architectures/hybrid-networking/",
    },
    # AKS — baseline architecture
    {
        "pattern_name": "AKS Baseline Architecture",
        "workload_type": "aks",
        "url": "https://learn.microsoft.com/en-us/azure/architecture/reference-architectures/containers/aks/baseline-aks",
    },
]

# ── Index schema ───────────────────────────────────────────────────────────────
#
#   id           – unique key (uuid)
#   pattern_name – searchable; human-readable name of the reference pattern
#   workload_type– filterable tag (avd, sap, networking, hybrid, aks, web)
#   source_url   – retrievable; the canonical URL of the source page
#   content      – searchable; the chunk text (BM25 keyword search)
#
# No vector field yet — upgrade to hybrid after spike proves retrieval fires.


def create_index(index_client: SearchIndexClient) -> None:
    fields = [
        SimpleField(
            name="id",
            type=SearchFieldDataType.String,
            key=True,
            filterable=False,
        ),
        SimpleField(
            name="workload_type",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
            retrievable=True,
        ),
        SimpleField(
            name="source_url",
            type=SearchFieldDataType.String,
            filterable=False,
            retrievable=True,
        ),
        SearchableField(
            name="pattern_name",
            type=SearchFieldDataType.String,
            analyzer_name="en.microsoft",
            retrievable=True,
        ),
        SearchableField(
            name="content",
            type=SearchFieldDataType.String,
            analyzer_name="en.microsoft",
            retrievable=True,
        ),
    ]
    idx = SearchIndex(name=INDEX_NAME, fields=fields)
    try:
        index_client.delete_index(INDEX_NAME)
        log.info("Dropped existing index '%s'", INDEX_NAME)
    except Exception:
        pass
    index_client.create_index(idx)
    log.info("Created index '%s' (keyword BM25 only)", INDEX_NAME)
    log.info("")
    log.info("Schema:")
    log.info("  id           String  key")
    log.info("  pattern_name String  searchable (en.microsoft)")
    log.info("  workload_type String  filterable, facetable")
    log.info("  source_url   String  retrievable")
    log.info("  content      String  searchable (en.microsoft), BM25")
    log.info("")


# ── HTML → plain text ──────────────────────────────────────────────────────────

_BLOCK_TAGS = re.compile(
    r'<(script|style|nav|header|footer|aside|noscript|form|svg)[^>]*>.*?</\1>',
    re.DOTALL | re.IGNORECASE,
)
_INLINE_TAGS = re.compile(r'<[^>]+>')
_ENTITIES = [
    ('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'), ('&nbsp;', ' '),
    ('&#39;', "'"), ('&quot;', '"'), ('&mdash;', ' - '), ('&ndash;', '-'),
    ('&hellip;', '...'),
]
_WS = re.compile(r'[ \t]+')
_MULTI_NL = re.compile(r'\n{3,}')


def _html_to_text(html: str) -> str:
    """Strip HTML to plain readable text, preserving paragraph breaks."""
    # Remove block-level noise first (scripts, nav, headers, etc.)
    html = _BLOCK_TAGS.sub(' ', html)

    # Try to isolate the main article body
    for tag in ('main', 'article'):
        m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', html, re.DOTALL | re.IGNORECASE)
        if m:
            html = m.group(1)
            break

    # Convert block elements to newlines so paragraphs survive tag stripping
    html = re.sub(r'<(p|h[1-6]|li|dt|dd|div|br|tr)[^>]*>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</(p|h[1-6]|li|dt|dd|div|tr)>', '\n', html, flags=re.IGNORECASE)

    # Strip remaining inline tags
    text = _INLINE_TAGS.sub(' ', html)

    # Decode common HTML entities
    for entity, char in _ENTITIES:
        text = text.replace(entity, char)

    # Clean up whitespace
    lines = []
    for line in text.splitlines():
        line = _WS.sub(' ', line).strip()
        if line:
            lines.append(line)
    text = '\n'.join(lines)
    text = _MULTI_NL.sub('\n\n', text).strip()
    return text


# ── Chunking ───────────────────────────────────────────────────────────────────

def _chunk_text(text: str, source: dict) -> list[dict]:
    """
    Split text into chunks of ~CHUNK_CHARS by sentence boundary.
    Each chunk carries the source metadata.
    """
    sentences = re.split(r'(?<=[.!?])\s+|\n\n+', text)
    chunks: list[dict] = []
    buf = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(buf) + len(sentence) > CHUNK_CHARS and buf:
            chunks.append(_make_doc(buf.strip(), source))
            buf = sentence + " "
        else:
            buf += sentence + " "

    if buf.strip():
        chunks.append(_make_doc(buf.strip(), source))

    return chunks


def _make_doc(content: str, source: dict) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "pattern_name": source["pattern_name"],
        "workload_type": source["workload_type"],
        "source_url": source["url"],
        "content": content,
    }


# ── Fetch ──────────────────────────────────────────────────────────────────────

async def fetch_and_chunk(client: httpx.AsyncClient, source: dict) -> list[dict]:
    url = source["url"]
    log.info("Fetching  %s", url)
    try:
        resp = await client.get(url, follow_redirects=True, timeout=30)
        if resp.status_code != 200:
            log.warning("  HTTP %d — skipped", resp.status_code)
            return []
        text = _html_to_text(resp.text)
        if len(text) < 300:
            log.warning("  Content too short (%d chars) — skipped", len(text))
            return []
        chunks = _chunk_text(text, source)
        log.info("  -> %d chunks, %d chars total", len(chunks), len(text))
        return chunks
    except Exception as exc:
        log.warning("  Failed: %s", exc)
        return []


# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    cred = AzureKeyCredential(SEARCH_API_KEY)
    index_client  = SearchIndexClient(endpoint=SEARCH_ENDPOINT, credential=cred)
    search_client = SearchClient(endpoint=SEARCH_ENDPOINT, index_name=INDEX_NAME, credential=cred)

    create_index(index_client)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    all_chunks: list[dict] = []
    async with httpx.AsyncClient(headers=headers) as client:
        for source in CORPUS:
            chunks = await fetch_and_chunk(client, source)
            all_chunks.extend(chunks)

    if not all_chunks:
        log.error("No chunks ingested — check URLs and .env credentials")
        return

    log.info("")
    log.info("Uploading %d chunks to '%s'...", len(all_chunks), INDEX_NAME)
    batch_size = 100
    total_ok = 0
    for i in range(0, len(all_chunks), batch_size):
        batch = all_chunks[i : i + batch_size]
        results = search_client.upload_documents(documents=batch)
        ok = sum(1 for r in results if r.succeeded)
        total_ok += ok
        log.info("  Batch %d: %d/%d succeeded", i // batch_size + 1, ok, len(batch))

    log.info("")
    log.info("=" * 60)
    log.info("INGEST COMPLETE")
    log.info("  Index:  %s", INDEX_NAME)
    log.info("  Search: %s", SEARCH_ENDPOINT)
    log.info("  Docs fetched from %d sources", len(CORPUS))
    log.info("  Chunks uploaded: %d / %d", total_ok, len(all_chunks))
    log.info("")
    log.info("Schema recap:")
    log.info("  id, pattern_name (searchable), workload_type (filterable),")
    log.info("  source_url (retrievable), content (searchable BM25 en.microsoft)")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
