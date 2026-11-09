#!/bin/bash
# Runs the whole test suite. Normally invoked through `make test` (inside the test container).
set -euo pipefail
cd "$(dirname "$0")/.."
echo "==> Python / shell tests"
python -m pytest -p no:cacheprovider -q tests "$@"
echo "==> Browser (jsdom) tests"
FX=$(mktemp -d)
python tests/js/make_fixtures.py "$FX"
node tests/js/run.js "$FX"
