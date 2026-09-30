#!/bin/bash
# Assemble deploy/build/: app code + Linux arm64 dependencies (Lambda runs arm64 Python 3.12).
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf deploy/build && mkdir -p deploy/build/vendor
cp server.py remote.py oauth_provider.py tokenstore.py config.py audit.py run.sh accounts.json deploy/build/
cp -R providers deploy/build/ && find deploy/build -name __pycache__ -prune -exec rm -rf {} +
uv pip install --python-platform aarch64-manylinux2014 --python-version 3.12 \
  --target deploy/build/vendor -r requirements-lambda.txt
