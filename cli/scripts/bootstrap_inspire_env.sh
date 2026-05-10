#!/usr/bin/env bash
set -euo pipefail

echo "Inspire CLI now uses account and project config by default."
echo ""
echo "Next steps:"
echo "1. Configure account, credentials, base URL, and proxy:"
echo "   inspire account add <name>"
echo "2. Bind the current repository to an Inspire project and remote path:"
echo "   inspire init"
echo "3. Validate config and auth:"
echo "   inspire config show --compact"
echo "   inspire config check"
