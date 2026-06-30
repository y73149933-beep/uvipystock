#!/usr/bin/env bash
# Compile the Cython matching engine (engine.pyx → engine.so).
# Used locally for development; the backend Dockerfile runs this automatically.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"

echo ">>> Building Cython matching engine"
echo "    Project root: $PROJECT_ROOT"
echo "    Backend dir:  $BACKEND_DIR"

cd "$BACKEND_DIR"

# Ensure Cython is installed
if ! python -c "import Cython" 2>/dev/null; then
    echo "    Installing Cython..."
    pip install cython setuptools wheel
fi

# Compile
PYTHONPATH="$BACKEND_DIR" python app/matching/setup.py build_ext --inplace

echo ""
echo "✓ Cython matching engine compiled."
echo "  Output: $(ls app/matching/engine*.so 2>/dev/null | head -1)"
