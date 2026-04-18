#!/usr/bin/env zsh

"""
usage: ./identify_weird_bytes.sh < strings.txt

prints out information about encoding gotchas in each string
"""
set -euo pipefail
print "Scanning for non-ASCII characters and normalization...\n"
while IFS= read -r file; do

# Use Python to:
# - extract non-ASCII chars
# - show per-char bytes
# - detect NFC vs NFD

result=$(python3 - <<'PY' "$file"
import sys, unicodedata
s = sys.argv[1]

# Extract non-ASCII characters
chars = [c for c in s if ord(c) > 127]
if not chars:
    sys.exit(0)

# Determine normalization
if s == unicodedata.normalize("NFC", s):
    norm = "NFC"
elif s == unicodedata.normalize("NFD", s):
    norm = "NFD"
else:
    norm = "MIXED"

print("File:", s)
print("Normalization:", norm)
print("Characters and bytes:")

for c in chars:
    utf8_bytes = c.encode("utf-8")
    hex_bytes = " ".join(f"{b:02x}" for b in utf8_bytes)
    name = unicodedata.name(c, "UNKNOWN")
    print(f"  '{c}'  ->  {hex_bytes}  ({name})")
    print()
PY
)

# Only print if Python produced output

if [[ -n "$result" ]]; then
    print "$result"
fi

done

print "Done."

