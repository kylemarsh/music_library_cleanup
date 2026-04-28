#!/usr/bin/env python3
"""
scan_library.py — Scan the NAS music library and build a recording index.

Produces two artifacts:

  music_audit_cache.json   — per-file metadata cache to speed up future runs
  index.jsonl              — one JSON object per recording (NAS + all sources)

Usage:
  python scan_library.py \\
    --root /volume1/Music \\
    --snapshots liz_snapshot.json kyle_snapshot.json \\
    [--output index.jsonl] \\
    [--cache music_audit_cache.json] \\
    [--ffprobe /usr/local/bin/ffprobe]
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import unicodedata
from collections import defaultdict

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4p", ".flac", ".aac", ".wav", ".alac"}
DEFAULT_CACHE    = "music_audit_cache.json"

# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def normalize_text(s):
    if not s:
        return ""
    s = s.lower().strip()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"\(.*?remaster.*?\)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\(.*?deluxe.*?\)",   "", s, flags=re.IGNORECASE)
    s = re.sub(r"\(.*?explicit.*?\)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\[.*?\]", "", s)
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def normalize_duration(d):
    try:
        return int(float(d))
    except Exception:
        return None

def build_key(meta):
    """Certain-match key: (artist, title, duration_seconds).
    Returns None if artist or title is missing."""
    artist = normalize_text(meta.get("artist"))
    title  = normalize_text(meta.get("title"))
    dur    = normalize_duration(meta.get("duration"))
    if not artist or not title:
        return None
    return (artist, title, dur)

def build_fuzzy_key(meta):
    """Probable-match key: (artist, title) — no duration.
    Catches re-encodes where duration shifted by a second or two."""
    artist = normalize_text(meta.get("artist"))
    title  = normalize_text(meta.get("title"))
    if not artist or not title:
        return None
    return (artist, title)

# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def is_audio_file(path):
    return os.path.splitext(path)[1].lower() in AUDIO_EXTENSIONS

def sha1(path, chunk_size=1 << 20):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()

def file_signature(path):
    s = os.stat(path)
    return {"size": s.st_size, "mtime": s.st_mtime}

# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

def get_metadata(path, ffprobe_bin):
    result = subprocess.run(
        [
            ffprobe_bin, "-v", "error",
            "-show_entries",
            "format=duration,bit_rate"
            ":format_tags=artist,title"
            ":stream=codec_name,bit_rate,sample_rate,bits_per_sample",
            "-of", "json", path,
        ],
        capture_output=True, text=True,
    )
    try:
        data = json.loads(result.stdout)
    except Exception:
        return {}

    fmt   = data.get("format", {})
    tags  = fmt.get("tags", {})
    audio = (data.get("streams") or [{}])[0]

    def to_int(x):
        try:    return int(x)
        except Exception: return None

    return {
        "artist":          tags.get("artist"),
        "title":           tags.get("title"),
        "duration":        fmt.get("duration"),
        "codec":           audio.get("codec_name"),
        "bitrate":         to_int(fmt.get("bit_rate")) or to_int(audio.get("bit_rate")),
        "sample_rate":     to_int(audio.get("sample_rate")),
        "bits_per_sample": to_int(audio.get("bits_per_sample")),
    }

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def load_cache(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def save_cache(cache, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, path)

# ---------------------------------------------------------------------------
# NAS scan
# ---------------------------------------------------------------------------

def scan_nas(root, cache, ffprobe_bin):
    """Walk the NAS root and return (records, updated_cache).

    Paths in records are relative to root with no leading separator.
    """
    records       = []
    updated_cache = {}
    scanned = reused = 0

    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            if not is_audio_file(full):
                continue

            scanned += 1
            sig = file_signature(full)

            if full in cache and cache[full]["sig"] == sig:
                reused += 1
                rec = dict(cache[full]["record"])
                # Back-fill fuzzy_key for cache entries written before this field existed
                if "fuzzy_key" not in rec:
                    rec["fuzzy_key"] = build_fuzzy_key({
                        "artist": rec.get("artist"),
                        "title":  rec.get("title"),
                    })
            else:
                meta = get_metadata(full, ffprobe_bin)
                rel  = full[len(root):].lstrip(os.sep)
                rec  = {
                    "path":            rel,
                    "hash":            sha1(full),
                    "artist":          meta.get("artist"),
                    "title":           meta.get("title"),
                    "duration":        meta.get("duration"),
                    "key":             build_key(meta),
                    "fuzzy_key":       build_fuzzy_key(meta),
                    "codec":           meta.get("codec"),
                    "bitrate":         meta.get("bitrate"),
                    "sample_rate":     meta.get("sample_rate"),
                    "bits_per_sample": meta.get("bits_per_sample"),
                }

            updated_cache[full] = {"sig": sig, "record": rec, "cached_at": time.time()}
            records.append(rec)

    print(f"  NAS: {scanned} files scanned, {reused} from cache")
    return records, updated_cache

# ---------------------------------------------------------------------------
# Source snapshot loading
# ---------------------------------------------------------------------------

def load_source_snapshots(snapshot_paths):
    """Load all source snapshot files and build lookup indexes.

    Returns:
      source_names   list of source identifiers, in snapshot order
      by_name        {source_name: [records]}
      by_hash        {hash:              [records]}
      by_key         {(artist,title,dur): [records]}
      by_fuzzy       {(artist,title):     [records]}
    """
    source_names = []
    by_name  = {}
    by_hash  = defaultdict(list)
    by_key   = defaultdict(list)
    by_fuzzy = defaultdict(list)

    for snap_path in snapshot_paths:
        with open(snap_path, encoding="utf-8") as f:
            raw = json.load(f)

        # Use the source field from the first record; fall back to filename stem
        src_name = (raw[0].get("source") if raw else None) or \
                   os.path.splitext(os.path.basename(snap_path))[0]

        if src_name not in by_name:
            source_names.append(src_name)
            by_name[src_name] = []

        for r in raw:
            r = dict(r)
            r["source"] = src_name
            by_name[src_name].append(r)

            if r.get("hash"):
                by_hash[r["hash"]].append(r)

            key = r.get("key")
            if key and key[0] and key[1]:
                by_key[tuple(key)].append(r)
                by_fuzzy[(key[0], key[1])].append(r)

    return source_names, by_name, by_hash, by_key, by_fuzzy

# ---------------------------------------------------------------------------
# Recording index — union-find grouping
# ---------------------------------------------------------------------------

class RecordingIndex:
    """Groups files from the NAS and all source libraries into recording entries.

    A recording entry represents one distinct song/recording.  Multiple files
    (possibly from different sources or left as duplicates on the NAS) may
    belong to the same entry.

    Grouping rules
    --------------
    Certain match (hash or full artist+title+duration key):
      Files are merged into the same group.  Two NAS files that both certainly
      match the same source file → one entry with a list of NAS files.

    Probable match (artist+title only, no duration):
      A new entry is always created.  If any matched source record is already
      claimed by a certain group, a 'related' cross-reference is added instead
      of merging.  We never merge on fuzzy evidence alone because two distinct
      studio versions of the same song can have different durations.

    No match:
      Each file becomes its own entry with identity_confidence "none".
    """

    def __init__(self):
        self._groups     = {}        # gid -> group dict
        self._parent     = {}        # union-find parent pointers
        self._src_to_gid = {}        # (source_name, path) -> gid
        self._nas_to_gid = {}        # nas_path -> gid
        self._next       = 0

    # -- union-find internals --

    def _new_gid(self, confidence):
        gid = self._next
        self._next += 1
        self._parent[gid] = gid
        self._groups[gid] = {
            "nas_files":    [],                 # [(record, identity_match)]
            "source_files": defaultdict(list),  # source_name -> [(record, identity_match)]
            "confidence":   confidence,
            "related":      set(),              # gids of related-but-not-merged entries
        }
        return gid

    def _find(self, gid):
        """Path-compressed root lookup."""
        while self._parent[gid] != gid:
            self._parent[gid] = self._parent[self._parent[gid]]
            gid = self._parent[gid]
        return gid

    def _union(self, gid_a, gid_b):
        """Merge gid_b into gid_a.  Returns the surviving root gid."""
        ra, rb = self._find(gid_a), self._find(gid_b)
        if ra == rb:
            return ra
        ga, gb = self._groups[ra], self._groups[rb]
        ga["nas_files"].extend(gb["nas_files"])
        for src, recs in gb["source_files"].items():
            ga["source_files"][src].extend(recs)
        ga["related"].update(gb["related"])
        if gb["confidence"] == "certain":
            ga["confidence"] = "certain"
        self._parent[rb] = ra
        del self._groups[rb]
        return ra

    def _find_for_src(self, sr):
        gid = self._src_to_gid.get((sr["source"], sr["path"]))
        return self._find(gid) if gid is not None else None

    def _find_for_nas(self, rec):
        gid = self._nas_to_gid.get(rec["path"])
        return self._find(gid) if gid is not None else None

    def _add_nas(self, gid, rec, match_type):
        if rec["path"] not in self._nas_to_gid:
            self._groups[gid]["nas_files"].append((rec, match_type))
            self._nas_to_gid[rec["path"]] = gid

    def _add_src(self, gid, sr, match_type, claim=True):
        k   = (sr["source"], sr["path"])
        src = sr["source"]
        existing = {r["path"] for r, _ in self._groups[gid]["source_files"][src]}
        if sr["path"] not in existing:
            self._groups[gid]["source_files"][src].append((sr, match_type))
        if claim:
            self._src_to_gid.setdefault(k, gid)

    # -- public interface --

    def add_certain(self, nas_rec, src_recs):
        """NAS file with certain-confidence source matches."""
        candidate_gids = set()
        for sr in src_recs:
            g = self._find_for_src(sr)
            if g is not None:
                candidate_gids.add(g)
        nas_g = self._find_for_nas(nas_rec)
        if nas_g is not None:
            candidate_gids.add(nas_g)

        if not candidate_gids:
            gid = self._new_gid("certain")
        else:
            gid = candidate_gids.pop()
            for other in candidate_gids:
                gid = self._union(gid, other)
            gid = self._find(gid)

        self._add_nas(gid, nas_rec, "certain")
        for sr in src_recs:
            self._add_src(gid, sr, "certain", claim=True)

    def add_probable(self, nas_rec, src_recs):
        """NAS file with probable-confidence (fuzzy) source matches.

        Always creates a new entry.  Adds 'related' links to any certain groups
        that already own the matched source records.
        """
        gid = self._new_gid("probable")
        self._add_nas(gid, nas_rec, "probable")

        for sr in src_recs:
            existing = self._find_for_src(sr)
            if existing is not None:
                self._groups[gid]["related"].add(existing)
                self._groups[existing]["related"].add(gid)
            # Add to this entry unclaimed (source record belongs to certain group
            # if existing is not None; we don't steal it)
            self._add_src(gid, sr, "probable", claim=(existing is None))

    def add_unknown_nas(self, nas_rec):
        """NAS file with no source match."""
        gid = self._new_gid("none")
        self._add_nas(gid, nas_rec, "none")

    def add_source_certain(self, src_recs):
        """Source-to-source certain match; no NAS file."""
        candidate_gids = set()
        for sr in src_recs:
            g = self._find_for_src(sr)
            if g is not None:
                candidate_gids.add(g)

        if not candidate_gids:
            gid = self._new_gid("certain")
        else:
            gid = candidate_gids.pop()
            for other in candidate_gids:
                gid = self._union(gid, other)
            gid = self._find(gid)

        for sr in src_recs:
            self._add_src(gid, sr, "certain", claim=True)

    def add_source_probable(self, primary_rec, other_recs):
        """Source-to-source probable match; no NAS file."""
        all_recs = [primary_rec] + other_recs
        candidate_gids = set()
        for sr in all_recs:
            g = self._find_for_src(sr)
            if g is not None:
                candidate_gids.add(g)

        if not candidate_gids:
            gid = self._new_gid("probable")
        else:
            gid = candidate_gids.pop()
            for other in candidate_gids:
                gid = self._union(gid, other)
            gid = self._find(gid)

        for sr in all_recs:
            self._add_src(gid, sr, "probable", claim=True)

    def add_source_only(self, src_rec):
        """Single source file with no matches anywhere."""
        gid = self._new_gid("none")
        self._add_src(gid, src_rec, "none", claim=True)

    def all_groups(self):
        """Yield (gid, group) for every live (non-merged) group."""
        for gid, g in self._groups.items():
            if self._find(gid) == gid:
                yield gid, g

    def claimed_sources(self):
        """Return the set of (source_name, path) keys that are claimed."""
        return set(self._src_to_gid.keys())

# ---------------------------------------------------------------------------
# Matching phases
# ---------------------------------------------------------------------------

def _dedup_src(recs):
    seen, out = set(), []
    for r in recs:
        k = (r["source"], r["path"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def match_nas_files(nas_records, by_hash, by_key, by_fuzzy, index):
    """Phases 1-3: match each NAS file against source records.

    Priority (first match wins):
      1. Hash match or full key match → certain
      2. Fuzzy key match (artist+title) → probable
      3. No match → unknown
    """
    for rec in nas_records:
        h_matches = by_hash.get(rec.get("hash"), [])

        key       = tuple(rec["key"]) if rec.get("key") else None
        k_matches = by_key.get(key, []) if key else []

        certain = _dedup_src(h_matches + k_matches)
        if certain:
            index.add_certain(rec, certain)
            continue

        fkey  = tuple(rec["fuzzy_key"]) if rec.get("fuzzy_key") else None
        fuzzy = by_fuzzy.get(fkey, []) if fkey else []
        if fuzzy:
            index.add_probable(rec, fuzzy)
            continue

        index.add_unknown_nas(rec)


def match_source_only(by_name, by_hash, by_key, by_fuzzy, index):
    """Phase 4: cross-match source files not claimed by any NAS-anchored group.

    Applies the same three tiers across source libraries.
    """
    claimed = index.claimed_sources()

    for source_name, records in by_name.items():
        for rec in records:
            k = (source_name, rec["path"])
            if k in claimed:
                continue

            # Certain: hash or key match in a *different* source library
            h_others = [r for r in by_hash.get(rec.get("hash"), [])
                        if r["source"] != source_name]
            key      = tuple(rec["key"]) if rec.get("key") else None
            k_others = [r for r in by_key.get(key, [])
                        if r["source"] != source_name] if key else []

            certain_others = _dedup_src(h_others + k_others)
            if certain_others:
                index.add_source_certain([rec] + certain_others)
                claimed.update((r["source"], r["path"]) for r in [rec] + certain_others)
                continue

            # Probable: fuzzy match in a different source library
            fkey         = tuple(rec["fuzzy_key"]) if rec.get("fuzzy_key") else None
            fuzzy_others = [r for r in by_fuzzy.get(fkey, [])
                            if r["source"] != source_name] if fkey else []
            if fuzzy_others:
                index.add_source_probable(rec, fuzzy_others)
                claimed.update((r["source"], r["path"]) for r in [rec] + fuzzy_others)
                continue

            index.add_source_only(rec)
            claimed.add(k)

# ---------------------------------------------------------------------------
# Entry serialisation
# ---------------------------------------------------------------------------

FIELDS_TO_COMPARE = [
    "artist", "title", "duration", "path",
    "codec", "bitrate", "sample_rate", "bits_per_sample",
]


def derive_entry_id(nas_recs, source_files_by_name):
    """Stable string ID for a recording entry.

    Prefers key from source records (typically more reliable tags), then NAS
    key, then path-based fallbacks.
    """
    for src_list in source_files_by_name.values():
        for r, _ in src_list:
            key = r.get("key")
            if key and key[0] and key[1]:
                dur = key[2] if key[2] is not None else "?"
                return f"{key[0]}|{key[1]}|{dur}"
    for r in nas_recs:
        key = r.get("key")
        if key and key[0] and key[1]:
            dur = key[2] if key[2] is not None else "?"
            return f"{key[0]}|{key[1]}|{dur}"
    if nas_recs:
        return f"path:nas:{nas_recs[0]['path']}"
    for src_name, src_list in source_files_by_name.items():
        if src_list:
            return f"path:{src_name}:{src_list[0][0]['path']}"
    return "unknown"


def make_file_record(rec, identity_match):
    return {
        "path":            rec.get("path"),
        "hash":            rec.get("hash"),
        "artist":          rec.get("artist"),
        "title":           rec.get("title"),
        "duration":        rec.get("duration"),
        "codec":           rec.get("codec"),
        "bitrate":         rec.get("bitrate"),
        "sample_rate":     rec.get("sample_rate"),
        "bits_per_sample": rec.get("bits_per_sample"),
        "identity_match":  identity_match,
    }


def build_fields(nas_recs, chosen_src_by_name, source_names):
    """Build the fields comparison structure.

    files.nas is always a list (or null), so fields[field]["nas"] is always a
    list (or null) for consistency.  Source values are scalars or null.
    """
    fields = {}
    for field in FIELDS_TO_COMPARE:
        values = {}
        values["nas"] = [r.get(field) for r in nas_recs] if nas_recs is not None else None
        for sn in source_names:
            rec = chosen_src_by_name.get(sn)
            values[sn] = rec.get(field) if rec is not None else None
        fields[field] = values
    return fields


def group_to_entry(gid, group, source_names, gid_to_entry_id):
    """Convert a RecordingIndex group to a serialisable index entry.

    'related' is stored as raw gids here and resolved to entry IDs in a second
    pass after all groups have been assigned IDs.
    """
    nas_recs  = [r for r, _ in group["nas_files"]]
    nas_files = [make_file_record(r, m) for r, m in group["nas_files"]] or None

    # For each source pick the best record (certain over probable)
    src_files       = {}  # sn -> file_record (for entry)
    chosen_src_recs = {}  # sn -> raw record  (for build_fields)

    for sn in source_names:
        candidates = group["source_files"].get(sn, [])
        if candidates:
            certain = [(r, m) for r, m in candidates if m == "certain"]
            r, m    = certain[0] if certain else candidates[0]
            src_files[sn]       = make_file_record(r, m)
            chosen_src_recs[sn] = r
        else:
            src_files[sn]       = None
            chosen_src_recs[sn] = None

    files    = {"nas": nas_files, **src_files}
    entry_id = derive_entry_id(nas_recs, group["source_files"])
    gid_to_entry_id[gid] = entry_id

    return {
        "id":                  entry_id,
        "identity_confidence": group["confidence"],
        "files":               files,
        "related":             list(group["related"]),   # gids — resolved below
        "fields":              build_fields(nas_recs, chosen_src_recs, source_names),
    }


def build_index(nas_records, source_names, by_name, by_hash, by_key, by_fuzzy):
    index = RecordingIndex()

    print("  Phase 1-3: matching NAS files to source libraries...")
    match_nas_files(nas_records, by_hash, by_key, by_fuzzy, index)

    print("  Phase 4: cross-matching unclaimed source files...")
    match_source_only(by_name, by_hash, by_key, by_fuzzy, index)

    print("  Serialising entries...")
    gid_to_entry_id = {}
    raw_entries     = []

    for gid, group in index.all_groups():
        entry = group_to_entry(gid, group, source_names, gid_to_entry_id)
        raw_entries.append((gid, entry))

    # Resolve raw gids in 'related' to entry ID strings
    for gid, entry in raw_entries:
        entry["related"] = sorted({
            gid_to_entry_id[rg]
            for rg in entry["related"]
            if rg in gid_to_entry_id and gid_to_entry_id.get(rg) != entry["id"]
        })

    return [e for _, e in raw_entries]

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scan NAS music library and build a recording index.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root",      required=True,
                        help="NAS music root directory")
    parser.add_argument("--snapshots", nargs="+", required=True, metavar="SNAPSHOT",
                        help="Source snapshot JSON files")
    parser.add_argument("--output",    default="index.jsonl",
                        help="Output JSONL file (default: index.jsonl)")
    parser.add_argument("--cache",     default=DEFAULT_CACHE,
                        help=f"Metadata cache file (default: {DEFAULT_CACHE})")
    parser.add_argument("--ffprobe",   default="ffprobe",
                        help="Path to ffprobe binary (default: ffprobe)")
    args = parser.parse_args()

    print("Scanning NAS...")
    cache = load_cache(args.cache)
    nas_records, cache = scan_nas(args.root, cache, args.ffprobe)
    save_cache(cache, args.cache)

    print("Loading source snapshots...")
    source_names, by_name, by_hash, by_key, by_fuzzy = load_source_snapshots(args.snapshots)
    total_src = sum(len(v) for v in by_name.values())
    print(f"  {total_src} source files across {len(source_names)} libraries: "
          + ", ".join(f"{n} ({len(by_name[n])})" for n in source_names))

    print("Building recording index...")
    entries = build_index(nas_records, source_names, by_name, by_hash, by_key, by_fuzzy)

    with open(args.output, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Summary
    conf  = defaultdict(int)
    multi = nas_only = src_only = 0
    for e in entries:
        conf[e["identity_confidence"]] += 1
        nas     = e["files"]["nas"]
        has_src = any(e["files"].get(sn) is not None for sn in source_names)
        if nas and len(nas) > 1: multi    += 1
        if nas and not has_src:  nas_only += 1
        if not nas:              src_only += 1

    print(f"\nRecording index: {len(entries)} entries → {args.output}")
    print(f"  Certain confidence:  {conf['certain']:>6}")
    print(f"  Probable confidence: {conf['probable']:>6}")
    print(f"  No match:            {conf['none']:>6}")
    print(f"  Multi-NAS entries:   {multi:>6}")
    print(f"  NAS-only entries:    {nas_only:>6}")
    print(f"  Source-only entries: {src_only:>6}")


if __name__ == "__main__":
    main()
