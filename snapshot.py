#!/usr/bin/env python3
"""
snapshot.py — Index a music library into a snapshot file.

The snapshot file doubles as a cache: files whose size and mtime are unchanged
on subsequent runs are not re-hashed or re-fingerprinted.

Usage:
    snapshot.py --root <path> --snapshot <path> [--fpcalc <path>]
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4p", ".flac", ".aac", ".wav", ".alac"}


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def is_audio_file(path):
    return os.path.splitext(path)[1].lower() in AUDIO_EXTENSIONS


def file_signature(path):
    st = os.stat(path)
    return {"size": st.st_size, "mtime": st.st_mtime}


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def sha1(path, chunk_size=1024 * 1024):
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                h.update(chunk)
        return h.hexdigest()
    except OSError as e:
        print(f"WARNING: could not read {path}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

def compute_fingerprint(path, fpcalc_bin):
    """
    Run fpcalc and return the raw fingerprint as a list of signed ints,
    or None if fpcalc fails.
    """
    try:
        result = subprocess.run(
            [fpcalc_bin, "-raw", "-json", path],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        print(f"ERROR: fpcalc not found at '{fpcalc_bin}'", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"WARNING: fpcalc timed out on {path}", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(
            f"WARNING: fpcalc failed on {path}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None

    try:
        data = json.loads(result.stdout)
        return data.get("fingerprint")
    except json.JSONDecodeError as e:
        print(f"WARNING: could not parse fpcalc output for {path}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Snapshot I/O
# ---------------------------------------------------------------------------

def load_snapshot(path):
    """Load an existing snapshot, returning an empty dict if missing or corrupt."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"WARNING: could not load snapshot {path}: {e}. Starting fresh.",
            file=sys.stderr,
        )
        return {}


def save_snapshot(data, path):
    """Write snapshot atomically via a .tmp file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def scan(root, existing_snapshot, fpcalc_bin):
    """
    Walk root, returning an updated snapshot dict and summary counts.

    The snapshot maps absolute path -> {sig, path, hash, fingerprint}.
    Files whose sig matches the existing snapshot are reused without re-processing.
    """
    updated = {}
    reused = 0
    scanned = 0
    skipped = 0

    for dirpath, _, filenames in os.walk(root):
        for filename in sorted(filenames):
            full = os.path.join(dirpath, filename)
            if not is_audio_file(full):
                continue

            sig = file_signature(full)

            # Cache hit: reuse existing record unchanged
            if full in existing_snapshot and existing_snapshot[full]["sig"] == sig:
                updated[full] = existing_snapshot[full]
                reused += 1
                continue

            # Cache miss: compute hash and fingerprint
            file_hash = sha1(full)
            if file_hash is None:
                skipped += 1
                continue

            fp = compute_fingerprint(full, fpcalc_bin)
            rel = os.path.relpath(full, root)

            updated[full] = {
                "sig": sig,
                "path": rel,
                "hash": file_hash,
                "fingerprint": fp,
            }

            scanned += 1
            if scanned % 25 == 0:
                print(f"  {scanned} newly scanned so far...")

    return updated, scanned, reused, skipped


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Snapshot a music library for later comparison against a NAS."
    )
    parser.add_argument("--root", required=True, help="Root of the music library to snapshot")
    parser.add_argument("--snapshot", required=True, help="Path to read/write the snapshot JSON")
    parser.add_argument(
        "--fpcalc", default="fpcalc", help="Path to fpcalc binary (default: fpcalc on $PATH)"
    )
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"ERROR: --root '{root}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    print(f"Loading existing snapshot from {args.snapshot} ...")
    existing = load_snapshot(args.snapshot)
    print(f"  {len(existing)} entries in existing snapshot.")

    print(f"Scanning {root} ...")
    updated, scanned, reused, skipped = scan(root, existing, args.fpcalc)

    print(f"Saving snapshot to {args.snapshot} ...")
    save_snapshot(updated, args.snapshot)

    total = scanned + reused
    print(
        f"\nDone. {total} audio files total: "
        f"{scanned} newly scanned, {reused} reused from cache, {skipped} skipped."
    )


if __name__ == "__main__":
    main()
