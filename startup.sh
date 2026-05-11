#!/bin/bash
# Oryx builds into a temp dir (e.g. /tmp/8deaf.../), placing antenv/ and app/ side-by-side.
# The gunicorn binary is at <BUILD_DIR>/antenv/bin/gunicorn, so we derive BUILD_DIR from it.
BUILD_DIR=$(cd "$(dirname "$(which gunicorn)")/../.." && pwd)
exec gunicorn -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --chdir "${BUILD_DIR}" app.main:app
