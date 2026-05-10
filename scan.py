#!/usr/bin/env python3
"""
scan.py — Compare a laptop music library snapshot against a NAS library.

Classifies each NAS audio file as one of:
  EXACT   — same hash and same relative path as a laptop file
  RENAME  — same hash, different relative path
  ORPHAN  — no hash match, but fingerprint similarity above threshold
  OTHER   — no match of any kind

Produces a tab-delimited report (one line per NAS file) and an updated NAS cache.

Usage:
    scan.py --snapshot <path>
            --nas-root <path>
            --output <path>
            --cache <path>
            [--fpcalc <path>]
            [--fingerprint-threshold <float>]
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4p", ".flac", ".aac", ".wav", ".alac"}
DEFAULT_THRESHOLD = 0.35  # BER threshold; files with BER < threshold are ORPHAN matches


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
    or None on failure.
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


def fingerprint_similarity(fp1, fp2):
    """
    Compute acoustic similarity between two raw Chromaprint fingerprints.

    Returns a float in [0.0, 1.0] where 1.0 is identical.
    Computed as 1 - BER (bit error rate) over the overlapping portion.
    """
    min_len = min(len(fp1), len(fp2))
    if min_len == 0:
        return 0.0
    errors = sum(bin(a ^ b).count("1") for a, b in zip(fp1[:min_len], fp2[:min_len]))
    ber = errors / (min_len * 32)
    return 1.0 - ber


# ---------------------------------------------------------------------------
# JSON I/O
# ---------------------------------------------------------------------------

def load_snapshot(path):
    """Load the laptop snapshot. Exits on missing or unreadable file."""
    if not os.path.exists(path):
        print(f"ERROR: snapshot '{path}' not found.", file=sys.stderr)
        sys.exit(1)
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: could not load snapshot '{path}': {e}", file=sys.stderr)
        sys.exit(1)


def load_cache(path):
    """Load the NAS cache. Returns an empty dict if missing or corrupt."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"WARNING: could not load NAS cache '{path}': {e}. Starting fresh.",
            file=sys.stderr,
        )
        return {}


