"""
scripts/ingest_arch_center.py

RAG spike: ingest curated Azure Architecture Center pages into the
"arch-center-spike" Azure AI Search index with:
  - Smaller ~400-char chunks (was 1200) for tighter semantic hits
  - text-embedding-3-small embeddings (1536 dims) on every chunk
  - Hybrid index: keyword BM25 (en.microsoft) + HNSW vector field

Run once before enabling RAG_ENABLED=true on the dev app.

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
    HnswAlgorithmConfiguration,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SEARCH_ENDPOINT      = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_API_KEY       = os.environ["AZURE_SEARCH_API_KEY"]
OPENAI_ENDPOINT      = os.environ["AZURE_OPENAI_ENDPOINT"]
OPENAI_API_KEY       = os.environ["AZURE_OPENAI_KEY"]
EMBEDDING_DEPLOYMENT = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
EMBEDDING_DIMS       = 1536
INDEX_NAME           = "arch-center-spike"
CHUNK_CHARS          = 400   # smaller chunks for tighter semantic hits

# ── Curated corpus (unchanged from phase-1 spike) ──────────────────────────────
CORPUS = [
    {
        "pattern_name": "Azure Virtual Desktop - Network Connectivity Model",
        "workload_type": "avd",
        "url": "https://learn.microsoft.com/en-us/azure/virtual-desktop/network-connectivity",
    },
    {
        "pattern_name": "Azure Virtual Desktop - CAF Network Topology",
        "workload_type": "avd",
        "url": "https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/scenarios/azure-virtual-desktop/eslz-network-topology-and-connectivity",
    },
    {
        "pattern_name": "Azure Virtual Desktop - Azure Firewall Protection",
        "workload_type": "avd",
        "url": "https://learn.microsoft.com/en-us/azure/firewall/protect-azure-virtual-desktop",
    },
    {
        "pattern_name": "SAP on Azure - VM Planning Guide",
        "workload_type": "sap",
        "url": "https://learn.microsoft.com/en-us/azure/virtual-machines/workloads/sap/planning-guide",
    },
    {
        "pattern_name": "SAP S/4HANA on Azure - Reference Architecture",
        "workload_type": "sap",
        "url": "https://learn.microsoft.com/en-us/azure/architecture/reference-architectures/sap/sap-s4hana",
    },
    {
        "pattern_name": "Hub-Spoke Network Topology on Azure",
        "workload_type": "networking",
        "url": "https://learn.microsoft.com/en-us/azure/architecture/networking/hub-spoke-vwan-architecture",
    },
    {
        "pattern_name": "Hybrid Connectivity - VPN vs ExpressRoute Comparison",
        "workload_type": "hybrid",
        "url": "https://learn.microsoft.com/en-us/azure/architecture/reference-architectures/hybrid-networking/",
    },
    {
        "pattern_name": "AKS Baseline Architecture",
        "workload_type": "aks",
        "url": "https://learn.microsoft.com/en-us/azure/architecture/reference-architectures/containers/aks/baseline-aks",
    },
]

# ── Index schema ───────────────────────────────────────────────────────────────

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
        SearchField(
            name="embedding",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBEDDING_DIMS,
            vector_search_profile_name="hnsw-profile",
        ),
    ]
    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="hnsw")],
        profiles=[VectorSearchProfile(name="hnsw-profile", algorithm_configuration_name="hnsw")],
    )
    idx = SearchIndex(name=INDEX_NAME, fields=fields, vector_search=vector_search)
    try:
        index_client.delete_index(INDEX_NAME)
        log.info("Dropped existing index '%s'", INDEX_NAME)
    except Exception:
        pass
    index_client.create_index(idx)
    log.info("Created index '%s' (BM25 keyword + HNSW vector)", INDEX_NAME)
    log.info("")
    log.info("Schema:")
    log.info("  id            String  key")
    log.info("  pattern_name  String  searchable (en.microsoft)")
    log.info("  workload_type String  filterable, facetable")
    log.info("  source_url    String  retrievable")
    log.info("  content       String  searchable (en.microsoft), BM25")
    log.info("  embedding     Collection(Single) 1536-dim HNSW vector")
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
    html = _BLOCK_TAGS.sub(' ', html)
    for tag in ('main', 'article'):
        m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', html, re.DOTALL | re.IGNORECASE)
        if m:
            html = m.group(1)
            break
    html = re.sub(r'<(p|h[1-6]|li|dt|dd|div|br|tr)[^>]*>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</(p|h[1-6]|li|dt|dd|div|tr)>', '\n', html, flags=re.IGNORECASE)
    text = _INLINE_TAGS.sub(' ', html)
    for entity, char in _ENTITIES:
        text = text.replace(entity, char)
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
        "embedding": None,  # filled in by embed_all_chunks()
    }


# ── Embedding ──────────────────────────────────────────────────────────────────

async def _embed_batch(texts: list[str]) -> list[list[float]]:
    url = f"{OPENAI_ENDPOINT}/openai/deployments/{EMBEDDING_DEPLOYMENT}/embeddings?api-version=2024-02-01"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            headers={"api-key": OPENAI_API_KEY, "Content-Type": "application/json"},
            json={"input": texts},
        )
    resp.raise_for_status()
    data = resp.json()["data"]
    return [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]


async def embed_all_chunks(chunks: list[dict]) -> list[dict]:
    batch_size = 16
    total_batches = (len(chunks) + batch_size - 1) // batch_size
    log.info("Embedding %d chunks with '%s' (%d batches)...", len(chunks), EMBEDDING_DEPLOYMENT, total_batches)
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        texts = [c["content"] for c in batch]
        embeddings = await _embed_batch(texts)
        for chunk, vec in zip(batch, embeddings):
            chunk["embedding"] = vec
        log.info("  Embedded batch %d/%d", i // batch_size + 1, total_batches)
    return chunks


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
    log.info("Total chunks before embedding: %d", len(all_chunks))

    # Embed all chunks
    await embed_all_chunks(all_chunks)

    # Upload to search index
    log.info("")
    log.info("Uploading %d chunks to '%s'...", len(all_chunks), INDEX_NAME)
    batch_size = 100
    total_ok = 0
    for i in range(0, len(all_chunks), batch_size):
        batch = all_chunks[i : i + batch_size]
        results = search_client.upload_documents(documents=batch)
        ok = sum(1 for r in results if r.succeeded)
        total_ok += ok
        log.info("  Upload batch %d: %d/%d succeeded", i // batch_size + 1, ok, len(batch))

    sources_ingested = len({c["source_url"] for c in all_chunks})
    log.info("")
    log.info("=" * 60)
    log.info("INGEST COMPLETE")
    log.info("  Index:            %s", INDEX_NAME)
    log.info("  Search endpoint:  %s", SEARCH_ENDPOINT)
    log.info("  Sources fetched:  %d / %d", sources_ingested, len(CORPUS))
    log.info("  Chunks uploaded:  %d / %d", total_ok, len(all_chunks))
    log.info("  Chunk size:       ~%d chars", CHUNK_CHARS)
    log.info("  Embedding model:  %s (%d dims)", EMBEDDING_DEPLOYMENT, EMBEDDING_DIMS)
    log.info("  Search type:      BM25 keyword + HNSW vector (hybrid)")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
