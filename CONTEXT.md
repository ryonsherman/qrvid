# QRVid — Agent Context

## What this is

Encodes arbitrary binary data into a QR code video for YouTube storage and
decodes it back from a downloaded video.

Single-file CLI tool: `qrvid.py` with `enc` / `dec` subcommands.

## Architecture — encoding pipeline

1. Optional AES-256-GCM encryption (PBKDF2 key derivation, 600k iterations)
2. `build_chunks(data)` — splits into MAX_CHUNK_DATA (480) byte chunks, each
   with a 19-byte header: `QRVD` magic, version, total_chunks, chunk_index,
   chunk_len, total_len, CRC32
3. Each chunk → QR code (qrcode library, EC H, auto version, border=2)
4. QRs placed on a 1920×1080 white canvas in a grid (default 6×5), each in a centered cell
5. Each layout held for N frames (`--hold 3`), white gap frames optional
6. Video written as H.264 (`avc1`) via OpenCV VideoWriter

## Architecture — decoding pipeline

1. If input is a YouTube URL, download with `yt-dlp` first
2. Iterate frames; skip frames where mean pixel value < 30 (black padding)
3. Decode QRs from each frame (pyzbar preferred, OpenCV fallback)
4. Deduplicate by raw payload hash; parse header; collect chunks by index
5. Reorder by index, concatenate, verify CRC32
6. Optionally decrypt (AES-256-GCM, MAC verified)

## Key decisions & gotchas

### Multi-QR grid layout (2×2 default)

Each frame holds a `cols`×`rows` grid of QR codes, each centered in its cell.
Default 6×5 gives 30 QRs/frame (~14.4 KB/frame at 480 B/chunk).
Total data scales linearly with grid size.

### zbar / pyzbar on macOS

Homebrew installs libzbar to `/opt/homebrew/lib/`, which isn't in the default
dynamic linker search path. `ctypes.util.find_library('zbar')` returns `None`.
The script sets `DYLD_LIBRARY_PATH` and `DYLD_FALLBACK_LIBRARY_PATH` at the
module level (before any pyzbar import) to fix this.

### zbar Latin-1 → UTF-8 conversion

zbar treats byte-mode QR data bytes > 127 as Latin-1 codepoints and converts
them to UTF-8 internally. This inflates any chunk with bytes ≥0x80 (1 byte →
2 bytes). The decoder reverses this via `d.decode('utf-8').encode('latin-1')`
on every pyzbar result. *Do not remove this step.*

### OpenCV QRCodeDetector API

`cv2.QRCodeDetector().detectAndDecode(gray)` returns
`(decoded_str, points, straight_qrcode)`. Index `[0]` is the decoded text
(empty string `""` when nothing found), **not** a boolean. Do not unpack as
`ok, decoded, *_` — that gives you `ok = decoded_str` and `decoded = points`
(a numpy array or None), which will fail on truthiness checks.

### PyCryptodome GCM nonce size

`AES.new(key, AES.MODE_GCM)` generates a **16-byte** default nonce (as of
PyCryptodome 3.x on macOS ARM64). `GCM_NONCE_SIZE` must match this. If you
upgrade PyCryptodome, verify the default nonce length hasn't changed.

### Multi-QR layout & scaling

QR codes are auto-scaled to fit their cell
(`(vw // cols - 20, vh // rows - 20)`). With `--box-size 8` and the default
6×5 grid, each QR is ~936 px after nearest-neighbor upscale from ~552 px
fitting in a 320×216 cell. The default chunk size (480) keeps QR version ≤ 24,
which keeps modules large enough to survive H.264 compression.

Max QR dimension printed during encoding gives a rough readability check. If
decoding fails, try reducing `--box-size` or `--cols`/`--rows`, or lowering
MAX_CHUNK_DATA in the code.

### Video codec

`avc1` (H.264) is used because `mp4v` fails on macOS with non-standard frame
dimensions. If re-encoding or re-muxing for specific platforms, avoid
transcoding the QR-content region to prevent data loss.

### CRC32

Computed on the original data pre-encryption (stored in every chunk header).
On decode without a password, CRC32 is verified. With a password, GCM MAC
verification is used instead.

### Chunk padding

When chunk count doesn't divide evenly by cols×rows, trailing frames are
filled by repeating the last chunk. The decoder deduplicates by payload hash,
so the duplicate is silently ignored.

## Round-trip test

```bash
dd if=/dev/urandom bs=1K count=50 of=/tmp/test.bin
python qrvid.py enc /tmp/test.bin -o /tmp/test.mp4
python qrvid.py dec /tmp/test.mp4 -o /tmp/out.bin
cmp /tmp/test.bin /tmp/out.bin && echo OK
```

## Test data

`test_data.bin` — 5 KB of random bytes at the project root. Used for quick
round-trip verification.

## Project files

- `qrvid.py` — single-file CLI tool (~390 lines)
- `requirements.txt` — Python dependencies
- `test_data.bin` — test payload
- `README.md` — user-facing documentation
- `CONTEXT.md` — this file (agent context)
