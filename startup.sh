#!/bin/bash
# startup.sh — runs as root on each app-process start.
#
# Self-healing: if the Oryx antenv is gone from /tmp (happens after az webapp restart
# without a preceding deployment), extract output.tar.zst ourselves.
# Extraction target is /home/site/oryx-build (persistent) so restarts reuse it.
# Re-extracts automatically when output.tar.zst is newer (i.e. after a new deployment).

ZSTD_SRC="/home/site/wwwroot/output.tar.zst"
EXTRACT_DIR="/home/site/oryx-build"

# ── System tools ──────────────────────────────────────────────────────────────

# Graphviz required by the diagrams library.
if ! command -v dot >/dev/null 2>&1; then
    echo "[startup] installing graphviz..."
    apt-get update -qq && apt-get install -y -qq graphviz
    echo "[startup] graphviz: $(dot -V 2>&1)"
fi

# zstd required to decompress output.tar.zst when antenv not in /tmp.
if ! command -v zstd >/dev/null 2>&1; then
    echo "[startup] installing zstd..."
    apt-get update -qq && apt-get install -y -qq zstd
fi

# ── Find or extract antenv ────────────────────────────────────────────────────

# Happy path: container init already extracted output.tar.zst to /tmp/<hash>/antenv/.
GUNICORN=$(find /tmp -maxdepth 4 -name gunicorn -path '*/antenv/bin/gunicorn' 2>/dev/null | head -1)

if [ -z "$GUNICORN" ]; then
    echo "[startup] antenv not in /tmp — checking ${EXTRACT_DIR}..."

    # Re-extract if EXTRACT_DIR is missing or output.tar.zst is newer.
    NEEDS_EXTRACT=0
    if [ ! -d "${EXTRACT_DIR}/antenv" ]; then
        NEEDS_EXTRACT=1
        echo "[startup] ${EXTRACT_DIR}/antenv not found — extracting."
    elif [ "${ZSTD_SRC}" -nt "${EXTRACT_DIR}/antenv" ]; then
        NEEDS_EXTRACT=1
        echo "[startup] ${ZSTD_SRC} is newer than extract dir — re-extracting."
    fi

    if [ "$NEEDS_EXTRACT" = "1" ]; then
        mkdir -p "${EXTRACT_DIR}"
        echo "[startup] extracting ${ZSTD_SRC} → ${EXTRACT_DIR}..."
        zstd -d "${ZSTD_SRC}" -c | tar -x -C "${EXTRACT_DIR}"
        EXTRACT_STATUS=$?
        echo "[startup] extraction exit=${EXTRACT_STATUS}"
    fi

    GUNICORN=$(find "${EXTRACT_DIR}" -maxdepth 4 -name gunicorn -path '*/antenv/bin/gunicorn' 2>/dev/null | head -1)
fi

# ── Launch gunicorn ───────────────────────────────────────────────────────────

if [ -n "$GUNICORN" ]; then
    BUILD_DIR=$(cd "$(dirname "$GUNICORN")/../.." && pwd)
    export PYTHONPATH="${BUILD_DIR}:${BUILD_DIR}/antenv/lib/python3.11/site-packages:${PYTHONPATH}"
    echo "[startup] BUILD_DIR=${BUILD_DIR} gunicorn=${GUNICORN}"
    exec "$GUNICORN" -w 1 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --chdir "${BUILD_DIR}" app.main:app
else
    echo "[startup] FATAL: cannot find gunicorn. Check ${ZSTD_SRC} and ${EXTRACT_DIR}."
    exit 1
fi
