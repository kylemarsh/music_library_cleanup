#!/usr/bin/env python3
"""
do_clean.py — Execute cleanup actions from a reviewed actions file.

Reads a single *_actions.txt file (produced by cleanup.py and reviewed by a
human) and moves files whose action codes call for removal to a backup
directory, preserving the original directory structure.

This script is machine-agnostic: run it on the NAS with nas_actions.txt, or
copy it to a source machine alongside that source's actions file and run it
there with the path to that machine's music library.

Actions that trigger a move to backup:
  D_MV    NAS file at wrong path / wrong encoding — move so rsync can replace
  D_LQ    NAS file lower quality than source — move so rsync can replace
  SRC_D   Source file is a duplicate of the canonical version — move to backup

Actions that are no-ops (silently skipped):
  OK          NAS file matches source exactly — nothing to do
  KEEP        File is the canonical version — keep in place
  UNKNOWN     No source match — leave alone
  SRC_ONLY    Source file not yet on NAS — will be rsynced later

Actions that are skipped with a warning (unresolved):
  CNFLCT      Conflict requiring manual review
  SRC_AMB     Source ambiguity requiring manual review

Any action code written in by a human during review that is not in the above
lists is treated as unresolved and reported in the summary.

Usage:
  python do_clean.py \\
    --music-root /path/to/library \\
    --backup-root /path/to/backup \\
    [--apply] \\
    actions_file.txt

  # NAS example:
  python do_clean.py --music-root /volume1/Music \\
                     --backup-root /volume1/Music.cleaned \\
                     nas_actions.txt

  # Source machine example (copy script + actions file to the machine first):
  python do_clean.py --music-root ~/Music \\
                     --backup-root ~/Music.cleaned \\
                     source1_actions.txt
"""

import argparse
import os
import shutil
import sys
from collections import defaultdict

# ---------------------------------------------------------------------------
# Action classification
# ---------------------------------------------------------------------------

# Actions that mean "move this file to backup"
MOVE_ACTIONS = {"D_MV", "D_LQ", "SRC_D"}

# Actions that are intentional no-ops
NOOP_ACTIONS = {"OK", "KEEP", "UNKNOWN", "SRC_ONLY"}

# Actions that indicate unresolved human review items
UNRESOLVED_ACTIONS = {"CNFLCT", "SRC_AMB", "CASE_COLLISION"}

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_actions_file(path):
    """Parse an actions file into a list of (action, file_path, notes, lineno).

    Format per line:  ACTION <TAB> PATH [<TAB> NOTES...]
    Blank lines and lines starting with # are ignored.
    """
    rows = []
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.strip().startswith("#"):
                continue
            parts = line.split("\t")
            action    = parts[0].strip() if len(parts) > 0 else ""
            file_path = parts[1].strip() if len(parts) > 1 else ""
            notes     = parts[2].strip() if len(parts) > 2 else ""
            rows.append((action, file_path, notes, lineno))
    return rows

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def strip_leading_sep(p):
    return p.lstrip("/") if p else ""

def resolve_path(file_path, music_root):
    """Resolve a possibly-relative or absolute path against music_root."""
    rel = strip_leading_sep(file_path)
    return os.path.join(music_root, rel)

def backup_path(file_path, backup_root):
    """Mirror the file's path under backup_root."""
    rel = strip_leading_sep(file_path)
    return os.path.join(backup_root, rel)

# ---------------------------------------------------------------------------
# Move operation
# ---------------------------------------------------------------------------

def move_to_backup(file_path, music_root, backup_root, dry_run):
    """Move file_path (relative to music_root) into backup_root.

    Returns one of: "moved", "skipped_missing", "error"
    """
    src = resolve_path(file_path, music_root)
    dst = backup_path(file_path, backup_root)

    if not os.path.exists(src):
        return "skipped_missing", src

    if dry_run:
        return "would_move", src

    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        return "moved", src
    except Exception as e:
        return "error", f"{src}: {e}"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Execute cleanup actions from a reviewed actions file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("actions_file",
                        help="Path to the *_actions.txt file to execute")
    parser.add_argument("--music-root", required=True,
                        help="Root directory of the music library on this machine")
    parser.add_argument("--backup-root", required=True,
                        help="Root directory to move removed files into "
                             "(directory structure is preserved)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually move files.  Default is dry-run.")
    args = parser.parse_args()

    dry_run = not args.apply

    if dry_run:
        print("Dry run — no files will be moved.  Pass --apply to execute.")
    print()

    # --- Parse ---
    rows = parse_actions_file(args.actions_file)
    print(f"Read {len(rows)} action line(s) from {args.actions_file}")

    # --- Classify ---
    to_move    = [(a, p, n, ln) for a, p, n, ln in rows if a in MOVE_ACTIONS]
    noops      = [(a, p, n, ln) for a, p, n, ln in rows if a in NOOP_ACTIONS]
    unresolved = [(a, p, n, ln) for a, p, n, ln in rows if a in UNRESOLVED_ACTIONS]
    unknown    = [(a, p, n, ln) for a, p, n, ln in rows
                  if a not in MOVE_ACTIONS | NOOP_ACTIONS | UNRESOLVED_ACTIONS]

    print(f"  {len(to_move):>5}  to move")
    print(f"  {len(noops):>5}  no-op")
    print(f"  {len(unresolved):>5}  unresolved (will be skipped)")
    if unknown:
        print(f"  {len(unknown):>5}  unrecognised action code(s) (will be skipped)")
    print()

    # --- Execute moves ---
    counts = defaultdict(int)
    errors = []

    for action, file_path, notes, lineno in to_move:
        if not file_path:
            print(f"  line {lineno}: empty path for action {action!r} — skipped")
            counts["skipped_empty"] += 1
            continue

        status, detail = move_to_backup(file_path, args.music_root,
                                        args.backup_root, dry_run)
        counts[status] += 1

        if status == "would_move":
            print(f"  [dry-run] {action}  {file_path}")
        elif status == "moved":
            print(f"  moved     {action}  {file_path}")
        elif status == "skipped_missing":
            print(f"  missing   {action}  {file_path}")
        elif status == "error":
            print(f"  ERROR     {action}  {detail}")
            errors.append((lineno, action, file_path, detail))

    # --- Summary ---
    print()
    print("=== Summary ===")

    if dry_run:
        print(f"  would move:      {counts['would_move']:>5}")
    else:
        print(f"  moved:           {counts['moved']:>5}")
        print(f"  missing (skipped): {counts['skipped_missing']:>3}")
        if counts["error"]:
            print(f"  errors:          {counts['error']:>5}")

    print(f"  no-op:           {len(noops):>5}")
    print(f"  unresolved:      {len(unresolved):>5}")
    if unknown:
        print(f"  unrecognised:    {len(unknown):>5}")

    if unresolved:
        print()
        print("=== Unresolved items (skipped — review required) ===")
        for action, file_path, notes, lineno in unresolved:
            note_str = f"  [{notes}]" if notes else ""
            print(f"  line {lineno:>5}  {action:<14}  {file_path}{note_str}")

    if unknown:
        print()
        print("=== Unrecognised action codes (skipped) ===")
        for action, file_path, notes, lineno in unknown:
            print(f"  line {lineno:>5}  {action!r:<16}  {file_path}")

    if errors:
        print()
        print("=== Errors ===")
        for lineno, action, file_path, detail in errors:
            print(f"  line {lineno:>5}  {action}  {detail}")

    print()
    if dry_run and counts["would_move"]:
        print("Pass --apply to execute the moves above.")

    # Exit non-zero if there were errors
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
