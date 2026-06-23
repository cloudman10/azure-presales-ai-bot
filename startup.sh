#!/bin/bash
# startup.sh — runs on each app-process start (cold or process restart).
#
# Key insight: Oryx extracts output.tar.zst to /tmp/<hash>/ on every COLD start
# using a deterministic hash, so gunicorn shebangs are always valid there.
# On a PROCESS-ONLY restart (Kudu DELETE /api/processes/0), the container stays
# alive — /tmp/<hash>/ still exists with the OLD code.  We detect this by
# comparing output.tar.zst mtime vs a marker in /tmp, then refresh ONLY the
# app/ + static/ directories (excluding antenv) so the gunicorn shebang paths
# remain valid.  Full antenv re-extraction is intentionally avoided because it
# embeds /tmp/<old-hash>/ shebangs that break on container recycle.

ZSTD_SRC="/home/site/wwwroot/output.tar.zst"

# ── System tools ──────────────────────────────────────────────────────────────

if ! command -v dot >/dev/null 2>&1; then
    echo "[startup] installing graphviz..."
    apt-get update -y 2>&1 | tail -3
    apt-get install -y graphviz 2>&1 | tail -5
    if command -v dot >/dev/null 2>&1; then
        echo "[startup] graphviz OK: $(dot -V 2>&1)"
    else
        echo "[startup] WARNING: graphviz install failed"
        [ -x /usr/bin/dot ] && export PATH="/usr/bin:$PATH"
    fi
else
    echo "[startup] graphviz already installed: $(dot -V 2>&1)"
fi

if ! command -v zstd >/dev/null 2>&1; then
    echo "[startup] installing zstd..."
    apt-get update -qq && apt-get install -y -qq zstd
fi

# ── Find gunicorn in /tmp (Oryx puts it there on cold start) ──────────────────

GUNICORN=$(find /tmp -maxdepth 4 -name gunicorn -path '*/antenv/bin/gunicorn' 2>/dev/null | head -1)

if [ -z "$GUNICORN" ]; then
    echo "[startup] FATAL: gunicorn not found in /tmp — Oryx extraction may have failed."
    exit 1
fi

APP_DIR=$(cd "$(dirname "$GUNICORN")/../.." && pwd)
MARKER="${APP_DIR}/.app_code_synced"

# ── Refresh app code if a new deployment arrived ──────────────────────────────
# Compare output.tar.zst mtime vs the marker file written after last sync.
# On a cold start the marker doesn't exist (fresh /tmp) so we always refresh
# once, then touch the marker so subsequent process restarts skip the extract.

if [ "${ZSTD_SRC}" -nt "$MARKER" ]; then
    echo "[startup] new deployment detected — refreshing app/ and static/ in ${APP_DIR}..."
    # Extract everything EXCEPT antenv (the venv).  Antenv has hardcoded /tmp
    # shebangs from build time — rewriting them breaks gunicorn on recycle.
    # Excluding it keeps the extract small (seconds, not minutes).
    zstd -d "${ZSTD_SRC}" -c | tar -x -C "${APP_DIR}" \
        --exclude='./antenv' --exclude='antenv' 2>/dev/null
    SYNC_EXIT=$?
    touch "$MARKER"
    echo "[startup] refresh done (exit=${SYNC_EXIT})"
else
    echo "[startup] app code up-to-date (marker newer than ${ZSTD_SRC})"
fi

# ── Launch gunicorn ───────────────────────────────────────────────────────────

export PYTHONPATH="${APP_DIR}:${APP_DIR}/antenv/lib/python3.11/site-packages:${PYTHONPATH}"
echo "[startup] APP_DIR=${APP_DIR} gunicorn=${GUNICORN}"
exec "$GUNICORN" -w 1 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --chdir "${APP_DIR}" app.main:app
