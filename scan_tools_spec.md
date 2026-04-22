# Music Library Audit System — Snapshot & Scan Script Specifications

## 1. Overview

This document defines two core components of the music library audit system:

1. **snapshot_source_music_library.py** (runs on source machines)
2. **scan_library.py** (runs on the NAS)

Together, these scripts enable:

* consistent indexing of music libraries
* cross-library comparison
* duplicate detection
* preparation for reconciliation and cleanup

---

## 2. Design Principles

* **Deterministic**: identical inputs produce identical outputs
* **Portable**: snapshot output is self-contained JSON
* **Composable**: scan results can be enriched without re-scanning sources
* **Conservative matching**: avoid false positives
* **Separation of concerns**:

  * Snapshot = data collection
  * Scan = comparison + classification

---

# 3. Library Snapshot Script
snapshot_source_music_library.py

## 3.1 Purpose

Generate a **normalized, portable representation** of a source music library for later comparison against the NAS.

---

## 3.2 Inputs

* Root directory of a music library
* Optional identifier for the source (e.g., `"liz_laptop"`)

---

## 3.3 Outputs

A JSON array of records:

```json
[
  {
    "path": "...",
    "source": "...",
    "hash": "...",
    "artist": "...",
    "title": "...",
    "duration": "...",
    "key": [...],
    "codec": "...",
    "bitrate": ...,
    "sample_rate": ...,
    "bits_per_sample": ...
  }
]
```

---

## 3.4 Record Fields

### Required Fields

| Field    | Description                   |
| -------- | ----------------------------- |
| `path`   | Path relative to library root |
| `source` | Source identifier             |
| `hash`   | SHA-1 hash of file contents   |

---

### Metadata Fields (for matching)

| Field      | Description                           |
| ---------- | ------------------------------------- |
| `artist`   | Raw tag value                         |
| `title`    | Raw tag value                         |
| `duration` | Duration in seconds (string or float) |

---

### Derived Field

#### `key`

A tuple:

```python
( normalized_artist, normalized_title, rounded_duration )
```

Rules:

* Normalize text:

  * lowercase
  * strip punctuation
  * trim whitespace
* Duration rounded to integer seconds
* If either artist or title is missing:
  → `key = null`

---

### Quality Fields

| Field             | Description                        |
| ----------------- | ---------------------------------- |
| `codec`           | Audio codec (e.g., mp3, aac, flac) |
| `bitrate`         | Bits per second (integer)          |
| `sample_rate`     | Hz                                 |
| `bits_per_sample` | For lossless formats               |

---

## 3.5 Metadata Extraction

Uses `ffprobe`:

```bash
ffprobe -v error \
  -show_entries format=duration,bit_rate:format_tags=artist,title \
  -show_entries stream=codec_name,bit_rate,sample_rate,bits_per_sample \
  -of json
```

---

## 3.6 Hashing

* Algorithm: SHA-1
* Entire file contents
* Used for exact matching

---

## 3.7 Path Handling

* Stored paths are **relative to the snapshot root**
* No leading slash
* Example:

```text
Artist/Album/Track.m4a
```

---

## 3.8 Error Handling

* Files that fail metadata extraction:

  * included with missing fields
* Files that fail hashing:

  * may be skipped or flagged (implementation-defined)

---

## 3.9 Non-Goals

* No duplicate detection
* No cross-file comparison
* No mutation of files or metadata

---

# 4. Library Scan Script (NAS)
scan_library.py

## 4.1 Purpose

Scan the NAS library and compare it against one or more source snapshots to produce:

* exact matches
* stale matches (same song, different file)
* unknown files

---

## 4.2 Inputs

* NAS music root directory
* One or more snapshot JSON files

---

## 4.3 Outputs

Three JSON files:

| File           | Description               |
| -------------- | ------------------------- |
| `exact.json`   | Files with hash matches   |
| `stale.json`   | Files matched by key only |
| `unknown.json` | Files with no match       |

---

## 4.4 NAS Record Format

Each record includes:

```json
{
  "path": "...",
  "hash": "...",
  "artist": "...",
  "title": "...",
  "duration": "...",
  "key": [...],
  "codec": "...",
  "bitrate": ...,
  "sample_rate": ...,
  "bits_per_sample": ...,
  "source_matches": [...]
}
```

---

## 4.5 Source Match Structure

Each entry in `source_matches` includes:

```json
{
  "path": "...",
  "source": "...",
  "hash": "...",
  "codec": "...",
  "bitrate": ...,
  "sample_rate": ...,
  "bits_per_sample": ...
}
```

---

## 4.6 Matching Logic

### 4.6.1 Exact Match

Condition:

```python
nas.hash == source.hash
```

→ record goes to `exact.json`

---

### 4.6.2 Stale Match

Condition:

```python
nas.key == source.key
```

AND key is not null

→ record goes to `stale.json`

---

### 4.6.3 Unknown

Condition:

* no hash match
* no key match

→ record goes to `unknown.json`

---

## 4.7 Matching Precedence

1. Hash match takes priority over key match
2. A file cannot appear in multiple categories

---

## 4.8 Multi-Snapshot Handling

* All snapshots are merged into:

  * `hash → [source entries]`
  * `key → [source entries]`
* Source identity preserved via `source` field

---

## 4.9 Key Handling

* If `key == null`:

  * file cannot participate in stale matching
  * only eligible for exact match

---

## 4.10 Quality Data Usage

* Not used during matching
* Included in output for later ranking

---

## 4.11 Path Handling

* NAS paths are absolute (relative to NAS root, typically prefixed with `/`)
* Source paths remain relative

---

## 4.12 Performance Considerations

* Hashing is the most expensive operation
* Metadata extraction via `ffprobe` is second
* Caching strategies may be used but are out of scope

---

## 4.13 Error Handling

* Files failing metadata extraction:

  * included with missing fields
* Files failing hashing:

  * may be skipped or flagged

---

## 4.14 Non-Goals

* No cleanup decisions
* No file movement or deletion
* No quality comparison logic
* No conflict resolution

---

# 5. End-to-End Data Flow

```text
Source Library A ──┐
                   ├── snapshot → source_A.json
Source Library B ──┘

NAS Library ── scan → exact.json
                      stale.json
                      unknown.json

→ consumed by cleanup/reconciliation tool
```

---

# 6. Key Guarantees

* Exact matches are **content-identical**
* Stale matches are **high-confidence but not guaranteed**
* Unknown files require **manual or heuristic review**
* No false positives from missing metadata (key = null safeguard)

---

# 7. Future Extensions

* Additional tags (album, track number)
* MusicBrainz IDs
* Filename-based fallback matching
* Incremental snapshot updates
* Hash caching

---

## 8. Summary

The snapshot and scan scripts together provide a **reliable, extensible foundation** for:

* auditing music libraries
* reconciling multiple sources
* enabling safe cleanup workflows

They deliberately avoid making decisions, instead producing **rich, structured data** for downstream processing.

