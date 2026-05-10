#!/usr/bin/env python3
"""
move_to_backup.py — Move a listed set of files to a backup directory.

Reads NAS-relative paths from a text file and moves each one to the backup
root, preserving the relative path structure. Operates in dry-run mode by
default; pass --apply to perform moves.

The input file format is compatible with the NAS_PATH column of the scan
report. A typical invocation after reviewing report.tsv:

    grep -E "^(RENAME|ORPHAN)" report.tsv | cut -f2 > to_remove.txt
    move_to_backup.py --nas-root /Volumes/NAS/Music \\
                      --backup-root /Volumes/NAS/Music.bak \\
                      --files to_remove.txt \\
                      --apply

Usage:
    move_to_backup.py --nas-root <path>
                      --backup-root <path>
                      --files <path>
                      [--apply]
"""

import argparse
import os
import shutil
import sys


def read_paths(files_path):
    """
    Read NAS-relative paths from a text file.
    Blank lines and lines beginning with '#' are ignored.
    """
    try:
        with open(files_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        print(f"ERROR: could not read '{files_path}': {e}", file=sys.stderr)
        sys.exit(1)

    paths = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            paths.append(stripped)
    return paths


def move_files(paths, nas_root, backup_root, apply):
    moved = 0
    warned = 0

    for rel_path in paths:
        src = os.path.join(nas_root, rel_path)
        dst = os.path.join(backup_root, rel_path)

        # Validate source exists
        if not os.path.exists(src):
            print(f"WARNING: source not found, skipping:\n  {src}", file=sys.stderr)
            warned += 1
            continue

        # Guard against overwriting an existing backup
        if os.path.exists(dst):
            print(
                f"WARNING: destination already exists, skipping:\n  {dst}",
                file=sys.stderr,
            )
            warned += 1
            continue

        if apply:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            print(f"MOVED  {src}\n    -> {dst}")
        else:
            print(f"MOVE   {src}\n    -> {dst}")

        moved += 1

    return moved, warned


def main():
    parser = argparse.ArgumentParser(
        description="Move listed NAS files to a backup directory."
    )
    parser.add_argument(
        "--nas-root", required=True,
        help="Root of the NAS music library"
    )
    parser.add_argument(
        "--backup-root", required=True,
        help="Destination root for moved files"
    )
    parser.add_argument(
        "--files", required=True,
        help="Text file containing NAS-relative paths to move, one per line"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Perform moves. Without this flag the script is a dry run."
    )
    args = parser.parse_args()

    nas_root = os.path.abspath(args.nas_root)
    backup_root = os.path.abspath(args.backup_root)

    if not os.path.isdir(nas_root):
        print(f"ERROR: --nas-root '{nas_root}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    if not args.apply:
        print("DRY RUN — no files will be moved. Pass --apply to perform moves.\n")

    paths = read_paths(args.files)
    if not paths:
        print("No paths to process.")
        return

    print(f"{len(paths)} path(s) to process.\n")
    moved, warned = move_files(paths, nas_root, backup_root, args.apply)

    verb = "moved" if args.apply else "to move"
    print(f"\nSummary: {moved} {verb}, {warned} warning(s).")


if __name__ == "__main__":
    main()
