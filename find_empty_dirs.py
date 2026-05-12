#!/usr/bin/env python3
"""
find_empty_dirs.py — Find directories under a music root that contain no audio files.

Usage:
    find_empty_dirs.py <music-root>
"""

import os
import sys

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4p", ".flac", ".aac", ".wav", ".alac"}


def main():
    if len(sys.argv) != 2:
        print("Usage: find_empty_dirs.py <music-root>", file=sys.stderr)
        sys.exit(1)

    root = os.path.abspath(sys.argv[1])

    # Walk bottom-up, tracking which directories contain audio
    # (directly or via a subdirectory).
    dirs_with_audio = set()
    no_audio_dirs = set()

    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath == root:
            continue

        has_audio = (
            any(os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS for f in filenames)
            or any(os.path.join(dirpath, d) in dirs_with_audio for d in dirnames)
        )

        if has_audio:
            dirs_with_audio.add(dirpath)
        else:
            no_audio_dirs.add(dirpath)

    # Only print the top-most no-audio directories — skip children of
    # directories already being reported to avoid redundant output.
    for d in sorted(no_audio_dirs):
        if os.path.dirname(d) not in no_audio_dirs:
            print(d)


if __name__ == "__main__":
    main()
