# QRVid

Encode arbitrary binary data into QR code videos for YouTube storage, and
decode it back from a downloaded video.

![QR code video preview](screenshots/preview.gif)

## How it works

1. **Chunking** — input data is split into 300-byte chunks. Each chunk gets a
   20-byte header: magic, version, flags, total chunks, index, chunk length,
   total data length, CRC32.
2. **Optional compression** — gzip level 9 (`--compress`).
3. **Optional encryption** — AES-256-GCM with PBKDF2 key derivation
   (`--password`).
4. **QR encoding** — each chunk is rendered as a QR code (error correction H /
   30%). QRs are laid out on a 1920×1080 H.264 video frame in a `cols`×`rows`
   grid (default 4×3 = 12 QRs/frame).
5. **Parallel frame generation** — frames are rendered in parallel using a
   process pool (`--workers`).
6. **Optional chunk duplication** — each chunk can be encoded multiple times at
   different positions in the video (`--recovery N`). Since the decoder
   deduplicates by payload hash, a chunk only needs one copy to survive.
7. **Video assembly** — layouts are written sequentially, each held for N
   frames (default 3 @ 30 fps). YouTube's 33-second minimum duration is
   enforced with trailing black frames.
8. **Decoding** — the video is scanned in parallel; QRs are detected with
   `pyzbar` (falling back to OpenCV). Duplicate payloads are discarded by hash.
9. **Reconstruction** — chunks are re-ordered by index, data is concatenated,
   optionally decrypted, optionally decompressed, and CRC32 is verified.
10. **YouTube download** — videos are downloaded via `yt-dlp` with Firefox
    cookies and bun JS runtime for YouTube's bot challenge.

## Requirements

- Python 3.8+
- [zbar](https://github.com/mchehab/zbar) shared library (for pyzbar)
- bun (for YouTube JS challenge solving)

### macOS (Homebrew)

```bash
brew install zbar
```

### Linux (apt)

```bash
sudo apt install libzbar0
```

### Windows

Download a pre-built zbar DLL or use `choco install zbar`.

## Install

```bash
pip install -r requirements.txt
```

## Usage

### Encode

```bash
# Basic
python qrvid.py enc myfile.bin -o myfile.mp4

# Encrypt with a password (AES-256-GCM + PBKDF2)
python qrvid.py enc myfile.bin -o myfile.mp4 --password "hunter2"

# Compress + encrypt + duplicate for YouTube reliability
python qrvid.py enc myfile.bin -o myfile.mp4 --compress --password "hunter2" \
  --cols 4 --rows 3 --recovery 1

# Pipe data via stdin
cat myfile.bin | python qrvid.py enc - -o myfile.mp4
```

### Decode

```bash
# Decode a local video
python qrvid.py dec myfile.mp4 -o restored.bin

# Decrypt with password
python qrvid.py dec myfile.mp4 -o restored.bin --password "hunter2"

# Decode from a YouTube URL (downloads first)
python qrvid.py dec "https://youtu.be/..." -o restored.bin --password "hunter2"

# Decode to stdout
python qrvid.py dec myfile.mp4 > restored.bin
```

### Options (encode)

| Flag | Default | Description |
|------|---------|-------------|
| `--fps` | 30 | Video frame rate |
| `--hold` | 3 | Frames to display each QR layout |
| `--gap` | 0 | White separator frames between layouts |
| `--box-size` | 8 | QR module size in pixels |
| `--cols` | 6 | QR code columns per frame |
| `--rows` | 5 | QR code rows per frame |
| `--max-duration` | — | Split output into segments of this many min each |
| `--workers` | cpu−1 | Parallel workers for frame generation |
| `--compress` | — | Gzip data before encoding |
| `--verify` | — | Decode output after encoding to check integrity |
| `--recovery` | 0 | Duplicate each chunk N extra times (loss recovery) |
| `-p` / `--password` | — | Encrypt with this password |

## Benchmarking

The `benchmark.py` script tests grid layouts for data loss and speed:

```bash
# All layouts with default 1 MB file
python benchmark.py

# Specific size and layouts
python benchmark.py --size 100K --layouts 6x5,5x4,4x4

# Test with extra qrvid.py flags (use -- separator)
python benchmark.py --size 1M --layouts 6x5,5x4 -- --compress --hold 1
```

Output columns: Layout, Video (bytes), Duration, Chunks, Loss (missing),
Encode time.

## Capacity

With defaults (4×3 = 12 QPF, 3 hold at 30 fps, 300 bytes/chunk, compressed):

| Duration | Frames | QRs | Raw data | Encrypted |
|---------|-------|-----|---------|-----------|
| 4 min | 7,200 | 86,400 | ~22 MB | ~22 MB |
| 15 min | 27,000 | 324,000 | ~82 MB | ~82 MB |
| 12 hours | 1,296,000 | 15,552,000 | ~3.9 GB | ~3.9 GB |

Tune `--cols`, `--rows`, `--hold`, and `--box-size` to trade density for
readability. Use `--compress` to shrink data before encoding. Use `--verify`
to check layout reliability for your specific file. Use `--recovery` to
duplicate chunks for extra loss protection.

## Format

Header (20 bytes, little-endian):

```
Offset  Size  Field
 0       4     MAGIC          "QRVD"
 4       1     format_version  2
 5       1     flags          Bit 0: gzip compressed
 6       2     total_chunks
 8       2     chunk_index
10       2     chunk_len      bytes of data in this chunk
12       4     total_len      original data length
16       4     crc32          CRC-32 of data (pre-encryption)
```

Each chunk payload = header (20) + data (max 300). Chunks are padded by
repeating the last to align with `cols × rows`.

## Disclaimer

This is a personal project for **educational purposes only**. It was not
intended to violate YouTube's Terms of Service or any other platform's
policies. Users are responsible for ensuring their use complies with all
applicable terms and laws.

## Project files

- `qrvid.py` — single-file CLI tool (subcommands: `enc`, `dec`)
- `requirements.txt` — Python dependencies
- `benchmark.py` — grid layout benchmark script
- `preview.gif` — animated preview of encoded QR grid
- `testdata/file_example_MP4_1920_18MG.mp4` — 18 MB test video
- `.context.md` — agent context / gotchas summary
