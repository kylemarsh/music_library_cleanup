#!/usr/bin/env python3
"""
scan_library.py — Scan the NAS music library, build a recording index, and
produce cleanup action files.

Produces:
  music_audit_cache.json     — per-file metadata cache (speeds up future runs)
  index.jsonl                — one entry per recording across all libraries
  decisions.jsonl            — one entry per recording with recommended actions
  nas_actions.txt            — one tab-delimited line per NAS file
  <source>_actions.txt       — one tab-delimited line per file in each source

Usage:
  python scan_library.py \\
    --root /volume1/Music \\
    --snapshots liz_snapshot.json kyle_snapshot.json \\
    --source-priority liz_laptop kyle_laptop \\
    [--output-dir ./results] \\
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

# ===========================================================================
# Part 1 — Normalisation & keys
# ===========================================================================

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
    """Probable-match key: (artist, title) — no duration."""
    artist = normalize_text(meta.get("artist"))
    title  = normalize_text(meta.get("title"))
    if not artist or not title:
        return None
    return (artist, title)

# ===========================================================================
# Part 2 — File helpers, metadata, cache
# ===========================================================================

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

# ===========================================================================
# Part 3 — NAS scan
# ===========================================================================

def scan_nas(root, cache, ffprobe_bin):
    """Walk the NAS root; return (records, updated_cache).
    Paths are relative to root with no leading separator."""
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

# ===========================================================================
# Part 4 — Source snapshot loading
# ===========================================================================

def load_source_snapshots(snapshot_paths):
    source_names = []
    by_name  = {}
    by_hash  = defaultdict(list)
    by_key   = defaultdict(list)
    by_fuzzy = defaultdict(list)

    for snap_path in snapshot_paths:
        with open(snap_path, encoding="utf-8") as f:
            raw = json.load(f)

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

# ===========================================================================
# Part 5 — Recording index (union-find grouping)
# ===========================================================================

class RecordingIndex:
    """Groups files from the NAS and all source libraries into recording entries.

    Grouping rules
    --------------
    Certain match (hash or full artist+title+duration key):
      Files are merged into the same group.  Two NAS files that both certainly
      match the same source → one entry with a list under "nas".

    Probable match (artist+title only, no duration):
      Always a new entry; cross-referenced via "related".  Never merged on
      fuzzy evidence alone — two studio versions of the same song can differ
      in duration.

    No match:
      Each file becomes its own entry with identity_confidence "none".
    """

    def __init__(self):
        self._groups     = {}
        self._parent     = {}
        self._src_to_gid = {}
        self._nas_to_gid = {}
        self._next       = 0

    def _new_gid(self, confidence):
        gid = self._next
        self._next += 1
        self._parent[gid] = gid
        self._groups[gid] = {
            "nas_files":    [],
            "source_files": defaultdict(list),
            "confidence":   confidence,
            "related":      set(),
        }
        return gid

    def _find(self, gid):
        while self._parent[gid] != gid:
            self._parent[gid] = self._parent[self._parent[gid]]
            gid = self._parent[gid]
        return gid

    def _union(self, gid_a, gid_b):
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
        src = sr["source"]
        existing = {r["path"] for r, _ in self._groups[gid]["source_files"][src]}
        if sr["path"] not in existing:
            self._groups[gid]["source_files"][src].append((sr, match_type))
        if claim:
            self._src_to_gid.setdefault((src, sr["path"]), gid)

    def add_certain(self, nas_rec, src_recs):
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
        gid = self._new_gid("probable")
        self._add_nas(gid, nas_rec, "probable")
        for sr in src_recs:
            existing = self._find_for_src(sr)
            if existing is not None:
                self._groups[gid]["related"].add(existing)
                self._groups[existing]["related"].add(gid)
            self._add_src(gid, sr, "probable", claim=(existing is None))

    def add_unknown_nas(self, nas_rec):
        gid = self._new_gid("none")
        self._add_nas(gid, nas_rec, "none")

    def add_source_certain(self, src_recs):
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
        gid = self._new_gid("none")
        self._add_src(gid, src_rec, "none", claim=True)

    def all_groups(self):
        for gid, g in self._groups.items():
            if self._find(gid) == gid:
                yield gid, g

    def claimed_sources(self):
        return set(self._src_to_gid.keys())

# ===========================================================================
# Part 6 — Matching phases
# ===========================================================================

FUZZY_DURATION_THRESHOLD = 10  # seconds; covers re-encode drift without bridging distinct recordings


def _duration_secs(rec):
    try:
        return float(rec.get("duration") or 0)
    except Exception:
        return 0.0


def _dedup_src(recs):
    seen, out = set(), []
    for r in recs:
        k = (r["source"], r["path"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def match_nas_files(nas_records, by_hash, by_key, by_fuzzy, index):
    for rec in nas_records:
        h_matches = by_hash.get(rec.get("hash"), [])
        key       = tuple(rec["key"]) if rec.get("key") else None
        k_matches = by_key.get(key, []) if key else []

        certain = _dedup_src(h_matches + k_matches)
        if certain:
            index.add_certain(rec, certain)
            continue

        fkey = tuple(rec["fuzzy_key"]) if rec.get("fuzzy_key") else None
        if fkey:
            nas_dur = _duration_secs(rec)
            fuzzy   = _dedup_src([
                r for r in by_fuzzy.get(fkey, [])
                if abs(_duration_secs(r) - nas_dur) <= FUZZY_DURATION_THRESHOLD
            ])
        else:
            fuzzy = []

        if fuzzy:
            index.add_probable(rec, fuzzy)
            continue

        index.add_unknown_nas(rec)


def match_source_only(by_name, by_hash, by_key, by_fuzzy, index):
    claimed = index.claimed_sources()

    for source_name, records in by_name.items():
        for rec in records:
            k = (source_name, rec["path"])
            if k in claimed:
                continue

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

            fkey         = tuple(rec["fuzzy_key"]) if rec.get("fuzzy_key") else None
            fuzzy_others = [r for r in by_fuzzy.get(fkey, [])
                            if r["source"] != source_name] if fkey else []
            if fuzzy_others:
                index.add_source_probable(rec, fuzzy_others)
                claimed.update((r["source"], r["path"]) for r in [rec] + fuzzy_others)
                continue

            index.add_source_only(rec)
            claimed.add(k)

# ===========================================================================
# Part 7 — Index serialisation
# ===========================================================================

FIELDS_TO_COMPARE = [
    "artist", "title", "duration", "path",
    "codec", "bitrate", "sample_rate", "bits_per_sample",
]


def derive_entry_id(nas_recs, source_files_by_name):
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
    fields = {}
    for field in FIELDS_TO_COMPARE:
        values = {}
        values["nas"] = [r.get(field) for r in nas_recs] if nas_recs is not None else None
        for sn in source_names:
            rec = chosen_src_by_name.get(sn)
            values[sn] = rec.get(field) if rec is not None else None
        fields[field] = values
    return fields


def group_to_index_entry(gid, group, source_names, gid_to_entry_id):
    nas_recs  = [r for r, _ in group["nas_files"]]
    nas_files = [make_file_record(r, m) for r, m in group["nas_files"]] or None

    src_files       = {}
    chosen_src_recs = {}

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

    entry_id = derive_entry_id(nas_recs, group["source_files"])
    gid_to_entry_id[gid] = entry_id

    return {
        "id":                  entry_id,
        "identity_confidence": group["confidence"],
        "files":               {"nas": nas_files, **src_files},
        "related":             list(group["related"]),   # raw gids; resolved below
        "fields":              build_fields(nas_recs, chosen_src_recs, source_names),
    }


def build_index(nas_records, source_names, by_name, by_hash, by_key, by_fuzzy):
    index = RecordingIndex()

    print("  Phase 1-3: matching NAS files to source libraries...")
    match_nas_files(nas_records, by_hash, by_key, by_fuzzy, index)

    print("  Phase 4: cross-matching unclaimed source files...")
    match_source_only(by_name, by_hash, by_key, by_fuzzy, index)

    print("  Serialising index entries...")
    gid_to_entry_id = {}
    raw_entries     = []

    for gid, group in index.all_groups():
        entry = group_to_index_entry(gid, group, source_names, gid_to_entry_id)
        raw_entries.append((gid, entry))

    for _, entry in raw_entries:
        entry["related"] = sorted({
            gid_to_entry_id[rg]
            for rg in entry["related"]
            if rg in gid_to_entry_id and gid_to_entry_id.get(rg) != entry["id"]
        })

    return [e for _, e in raw_entries]

# ===========================================================================
# Part 8 — Quality scoring & path utilities (for action generation)
# ===========================================================================

CODEC_RANK = {"flac": 4, "alac": 4, "wav": 3, "aac": 2, "mp3": 1}

def quality_score(rec):
    """Comparable 4-tuple; higher is better.

    Lossless (rank 4) always beats lossy (rank ≤ 3).
    Lossless tiebreak: bit depth then sample rate.
    Lossy tiebreak: bitrate.  Never compare bitrate across codecs.
    """
    codec = (rec.get("codec") or "").lower()
    rank  = CODEC_RANK.get(codec, 0)
    if rank >= 4:
        return (rank, rec.get("bits_per_sample") or 16, rec.get("sample_rate") or 44100, 0)
    return (rank, 0, 0, rec.get("bitrate") or 0)


def strip_leading_sep(p):
    return p.lstrip("/") if p else ""

def nfc(p):
    return unicodedata.normalize("NFC", p) if p else ""

def path_notes(nas_path, src_path):
    a = strip_leading_sep(nas_path or "")
    b = strip_leading_sep(src_path  or "")
    if a == b:       return []
    if nfc(a) == nfc(b): return ["PATH_NFD"]
    if a.lower() == b.lower(): return ["PATH_CASE"]
    return ["PATH_MISMATCH"]

# ===========================================================================
# Part 9 — Action generation
# ===========================================================================

COMPARABLE_FIELDS = FIELDS_TO_COMPARE   # same list, aliased for clarity

def compute_conflicts(entry, source_names):
    fields    = entry.get("fields", {})
    conflicts = []
    for field in COMPARABLE_FIELDS:
        fdata    = fields.get(field, {})
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


def find_case_collisions(entries):
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


def select_canonical(src_file_recs, source_priority):
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


def nas_action_for(nas_frec, canonical_src, is_clear, confidence,
                   collision_paths, matched_nas_by_fuzzy):
    path  = nas_frec.get("path", "")
    notes = []

    if path in collision_paths:
        notes.append("CASE_COLLISION")

    if canonical_src is None:
        if "CASE_COLLISION" in notes:
            return "CASE_COLLISION", notes
        # Check whether any other NAS file with a source match shares our
        # fuzzy key — if so this is likely a stale duplicate (renamed/moved).
        fkey = build_fuzzy_key(nas_frec)
        if fkey and fkey in matched_nas_by_fuzzy:
            dup_paths = [p for p in matched_nas_by_fuzzy[fkey] if p != path]
            if dup_paths:
                notes.append(f"LIKELY_DUP_OF:{dup_paths[0]}")
                return "ORPHAN", notes
        return "UNKNOWN", notes

    if confidence == "probable":
        notes.append("FUZZY_MATCH")
        return "CNFLCT", notes

    if not is_clear:
        return "CNFLCT", notes

    pnotes = path_notes(path, canonical_src.get("path", ""))
    notes.extend(pnotes)

    src_q = quality_score(canonical_src)
    nas_q = quality_score(nas_frec)

    if src_q >= nas_q:
        if pnotes or nas_frec.get("hash") != canonical_src.get("hash"):
            return "D_MV", notes
        return "OK", notes
    else:
        return "KEEP", notes


def source_actions_for(src_file_recs, canonical_src, is_clear, nas_action, nas_frec):
    results = []
    for sr in src_file_recs:
        notes = []
        if sr is canonical_src:
            if nas_action in ("KEEP", "CNFLCT", "CASE_COLLISION"):
                act = "SRC_AMB"
            else:
                act = "KEEP"
                if nas_frec:
                    notes.extend(path_notes(nas_frec.get("path", ""),
                                            sr.get("path", "")))
        else:
            if not is_clear:
                act = "SRC_AMB"
            elif (canonical_src is not None
                  and sr.get("hash") == canonical_src.get("hash")
                  and strip_leading_sep(sr.get("path", "")) ==
                      strip_leading_sep(canonical_src.get("path", ""))):
                act = "KEEP"
            else:
                act = "SRC_D"
        results.append((sr, act, notes))
    return results


def resolve_multi_nas(nas_frecs, canonical_src):
    """Returns {path: is_preferred} for a list of NAS files."""
    if not nas_frecs:
        return {}
    if canonical_src:
        hash_match = [r for r in nas_frecs
                      if r.get("hash") == canonical_src.get("hash")]
        preferred  = hash_match[0] if hash_match else \
                     max(nas_frecs, key=quality_score)
    else:
        preferred = max(nas_frecs, key=quality_score)
    return {r.get("path"): (r is preferred) for r in nas_frecs}


def process_entry(entry, source_names, source_priority, collision_paths,
                  matched_nas_by_fuzzy):
    nas_frecs  = entry["files"].get("nas") or []
    confidence = entry["identity_confidence"]

    src_file_recs = []
    for sn in source_names:
        frec = entry["files"].get(sn)
        if frec is not None:
            frec = dict(frec)
            frec["source"] = sn
            src_file_recs.append(frec)

    canonical_src, is_clear = select_canonical(src_file_recs, source_priority)

    # No NAS file — source-only entry
    if not nas_frecs:
        src_results = source_actions_for(src_file_recs, canonical_src, is_clear,
                                         nas_action="UNKNOWN", nas_frec=None)
        sources_out = [{**sr, "action": act, "notes": notes}
                       for sr, act, notes in src_results]
        return [{
            "id":                  entry["id"],
            "identity_confidence": confidence,
            "nas":                 None,
            "sources":             sources_out,
            "conflicts":           compute_conflicts(entry, source_names),
            "related":             entry.get("related", []),
            "fields":              entry.get("fields", {}),
        }]

    preferred_map = resolve_multi_nas(nas_frecs, canonical_src)
    decisions     = []

    for nas_frec in nas_frecs:
        is_preferred = preferred_map.get(nas_frec.get("path"), True)

        if not is_preferred:
            nas_action, nas_notes = "D_LQ", ["NAS_DUPLICATE"]
        else:
            nas_action, nas_notes = nas_action_for(
                nas_frec, canonical_src, is_clear, confidence,
                collision_paths, matched_nas_by_fuzzy)

        src_results = source_actions_for(src_file_recs, canonical_src, is_clear,
                                         nas_action=nas_action, nas_frec=nas_frec)
        sources_out = [{**sr, "action": act, "notes": notes}
                       for sr, act, notes in src_results]

        decisions.append({
            "id":                  entry["id"],
            "identity_confidence": confidence,
            "nas":                 {**nas_frec, "action": nas_action, "notes": nas_notes},
            "sources":             sources_out,
            "conflicts":           compute_conflicts(entry, source_names),
            "related":             entry.get("related", []),
            "fields":              entry.get("fields", {}),
        })

    return decisions

# ===========================================================================
# Part 10 — Output writers
# ===========================================================================

def write_jsonl(records, path):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_nas_actions(decisions, path):
    with open(path, "w", encoding="utf-8") as f:
        for d in decisions:
            nas = d.get("nas")
            if nas is None:
                continue
            line = f"{nas.get('action', '?')}\t{nas.get('path', '')}"
            notes = nas.get("notes", [])
            if notes:
                line += "\t" + " ".join(notes)
            f.write(line + "\n")


def write_source_actions(decisions, output_dir):
    # A source file can appear in multiple decision entries when a recording
    # has more than one NAS file.  Deduplicate by (source, path), keeping the
    # highest-priority action.
    #
    # Priority (highest first): KEEP > SRC_AMB > SRC_D > anything else.
    # KEEP always wins — we never want a redundant decision entry to demote a
    # file from KEEP to SRC_D.
    ACTION_PRIORITY = {"KEEP": 0, "SRC_AMB": 1, "SRC_D": 2}

    # best[(source_name, path)] = (action, notes)
    best = {}

    for d in decisions:
        for sr in d.get("sources", []):
            sn    = sr.get("source", "unknown")
            fpath = sr.get("path", "")
            act   = sr.get("action", "?")
            notes = sr.get("notes", [])
            key   = (sn, fpath)

            existing = best.get(key)
            if existing is None:
                best[key] = (act, notes)
            else:
                existing_act, existing_notes = existing
                if ACTION_PRIORITY.get(act, 99) < ACTION_PRIORITY.get(existing_act, 99):
                    best[key] = (act, notes)

    # Group by source name for writing
    by_source = defaultdict(list)
    for (sn, fpath), (act, notes) in best.items():
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

# ===========================================================================
# Part 11 — Summary
# ===========================================================================

def print_summary(index_entries, decisions, source_names):
    conf   = defaultdict(int)
    multi  = nas_only = src_only = 0
    for e in index_entries:
        conf[e["identity_confidence"]] += 1
        nas     = e["files"]["nas"]
        has_src = any(e["files"].get(sn) is not None for sn in source_names)
        if nas and len(nas) > 1: multi    += 1
        if nas and not has_src:  nas_only += 1
        if not nas:              src_only += 1

    nas_counts = defaultdict(int)
    src_counts = defaultdict(int)
    for d in decisions:
        if d.get("nas"):
            nas_counts[d["nas"].get("action", "?")] += 1
        for sr in d.get("sources", []):
            src_counts[sr.get("action", "?")] += 1

    print(f"\n{'='*50}")
    print("Recording index")
    print(f"  Total entries:       {len(index_entries):>6}")
    print(f"  Certain confidence:  {conf['certain']:>6}")
    print(f"  Probable confidence: {conf['probable']:>6}")
    print(f"  No match:            {conf['none']:>6}")
    print(f"  Multi-NAS entries:   {multi:>6}")
    print(f"  NAS-only entries:    {nas_only:>6}")
    print(f"  Source-only entries: {src_only:>6}")

    print("\nNAS actions")
    for action in sorted(nas_counts):
        print(f"  {action:<16} {nas_counts[action]:>6}")

    print("\nSource actions")
    for action in sorted(src_counts):
        print(f"  {action:<16} {src_counts[action]:>6}")

    needs_review = sum(nas_counts.get(a, 0) for a in ("CNFLCT", "CASE_COLLISION", "ORPHAN"))
    to_move      = sum(nas_counts.get(a, 0) for a in ("D_MV", "D_LQ"))
    print()
    if needs_review:
        print(f"  ⚠  {needs_review} NAS file(s) need manual review "
              f"(includes {nas_counts.get('ORPHAN', 0)} likely orphan(s)).")
    print(f"  →  {to_move} NAS file(s) flagged for move to backup.")
    print()

# ===========================================================================
# Part 12 — Main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Scan NAS music library and produce cleanup action files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", required=True,
                        help="NAS music root directory")
    parser.add_argument("--snapshots", nargs="+", required=True,
                        metavar="SNAPSHOT",
                        help="Source snapshot JSON files")
    parser.add_argument("--source-priority", nargs="+", required=True,
                        metavar="SOURCE",
                        help="Source names in descending priority order; "
                             "first is primary")
    parser.add_argument("--output-dir", default="./results",
                        help="Directory for all output files (default: ./results)")
    parser.add_argument("--cache", default=DEFAULT_CACHE,
                        help=f"Metadata cache file (default: {DEFAULT_CACHE})")
    parser.add_argument("--ffprobe", default="ffprobe",
                        help="Path to ffprobe binary (default: ffprobe)")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    # --- Scan NAS ---
    print("Scanning NAS...")
    cache = load_cache(args.cache)
    nas_records, cache = scan_nas(args.root, cache, args.ffprobe)
    save_cache(cache, args.cache)

    # --- Load sources ---
    print("Loading source snapshots...")
    source_names, by_name, by_hash, by_key, by_fuzzy = \
        load_source_snapshots(args.snapshots)
    total_src = sum(len(v) for v in by_name.values())
    print(f"  {total_src} source files across {len(source_names)} libraries: "
          + ", ".join(f"{n} ({len(by_name[n])})" for n in source_names))

    # Warn about any source names in priority list not found in snapshots
    known = set(source_names)
    for name in args.source_priority:
        if name not in known:
            print(f"  warning: --source-priority '{name}' not found in any snapshot")

    # Extend source_names with any not mentioned in priority (lowest priority)
    for name in source_names:
        if name not in args.source_priority:
            print(f"  warning: source '{name}' not in --source-priority; "
                  f"appended with lowest priority")

    # Build the ordered source list: priority order first, then any extras
    ordered_sources = list(args.source_priority) + \
                      [n for n in source_names if n not in args.source_priority]

    # --- Build index ---
    print("Building recording index...")
    index_entries = build_index(nas_records, ordered_sources,
                                by_name, by_hash, by_key, by_fuzzy)

    index_path = os.path.join(args.output_dir, "index.jsonl")
    write_jsonl(index_entries, index_path)
    print(f"  Wrote {len(index_entries)} entries → {index_path}")

    # --- Generate decisions & actions ---
    print("Generating decisions and action files...")
    collision_paths = find_case_collisions(index_entries)
    if collision_paths:
        print(f"  {len(collision_paths)} NAS path(s) involved in case collisions.")

    # Build fuzzy-key -> [nas_paths] map for NAS files that have source matches.
    # Used to detect ORPHAN files: UNKNOWN NAS files whose fuzzy key appears
    # here are likely stale duplicates of a matched file (e.g. after a rename).
    matched_nas_by_fuzzy = defaultdict(list)
    for entry in index_entries:
        has_src = any(entry["files"].get(sn) is not None for sn in ordered_sources)
        if has_src:
            for frec in (entry["files"].get("nas") or []):
                fkey = build_fuzzy_key(frec)
                if fkey:
                    matched_nas_by_fuzzy[fkey].append(frec["path"])

    all_decisions = []
    for entry in index_entries:
        all_decisions.extend(
            process_entry(entry, ordered_sources, args.source_priority,
                          collision_paths, matched_nas_by_fuzzy)
        )

    dec_path = os.path.join(args.output_dir, "decisions.jsonl")
    nas_path = os.path.join(args.output_dir, "nas_actions.txt")

    write_jsonl(all_decisions, dec_path)
    write_nas_actions(all_decisions, nas_path)
    write_source_actions(all_decisions, args.output_dir)

    print(f"\nOutput written to {args.output_dir}/")
    print(f"  index.jsonl         ({len(index_entries)} entries)")
    print(f"  decisions.jsonl     ({len(all_decisions)} entries)")
    nas_count = sum(1 for d in all_decisions if d.get("nas"))
    print(f"  nas_actions.txt     ({nas_count} lines)")
    for sn in ordered_sources:
        safe = sn.replace("/", "_").replace(" ", "_")
        n    = sum(sum(1 for sr in d.get("sources", []) if sr.get("source") == sn)
                   for d in all_decisions)
        print(f"  {safe}_actions.txt  ({n} lines)")

    # --- Summary ---
    print_summary(index_entries, all_decisions, ordered_sources)


if __name__ == "__main__":
    main()
