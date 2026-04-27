#!/usr/bin/env python3

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
    """Full key: (normalized_artist, normalized_title, duration_seconds).
    Used for stale matching. Returns None if artist or title is missing."""
    artist = normalize_text(meta.get("artist"))
    title = normalize_text(meta.get("title"))
    duration = normalize_duration(meta.get("duration"))

    if not artist or not title:
        return None
    return (artist, title, duration)

def build_fuzzy_key(meta):
    """Fuzzy key: (normalized_artist, normalized_title) — no duration.
    Used as a fallback when stale matching fails (e.g. re-encodes that shifted
    duration). Returns None if artist or title is missing."""
    artist = normalize_text(meta.get("artist"))
    title = normalize_text(meta.get("title"))

    if not artist or not title:
        return None
    return (artist, title)

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
            "format=duration,bit_rate:format_tags=artist,title:stream=codec_name,bit_rate,sample_rate,bits_per_sample",
            "-of", "json",
            path
        ],
        capture_output=True,
        text=True
    )

    try:
        data = json.loads(result.stdout)
    except:
        return {}

    fmt = data.get("format", {})
    tags = fmt.get("tags", {})
    streams = data.get("streams", [])
    audio = streams[0] if streams else {}

    def to_int(x):
        try:
            return int(x)
        except:
            return None

    return {
        # -- Matching fields --
        "artist": tags.get("artist"),
        "title": tags.get("title"),
        "duration": fmt.get("duration"),
        # -- quality fields --
        "codec": audio.get("codec_name"),
        "bitrate": to_int(fmt.get("bit_rate")) or to_int(audio.get("bit_rate")),
        "sample_rate": to_int(audio.get("sample_rate")),
        "bits_per_sample": to_int(audio.get("bits_per_sample")),
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
                # Back-fill fuzzy_key for records cached before this field existed
                if "fuzzy_key" not in record:
                    record["fuzzy_key"] = build_fuzzy_key({
                        "artist": record.get("artist"),
                        "title": record.get("title"),
                    })
            else:
                meta = get_metadata(full, ffprobe_bin)
                if full.startswith(root):
                    relative = full[len(root):]
                record = {
                    "path": relative,
                    "hash": sha1(full),

                    "artist": meta.get("artist"),
                    "title": meta.get("title"),
                    "duration": meta.get("duration"),
                    "key": build_key(meta),
                    "fuzzy_key": build_fuzzy_key(meta),

                    "codec": meta.get("codec"),
                    "bitrate": meta.get("bitrate"),
                    "sample_rate": meta.get("sample_rate"),
                    "bits_per_sample": meta.get("bits_per_sample"),
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
    """Build lookup maps from source snapshot files.

    Returns three dicts:
      source_hash_map:       hash       -> [source entries]
      source_key_map:        key tuple  -> [source entries]  (artist+title+duration)
      source_fuzzy_key_map:  fuzzy tuple-> [source entries]  (artist+title only)
    """
    source_hash_map = defaultdict(list)
    source_key_map = defaultdict(list)
    source_fuzzy_key_map = defaultdict(list)

    for snapshot_path in paths:
        with open(snapshot_path) as f:
            data = json.load(f)

        for r in data:
            entry = {
                "path": r["path"],
                "source": r.get("source", snapshot_path),
                "hash": r["hash"],

                "codec": r.get("codec"),
                "bitrate": r.get("bitrate"),
                "sample_rate": r.get("sample_rate"),
                "bits_per_sample": r.get("bits_per_sample"),
            }

            source_hash_map[r["hash"]].append(entry)

            if r.get("key") is not None:
                source_key_map[tuple(r["key"])].append(entry)
                # Derive fuzzy key from the full key (first two elements: artist, title)
                fuzzy_key = (r["key"][0], r["key"][1])
                source_fuzzy_key_map[fuzzy_key].append(entry)

    return source_hash_map, source_key_map, source_fuzzy_key_map


def compare(nas_records, snapshot_paths):
    """Classify NAS records against source snapshots into four tiers.

    Tiers (in priority order — a record appears in exactly one):
      exact  — SHA-1 hash matches a source file
      stale  — (artist, title, duration) key matches; hash differs
      fuzzy  — (artist, title) matches; hash and duration both differ
      unknown — no match at any tier

    Fuzzy matches are a lower-confidence fallback for tracks that were
    re-encoded with a slightly different duration. They always require
    manual review in the cleanup tool.
    """
    source_hash_map, source_key_map, source_fuzzy_key_map = load_snapshots(snapshot_paths)

    exact = []
    stale = []
    fuzzy = []
    unknown = []

    for r in nas_records:
        record = dict(r)

        if r["hash"] in source_hash_map:
            record["source_matches"] = source_hash_map[r["hash"]]
            exact.append(record)

        elif r.get("key") is not None and tuple(r["key"]) in source_key_map:
            record["source_matches"] = source_key_map[tuple(r["key"])]
            stale.append(record)

        elif r.get("fuzzy_key") is not None and tuple(r["fuzzy_key"]) in source_fuzzy_key_map:
            record["source_matches"] = source_fuzzy_key_map[tuple(r["fuzzy_key"])]
            fuzzy.append(record)

        else:
            record["source_matches"] = []
            unknown.append(record)

    return exact, stale, fuzzy, unknown

# ---------- main ----------

if __name__ == "__main__":
    NAS_ROOT = "/volume/Music"
    SNAPSHOTS = [
        "/home/nas_user/music_cleanup/source1_snapshot.json",
        "/home/nas_user/music_cleanup/source2_snapshot.json"
    ]
    OUTDIR = "./scan_results"
    FFPROBE = "/usr/local/bin/ffprobe"

    cache = load_cache()

    print("Scanning NAS...")
    records, cache = scan_library(NAS_ROOT, cache, FFPROBE)

    save_cache(cache)

    print("Comparing...")
    exact, stale, fuzzy, unknown = compare(records, SNAPSHOTS)

    print("\nResults:")
    print(f"  Exact matches:              {len(exact)}")
    print(f"  Stale matches (key only):   {len(stale)}")
    print(f"  Fuzzy matches (no duration):{len(fuzzy)}")
    print(f"  Unknown (NAS only):         {len(unknown)}")

    if not os.path.exists(OUTDIR):
        os.mkdir(OUTDIR)

    with open(os.path.join(OUTDIR, "exact.json"), "w") as f:
        json.dump(exact, f)

    with open(os.path.join(OUTDIR, "stale.json"), "w") as f:
        json.dump(stale, f)

    with open(os.path.join(OUTDIR, "fuzzy.json"), "w") as f:
        json.dump(fuzzy, f)

    with open(os.path.join(OUTDIR, "unknown.json"), "w") as f:
        json.dump(unknown, f)
