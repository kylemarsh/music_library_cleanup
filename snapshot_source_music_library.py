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

                # matching fields
                "artist": meta["artist"],
                "title": meta["title"],
                "duration": meta["duration"],
                "key": build_key(meta),

                # quality fields
                "codec": meta.get("codec"),
                "bitrate": meta.get("bitrate"),
                "sample_rate": meta.get("sample_rate"),
                "bits_per_sample": meta.get("bits_per_sample"),
            })

    return records

if __name__ == "__main__":
    ROOT = "/path/to/my/Music"
    OUTPUT = "kyle_snapshot.json"

    records = scan(ROOT)

    with open(OUTPUT, "w") as f:
        json.dump(records, f)

    print(f"Wrote {len(records)} records to {OUTPUT}")
