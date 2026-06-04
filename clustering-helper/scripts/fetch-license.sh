#!/usr/bin/env bash
# Fetch the canonical GPL-3.0 license text from gnu.org and write LICENSE.
#
# Run at release time:
#     bash scripts/fetch-license.sh
#
# The release CI hard-fails if LICENSE still contains the word "PLACEHOLDER",
# so this MUST run before tagging v1.0.0 (or any subsequent release).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${REPO_ROOT}/LICENSE"
URL="https://www.gnu.org/licenses/gpl-3.0.txt"

curl --fail --silent --show-error -o "${DEST}.tmp" "${URL}"

# Sanity check: the canonical file is ~35KB and starts with the license name.
if ! head -n 1 "${DEST}.tmp" | grep -qi "GNU GENERAL PUBLIC LICENSE"; then
  echo "ERROR: fetched LICENSE text doesn't start with GNU GENERAL PUBLIC LICENSE"
  rm -f "${DEST}.tmp"
  exit 1
fi
mv -f "${DEST}.tmp" "${DEST}"
echo "Wrote ${DEST} ($(wc -c < "${DEST}") bytes)"
