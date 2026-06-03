# QRVid — Agent Context

## What this is

Encodes arbitrary binary data into a QR code video for YouTube storage and
decodes it back from a downloaded video.

Single-file CLI tool: `qrvid.py` with `enc` / `dec` subcommands.

## Architecture — encoding pipeline

1. Optional gzip compression (`--compress`)
2. Optional AES-256-GCM encryption (PBKDF2 key derivation, 600k iterations)
3. `build_chunks(data, flags=0)` — splits into MAX_CHUNK_DATA (480) byte
   chunks, each with a 20-byte header: `QRVD` magic, version (2), flags,
   total_chunks, chunk_index, chunk_len, total_len, CRC32
4. Each chunk → QR code (qrcode library, EC H, auto version, border=2)
5. QRs placed on a 1920×1080 white canvas in a grid (default 6×5), each in a
   centered cell
6. Frame generation is **parallel** (ProcessPoolExecutor, `--workers` flag)
7. Each layout held for N frames (`--hold 3`), white gap frames optional
8. Video written as H.264 (`avc1`) via OpenCV VideoWriter
9. YouTube minimum duration enforced with trailing black frames
10. Optional **verify pass** (`--verify`): decodes the just-encoded video and
    checks CRC to catch chunk loss immediately
11. Optional **multi-part** (`--max-duration`): splits data across multiple
    standalone `.partNN.mp4` videos

## Architecture — decoding pipeline

1. Accepts YouTube URL(s), local file(s), glob, or directory via `nargs='+'`
2. YouTube URLs downloaded with `yt-dlp` (temp files cleaned up on completion)
3. Auto-discovers multi-part sibling files (`stem.part*.mp4`)
4. Parallel frame scanning (ProcessPoolExecutor, `--workers` flag). Workers
   seek to frame ranges and decode independently. Falls back to sequential
   with checkpoint resume for small videos.
5. Skip frames where mean pixel value < 30 (black padding)
6. Decode QRs from each frame (pyzbar preferred with stderr suppressed, then
   OpenCV fallback). zbar's Latin-1→UTF-8 inflation is reversed.
7. Deduplicate by raw payload hash; parse header; collect chunks by index
8. Reorder by index, concatenate, verify CRC32 per part
9. Optionally decrypt (AES-256-GCM, MAC verified)
10. Auto-decompress if header flags indicate gzip compression

## Key decisions & gotchas

### Multi-QR grid layout (6×5 default)

Each frame holds a `cols`×`rows` grid of QR codes, each centered in its cell.
Default 6×5 gives 30 QRs/frame (~14.4 KB/frame at 480 B/chunk).
Total data scales linearly with grid size.

5×4 (20 QRs/frame) recommended for reliability with dense data — modules are
2.21 px vs 1.73 px, significantly better after H.264 compression.

### Performance

With 10 cores and 9 workers:
- Generate 1859 frames (18 MB, 5×4 layout): ~3m30s
- Verify/decode same: ~56s (parallel) / ~3m30s (sequential)

### zbar / pyzbar on macOS

Homebrew installs libzbar to `/opt/homebrew/lib/`, which isn't in the default
dynamic linker search path. `ctypes.util.find_library('zbar')` returns `None`.
The script sets `DYLD_LIBRARY_PATH` and `DYLD_FALLBACK_LIBRARY_PATH` at the
module level (before any pyzbar import) to fix this.

### zbar stderr noise

zbar's C library prints assertion messages to stderr
(`_zbar_decode_databar: Assertion "seg->finder >= 0" failed`). These are
harmless but noisy. `decode_qr_from_frame()` redirects stderr to `/dev/null`
during the zbar call to suppress them.

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
decoding fails, try reducing `--cols`/`--rows` or use `--verify` after encode
to catch losses early.

### qrcode library glog(0) bug

The `qrcode` library can crash with `ValueError: glog(0)` on certain data
patterns (e.g., all-zero files). This is a bug in the Reed-Solomon encoder
when all polynomial coefficients are zero. Always test with random data.

### Video codec

`avc1` (H.264) is used because `mp4v` fails on macOS with non-standard frame
dimensions. If re-encoding or re-muxing for specific platforms, avoid
transcoding the QR-content region to prevent data loss.

### CRC32

Computed on the data pre-compression (stored in every chunk header as part of
the chunk data segment). On decode without a password, CRC32 is verified.
With a password, GCM MAC verification is used instead. When compression is
enabled, the final decompressed CRC is verified in `--verify` mode.

### Chunk padding

When chunk count doesn't divide evenly by cols×rows, trailing frames are
filled by repeating the last chunk. The decoder deduplicates by payload hash,
so the duplicate is silently ignored.

### YouTube limits

- Minimum duration: 33 seconds (enforced with black padding)
- Maximum (unverified): 15 min / (verified): 12 h
- `--max-duration` is clamped to these bounds automatically

## Round-trip test

```bash
dd if=/dev/urandom bs=1K count=50 of=/tmp/test.bin
python qrvid.py enc /tmp/test.bin -o /tmp/test.mp4 --verify
python qrvid.py dec /tmp/test.mp4 -o /tmp/out.bin
cmp /tmp/test.bin /tmp/out.bin && echo OK
```

## Test data

`test_data.bin` — 5 KB of random bytes at the project root. Used for quick
round-trip verification.

## Project files

- `qrvid.py` — single-file CLI tool (~740 lines)
- `requirements.txt` — Python dependencies
- `test_data.bin` — test payload
- `benchmark.py` — grid layout benchmark script
- `README.md` — user-facing documentation
- `CONTEXT.md` — this file (agent context)
