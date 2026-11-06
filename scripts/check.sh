#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
mkdir -p runtime
chmod 700 runtime
python_bin="${1:-python3}"
"$python_bin" scripts/check-tests.py
node --check web/app.js
