#!/usr/bin/env bash
# Compare CDN-pinned package versions in templates/index.html against npm latest.
# Run on every PR. If anything is behind, bump the version + regenerate the SRI hash:
#   curl -fsS https://cdn.jsdelivr.net/npm/<pkg>@<ver> | openssl dgst -sha384 -binary | openssl base64 -A
# Exits 0 if all current, 1 if anything behind, 2 on lookup error.
set -eu

cd "$(dirname "$0")/.."

pkgs=(
  bootstrap
  chart.js
  dayjs
  chartjs-adapter-dayjs-4
  chartjs-plugin-zoom
)

stale=0
for pkg in "${pkgs[@]}"; do
  esc=$(printf '%s' "$pkg" | sed 's/\./\\./g')
  pinned=$(grep -oE "${esc}@[0-9]+\.[0-9]+\.[0-9]+" templates/index.html | head -1 | sed 's/.*@//')
  if [ -z "$pinned" ]; then
    printf '%-32s MISSING pin in templates/index.html\n' "$pkg"
    stale=1
    continue
  fi
  latest=$(curl -fsS "https://registry.npmjs.org/$pkg/latest" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])") \
    || { printf '%-32s lookup failed\n' "$pkg"; exit 2; }
  if [ "$pinned" = "$latest" ]; then
    printf '%-32s %s (current)\n' "$pkg" "$pinned"
  else
    printf '%-32s %s -> %s (behind)\n' "$pkg" "$pinned" "$latest"
    stale=1
  fi
done

exit $stale
