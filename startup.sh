#!/bin/bash
# Oryx builds into a temp dir (e.g. /tmp/8deaf.../), placing antenv/ and app/ side-by-side.
# The gunicorn binary is at <BUILD_DIR>/antenv/bin/gunicorn, so we derive BUILD_DIR from it.

# Graphviz is required by the diagrams library (Solution Architecture Designer).
# Not pre-installed in the App Service container; install once per container lifetime.
if ! command -v dot >/dev/null 2>&1; then
    echo "[startup] graphviz not found — installing..."
    apt-get update -qq && apt-get install -y -qq graphviz 2>&1 | tail -2
    echo "[startup] graphviz installed: $(dot -V 2>&1)"
fi

BUILD_DIR=$(cd "$(dirname "$(which gunicorn)")/../.." && pwd)
exec gunicorn -w 1 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --chdir "${BUILD_DIR}" app.main:app
