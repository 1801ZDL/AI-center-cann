#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
mkdir -p runtime
chmod 700 runtime
python_bin="${1:-python3}"
"$python_bin" -m unittest discover -s tests -p 'test_*.py' -v
node --check web/app.js