def save_json(data, path):
    """Write JSON atomically via a .tmp file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------

def build_indexes(snapshot):
    """
    Build lookup structures from the laptop snapshot.

    Returns:
        hash_index:  dict mapping hash -> list of records
        fp_records:  list of (fingerprint, record) for all records with fingerprints
    """
    hash_index = {}
    fp_records = []

    for entry in snapshot.values():
        record = {
            "path": entry["path"],
            "hash": entry["hash"],
            "fingerprint": entry.get("fingerprint"),
        }

        hash_index.setdefault(entry["hash"], []).append(record)

        if entry.get("fingerprint"):
            fp_records.append((entry["fingerprint"], record))

    return hash_index, fp_records


# ---------------------------------------------------------------------------
# NAS scanning
# ---------------------------------------------------------------------------

def scan_nas(nas_root, cache, fpcalc_bin):
    """
    Walk the NAS root and return a list of records plus an updated cache.

    Each record contains {path, hash, fingerprint} with path relative to nas_root.
    Files that fail hashing are skipped entirely.
    """
    records = []
    updated_cache = {}
    reused = 0
    scanned = 0
    skipped = 0

    for dirpath, _, filenames in os.walk(nas_root):
        for filename in sorted(filenames):
            full = os.path.join(dirpath, filename)
            if not is_audio_file(full):
                continue

            sig = file_signature(full)

            # Cache hit
            if full in cache and cache[full]["sig"] == sig:
                entry = cache[full]
                updated_cache[full] = entry
                records.append({
                    "path": entry["path"],
                    "hash": entry["hash"],
                    "fingerprint": entry.get("fingerprint"),
                })
                reused += 1
                continue

            # Cache miss: compute hash and fingerprint
            file_hash = sha1(full)
            if file_hash is None:
                skipped += 1
                continue

            fp = compute_fingerprint(full, fpcalc_bin)
            rel = os.path.relpath(full, nas_root)

            entry = {
                "sig": sig,
                "path": rel,
                "hash": file_hash,
                "fingerprint": fp,
            }
            updated_cache[full] = entry
            records.append({
                "path": rel,
                "hash": file_hash,
                "fingerprint": fp,
            })

            scanned += 1
            if scanned % 25 == 0:
                print(f"  {scanned} NAS files newly scanned so far...")

    return records, updated_cache, scanned, reused, skipped


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(nas_records, hash_index, fp_records, ber_threshold):
    """
    Classify each NAS record and return a list of report row dicts.

    Each row contains:
        classification: EXACT | RENAME | ORPHAN | OTHER
        nas_path:       relative path on NAS
        source_field:   formatted source info (see spec §7.7)
        sort_key:       (source_path, nas_path) tuple, or None for OTHER
    """
    similarity_threshold = 1.0 - ber_threshold
    rows = []

    for nas_rec in nas_records:
        nas_hash = nas_rec["hash"]
        nas_path = nas_rec["path"]
        nas_fp = nas_rec.get("fingerprint")

        # ------------------------------------------------------------------
        # EXACT / RENAME — hash match takes priority
        # ------------------------------------------------------------------
        if nas_hash in hash_index:
            laptop_matches = hash_index[nas_hash]
            exact = [m for m in laptop_matches if m["path"] == nas_path]

            if exact:
                # Same hash, same path — byte-for-byte identical in both senses
                rows.append({
                    "classification": "EXACT",
                    "nas_path": nas_path,
                    "source_field": nas_path,
                    "sort_key": (nas_path, nas_path),
                })
            else:
                # Same hash, different path(s) — moved or renamed on the laptop
                source_field = ";".join(m["path"] for m in laptop_matches)
                rows.append({
                    "classification": "RENAME",
                    "nas_path": nas_path,
                    "source_field": source_field,
                    "sort_key": (laptop_matches[0]["path"], nas_path),
                })
            continue

        # ------------------------------------------------------------------
        # ORPHAN — fingerprint match
        # ------------------------------------------------------------------
        if nas_fp:
            matches = []
            for src_fp, src_rec in fp_records:
                sim = fingerprint_similarity(nas_fp, src_fp)
                if sim >= similarity_threshold:
                    matches.append((sim, src_rec))

            if matches:
                matches.sort(key=lambda x: -x[0])
                source_field = ";".join(
                    f"{m[1]['path']}:{m[0]:.2f}" for m in matches
                )
                rows.append({
                    "classification": "ORPHAN",
                    "nas_path": nas_path,
                    "source_field": source_field,
                    "sort_key": (matches[0][1]["path"], nas_path),
                })
                continue

        # ------------------------------------------------------------------
        # OTHER — no match of any kind (or fingerprint unavailable)
        # ------------------------------------------------------------------
        rows.append({
            "classification": "OTHER",
            "nas_path": nas_path,
            "source_field": "",
            "sort_key": None,
        })

    return rows


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------

def write_report(rows, output_path):
    """
    Write the classification report as a UTF-8 tab-delimited file.

    Sort order:
      1. Rows with a source match, sorted by source path then NAS path.
      2. OTHER rows (no source), sorted by NAS path.
    """
    with_source = [r for r in rows if r["sort_key"] is not None]
    other = [r for r in rows if r["sort_key"] is None]

    with_source.sort(key=lambda r: r["sort_key"])
    other.sort(key=lambda r: r["nas_path"])

    with open(output_path, "w", encoding="utf-8") as f:
        for r in with_source + other:
            f.write(f"{r['classification']}\t{r['nas_path']}\t{r['source_field']}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scan a NAS music library against a laptop snapshot."
    )
    parser.add_argument(
        "--snapshot", required=True,
        help="Path to the laptop snapshot JSON (output of snapshot.py)"
    )
    parser.add_argument(
        "--nas-root", required=True,
        help="Root of the NAS music library (e.g. /Volumes/NAS/Music)"
    )
    parser.add_argument(
        "--output", required=True,
        help="Path to write the classification report (TSV)"
    )
    parser.add_argument(
        "--cache", required=True,
        help="Path to read/write the NAS scan cache JSON"
    )
    parser.add_argument(
        "--fpcalc", default="fpcalc",
        help="Path to fpcalc binary (default: fpcalc on $PATH)"
    )
    parser.add_argument(
        "--fingerprint-threshold", type=float, default=DEFAULT_THRESHOLD,
        metavar="BER",
        help=(
            f"Bit error rate threshold for fingerprint matches (default: {DEFAULT_THRESHOLD}). "
            "Files with BER below this value are classified as ORPHAN. "
            "Lower values are stricter; 0.35 is a reasonable starting point."
        ),
    )
    args = parser.parse_args()

    nas_root = os.path.abspath(args.nas_root)
    if not os.path.isdir(nas_root):
        print(f"ERROR: --nas-root '{nas_root}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    # Load laptop snapshot and build indexes
    print(f"Loading laptop snapshot from {args.snapshot} ...")
    snapshot = load_snapshot(args.snapshot)
    hash_index, fp_records = build_indexes(snapshot)
    print(
        f"  {len(snapshot)} laptop files loaded, "
        f"{len(fp_records)} with fingerprints."
    )

    # Load NAS cache
    print(f"Loading NAS cache from {args.cache} ...")
    cache = load_cache(args.cache)
    print(f"  {len(cache)} entries in existing NAS cache.")

    # Scan NAS
    print(f"Scanning NAS at {nas_root} ...")
    nas_records, updated_cache, scanned, reused, skipped = scan_nas(
        nas_root, cache, args.fpcalc
    )
    print(
        f"  {len(nas_records)} NAS files total: "
        f"{scanned} newly scanned, {reused} reused from cache, {skipped} skipped."
    )

    # Save updated NAS cache
    print(f"Saving NAS cache to {args.cache} ...")
    save_json(updated_cache, args.cache)

    # Classify
    print(f"Classifying (fingerprint threshold BER < {args.fingerprint_threshold}) ...")
    rows = classify(nas_records, hash_index, fp_records, args.fingerprint_threshold)

    # Write report
    print(f"Writing report to {args.output} ...")
    write_report(rows, args.output)

    # Summary
    counts = {}
    for r in rows:
        counts[r["classification"]] = counts.get(r["classification"], 0) + 1

    print("\nResults:")
    for code in ["EXACT", "RENAME", "ORPHAN", "OTHER"]:
        print(f"  {code:8s} {counts.get(code, 0)}")
    print(f"  {'TOTAL':8s} {len(rows)}")


if __name__ == "__main__":
    main()
