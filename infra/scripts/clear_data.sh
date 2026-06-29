#!/usr/bin/env bash
# Full data cleanup script — stops all containers, removes volumes + images.
# Use this to completely reset the exchange to a fresh state.
#
# Usage:
#   bash infra/scripts/clear_data.sh        # stop + remove volumes + images
#   bash infra/scripts/clear_data.sh --keep-images  # stop + remove volumes only

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

KEEP_IMAGES=false
if [ "${1:-}" = "--keep-images" ]; then
    KEEP_IMAGES=true
fi

echo "============================================"
echo "  Crypto Exchange — Full Data Cleanup"
echo "============================================"
echo ""

# 1. Stop and remove all containers
echo ">>> Stopping all containers..."
docker compose down --remove-orphans

# 2. Remove Docker volumes (PostgreSQL data + Redis data)
echo ""
echo ">>> Removing Docker volumes (PostgreSQL + Redis data)..."
docker compose down -v

# 3. Remove Docker images (if not --keep-images)
if [ "$KEEP_IMAGES" = false ]; then
    echo ""
    echo ">>> Removing Docker images..."
    docker rmi -f \
        crypto-exchange-backend \
        crypto-exchange-frontend \
        crypto-exchange-admin \
        2>/dev/null || true
fi

# 4. Clear Redis data (in case Redis is running externally)
echo ""
echo ">>> Clearing Redis data (if running)..."
docker compose exec -T redis redis-cli FLUSHALL 2>/dev/null || true

# 5. Remove build artifacts
echo ""
echo ">>> Removing build artifacts..."
find backend -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find backend -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
find backend -name "*.pyc" -delete 2>/dev/null || true
find backend -name "*.so" -path "*/matching/*" -delete 2>/dev/null || true
find backend -name "engine.c" -path "*/matching/*" -delete 2>/dev/null || true
rm -rf backend/app/matching/build 2>/dev/null || true
rm -rf frontend/dist admin/dist 2>/dev/null || true

echo ""
echo "============================================"
echo "  Cleanup complete!"
echo "============================================"
echo ""
echo "To start fresh:"
echo "  docker compose up -d --build"
echo ""
echo "To start with local development:"
echo "  bash infra/scripts/dev.sh"
