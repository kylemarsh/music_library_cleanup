#!/usr/bin/env python3
"""
cleanup.py — Music library reconciliation tool.

Consumes the output of scan_library.py (exact.json, stale.json, fuzzy.json,
unknown.json) together with the original source snapshot files to produce:

  decisions.jsonl          — one JSON object per song (grouped across NAS + sources)
  nas_actions.txt          — one tab-delimited line per NAS file
  <source>_actions.txt     — one tab-delimited line per file in each source library

A summary of counts is printed to stdout. File moves are dry-run by default;
pass --apply to execute them.

Usage:
  python cleanup.py \\
    --scan-dir ./scan_results \\
    --source-snapshots src1_snapshot.json src2_snapshot.json \\
    --source-priority src1 src2 \\
    --output-dir ./cleanup_results \\
    [--music-root /volume/Music] \\
    [--backup-root /volume/Music.cleaned] \\
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

# Codec rank: lossless (4) always beats lossy (≤3).
# Within each tier, secondary signals break ties.
CODEC_RANK = {
    "flac": 4,
    "alac": 4,
    "wav":  3,
    "aac":  2,
    "mp3":  1,
}

def quality_score(rec):
    """Return a comparable 4-tuple representing the quality of an audio file.

    Comparison is lexicographic, so higher is better.
    Lossless codecs always outscore lossy ones.

    For lossless: tiebreak by bit depth (16 vs 24 bit), then sample rate.
      Rationale: 24-bit > 16-bit is audibly meaningful; 96kHz vs 44.1kHz is
      debatable but 96kHz is never worse, so we prefer it.

    For lossy: tiebreak by bitrate.
      Rationale: within a given lossy codec higher bitrate means less
      compression; we never compare across codecs using bitrate alone.
    """
    codec = (rec.get("codec") or "").lower()
    rank = CODEC_RANK.get(codec, 0)
    if rank >= 4:                               # lossless
        bits = rec.get("bits_per_sample") or 16
        sr   = rec.get("sample_rate")      or 44100
        return (rank, bits, sr, 0)
    else:                                       # lossy / unknown
        br = rec.get("bitrate") or 0
        return (rank, 0, 0, br)


# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------

def strip_leading_sep(p):
    """Normalise path representation: remove leading slash if present.
    NAS paths from scan_library may have a leading '/' because the relative
    path is computed as full_path[len(root):] without stripping the sep.
    Source paths are stored without a leading slash.
    We normalise both sides before comparing."""
    return p.lstrip("/") if p else p

def path_nfc(p):
    return unicodedata.normalize("NFC", p) if p else p

def path_diff_notes(nas_path, src_path):
    """Return a list of diff-flag strings describing how two paths differ.
    Returns an empty list when the paths are identical (after leading-sep strip).
    The flags are mutually exclusive; only the most specific one is returned."""
    a = strip_leading_sep(nas_path or "")
    b = strip_leading_sep(src_path  or "")

    if a == b:
        return []

    # NFC-normalise both: if they match after normalisation the difference is
    # purely a Unicode NFC/NFD encoding mismatch (common from AFP vs SMB mounts).
    if path_nfc(a) == path_nfc(b):
        return ["PATH_NFD"]

    # Case-only difference (same bytes modulo ASCII case).
    if a.lower() == b.lower():
        return ["PATH_CASE"]

    # Anything else — directory restructure, rename, etc.
    return ["PATH_MISMATCH"]


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------

def compute_diffs(nas_rec, src_recs):
    """Return a sorted list of all diff flags between a NAS record and a list
    of source records.  Each flag appears at most once regardless of how many
    source records trigger it."""
    diffs = set()

    for src in src_recs:
        # Path
        for flag in path_diff_notes(nas_rec.get("path", ""), src.get("path", "")):
            diffs.add(flag)

        # Hash (will always be present for stale/fuzzy; absent for exact)
        nh = nas_rec.get("hash")
        sh = src.get("hash")
        if nh and sh and nh != sh:
            diffs.add("HASH_DIFF")

        # Metadata tags
        for field in ("artist", "title"):
            nv = (nas_rec.get(field) or "").strip()
            sv = (src.get(field)  or "").strip()
            if nv != sv:
                diffs.add(f"META_{field.upper()}_DIFF")

        # Encoding / quality
        for field in ("codec", "bitrate", "sample_rate", "bits_per_sample"):
            if nas_rec.get(field) != src.get(field):
                diffs.add(f"ENC_{field.upper()}_DIFF")

    return sorted(diffs)


# ---------------------------------------------------------------------------
# Record helpers
# ---------------------------------------------------------------------------

def record_info(rec, action=None, notes=None):
    """Return a dict with all relevant fields from rec, plus optional action/notes."""
    d = {
        "path":            rec.get("path"),
        "hash":            rec.get("hash"),
        "artist":          rec.get("artist"),
        "title":           rec.get("title"),
        "duration":        rec.get("duration"),
        "codec":           rec.get("codec"),
        "bitrate":         rec.get("bitrate"),
        "sample_rate":     rec.get("sample_rate"),
        "bits_per_sample": rec.get("bits_per_sample"),
    }
    if action is not None:
        d["action"] = action
    if notes is not None:
        d["notes"] = notes
    return d


# ---------------------------------------------------------------------------
# Source snapshot loading
# ---------------------------------------------------------------------------

def load_source_snapshots(snapshot_paths):
    """Load source snapshot JSON files and build lookup indexes.

    Returns a tuple:
      by_source  {source_name: [full records]}
      by_hash    {hash: [full records]}
      by_key     {(artist,title,dur): [full records]}
      by_fuzzy   {(artist,title):     [full records]}
    """
    by_source = defaultdict(list)
    by_hash   = defaultdict(list)
    by_key    = defaultdict(list)
    by_fuzzy  = defaultdict(list)

    for path in snapshot_paths:
        with open(path, encoding="utf-8") as f:
            records = json.load(f)

        for r in records:
            source = r.get("source") or os.path.basename(path)
            r = dict(r)
            r["source"] = source          # normalise in place

            by_source[source].append(r)
            if r.get("hash"):
                by_hash[r["hash"]].append(r)
            if r.get("key") and r["key"][0] and r["key"][1]:
                by_key[tuple(r["key"])].append(r)
                fk = (r["key"][0], r["key"][1])
                by_fuzzy[fk].append(r)

    return by_source, by_hash, by_key, by_fuzzy


def enrich_source_matches(scan_source_matches, by_hash):
    """Replace the lean entries stored in scan output's source_matches with
    the full records from the source snapshots (which include artist/title etc).

    Falls back to the lean entry if the full record cannot be found (e.g. if
    not all snapshot files were passed to this run of cleanup.py)."""
    full = []
    seen = set()

    for sm in scan_source_matches:
        src_hash   = sm.get("hash")
        src_source = sm.get("source")
        dedup_key  = (src_source, src_hash)

        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        # Find the matching full record in the loaded snapshots
        candidates = [r for r in by_hash.get(src_hash, [])
                      if r.get("source") == src_source]
        if candidates:
            full.append(candidates[0])
        else:
            full.append(sm)     # fallback: lean entry

    return full


# ---------------------------------------------------------------------------
# Canonical source selection
# ---------------------------------------------------------------------------

def select_best_source(src_recs, source_priority):
    """Pick the canonical source record from a list.

    Ranking:
      1. Highest quality_score (lossless beats lossy; bitrate breaks ties)
      2. Position in source_priority (earlier = higher priority)

    Returns (best_record, is_clear_winner).
    is_clear_winner is False only when two records are equal on *both* quality
    and source priority — meaning genuine ambiguity.
    """
    if not src_recs:
        return None, False

    def sort_key(r):
        q   = quality_score(r)
        pri = source_priority.index(r["source"]) if r["source"] in source_priority else 999
        # Primary sort: quality DESC; secondary: priority index ASC
        return (-q[0], -q[1], -q[2], -q[3], pri)

    ranked = sorted(src_recs, key=sort_key)
    best   = ranked[0]

    if len(ranked) == 1:
        return best, True

    # Two records with identical sort key → genuine ambiguity
    if sort_key(ranked[0]) == sort_key(ranked[1]):
        return best, False

    return best, True


# ---------------------------------------------------------------------------
# Case-collision detection
# ---------------------------------------------------------------------------

def find_case_collisions(all_nas_records):
    """Return a set of NAS paths that share a case-insensitive path with at
    least one other NAS file.  These represent files that would silently
    overwrite each other on a case-insensitive filesystem."""
    by_lower = defaultdict(list)
    for r in all_nas_records:
        by_lower[strip_leading_sep(r.get("path", "")).lower()].append(r)

    colliding = set()
    for group in by_lower.values():
        if len(group) > 1:
            for r in group:
                colliding.add(r.get("path", ""))
    return colliding


# ---------------------------------------------------------------------------
# Exclusion helpers
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
    for e in exclusions:
        e = strip_leading_sep(e)
        if p.startswith(e):
            return True
    return False


# ---------------------------------------------------------------------------
# NAS action logic
# ---------------------------------------------------------------------------

def nas_action_exact(nas_rec, canonical, collision_paths):
    """Determine NAS action for an exact (hash) match."""
    notes = []
    path = nas_rec.get("path", "")

    if path in collision_paths:
        notes.append("CASE_COLLISION")

    if canonical:
        notes.extend(path_diff_notes(path, canonical.get("path", "")))

    if "PATH_NFD" in notes or "PATH_CASE" in notes or "PATH_MISMATCH" in notes:
        action = "D_MV"
    elif "CASE_COLLISION" in notes:
        # Hash match but no path mismatch vs canonical — the collision is with
        # a *different* NAS file.  Keep this one, flag the issue.
        action = "OK"
    else:
        action = "OK"

    return action, notes


def nas_action_stale(nas_rec, canonical, is_clear, collision_paths):
    """Determine NAS action for a stale (key) match."""
    notes = []
    path = nas_rec.get("path", "")

    if path in collision_paths:
        notes.append("CASE_COLLISION")

    if not is_clear or canonical is None:
        return "CNFLCT", notes

    nas_q = quality_score(nas_rec)
    src_q = quality_score(canonical)

    if src_q >= nas_q:
        # Source is equal or better — NAS copy is redundant
        action = "D_LQ"
    else:
        # NAS copy is better quality than anything in the source libraries
        action = "KEEP"

    if canonical:
        notes.extend(path_diff_notes(path, canonical.get("path", "")))

    return action, notes


def nas_action_fuzzy(nas_rec, collision_paths):
    """Fuzzy matches always go to manual review (CNFLCT)."""
    notes = ["FUZZY_MATCH"]
    if nas_rec.get("path", "") in collision_paths:
        notes.append("CASE_COLLISION")
    return "CNFLCT", notes


def nas_action_unknown(nas_rec, collision_paths):
    """Unknown files: flag case collisions, otherwise just UNKNOWN."""
    path = nas_rec.get("path", "")
    if path in collision_paths:
        return "CASE_COLLISION", ["CASE_COLLISION"]
    return "UNKNOWN", []


# ---------------------------------------------------------------------------
# Source action logic
# ---------------------------------------------------------------------------

def source_actions_for(src_recs, canonical, is_clear, nas_action):
    """Return a list of (record, action, notes) for source files.

    Rules:
      - canonical source file:
          * OK NAS          → KEEP
          * D_LQ / D_MV NAS → KEEP (it will be rsynced to replace the NAS copy)
          * KEEP NAS        → SRC_AMB (NAS has better quality; manual review)
          * CNFLCT NAS      → SRC_AMB
      - non-canonical source files:
          * same hash as canonical → SRC_D (clear duplicate)
          * different hash, clear winner exists → SRC_D
          * no clear winner → SRC_AMB
    """
    results = []

    for sr in src_recs:
        notes = []

        if sr is canonical:
            if nas_action in ("KEEP", "CNFLCT", "CASE_COLLISION"):
                src_act = "SRC_AMB"
            else:
                src_act = "KEEP"
        else:
            if not is_clear:
                src_act = "SRC_AMB"
            elif canonical and sr.get("hash") == canonical.get("hash"):
                # Byte-for-byte duplicate of canonical
                src_act = "SRC_D"
                notes.append("DUPLICATE_OF_CANONICAL")
            else:
                src_act = "SRC_D"

        results.append((sr, src_act, notes))

    return results


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_all(exact, stale, fuzzy, unknown,
                source_snapshots, source_priority, exclusions):
    """Process all NAS records and produce decisions + source action tracking.

    Returns:
      decisions            list of decision dicts (→ decisions.jsonl)
      source_file_actions  {(source_name, path): (action, notes)}
    """
    by_source, by_hash, by_key, by_fuzzy_src = source_snapshots

    all_nas = exact + stale + fuzzy + unknown
    collision_paths = find_case_collisions(all_nas)

    decisions = []
    # Track which source files have been assigned an action so we can emit
    # SRC_ONLY for the remainder at the end.
    source_file_actions = {}   # (source_name, path) -> (action, notes)

    def claim_source(sr, action, notes):
        """Record a source file's action, preferring KEEP over other actions
        if the same file is referenced by multiple NAS records."""
        k = (sr.get("source"), strip_leading_sep(sr.get("path", "")))
        existing = source_file_actions.get(k)
        if existing is None or (existing[0] != "KEEP" and action == "KEEP"):
            source_file_actions[k] = (action, notes)

    def process_one(nas_rec, tier):
        if is_excluded(nas_rec.get("path", ""), exclusions):
            return None

        # Enrich source matches with full records from snapshot indexes
        scan_matches = nas_rec.get("source_matches", [])
        src_recs = enrich_source_matches(scan_matches, by_hash)

        # Pick canonical source
        canonical, is_clear = select_best_source(src_recs, source_priority)

        # NAS action
        if tier == "exact":
            nas_action, nas_notes = nas_action_exact(nas_rec, canonical, collision_paths)
        elif tier == "stale":
            nas_action, nas_notes = nas_action_stale(nas_rec, canonical, is_clear, collision_paths)
        elif tier == "fuzzy":
            nas_action, nas_notes = nas_action_fuzzy(nas_rec, collision_paths)
        else:  # unknown
            nas_action, nas_notes = nas_action_unknown(nas_rec, collision_paths)

        # Source actions
        src_results = source_actions_for(src_recs, canonical, is_clear, nas_action)
        sources_out = []
        for sr, src_act, src_notes in src_results:
            claim_source(sr, src_act, src_notes)
            sources_out.append(record_info(sr, action=src_act, notes=src_notes))

        # Diffs (NAS vs all matched sources)
        diffs = compute_diffs(nas_rec, src_recs)

        # Key for this entry
        key = nas_rec.get("key") or nas_rec.get("fuzzy_key") or nas_rec.get("path")

        return {
            "key":        key,
            "match_tier": tier,
            "nas":        record_info(nas_rec, action=nas_action, notes=nas_notes),
            "sources":    sources_out,
            "diffs":      diffs,
        }

    for r in exact:
        d = process_one(r, "exact")
        if d:
            decisions.append(d)

    for r in stale:
        d = process_one(r, "stale")
        if d:
            decisions.append(d)

    for r in fuzzy:
        d = process_one(r, "fuzzy")
        if d:
            decisions.append(d)

    for r in unknown:
        d = process_one(r, "unknown")
        if d:
            decisions.append(d)

    # Source files with no NAS counterpart → SRC_ONLY (will be rsynced)
    for source_name, recs in by_source.items():
        for r in recs:
            k = (source_name, strip_leading_sep(r.get("path", "")))
            if k not in source_file_actions:
                source_file_actions[k] = ("SRC_ONLY", [])

    return decisions, source_file_actions


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_decisions(decisions, output_path):
    """Write decisions.jsonl — one JSON object per line, UTF-8."""
    with open(output_path, "w", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


def write_nas_actions(decisions, output_path):
    """Write nas_actions.txt — tab-delimited, one line per NAS file.

    Format:  ACTION <TAB> PATH [<TAB> note note ...]
    """
    with open(output_path, "w", encoding="utf-8") as f:
        for d in decisions:
            nas    = d["nas"]
            action = nas.get("action", "?")
            path   = nas.get("path", "")
            notes  = nas.get("notes", [])
            line   = f"{action}\t{path}"
            if notes:
                line += "\t" + " ".join(notes)
            f.write(line + "\n")


def write_source_actions(source_file_actions, output_dir):
    """Write one <source>_actions.txt per source library.

    Format:  ACTION <TAB> PATH [<TAB> note note ...]
    """
    by_source = defaultdict(list)
    for (source, path), (action, notes) in source_file_actions.items():
        by_source[source].append((path, action, notes))

    for source_name, entries in by_source.items():
        # Sanitise source name for use as filename
        safe_name = source_name.replace("/", "_").replace(" ", "_")
        out_path  = os.path.join(output_dir, f"{safe_name}_actions.txt")

        with open(out_path, "w", encoding="utf-8") as f:
            for path, action, notes in sorted(entries, key=lambda x: x[0]):
                line = f"{action}\t{path}"
                if notes:
                    line += "\t" + " ".join(notes)
                f.write(line + "\n")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(decisions, source_file_actions):
    nas_counts = defaultdict(int)
    for d in decisions:
        nas_counts[d["nas"].get("action", "?")] += 1

    src_counts = defaultdict(int)
    for (action, _) in source_file_actions.values():
        src_counts[action] += 1

    tier_counts = defaultdict(int)
    for d in decisions:
        tier_counts[d["match_tier"]] += 1

    total_nas = sum(nas_counts.values())

    print("\n=== Match tiers ===")
    for tier in ("exact", "stale", "fuzzy", "unknown"):
        n = tier_counts.get(tier, 0)
        print(f"  {tier:<10} {n:>6}  ({100*n/max(total_nas,1):.1f}%)")

    print("\n=== NAS actions ===")
    for action, count in sorted(nas_counts.items()):
        print(f"  {action:<16} {count:>6}")

    print("\n=== Source actions ===")
    for action, count in sorted(src_counts.items()):
        print(f"  {action:<16} {count:>6}")

    print()

    needs_review = sum(nas_counts.get(a, 0) for a in ("CNFLCT", "CASE_COLLISION", "FUZZY_MATCH"))
    if needs_review:
        print(f"  ⚠  {needs_review} NAS files need manual review (CNFLCT / CASE_COLLISION).")

    to_move = sum(nas_counts.get(a, 0) for a in ("D_MV", "D_LQ"))
    print(f"  →  {to_move} NAS files would be moved to backup.")
    print()


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

def move_to_backup(nas_path, music_root, backup_root):
    """Move a NAS file into the backup tree, preserving relative structure."""
    rel  = strip_leading_sep(nas_path)
    src  = os.path.join(music_root,  rel)
    dst  = os.path.join(backup_root, rel)

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    print(f"  moved  {src}")
    print(f"      →  {dst}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Music library reconciliation & cleanup tool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--scan-dir", default="./scan_results",
                        help="Directory containing exact/stale/fuzzy/unknown .json files "
                             "(default: ./scan_results)")
    parser.add_argument("--source-snapshots", nargs="+", required=True, metavar="SNAPSHOT",
                        help="Source snapshot JSON files produced by "
                             "snapshot_source_music_library.py")
    parser.add_argument("--source-priority", nargs="+", required=True, metavar="SOURCE",
                        help="Source names in descending priority order. "
                             "The first name is canonical for path/metadata conflicts.")
    parser.add_argument("--output-dir", default="./cleanup_results",
                        help="Directory for output files (default: ./cleanup_results)")
    parser.add_argument("--music-root", default="/volume/Music",
                        help="NAS music root (default: /volume/Music)")
    parser.add_argument("--backup-root", default=None,
                        help="Root for moved files (default: <music-root>.cleaned)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually move files.  Default is dry-run.")
    parser.add_argument("--exclude", nargs="*", default=[], metavar="PATH",
                        help="NAS path prefix to exclude (repeatable)")
    parser.add_argument("--exclude-from-file", metavar="FILE",
                        help="File of NAS path prefixes to exclude (one per line)")
    args = parser.parse_args()

    if args.backup_root is None:
        args.backup_root = args.music_root.rstrip("/") + ".cleaned"

    # ------------------------------------------------------------------
    # Load scan results
    # ------------------------------------------------------------------

    def load_json_file(name):
        path = os.path.join(args.scan_dir, name)
        if not os.path.exists(path):
            print(f"  warning: {path} not found — treating as empty.")
            return []
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    print("Loading scan results...")
    exact   = load_json_file("exact.json")
    stale   = load_json_file("stale.json")
    fuzzy   = load_json_file("fuzzy.json")
    unknown = load_json_file("unknown.json")
    total_nas = len(exact) + len(stale) + len(fuzzy) + len(unknown)
    print(f"  {total_nas} NAS files: "
          f"{len(exact)} exact, {len(stale)} stale, "
          f"{len(fuzzy)} fuzzy, {len(unknown)} unknown")

    # ------------------------------------------------------------------
    # Load source snapshots
    # ------------------------------------------------------------------

    print("Loading source snapshots...")
    source_data = load_source_snapshots(args.source_snapshots)
    by_source   = source_data[0]
    total_src   = sum(len(v) for v in by_source.values())
    print(f"  {total_src} source files across {len(by_source)} libraries: "
          + ", ".join(f"{k} ({len(v)})" for k, v in by_source.items()))

    # Validate source-priority names
    known_sources = set(by_source.keys())
    for name in args.source_priority:
        if name not in known_sources:
            print(f"  warning: --source-priority '{name}' not found in any snapshot "
                  f"(known: {', '.join(sorted(known_sources))})")

    # ------------------------------------------------------------------
    # Exclusions
    # ------------------------------------------------------------------

    exclusions = load_exclusions(args.exclude, args.exclude_from_file)
    if exclusions:
        print(f"  {len(exclusions)} exclusion prefix(es) loaded.")

    # ------------------------------------------------------------------
    # Process
    # ------------------------------------------------------------------

    print("Processing...")
    decisions, source_file_actions = process_all(
        exact, stale, fuzzy, unknown,
        source_data,
        args.source_priority,
        exclusions,
    )

    # ------------------------------------------------------------------
    # Write output
    # ------------------------------------------------------------------

    os.makedirs(args.output_dir, exist_ok=True)

    decisions_path   = os.path.join(args.output_dir, "decisions.jsonl")
    nas_actions_path = os.path.join(args.output_dir, "nas_actions.txt")

    write_decisions(decisions, decisions_path)
    write_nas_actions(decisions, nas_actions_path)
    write_source_actions(source_file_actions, args.output_dir)

    print(f"\nOutput written to {args.output_dir}/")
    print(f"  decisions.jsonl         ({len(decisions)} entries)")
    print(f"  nas_actions.txt         ({len(decisions)} lines)")
    for source_name in sorted(by_source.keys()):
        safe = source_name.replace("/", "_").replace(" ", "_")
        n = sum(1 for (s, _) in source_file_actions if s == source_name)
        print(f"  {safe}_actions.txt  ({n} lines)")

    # ------------------------------------------------------------------
    # Summary to stdout
    # ------------------------------------------------------------------

    print_summary(decisions, source_file_actions)

    # ------------------------------------------------------------------
    # Apply moves
    # ------------------------------------------------------------------

    to_move = [d for d in decisions if d["nas"].get("action") in ("D_MV", "D_LQ")]

    if args.apply:
        if to_move:
            print(f"Moving {len(to_move)} NAS files to {args.backup_root} ...")
            for d in to_move:
                move_to_backup(d["nas"]["path"], args.music_root, args.backup_root)
            print("Done.")
        else:
            print("Nothing to move.")
    else:
        if to_move:
            print(f"Dry run: {len(to_move)} NAS files would be moved to {args.backup_root}")
            print("Pass --apply to execute.")


if __name__ == "__main__":
    main()
