#!/usr/bin/env zsh

set -euo pipefail

# Requires: python3 (for Unicode normalization)

typeset -A seen

print "Scanning for mixed Unicode normalization...\n"

find $MUSIC_LIBRARY. -print0 | while IFS= read -r -d '' file; do

# Normalize to NFC using Python

nfc=$(python3 -c '
import sys, unicodedata
print(unicodedata.normalize("NFC", sys.argv[1]))
' "$file")

# If we've seen this normalized path before, compare

if [[ -n "${seen[$nfc]:-}" ]]; then
if [[ "$file" != "${seen[$nfc]}" ]]; then
print "⚠️  Possible normalization duplicate:"
print "  Original: ${seen[$nfc]}"
print "  Variant : $file"
print ""
fi
else
seen[$nfc]="$file"
fi
done

print "Done."

