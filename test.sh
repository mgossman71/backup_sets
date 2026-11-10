#!/bin/bash
# Run the full test suite in an isolated container: no network, repository mounted
# read-only. Nothing touches the real config, flags, logs or disks.
#
#   ./test.sh                 everything
#   ./test.sh -k selection    only tests whose name matches (pytest -k), skips the browser tests
#   ./test.sh tests/shell     one directory or file
set -euo pipefail
cd "$(dirname "$0")"
docker build -q -t backup-sets-tests -f tests/Dockerfile . >/dev/null
if [ $# -gt 0 ]; then
    # options only (e.g. -k name) -> run them against the whole tests/ tree
    [[ "$1" == -* ]] && set -- tests "$@"
    exec docker run --rm --network none -v "$PWD":/src:ro backup-sets-tests \
        python -m pytest -p no:cacheprovider -q "$@"
fi
exec docker run --rm --network none -v "$PWD":/src:ro backup-sets-tests
