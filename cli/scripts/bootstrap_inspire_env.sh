#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${1:-$ROOT/scripts/inspire.env.template}"
OUTPUT="${2:-$ROOT/.env.inspire.local}"

if [[ ! -f "$TEMPLATE" ]]; then
  echo "Template not found: $TEMPLATE" >&2
  exit 1
fi

if [[ -f "$OUTPUT" ]]; then
  echo "Env file already exists: $OUTPUT"
else
  cp "$TEMPLATE" "$OUTPUT"
  echo "Created env file: $OUTPUT"
fi

echo ""
echo "Next steps:"
echo "1. Edit $OUTPUT and fill INSPIRE_USERNAME / INSPIRE_PASSWORD / INSPIRE_TARGET_DIR."
echo "2. Load env into current shell:"
echo "   set -a; source $OUTPUT; set +a"
echo "3. Validate config and auth:"
echo "   inspire config check --json"
