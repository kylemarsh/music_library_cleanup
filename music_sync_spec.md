# Music Library Sync — System Specification

## 1. Overview

This system reconciles a laptop's music library against a NAS music library. It identifies
which NAS files are exact copies of laptop files, which have been moved or renamed, which
appear to be stale copies of songs that have since been updated on the laptop, and which have
no counterpart on the laptop at all.

The system deliberately does not make decisions. It produces a human-readable report that a
reviewer edits, then drives a safe, reversible cleanup operation from the reviewed output.

---

## 2. Components

| Script | Runs on | Purpose |
|---|---|---|
| `snapshot.py` | Laptop | Index the laptop library; write a snapshot file |
| `scan.py` | Laptop (NAS via SMB) | Compare snapshot against NAS; write a classification report |
| `move_to_backup.py` | Laptop or NAS | Move a listed set of NAS files to a backup directory |

---

## 3. Design Principles

- **No metadata dependency.** Matching uses only file content (SHA-1 hash) and acoustic
  fingerprint (Chromaprint). No ID3/AAC tags are read or required.
- **No deletions.** Files are moved to a backup location, never deleted.
- **Dry-run by default.** Any script that modifies the filesystem prints its intended actions
  and exits unless `--apply` is passed.
- **Incremental.** Both `snapshot.py` and `scan.py` maintain a cache on disk. Files whose
  size and modification time are unchanged are not re-hashed or re-fingerprinted.
- **Deterministic output.** Given the same inputs, scripts produce identical output.
- **Single source file per script.** No shared library; scripts are self-contained.

---

## 4. Supported Audio Formats

The following file extensions are treated as audio files:

`.mp3` `.m4a` `.m4p` `.flac` `.aac` `.wav` `.alac`

All other files are silently skipped.

---

## 5. External Dependencies

| Tool | Used by | Purpose |
|---|---|---|
| `fpcalc` | `snapshot.py`, `scan.py` | Compute Chromaprint acoustic fingerprint |
| Python 3.9+ | all scripts | Runtime |

`ffprobe` is **not** required. `fpcalc` (the Chromaprint CLI) is the only external binary.

---

## 6. `snapshot.py`

### 6.1 Purpose

Walk the laptop's music library and produce a snapshot file that records the SHA-1 hash and
Chromaprint fingerprint of every audio file. The snapshot file doubles as a cache: on
subsequent runs, files whose size and mtime are unchanged are not re-processed.

### 6.2 CLI

```
snapshot.py --root <path> --snapshot <path> [--fpcalc <path>]
```

| Argument | Required | Description |
|---|---|---|
| `--root` | yes | Absolute path to the root of the laptop music library |
| `--snapshot` | yes | Path to read/write the snapshot JSON file |
| `--fpcalc` | no | Path to `fpcalc` binary (default: `fpcalc` on `$PATH`) |

### 6.3 Behavior

1. If `--snapshot` exists, load it as the current cache.
2. Walk `--root` recursively. For each audio file:
   a. Compute the file signature: `{size, mtime}`.
   b. If the file's absolute path is present in the loaded cache and its signature matches,
      reuse the cached record.
   c. Otherwise, compute the SHA-1 hash and Chromaprint fingerprint, and write a new record.
3. Write the updated snapshot to `--snapshot`, replacing any previous version atomically
   (write to a `.tmp` file, then rename).
4. Print a summary line: `Processed N files (M reused from cache, K newly scanned)`.

### 6.4 Snapshot File Format

The snapshot is a JSON object. Keys are absolute paths on the laptop. Each value holds the
file signature (for cache validation on future runs) and the computed record.

```json
{
  "/Users/kyle/Music/Artist/Album/Track.flac": {
    "sig": {
      "size": 28491022,
      "mtime": 1714500000.0
    },
    "path": "Artist/Album/Track.flac",
    "hash": "a3f1c8...",
    "fingerprint": [174931045, -2074784513, ...]
  }
}
```

#### Field reference

| Field | Type | Description |
|---|---|---|
| `sig.size` | integer | File size in bytes |
| `sig.mtime` | float | File modification time (Unix timestamp) |
| `path` | string | Path relative to `--root`, no leading slash |
| `hash` | string | Hex-encoded SHA-1 of full file contents |
| `fingerprint` | int array | Raw Chromaprint fingerprint integers (`fpcalc -raw`) |

### 6.5 Path Handling

Relative paths are computed by stripping `--root` from the absolute path. No additional
normalization is applied. Paths are stored and compared as the OS presents them.

### 6.6 Fingerprint Computation

```
fpcalc -raw -json <path>
```

