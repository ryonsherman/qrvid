# QRVid — Agent Context

## What this is

Encodes arbitrary binary data into a QR code video for YouTube storage and
decodes it back from a downloaded video.

Single-file CLI tool: `qrvid.py` with `enc` / `dec` subcommands.

## Architecture — encoding pipeline

1. Optional gzip compression (`--compress`, level 9)
2. Optional AES-256-GCM encryption (PBKDF2 key derivation, 600k iterations)
3. `build_chunks(data, flags=0)` — splits into MAX_CHUNK_DATA (300) byte
   chunks, each with a 20-byte header: `QRVD` magic, version (2), flags,
   total_chunks, chunk_index, chunk_len, total_len, CRC32
4. Each chunk → QR code (qrcode library, EC H, auto version, border=2)
5. QRs placed on a 1920×1080 white canvas in a grid (default 6×5), each in a
   centered cell
6. Frame generation is **parallel** (ProcessPoolExecutor, `--workers` flag)
7. Each layout held for N frames (`--hold 3`), white gap frames optional
8. Video written as H.264 (`avc1`) via OpenCV VideoWriter
9. YouTube minimum duration enforced with trailing black frames
10. Optional **PAR2 recovery** (`--recovery N`): generates N PAR2 recovery
    blocks from the (compressed+encrypted) data, modulates them as FSK audio
    at 14400 baud via minimodem, and embeds the audio track in the video
11. Optional **verify pass** (`--verify`): decodes the just-encoded video and
    checks CRC to catch chunk loss immediately
12. Optional **multi-part** (`--max-duration`): splits data across multiple
    standalone `.partNN.mp4` videos

## Architecture — decoding pipeline

1. Accepts YouTube URL(s), local file(s), glob, or directory via `nargs='+'`
2. YouTube URLs downloaded with `yt-dlp` (bun JS runtime + firefox cookies)
3. Auto-discovers multi-part sibling files (`stem.part*.mp4`)
4. **Audio recovery**: extracts audio track, demodulates with minimodem to
    recover PAR2 recovery blocks
5. Parallel frame scanning (ProcessPoolExecutor, `--workers` flag). Workers
   seek to frame ranges and decode independently. Falls back to sequential
   with checkpoint resume for small videos.
6. Skip frames where mean pixel value < 30 (black padding)
7. Decode QRs from each frame (pyzbar preferred with stderr suppressed, then
   OpenCV fallback). zbar's Latin-1→UTF-8 inflation is reversed.
8. Deduplicate by raw payload hash; parse header; collect chunks by index
9. **PAR2 repair**: if chunks are missing, recover with PAR2 blocks from audio
10. Reorder by index, concatenate, verify CRC32 per part
11. Optionally decrypt (AES-256-GCM, MAC verified)
12. Auto-decompress if header flags indicate gzip compression

## Key decisions & gotchas

### Multi-QR grid layout (6×5 default)

Each frame holds a `cols`×`rows` grid of QR codes, each centered in its cell.
Default 6×5 gives 30 QRs/frame (~9 KB/frame at 300 B/chunk).
Total data scales linearly with grid size.

5×4 (20 QRs/frame) recommended for reliability with dense data — modules are
2.21 px vs 1.73 px, significantly better after H.264 compression.
4×3 (12 QRs/frame, 4.0 px/module) is the most reliable tested layout,
losing only 42/56213 chunks (0.075%) through YouTube re-encode.

### Performance

With 10 cores and 9 workers:
- Generate 1859 frames (18 MB, 5×4 layout): ~3m30s
- Verify/decode same: ~56s (parallel) / ~3m30s (sequential)

### PAR2 + audio recovery

PAR2 recovery blocks are generated from the compressed+encrypted data blob
and transmitted as FSK audio at 14400 baud via minimodem. The base64-encoded
data is embedded as an AAC audio track (128k) in the video.

- 7m48s video capacity: ~630KB of PAR2 data (15+ recovery blocks)
- YT-minimum 33s video capacity: ~14KB (~1 partial block)
- Recovery blocks are small enough to fit in longer videos
- `par2 create -cN` generates N recovery blocks (each ~40KB for 17MB data)

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
fitting in a 320×216 cell. The default chunk size (300) keeps QR version ≤ 19,
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

### yt-dlp JS challenge

YouTube's bot detection requires JavaScript challenge solving. The script
uses bun as the JS runtime via `--remote-components ejs:github`. Firefox
cookies (`--cookies-from-browser firefox`) are used for authenticated access.

## Round-trip test

```bash
dd if=/dev/urandom bs=1K count=50 of=/tmp/test.bin
python qrvid.py enc /tmp/test.bin -o /tmp/test.mp4 --verify --compress
python qrvid.py dec /tmp/test.mp4 -o /tmp/out.bin
cmp /tmp/test.bin /tmp/out.bin && echo OK
```

## Test data

- `testdata/file_example_MP4_1920_18MG.mp4` — 18 MB example video (free to use)

## Project files

- `qrvid.py` — single-file CLI tool (~900 lines)
- `requirements.txt` — Python dependencies
- `test_data.bin` — test payload
- `benchmark.py` — grid layout benchmark script
- `Brewfile` — Homebrew dependencies (zbar, ffmpeg, par2, minimodem)
- `preview.gif` — animated preview of encoded QR grid
- `README.md` — user-facing documentation
- `CONTEXT.md` — this file (agent context)
