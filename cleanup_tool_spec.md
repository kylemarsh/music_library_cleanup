# Music Library Reconciliation & Cleanup Tool — Specification

## 1. Overview

This tool audits and reconciles a **NAS-based music library** against one or more **source libraries**, producing a structured, human-readable, and machine-parseable report of:

* which files on the NAS should be kept, moved, or replaced
* which files in source libraries are redundant and should be deleted
* where conflicts or unknowns require manual review

The tool **does not delete files**. It generates:

* a **report** (primary output)
* optionally executes **safe move operations** on the NAS (to a backup directory)

---

## 2. Goals

### 2.1 Functional Goals

* Identify **exact duplicates** via hash
* Identify **probable duplicates** via metadata (artist/title/duration)
* Compare **quality metrics** to determine best version
* Detect **misplaced or stale files** on NAS
* Identify **non-canonical files in source libraries**

### 2.2 Safety Goals

* Never delete files automatically
* All destructive actions are:

  * explicit
  * reversible (move to backup)
* Provide a **dry-run mode by default**

### 2.3 Output Goals

* Human-readable (like `git status`)
* Tab-delimited for scripting
* Deterministic and stable across runs

---

## 3. Inputs

### 3.1 NAS Scan Output

* `exact.json`
* `stale.json`
* `unknown.json`

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

### 3.2 Source Snapshot(s)

Generated independently per source library.

Each record includes:

* `path`
* `source` (identifier)
* `hash`
* metadata fields
* quality metrics

---

## 4. Core Concepts

### 4.1 Matching Tiers

| Tier    | Method                    | Confidence |
| ------- | ------------------------- | ---------- |
| Exact   | Hash match                | High       |
| Stale   | Artist + Title + Duration | Medium     |
| Unknown | No match                  | None       |

---

### 4.2 Quality Evaluation

Quality is determined using:

1. Codec (primary signal)
2. Bitrate
3. Bit depth (lossless)
4. Sample rate

#### Codec Ranking

| Codec | Rank |
| ----- | ---- |
| flac  | 4    |
| alac  | 4    |
| wav   | 3    |
| aac   | 2    |
| mp3   | 1    |

---

### 4.3 Canonical Source Selection

When multiple source matches exist:

1. Select highest **quality score**
2. Break ties using **source priority list**

---

## 5. Output Format

### 5.1 Structure

Each line is tab-delimited:

```text
STATUS\tNAS_PATH\tDETAILS...
```

* `STATUS`: fixed-width code
* `NAS_PATH`: always present (or `-` for source-only actions)
* additional fields vary by status

---

## 6. Status Codes

### 6.1 Exact Matches

#### `OK`

* Hash matches
* Metadata matches
* Relative path matches

**Action:** keep NAS file

```text
OK\t/nas/path\t=source:/path
```

---

#### `D_MV`

* Hash matches
* Path or metadata differs

**Action:** move NAS file (allow correct version to sync)

```text
D_MV\t/nas/path\t->source:/correct/path
```

---

### 6.2 Stale Matches

#### `D_LQ`

* Same track (key match)
* NAS version is worse

**Action:** move NAS file

```text
D_LQ\t/nas/path\t->source:/better/file
```

---

#### `KEEP`

* NAS version better than any source

**Action:** keep NAS file

```text
KEEP\t/nas/path\t>source:/worse/file
```

---

#### `CNFLCT`

* Multiple source matches
* No clear best candidate

**Action:** manual review

```text
CNFLCT\t/nas/path\t?source1:/a\t?source2:/b
```

---

### 6.3 Unknown

#### `UNKNOWN`

* No match in any source

**Action:** keep (no automatic change)

```text
UNKNOWN\t/nas/path
```

---

### 6.4 Source Actions

#### `SRC_D`

* Non-canonical source file

**Action:** delete on source system

```text
SRC_D\t-\tsource:/duplicate\tKEEP source:/best
```

---

#### `SRC_AMB`

* Multiple equally valid source files

**Action:** manual resolution

```text
SRC_AMB\t-\tsource1:/fileA\tsource2:/fileB
```

---

## 7. Decision Logic

### 7.1 Exact Records

If hash matches:

* If normalized metadata AND relative path match:
  → `OK`

* Else:
  → `D_MV`

---

### 7.2 Stale Records

If key matches:

1. Compute quality score for:

   * NAS file
   * all source matches

2. Determine best source

3. Compare:

   * If source ≥ NAS:
     → `D_LQ`
   * If NAS > source:
     → `KEEP`
   * If ambiguous:
     → `CNFLCT`

---

### 7.3 Source Cleanup

If multiple source matches:

* Select canonical source
* Emit `SRC_D` for all others

If no clear winner:

* emit `SRC_AMB`

---

## 8. File Operations

### 8.1 NAS Cleanup

* Files are **moved**, not deleted
* Destination:

```text
<MusicRoot>.cleaned/
```

Example:

```text
/volume1/Music/Artist/Album/song.mp3
→
/volume1/Music.cleaned/Artist/Album/song.mp3
```

---

### 8.2 Source Cleanup

* No automatic actions
* Output only (for separate execution)

---

## 9. CLI Interface

### 9.1 Options

| Option                | Description                         |
| --------------------- | ----------------------------------- |
| `--music-root`        | NAS root (default `/volume1/Music`) |
| `--backup-root`       | backup location                     |
| `--apply`             | perform moves (default: dry-run)    |
| `--exclude`           | exclude path (repeatable)           |
| `--exclude-from-file` | file of exclusions                  |

---

### 9.2 Behavior

* Default: **dry-run**
* All actions printed before execution
* Exclusions apply to NAS paths only

---

## 10. Exclusions

Paths may be excluded via:

* CLI (`--exclude`)
* file (`--exclude-from-file`)

Matching rule:

* prefix match on NAS path

---

## 11. Non-Goals

* No automatic deletion of source files
* No metadata rewriting
* No transcoding or re-encoding
* No fuzzy filename matching (future work)

---

## 12. Future Enhancements

* Playlist-aware protection (Sonos `.m3u`)
* Filename-based fallback matching
* Duplicate clustering within NAS
* Automatic “best-of” consolidation
* Reporting UI / HTML output

---

## 13. Summary

This system provides a **safe, deterministic, and explainable** method to:

* clean a messy NAS music library
* reconcile multiple source libraries
* preserve highest-quality versions
* avoid accidental data loss

It separates:

* **matching** (what is the same song)
* **ranking** (which version is best)
* **actions** (what should be done)

This separation ensures correctness, safety, and extensibility.