The `-raw` flag returns the fingerprint as an array of signed 32-bit integers rather than a
compressed base64 string. This is required for local similarity comparison.

If `fpcalc` fails on a file (non-zero exit, unparseable output), the file is included in the
snapshot with `"fingerprint": null` and a warning is printed to stderr.

### 6.7 Hash Computation

SHA-1 over the full file contents, read in 1 MB chunks. If the file cannot be read, it is
skipped entirely and a warning is printed to stderr.

---

## 7. `scan.py`

### 7.1 Purpose

Compare every audio file on the NAS against the laptop snapshot and classify each NAS file
as `EXACT`, `RENAME`, `ORPHAN`, or `OTHER`. Write a tab-delimited report for human review.

### 7.2 CLI

```
scan.py --snapshot <path>
        --nas-root <path>
        --output <path>
        --cache <path>
        [--fpcalc <path>]
        [--fingerprint-threshold <float>]
```

| Argument | Required | Description |
|---|---|---|
| `--snapshot` | yes | Path to laptop snapshot JSON (output of `snapshot.py`) |
| `--nas-root` | yes | Absolute path to the root of the NAS music library |
| `--output` | yes | Path to write the classification report (TSV) |
| `--cache` | yes | Path to read/write the NAS scan cache JSON |
| `--fpcalc` | no | Path to `fpcalc` binary (default: `fpcalc` on `$PATH`) |
| `--fingerprint-threshold` | no | BER threshold for fingerprint matches (default: `0.35`) |

### 7.3 Behavior

1. Load the laptop snapshot from `--snapshot`.
2. Build two in-memory indexes over the snapshot records:
   - `hash_index`: `hash → [records]`
   - `fingerprint_index`: list of `(fingerprint, record)` pairs (used for similarity search)
3. Load the NAS cache from `--cache` if it exists.
4. Walk `--nas-root` recursively. For each audio file:
   a. Compute file signature.
   b. If the absolute path is in the NAS cache and signature matches, reuse the cached
      record. Otherwise compute hash and fingerprint; update the cache entry.
   c. Classify the file (see §7.5).
   d. Append a row to the report.
5. Write the updated NAS cache atomically.
6. Sort and write the report (see §7.6).
7. Print a summary: counts per classification code.

### 7.4 NAS Cache File Format

Identical in structure to the snapshot file format (§6.4). Keys are absolute NAS paths.
The cache is written to `--cache`, a separate file from the laptop snapshot.

### 7.5 Classification Logic

Classification is applied in priority order. A file receives the first classification whose
condition is satisfied.

#### EXACT

Condition: the NAS file's hash matches a laptop record's hash **and** the NAS file's
relative path matches that record's relative path **byte-for-byte**.

Path comparison is strict. Files that differ only in Unicode normalization (NFC vs NFD)
or case do **not** qualify as EXACT; they fall through to RENAME.

#### RENAME

Condition: the NAS file's hash matches at least one laptop record's hash, but no laptop
record with that hash has the same relative path as the NAS file.

#### ORPHAN

Condition: no hash match exists, but the NAS file's fingerprint matches at least one
laptop record's fingerprint with a bit error rate (BER) below `--fingerprint-threshold`.

If the NAS file has `fingerprint: null`, it cannot be classified as ORPHAN.

#### OTHER

Condition: none of the above.

### 7.6 Fingerprint Similarity

Bit error rate between two fingerprints `A` and `B`:

```
BER = popcount(A[i] XOR B[i]) summed over min(len(A), len(B)) terms
      divided by (min(len(A), len(B)) * 32)
```

Similarity score reported in the output = `1.0 - BER`. A score of `1.0` is a perfect match;
`0.0` is completely dissimilar. The default threshold of `0.35` means files with similarity
≥ `0.65` are considered matches.

When multiple laptop records meet the threshold for a given NAS file, all are included in
the output, sorted by similarity score descending.

### 7.7 Report Format

The report is a UTF-8 text file. Each line is tab-delimited with three fields:

```
CLASSIFICATION<TAB>NAS_PATH<TAB>SOURCE_FIELD
```

| Field | Description |
|---|---|
| `CLASSIFICATION` | One of `EXACT`, `RENAME`, `ORPHAN`, `OTHER` |
| `NAS_PATH` | NAS file path relative to `--nas-root`, no leading slash |
| `SOURCE_FIELD` | See below; empty for `OTHER` |

#### SOURCE_FIELD format by classification

**EXACT** — single source path:

```
Artist/Album/Track.flac
```

**RENAME** — semicolon-separated list of matching source paths (one per hash-matching laptop
file, in case the laptop itself has duplicates):

```
New/Path/Track.flac;Alt/Path/Track.flac
```

