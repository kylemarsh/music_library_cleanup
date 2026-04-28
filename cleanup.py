#!/usr/bin/env python3
"""
cleanup.py — Music library reconciliation & cleanup tool.

Reads the recording index produced by scan_library.py and produces:

  decisions.jsonl            — one JSON object per recording
  nas_actions.txt            — one tab-delimited line per NAS file
  <source>_actions.txt       — one tab-delimited line per source file

A summary of counts is printed to stdout.  File moves are dry-run by default;
pass --apply to execute them on the NAS.

Usage:
  python cleanup.py \\
    --index index.jsonl \\
    --source-priority liz_laptop kyle_laptop \\
    [--output-dir ./cleanup_results] \\
    [--music-root /volume1/Music] \\
    [--backup-root /volume1/Music.cleaned] \\
    [--apply] \\
    [--exclude /path/prefix] \\
    [--exclude-from-file exclusions.txt]
"""

import argparse
import json
import os
import shutil
import unicodedata
from collections import defaultdict

# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------

CODEC_RANK = {
    "flac": 4,
    "alac": 4,
    "wav":  3,
    "aac":  2,
    "mp3":  1,
}

def quality_score(rec):
    """Comparable 4-tuple; higher is better.

    Lossless (rank 4) always beats lossy (rank ≤ 3).
    Lossless tiebreak: bit depth, then sample rate.
      Rationale: 24-bit captures more dynamic range than 16-bit; higher sample
      rate is never worse.
    Lossy tiebreak: bitrate.
      Rationale: within a codec, more bits = less compression.  We never use
      bitrate to compare across codecs — AAC always outranks MP3 regardless of
      bitrate.
    """
    codec = (rec.get("codec") or "").lower()
    rank  = CODEC_RANK.get(codec, 0)
    if rank >= 4:
        bits = rec.get("bits_per_sample") or 16
        sr   = rec.get("sample_rate")     or 44100
        return (rank, bits, sr, 0)
    else:
        br = rec.get("bitrate") or 0
        return (rank, 0, 0, br)

# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------

def strip_leading_sep(p):
    return p.lstrip("/") if p else (p or "")

def nfc(p):
    return unicodedata.normalize("NFC", p) if p else (p or "")

def path_notes(nas_path, src_path):
    """Flags describing how two paths differ.  Returns [] if identical."""
    a = strip_leading_sep(nas_path or "")
    b = strip_leading_sep(src_path  or "")
    if a == b:
        return []
    if nfc(a) == nfc(b):
        return ["PATH_NFD"]
    if a.lower() == b.lower():
        return ["PATH_CASE"]
    return ["PATH_MISMATCH"]

# ---------------------------------------------------------------------------
# Diff computation (for decisions.jsonl)
# ---------------------------------------------------------------------------

COMPARABLE_FIELDS = [
    "artist", "title", "duration", "path",
    "codec", "bitrate", "sample_rate", "bits_per_sample",
]

def compute_conflicts(entry, source_names):
    """Return sorted list of field names that differ across any two locations."""
    fields    = entry.get("fields", {})
    conflicts = []

    for field in COMPARABLE_FIELDS:
        fdata = fields.get(field, {})

        # Collect all values: flatten NAS list, gather source scalars
        all_vals = []
        nas_vals = fdata.get("nas")
        if nas_vals is not None:
            all_vals.extend(nas_vals)
        for sn in source_names:
            sv = fdata.get(sn)
            if sv is not None:
                all_vals.append(sv)

        if len(set(str(v) for v in all_vals)) > 1:
            conflicts.append(field)

    return conflicts

# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------

