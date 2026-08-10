#!/usr/bin/env bash
# Run unit tests, then build a standalone ai-usage-monitor binary.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "==> Running unit tests"
uv run --extra dev pytest

echo "==> Building binary"
rm -rf build dist ai-usage-monitor.spec
uv run --with pyinstaller pyinstaller \
    --onefile \
    --name ai-usage-monitor \
    src/ai_usage_monitor/__main__.py

echo "==> Build complete: dist/ai-usage-monitor"