**ORPHAN** — semicolon-separated list of `path:score` pairs, sorted by score descending:

```
Artist/Album/Track.flac:0.94;Artist/Album/Track (Alt).flac:0.87
```

**OTHER** — empty string.

#### Sort order

1. Rows with a non-empty source field are sorted by source path ascending, then NAS path
   ascending. For ORPHAN rows with multiple matches, the first (highest-scoring) source path
   is used as the sort key.
2. `OTHER` rows (empty source field) follow, sorted by NAS path ascending.

#### Example

```
EXACT    Artist/Album/Track.flac           Artist/Album/Track.flac
RENAME   Old/Path/Track.flac               New/Path/Track.flac;New/Path/Track (Copy).flac
ORPHAN   Artist/Album/OldRip.mp3           Artist/Album/Track.flac:0.94;Artist/Album/Track (Alt).flac:0.87
OTHER    SomeoneElse/Album/Track.flac
```

---

## 8. `move_to_backup.py`

### 8.1 Purpose

Given a list of NAS-relative file paths, move each file to a backup directory, preserving
the relative path structure. Operates in dry-run mode by default.

### 8.2 CLI

```
move_to_backup.py --nas-root <path>
                  --backup-root <path>
                  --files <path>
                  [--apply]
```

| Argument | Required | Description |
|---|---|---|
| `--nas-root` | yes | Absolute path to the root of the NAS music library |
| `--backup-root` | yes | Absolute path to the backup directory |
| `--files` | yes | Path to a text file containing NAS-relative paths, one per line |
| `--apply` | no | Actually perform moves; default is dry-run |

### 8.3 Input File Format

Plain text, UTF-8, one NAS-relative path per line. Blank lines and lines beginning with `#`
are ignored. This format is compatible with the NAS_PATH column of the report: the reviewer
can produce the input file with a simple shell pipeline:

```bash
grep -E "^(RENAME|ORPHAN)" report.tsv | cut -f2 > to_remove.txt
```

### 8.4 Behavior

1. Read and validate `--files`. Warn and skip any path that does not exist under `--nas-root`.
2. For each path:
   - Source: `<nas-root>/<relative-path>`
   - Destination: `<backup-root>/<relative-path>`
3. In dry-run mode: print `MOVE <source> -> <destination>` for each file, then print a
   summary count. Make no filesystem changes.
4. In `--apply` mode:
   - Create destination parent directories as needed.
   - Move the file using `shutil.move()`. On the same filesystem this is an atomic rename;
     across filesystems it falls back to copy-then-delete.
   - If the destination already exists, skip the file and print a warning.
   - Print `MOVED <source> -> <destination>` for each successful move.
5. Print a final summary: total files processed, moved, skipped, warned.

---

## 9. Workflow

```
1.  snapshot.py
      --root ~/Music
      --snapshot /Volumes/NAS/music_sync/laptop_snapshot.json

2.  scan.py
      --snapshot /Volumes/NAS/music_sync/laptop_snapshot.json
      --nas-root /Volumes/NAS/Music
      --output /Volumes/NAS/music_sync/report.tsv
      --cache /Volumes/NAS/music_sync/nas_cache.json

3.  Human reviews report.tsv.
    - EXACT rows: ignored.
    - RENAME rows: verify the source path looks right; remove row to keep the NAS file.
    - ORPHAN rows: verify the match is genuine; remove row to keep the NAS file.
    - OTHER rows: no action taken by move script regardless.

4.  Generate the removal list:
      grep -E "^(RENAME|ORPHAN)" report.tsv | cut -f2 > to_remove.txt

5.  move_to_backup.py
      --nas-root /Volumes/NAS/Music
      --backup-root /Volumes/NAS/Music.bak
      --files to_remove.txt
      [--apply]

6.  Find directories left empty by the cleanup (run on NAS via SSH):
      find /volume/media/Music -mindepth 1 -type d -empty
```

---

## 10. Error Handling

| Situation | Behavior |
|---|---|
| Audio file cannot be read | Skip; print warning to stderr |
| `fpcalc` fails on a file | Include file with `fingerprint: null`; print warning to stderr |
| NAS file in `to_remove.txt` does not exist | Skip; print warning |
| Backup destination already exists | Skip that file; print warning; continue |
| Snapshot file missing when `scan.py` runs | Fatal error with clear message |
| Cache file missing or corrupt | Treat as empty cache; proceed |

---

## 11. Non-Goals

- No metadata reading or writing.
- No transcoding or re-encoding.
- No automatic deletion of any file.
- No comparison of multiple source libraries against each other.
- No quality ranking or codec preference logic.
- No fuzzy filename or folder name matching.
- No playlist awareness.
