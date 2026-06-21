#!/bin/bash
# startup.sh — runs as root (PID 1) on each container-process start.
#
# Graphviz is required by the diagrams library. Install once per container lifetime.
# The 'command -v dot' guard skips the ~30s install on restarts within the same container.
if ! command -v dot >/dev/null 2>&1; then
    echo "[startup] graphviz not found — installing..."
    apt-get update -qq && apt-get install -y -qq graphviz
    echo "[startup] graphviz installed: $(dot -V 2>&1)"
fi

# Locate the Oryx antenv gunicorn in /tmp (e.g. /tmp/<hash>/antenv/bin/gunicorn).
# On a GitHub-Actions-triggered deploy, Oryx puts it there and PATH is updated before
# this script runs.  On a bare 'az webapp restart' after a long-lived container, /tmp
# may be empty — extract output.tar.zst from wwwroot as a fallback.
GUNICORN=$(find /tmp -maxdepth 4 -name gunicorn -path '*/antenv/bin/gunicorn' 2>/dev/null | head -1)

if [ -z "$GUNICORN" ]; then
    ZSTD_SRC="/home/site/wwwroot/output.tar.zst"
    if [ -f "$ZSTD_SRC" ]; then
        echo "[startup] antenv not in /tmp — extracting ${ZSTD_SRC}..."
        EXTRACT_DIR=$(mktemp -d /tmp/oryx-XXXXXX)
        # GNU tar on Debian Bullseye supports zstd via --use-compress-program
        tar --use-compress-program=zstd -xf "$ZSTD_SRC" -C "$EXTRACT_DIR" 2>/dev/null \
            || tar -xf "$ZSTD_SRC" -C "$EXTRACT_DIR" 2>/dev/null \
            || { echo "[startup] extraction failed; trying zstd pipe"; zstd -d "$ZSTD_SRC" -c | tar -x -C "$EXTRACT_DIR"; }
        GUNICORN=$(find "$EXTRACT_DIR" -name gunicorn -path '*/antenv/bin/gunicorn' 2>/dev/null | head -1)
        echo "[startup] extraction done; gunicorn=${GUNICORN}"
    fi
fi

if [ -n "$GUNICORN" ]; then
    BUILD_DIR=$(cd "$(dirname "$GUNICORN")/../.." && pwd)
    export PYTHONPATH="${BUILD_DIR}:${BUILD_DIR}/antenv/lib/python3.11/site-packages:${PYTHONPATH}"
    echo "[startup] BUILD_DIR=${BUILD_DIR}"
    exec "$GUNICORN" -w 1 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --chdir "${BUILD_DIR}" app.main:app
else
    # Last resort: system gunicorn.  PYTHONPATH from Azure already includes antenv
    # site-packages when set up by a prior Oryx run; app.main:app discovery depends on it.
    echo "[startup] WARNING: antenv gunicorn not found — falling back to system gunicorn"
    exec gunicorn -w 1 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 app.main:app
fi
