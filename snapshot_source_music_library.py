#!/usr/bin/env python3

import os
import hashlib
import subprocess
import json
import re
import unicodedata

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4p", ".flac", ".aac", ".wav", ".alac"}
SOURCE_NAME = 'kyle_laptop'

def is_audio_file(path):
    return os.path.splitext(path)[1].lower() in AUDIO_EXTENSIONS

def sha1(path, chunk_size=1024 * 1024):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()

def normalize_text(s):
    if not s:
        return ""

    s = s.lower().strip()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()

    # remove common noise
    s = re.sub(r"\(.*?remaster.*?\)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\(.*?deluxe.*?\)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\(.*?explicit.*?\)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\[.*?\]", "", s)

    # remove punctuation
    s = re.sub(r"[^\w\s]", "", s)

    # collapse whitespace
    s = re.sub(r"\s+", " ", s)

    return s.strip()

def normalize_duration(d):
    try:
        return int(float(d))
    except:
        return None

def get_metadata(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries",
            "format=duration:format_tags=artist,title",
            "-of", "default=noprint_wrappers=1:nokey=0",
            path
        ],
        capture_output=True,
        text=True
    )

    data = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()

    tags = {k.replace("TAG:", ""): v for k, v in data.items() if k.startswith("TAG:")}

    return {
        "artist": tags.get("artist"),
        "title": tags.get("title"),
        "duration": data.get("duration"),
    }

def build_key(meta):
    return (
        normalize_text(meta.get("artist")),
        normalize_text(meta.get("title")),
        normalize_duration(meta.get("duration")),
    )

def scan(root):
    records = []

    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            full = os.path.join(dirpath, f)
            if not is_audio_file(full):
                continue

            print("Processing:", full)

            meta = get_metadata(full)

            records.append({
                "path": full.removeprefix(root),
                "source": SOURCE_NAME,
                "hash": sha1(full),
                "artist": meta["artist"],
                "title": meta["title"],
                "duration": meta["duration"],
                "key": build_key(meta),
            })

    return records

if __name__ == "__main__":
    ROOT = "/Users/kylem/Music/Music/Media.localized/Music"
    OUTPUT = "kyle_snapshot.json"

    records = scan(ROOT)

    with open(OUTPUT, "w") as f:
        json.dump(records, f)

    print(f"Wrote {len(records)} records to {OUTPUT}")
