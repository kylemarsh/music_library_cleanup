#!/usr/bin/env python3

"""
Intended to help detect duplicates between a music library on a NAS and the
source libraries that feed it new music.

Run this on the NAS, or wherever your library lives, after having run
`snapshot_source_music_library.py` on the source library.

Requires ffprobe
"""

import os
import hashlib
import subprocess
import json
import time
import re
import unicodedata
from collections import defaultdict

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4p", ".flac", ".aac", ".wav", ".alac"}
CACHE_FILE = "music_audit_cache.json"

# ---------- normalization ----------

def normalize_text(s):
    if not s:
        return ""

    s = s.lower().strip()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()

    s = re.sub(r"\(.*?remaster.*?\)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\(.*?deluxe.*?\)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\(.*?explicit.*?\)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\[.*?\]", "", s)

    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)

    return s.strip()

def normalize_duration(d):
    try:
        return int(float(d))
    except:
        return None

def build_key(meta):
    return (
        normalize_text(meta.get("artist")),
        normalize_text(meta.get("title")),
        normalize_duration(meta.get("duration")),
    )

# ---------- file helpers ----------

def is_audio_file(path):
    return os.path.splitext(path)[1].lower() in AUDIO_EXTENSIONS

def sha1(path, chunk_size=1024 * 1024):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()

def file_signature(path):
    stat = os.stat(path)
    return {"size": stat.st_size, "mtime": stat.st_mtime}

# ---------- metadata ----------

def get_metadata(path, ffprobe_bin):
    result = subprocess.run(
        [
            ffprobe_bin,
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

# ---------- cache ----------

def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    with open(CACHE_FILE) as f:
        return json.load(f)

def save_cache(cache):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_FILE)

# ---------- scanning ----------

def scan_library(root, cache, ffprobe_bin):
    records = []
    updated_cache = {}
    reused = 0
    scanned = 0

    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            full = os.path.join(dirpath, f)
            if not is_audio_file(full):
                continue

            scanned += 1
            sig = file_signature(full)

            if full in cache and cache[full]["sig"] == sig:
                reused += 1
                record = cache[full]["record"]
            else:
                meta = get_metadata(full, ffprobe_bin)
                if full.startswith(root):
                    relative = full[len(root):]
                record = {
                    "path": relative,
                    "hash": sha1(full),
                    "artist": meta["artist"],
                    "title": meta["title"],
                    "duration": meta["duration"],
                    "key": build_key(meta),
                }

            updated_cache[full] = {
                "sig": sig,
                "record": record,
                "cached_at": time.time()
            }

            records.append(record)

    print(f"Scanned: {scanned}, reused: {reused}")
    return records, updated_cache

# ---------- comparison ----------

def load_snapshots(paths):
    source_hash_map = defaultdict(list)
    source_key_map = defaultdict(list)

    for snapshot_path in paths:
        with open(snapshot_path) as f:
            data = json.load(f)

        for r in data:
            entry = {
                "path": r["path"],
                "source": r.get("source", snapshot_path)
            }

            source_hash_map[r["hash"]].append(entry)
            source_key_map[tuple(r["key"])].append(entry)

    return source_hash_map, source_key_map

def compare(nas_records, snapshot_paths):
    source_hash_map, source_key_map = load_snapshots(snapshot_paths)

    exact = []
    stale = []
    unknown = []

    for r in nas_records:
        record = dict(r)

        if r["hash"] in source_hash_map:
            record["source_matches"] = source_hash_map[r["hash"]]
            exact.append(record)

        elif tuple(r["key"]) in source_key_map:
            record["source_matches"] = source_key_map[tuple(r["key"])]
            stale.append(record)

        else:
            record["source_matches"] = []
            unknown.append(record)

    return exact, stale, unknown

# ---------- main ----------

if __name__ == "__main__":
    NAS_ROOT = "/volume//Music"
    SNAPSHOTS = [
        "/home/nas_user/music_cleanup/source1_snapshot.json"
        "/home/nas_user/music_cleanup/source2_snapshot.json"
    ]
    FFPROBE = "/usr/local/bin/ffprobe"

    cache = load_cache()

    print("Scanning NAS...")
    records, cache = scan_library(NAS_ROOT, cache, FFPROBE)

    save_cache(cache)

    print("Comparing...")
    exact, stale, unknown = compare(records, SNAPSHOTS)

    print("\nResults:")
    print(f"Exact matches: {len(exact)}")
    print(f"Likely duplicates (metadata match): {len(stale)}")
    print(f"Only on NAS: {len(unknown)}")

    # optional: dump lists
    with open("exact.json", "w") as f:
        json.dump(exact, f)

    with open("stale.json", "w") as f:
        json.dump(stale, f)

    with open("unknown.json", "w") as f:
        json.dump(unknown, f)
