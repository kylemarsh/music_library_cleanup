#!/usr/bin/env python3
import fileinput, sys, unicodedata

"""
usage: ./identify_weird_bytes.py < strings.txt

prints out information about encoding gotchas in each string
"""
print("Scanning for non-ASCII characters and normalization...\n")
for line in fileinput.input():
    s = line.rstrip()

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

    print("String:", s)
    print("Normalization:", norm)
    print("Characters and bytes:")

    for c in chars:
        utf8_bytes = c.encode("utf-8")
        hex_bytes = " ".join(f"{b:02x}" for b in utf8_bytes)
        name = unicodedata.name(c, "UNKNOWN")
        print(f"  '{c}'  ->  {hex_bytes}  ({name})")
        print()

print("Done.\n")

