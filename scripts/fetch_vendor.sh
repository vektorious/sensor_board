#!/usr/bin/env bash
# Download ECharts into app/static/js so the app has no external runtime
# dependency (CSP-friendly, works offline). Re-run to update the version.
set -euo pipefail

VERSION="${ECHARTS_VERSION:-5.5.1}"
DEST="$(dirname "$0")/../app/static/js/echarts.min.js"

echo "Fetching ECharts ${VERSION} -> ${DEST}"
curl -fsSL "https://cdn.jsdelivr.net/npm/echarts@${VERSION}/dist/echarts.min.js" -o "$DEST"
echo "Done ($(wc -c < "$DEST") bytes)."