def load_exclusions(exclude_list, exclude_file):
    excl = set(exclude_list or [])
    if exclude_file:
        with open(exclude_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    excl.add(line)
    return excl

def is_excluded(path, exclusions):
    p = strip_leading_sep(path or "")
    return any(p.startswith(strip_leading_sep(e)) for e in exclusions)

# ---------------------------------------------------------------------------
# Case-collision detection
# ---------------------------------------------------------------------------

def find_case_collisions(entries):
    """Return set of NAS paths that share a case-insensitive path with another
    NAS file anywhere in the index."""
    by_lower = defaultdict(list)
    for entry in entries:
        for frec in (entry["files"].get("nas") or []):
            p = strip_leading_sep(frec.get("path", ""))
            by_lower[p.lower()].append(frec.get("path", ""))

    colliding = set()
    for group in by_lower.values():
        if len(group) > 1:
            colliding.update(group)
    return colliding

# ---------------------------------------------------------------------------
# Canonical source selection
# ---------------------------------------------------------------------------

def select_canonical(src_file_recs, source_priority):
    """Pick the canonical source file record from a list.

    Ranking: quality score DESC, then source priority ASC (lower index = higher
    priority).  Returns (best_record, is_clear_winner).
    is_clear_winner is False only when two records tie on both quality and
    source priority.
    """
    if not src_file_recs:
        return None, False

    def sort_key(r):
        q   = quality_score(r)
        pri = source_priority.index(r["source"]) \
              if r["source"] in source_priority else 999
        return (-q[0], -q[1], -q[2], -q[3], pri)

    ranked = sorted(src_file_recs, key=sort_key)
    best   = ranked[0]

    if len(ranked) == 1:
        return best, True
    if sort_key(ranked[0]) == sort_key(ranked[1]):
        return best, False
    return best, True

# ---------------------------------------------------------------------------
# NAS action logic
# ---------------------------------------------------------------------------

def nas_action_for(nas_frec, canonical_src, is_clear, confidence, collision_paths):
    """Return (action, notes) for one NAS file record.

    confidence: identity_confidence of the recording entry
    """
    path  = nas_frec.get("path", "")
    notes = []

    if path in collision_paths:
        notes.append("CASE_COLLISION")

    # No source match at all
    if canonical_src is None:
        if "CASE_COLLISION" in notes:
            return "CASE_COLLISION", notes
        return "UNKNOWN", notes

    # Fuzzy/probable match — always manual review
    if confidence == "probable":
        notes.append("FUZZY_MATCH")
        return "CNFLCT", notes

    # Certain match — compare quality and paths
    nas_q = quality_score(nas_frec)
    src_q = quality_score(canonical_src)

    if not is_clear:
        return "CNFLCT", notes

    pnotes = path_notes(path, canonical_src.get("path", ""))
    notes.extend(pnotes)

    if src_q >= nas_q:
        # Source is equal or better quality
        if pnotes or nas_frec.get("hash") != canonical_src.get("hash"):
            # Same song, but NAS copy differs (path or bytes) — move it out
            # so the correct version can rsync in
            return "D_MV", notes
        return "OK", notes
    else:
        # NAS has a better version than any source
        return "KEEP", notes

# ---------------------------------------------------------------------------
# Source action logic
# ---------------------------------------------------------------------------

def source_actions_for(src_file_recs, canonical_src, is_clear, nas_action,
                       nas_frec, source_priority):
    """Return list of (file_record, action, notes) for all source files."""
    results = []

    for sr in src_file_recs:
        notes = []

        if sr is canonical_src:
            if nas_action in ("KEEP", "CNFLCT", "CASE_COLLISION"):
                # NAS is better or ambiguous — flag for human review
                act = "SRC_AMB"
            else:
                # Canonical source — keep it, it will rsync to NAS
                act = "KEEP"
                if nas_frec:
                    notes.extend(path_notes(nas_frec.get("path", ""),
                                            sr.get("path", "")))
        else:
            if not is_clear:
                act = "SRC_AMB"
            else:
                act = "SRC_D"

        results.append((sr, act, notes))

    return results

# ---------------------------------------------------------------------------
# Multi-NAS handling
# ---------------------------------------------------------------------------

def resolve_multi_nas(nas_frecs, canonical_src, source_priority):
    """When multiple NAS files map to the same recording, pick one to keep.

    Strategy:
      1. If one of them hash-matches the canonical source, prefer it.
      2. Otherwise prefer the highest quality NAS file.
      3. All others → D_LQ (lower quality duplicate on NAS).

    Returns list of (nas_frec, preferred=bool).
    """
    if not nas_frecs:
        return []

    if canonical_src:
        hash_match = [r for r in nas_frecs
                      if r.get("hash") == canonical_src.get("hash")]
        if hash_match:
            preferred = hash_match[0]
        else:
            preferred = sorted(nas_frecs,
                               key=lambda r: quality_score(r),
                               reverse=True)[0]
    else:
        preferred = sorted(nas_frecs,
                           key=lambda r: quality_score(r),
                           reverse=True)[0]

    # Return a path -> bool map instead of using dicts as keys
    return {r.get("path"): (r is preferred) for r in nas_frecs}

# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_entry(entry, source_names, source_priority, collision_paths, exclusions):
    """Process one recording entry.  Returns a list of decision dicts — one
    per NAS file (or one for the whole entry when there is no NAS file)."""

    nas_frecs = entry["files"].get("nas") or []
    confidence = entry["identity_confidence"]

    # Collect source file records from entry (already enriched by scan_library)
    src_file_recs = []
    for sn in source_names:
        frec = entry["files"].get(sn)
        if frec is not None:
            frec = dict(frec)
            frec["source"] = sn
            src_file_recs.append(frec)

    canonical_src, is_clear = select_canonical(src_file_recs, source_priority)

    # --- Entries with no NAS file ---
    if not nas_frecs:
        src_results = source_actions_for(
            src_file_recs, canonical_src, is_clear,
            nas_action="UNKNOWN", nas_frec=None,
            source_priority=source_priority,
        )
        sources_out = []
        for sr, act, notes in src_results:
            sources_out.append({**sr, "action": act, "notes": notes})

        return [{
            "id":                  entry["id"],
            "identity_confidence": confidence,
            "match_tier":          confidence,
            "nas":                 None,
            "sources":             sources_out,
            "conflicts":           compute_conflicts(entry, source_names),
            "related":             entry.get("related", []),
            "fields":              entry.get("fields", {}),
        }]

    # --- Entries with one or more NAS files ---
    # preferred_map: path -> bool (True = this is the one NAS copy to keep)
    preferred_map = resolve_multi_nas(nas_frecs, canonical_src, source_priority)
    decisions     = []

    for nas_frec in nas_frecs:
        if is_excluded(nas_frec.get("path", ""), exclusions):
            continue

        is_preferred = preferred_map.get(nas_frec.get("path"), True)

        if not is_preferred:
            # Secondary NAS duplicate — always move out
            nas_action = "D_LQ"
            nas_notes  = ["NAS_DUPLICATE"]
        else:
            nas_action, nas_notes = nas_action_for(
                nas_frec, canonical_src, is_clear, confidence, collision_paths
            )

        src_results = source_actions_for(
            src_file_recs, canonical_src, is_clear,
            nas_action=nas_action, nas_frec=nas_frec,
            source_priority=source_priority,
        )
        sources_out = []
        for sr, act, notes in src_results:
            sources_out.append({**sr, "action": act, "notes": notes})

        decisions.append({
            "id":                  entry["id"],
            "identity_confidence": confidence,
            "match_tier":          confidence,
            "nas":                 {**nas_frec, "action": nas_action, "notes": nas_notes},
            "sources":             sources_out,
            "conflicts":           compute_conflicts(entry, source_names),
            "related":             entry.get("related", []),
            "fields":              entry.get("fields", {}),
        })

    return decisions

# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_decisions(decisions, path):
    with open(path, "w", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


def write_nas_actions(decisions, path):
    """FORMAT: ACTION <TAB> PATH [<TAB> NOTES...]"""
    with open(path, "w", encoding="utf-8") as f:
        for d in decisions:
            nas = d.get("nas")
            if nas is None:
                continue
            action = nas.get("action", "?")
            fpath  = nas.get("path", "")
            notes  = nas.get("notes", [])
            line   = f"{action}\t{fpath}"
            if notes:
                line += "\t" + " ".join(notes)
            f.write(line + "\n")


def write_source_actions(decisions, output_dir):
    """Write one <source>_actions.txt per source library."""
    by_source = defaultdict(list)  # source_name -> [(path, action, notes)]

    for d in decisions:
        for sr in d.get("sources", []):
            sn    = sr.get("source", "unknown")
            fpath = sr.get("path", "")
            act   = sr.get("action", "?")
            notes = sr.get("notes", [])
            by_source[sn].append((fpath, act, notes))

    for sn, rows in by_source.items():
        safe = sn.replace("/", "_").replace(" ", "_")
        out  = os.path.join(output_dir, f"{safe}_actions.txt")
        with open(out, "w", encoding="utf-8") as f:
            for fpath, act, notes in sorted(rows, key=lambda x: x[0]):
                line = f"{act}\t{fpath}"
                if notes:
                    line += "\t" + " ".join(notes)
                f.write(line + "\n")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(decisions, source_names):
    nas_counts = defaultdict(int)
    src_counts = defaultdict(int)
    tier_counts = defaultdict(int)

    for d in decisions:
        tier_counts[d["identity_confidence"]] += 1
        nas = d.get("nas")
        if nas:
            nas_counts[nas.get("action", "?")] += 1
        for sr in d.get("sources", []):
            src_counts[sr.get("action", "?")] += 1

    total = sum(nas_counts.values())

    print("\n=== Identity confidence ===")
    for tier in ("certain", "probable", "none"):
        n = tier_counts.get(tier, 0)
        pct = 100 * n / max(sum(tier_counts.values()), 1)
        print(f"  {tier:<12} {n:>6}  ({pct:.1f}%)")

    print("\n=== NAS actions ===")
    for action in sorted(nas_counts):
        print(f"  {action:<16} {nas_counts[action]:>6}")

    print("\n=== Source actions ===")
    for action in sorted(src_counts):
        print(f"  {action:<16} {src_counts[action]:>6}")

    needs_review = sum(nas_counts.get(a, 0) for a in ("CNFLCT", "CASE_COLLISION"))
    to_move      = sum(nas_counts.get(a, 0) for a in ("D_MV", "D_LQ"))

    print()
    if needs_review:
        print(f"  ⚠  {needs_review} NAS file(s) need manual review.")
    print(f"  →  {to_move} NAS file(s) would be moved to backup.")
    print()

# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

def move_to_backup(nas_path, music_root, backup_root):
    rel = strip_leading_sep(nas_path)
    src = os.path.join(music_root,  rel)
    dst = os.path.join(backup_root, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    print(f"  moved {src}")
    print(f"     -> {dst}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Music library reconciliation & cleanup tool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--index", default="index.jsonl",
                        help="Recording index produced by scan_library.py "
                             "(default: index.jsonl)")
    parser.add_argument("--source-priority", nargs="+", required=True,
                        metavar="SOURCE",
                        help="Source names in descending priority order. "
                             "First entry is primary.")
    parser.add_argument("--output-dir", default="./cleanup_results",
                        help="Directory for output files (default: ./cleanup_results)")
    parser.add_argument("--music-root", default="/volume1/Music",
                        help="NAS music root (default: /volume1/Music)")
    parser.add_argument("--backup-root", default=None,
                        help="Root for moved files "
                             "(default: <music-root>.cleaned)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually move files.  Default is dry-run.")
    parser.add_argument("--exclude", nargs="*", default=[],
                        metavar="PATH",
                        help="NAS path prefix to exclude (repeatable)")
    parser.add_argument("--exclude-from-file", metavar="FILE",
                        help="File of NAS path prefixes to exclude")
    args = parser.parse_args()

    if args.backup_root is None:
        args.backup_root = args.music_root.rstrip("/") + ".cleaned"

    # --- Load index ---
    print(f"Loading index from {args.index}...")
    entries = []
    with open(args.index, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    print(f"  {len(entries)} recording entries loaded.")

    # Derive source names present in the index
    source_names = list(args.source_priority)
    # Also pick up any sources in the index not mentioned in priority list
    seen = set(source_names)
    for e in entries:
        for k in e.get("files", {}):
            if k != "nas" and k not in seen:
                source_names.append(k)
                seen.add(k)
                print(f"  warning: source '{k}' found in index but not in "
                      f"--source-priority; appended with lowest priority.")

    # --- Exclusions ---
    exclusions = load_exclusions(args.exclude, args.exclude_from_file)
    if exclusions:
        print(f"  {len(exclusions)} exclusion prefix(es) loaded.")

    # --- Case collisions ---
    collision_paths = find_case_collisions(entries)
    if collision_paths:
        print(f"  {len(collision_paths)} NAS path(s) involved in case collisions.")

    # --- Process ---
    print("Processing entries...")
    all_decisions = []
    for entry in entries:
        all_decisions.extend(
            process_entry(entry, source_names, args.source_priority,
                          collision_paths, exclusions)
        )

    # --- Write output ---
    os.makedirs(args.output_dir, exist_ok=True)

    dec_path = os.path.join(args.output_dir, "decisions.jsonl")
    nas_path = os.path.join(args.output_dir, "nas_actions.txt")

    write_decisions(all_decisions, dec_path)
    write_nas_actions(all_decisions, nas_path)
    write_source_actions(all_decisions, args.output_dir)

    print(f"\nOutput written to {args.output_dir}/")
    print(f"  decisions.jsonl  ({len(all_decisions)} entries)")
    nas_count = sum(1 for d in all_decisions if d.get("nas"))
    print(f"  nas_actions.txt  ({nas_count} lines)")
    for sn in source_names:
        safe = sn.replace("/", "_").replace(" ", "_")
        n    = sum(
            sum(1 for sr in d.get("sources", []) if sr.get("source") == sn)
            for d in all_decisions
        )
        print(f"  {safe}_actions.txt  ({n} lines)")

    # --- Summary ---
    print_summary(all_decisions, source_names)

    # --- Apply ---
    to_move = [d for d in all_decisions
               if d.get("nas") and d["nas"].get("action") in ("D_MV", "D_LQ")]

    if args.apply:
        if to_move:
            print(f"Moving {len(to_move)} NAS file(s) to {args.backup_root}...")
            for d in to_move:
                move_to_backup(d["nas"]["path"], args.music_root, args.backup_root)
            print("Done.")
        else:
            print("Nothing to move.")
    else:
        if to_move:
            print(f"Dry run: {len(to_move)} NAS file(s) would be moved "
                  f"to {args.backup_root}")
            print("Pass --apply to execute.")


if __name__ == "__main__":
    main()
