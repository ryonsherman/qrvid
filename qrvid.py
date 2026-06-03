#!/usr/bin/env python3
import struct
import zlib
import argparse
import sys
import os
import time
import tempfile
import json
import glob
import re

_brew_lib = '/opt/homebrew/lib'
if os.path.isdir(_brew_lib):
    os.environ.setdefault('DYLD_LIBRARY_PATH', _brew_lib)
    os.environ.setdefault('DYLD_FALLBACK_LIBRARY_PATH', _brew_lib)

import warnings
warnings.filterwarnings('ignore', category=UserWarning,
                        module='multiprocessing.resource_tracker')

import cv2
import numpy as np
from PIL import Image
import qrcode
import qrcode.constants
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Random import get_random_bytes

MAGIC = b'QRVD'
FORMAT_VERSION = 2
HEADER_FORMAT = '<4sBBHHHII'
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
COMPRESSED_FLAG = 1

QR_MAX_PAYLOAD = 1273
MAX_CHUNK_DATA = 480

BLACK_THRESHOLD = 30
YT_MIN_SECONDS = 33
YT_UNVERIFIED_MAX = 15 * 60  # 15 min for unverified accounts
YT_VERIFIED_MAX = 12 * 60 * 60  # 12 h for verified accounts

PBKDF2_SALT_SIZE = 16
GCM_NONCE_SIZE = 16
GCM_TAG_SIZE = 16
ENC_OVERHEAD = PBKDF2_SALT_SIZE + GCM_NONCE_SIZE + GCM_TAG_SIZE


def encrypt_data(data, password):
    salt = get_random_bytes(PBKDF2_SALT_SIZE)
    key = PBKDF2(password, salt, dkLen=32, count=600_000)
    cipher = AES.new(key, AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(data)
    return salt + cipher.nonce + tag + ciphertext


def decrypt_data(encrypted, password):
    if len(encrypted) < ENC_OVERHEAD:
        raise ValueError("Encrypted data too short")
    salt = encrypted[:PBKDF2_SALT_SIZE]
    nonce = encrypted[PBKDF2_SALT_SIZE:PBKDF2_SALT_SIZE + GCM_NONCE_SIZE]
    tag = encrypted[PBKDF2_SALT_SIZE + GCM_NONCE_SIZE:ENC_OVERHEAD]
    ciphertext = encrypted[ENC_OVERHEAD:]
    key = PBKDF2(password, salt, dkLen=32, count=600_000)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag)


def build_chunks(data, flags=0):
    total_len = len(data)
    crc = zlib.crc32(data)
    total_chunks = (total_len + MAX_CHUNK_DATA - 1) // MAX_CHUNK_DATA
    chunks = []
    for i in range(0, total_len, MAX_CHUNK_DATA):
        chunk_data = data[i:i + MAX_CHUNK_DATA]
        header = struct.pack(
            HEADER_FORMAT,
            MAGIC,
            FORMAT_VERSION,
            flags,
            total_chunks,
            len(chunks),
            len(chunk_data),
            total_len,
            crc,
        )
        chunks.append(header + chunk_data)
    return chunks


def make_qr_image(payload, box_size=8):
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=box_size,
        border=2,
    )
    qr.add_data(payload, optimize=0)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    return np.array(img.convert('RGB'))


def parse_header(payload):
    if len(payload) < HEADER_SIZE:
        return None
    fields = struct.unpack(HEADER_FORMAT, payload[:HEADER_SIZE])
    magic, ver, flags, total_chunks, idx, chunk_len, total_data_len, crc = fields
    if magic != MAGIC:
        return None
    chunk_data = payload[HEADER_SIZE:HEADER_SIZE + chunk_len]
    if len(chunk_data) != chunk_len:
        return None
    return {
        'version': ver,
        'flags': flags,
        'total_chunks': total_chunks,
        'chunk_index': idx,
        'chunk_len': chunk_len,
        'total_data_len': total_data_len,
        'crc': crc,
        'data': chunk_data,
    }


def is_black_frame(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return gray.mean() < BLACK_THRESHOLD


def decode_qr_from_frame(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    try:
        from pyzbar.pyzbar import decode as zbar_decode
        _err = os.dup(2)
        _null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(_null, 2)
        os.close(_null)
        try:
            results = zbar_decode(gray)
        finally:
            os.dup2(_err, 2)
            os.close(_err)
        if results:
            out = []
            for r in results:
                d = r.data
                if len(d) > 0:
                    try:
                        d = d.decode('utf-8').encode('latin-1')
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        pass
                    out.append(d)
            return out
    except ImportError:
        pass
    detector = cv2.QRCodeDetector()
    decoded = detector.detectAndDecode(gray)[0]
    if decoded and isinstance(decoded, str) and len(decoded) > 0:
        try:
            return [decoded.encode('latin-1')]
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return []


# ---------------------------------------------------------------------------
# COMMAND: enc
# ---------------------------------------------------------------------------

def make_frame_image(chunks, fi, qpf, cols, rows, box, vw=1920, vh=1080):
    cell_w = vw // cols
    cell_h = vh // rows
    white = np.ones((vh, vw, 3), dtype=np.uint8) * 255
    canvas = white.copy()
    for pos in range(qpf):
        qi = fi * qpf + pos
        qr_img = make_qr_image(chunks[qi], box)
        qh, qw = qr_img.shape[:2]
        scale = min((cell_w - 20) / qw, (cell_h - 20) / qh)
        nw = max(4, int(qw * scale // 2 * 2))
        nh = max(4, int(qh * scale // 2 * 2))
        if (nw, nh) != (qw, qh):
            qr_img = cv2.resize(qr_img, (nw, nh), interpolation=cv2.INTER_NEAREST)
            qh, qw = qr_img.shape[:2]
        col = pos % cols
        row = pos // cols
        cx = col * cell_w + (cell_w - qw) // 2
        cy = row * cell_h + (cell_h - qh) // 2
        canvas[cy:cy + qh, cx:cx + qw] = qr_img
    return canvas


def part_path(base, num, total):
    if total == 1:
        return base
    root, ext = os.path.splitext(base)
    return f"{root}.part{num:02d}{ext}"


def fmt_dur(secs):
    secs = int(secs)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    if h:
        return f"{h}h{m}m{s}s"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


def _render_frame(args):
    chunk_slice, qpf, cols, rows, box, vw, vh, frame_path = args
    canvas = make_frame_image(chunk_slice, 0, qpf, cols, rows, box, vw, vh)
    cv2.imwrite(frame_path, canvas)
    return frame_path


def encode_video(chunks, output_path, fps, hold, gap, box, cols, rows,
                 vw=1920, vh=1080, workers=None):
    qpf = cols * rows
    pad = (qpf - len(chunks) % qpf) % qpf
    for _ in range(pad):
        chunks.append(chunks[-1])

    frames_needed = len(chunks) // qpf
    yt_frames = YT_MIN_SECONDS * fps
    qr_frames = frames_needed * hold
    sep_frames = frames_needed * gap + gap * 10
    total_frames = qr_frames + sep_frames
    yt_pad = max(0, yt_frames - total_frames)

    tmp_dir = output_path + '.qrvid_frames'
    os.makedirs(tmp_dir, exist_ok=True)

    if workers is None:
        workers = max(1, multiprocessing.cpu_count() - 1)

    # Validate existing PNGs (stale files from aborted runs)
    for fi in range(frames_needed):
        frame_path = os.path.join(tmp_dir, f'{fi:06d}.png')
        if os.path.exists(frame_path):
            img = cv2.imread(frame_path)
            if img is None or img.shape != (vh, vw, 3):
                os.unlink(frame_path)

    existing = sum(1 for fi in range(frames_needed)
                   if os.path.exists(os.path.join(tmp_dir, f'{fi:06d}.png')))

    print(f"  Generating frames ({frames_needed} layouts, {workers} workers)...")
    pending = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for fi in range(frames_needed):
            frame_path = os.path.join(tmp_dir, f'{fi:06d}.png')
            if os.path.exists(frame_path):
                continue
            start = fi * qpf
            chunk_slice = list(chunks[start:start + qpf])
            pending.append(pool.submit(
                _render_frame,
                (chunk_slice, qpf, cols, rows, box, vw, vh, frame_path),
            ))

        done_count = existing
        t0 = time.time()
        last_print = existing
        for fut in as_completed(pending):
            fut.result()
            done_count += 1
            if done_count - last_print >= 10 or done_count == frames_needed:
                last_print = done_count
                elapsed = time.time() - t0
                completed = done_count - existing
                rate = completed / elapsed if elapsed > 0 else 0
                remaining = len(pending) - completed
                eta = remaining / rate if rate > 0 else 0
                print(f"    Frame {done_count}/{frames_needed}  "
                      f"[{fmt_dur(elapsed)} elapsed, ETA {fmt_dur(eta)}]")

    print(f"  Assembling video...")
    fourcc = cv2.VideoWriter_fourcc(*'avc1')
    out = cv2.VideoWriter(output_path, fourcc, fps, (vw, vh))

    black = np.zeros((vh, vw, 3), dtype=np.uint8)
    white = np.ones((vh, vw, 3), dtype=np.uint8) * 255

    for _ in range(gap * 5):
        out.write(white)

    for fi in range(frames_needed):
        if gap > 0:
            for _ in range(gap):
                out.write(white)

        frame_path = os.path.join(tmp_dir, f'{fi:06d}.png')
        canvas = cv2.imread(frame_path)
        for _ in range(hold):
            out.write(canvas)

    for _ in range(gap * 5):
        out.write(black)

    for _ in range(yt_pad):
        out.write(black)

    out.release()

    import shutil
    shutil.rmtree(tmp_dir)

    total_frames += yt_pad
    est_s = total_frames / fps
    size = os.path.getsize(output_path)
    print(f"  Saved: {output_path} ({size} bytes, {fmt_dur(est_s)})")


def max_chunks_per_video(qpf, hold, gap, fps, max_duration_sec):
    yt_min_frames = YT_MIN_SECONDS * fps
    max_frames = int(max_duration_sec * fps)

    def total_frames(uniq):
        return max(yt_min_frames, gap * 10 + uniq * (hold + gap))

    lo, hi = 0, 10 ** 9
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if total_frames(mid) <= max_frames:
            lo = mid
        else:
            hi = mid - 1

    return lo * qpf


def cmd_enc(args):
    if args.input == '-':
        data = sys.stdin.buffer.read()
    else:
        with open(args.input, 'rb') as f:
            data = f.read()

    flags = 0
    data_to_chunk = data
    if args.compress:
        import gzip
        data_to_chunk = gzip.compress(data_to_chunk, compresslevel=9)
        flags |= COMPRESSED_FLAG
        ratio = len(data_to_chunk) * 100 // len(data)
        print(f"Compressed: {len(data)} → {len(data_to_chunk)} bytes ({ratio}%)")
    if args.password:
        print(f"Encrypting {len(data_to_chunk)} bytes (PBKDF2 + AES-256-GCM)...")
        data_to_chunk = encrypt_data(data_to_chunk, args.password)

    full_crc = zlib.crc32(data)
    full_len = len(data)
    cols, rows = args.cols, args.rows
    qpf = cols * rows
    box = args.box_size
    vw, vh = 1920, 1080
    data_per_chunk = MAX_CHUNK_DATA

    total_chunks = (len(data_to_chunk) + data_per_chunk - 1) // data_per_chunk

    print(f"Total data: {full_len} bytes {'(encrypted)' if args.password else ''}")
    print(f"CRC32: {full_crc:08x}")
    print(f"Chunks: {total_chunks}, Layout: {cols}x{rows} ({qpf} QRs/frame)")

    max_mod = max(make_qr_image(b'\0' * HEADER_SIZE, box).shape[:2])
    print(f"Max QR dimension: {max_mod}px")

    frames_needed = (total_chunks + qpf - 1) // qpf
    vid_frames = max(YT_MIN_SECONDS * args.fps,
                     frames_needed * args.hold + frames_needed * args.gap)
    vid_dur = vid_frames / args.fps
    print(f"Video: {vid_frames} frames, {fmt_dur(vid_dur)} @ {args.fps} FPS")

    workers = args.workers if args.workers is not None else max(1, multiprocessing.cpu_count() - 1)

    if args.max_duration:
        max_sec = args.max_duration * 60
        if max_sec < YT_MIN_SECONDS:
            print(f"Clamping max-duration to {YT_MIN_SECONDS}s (YouTube minimum)")
            max_sec = YT_MIN_SECONDS
        elif max_sec > YT_VERIFIED_MAX:
            print(f"Clamping max-duration to {YT_VERIFIED_MAX // 3600}h (YouTube absolute max)")
            max_sec = YT_VERIFIED_MAX
        elif max_sec > YT_UNVERIFIED_MAX:
            print(f"Warning: {args.max_duration}min exceeds 15 min unverified YouTube limit")
        max_chunks = max_chunks_per_video(qpf, args.hold, args.gap, args.fps,
                                          max_sec)
        max_bytes = max_chunks * data_per_chunk
        nparts = (len(data_to_chunk) + max_bytes - 1) // max_bytes
        if nparts > 1:
            print(f"Max duration: {args.max_duration} min → {nparts} files")
            for pi in range(nparts):
                start = pi * max_bytes
                end = min(start + max_bytes, len(data_to_chunk))
                segment = data_to_chunk[start:end]
                part_chunks = build_chunks(segment, flags=flags)
                out_path = part_path(args.output, pi + 1, nparts)
                print(f"\nPart {pi + 1}/{nparts} ({len(segment)} bytes, "
                      f"{len(part_chunks)} chunks)")
                encode_video(part_chunks, out_path, args.fps, args.hold,
                             args.gap, box, cols, rows, vw, vh,
                             workers=workers)
            print(f"\nAll {nparts} files saved.")
            return
        print(f"Fits within {args.max_duration} min — single file")

    chunks = build_chunks(data_to_chunk, flags=flags)
    encode_video(chunks, args.output, args.fps, args.hold, args.gap, box,
                 cols, rows, vw, vh, workers=workers)

    if args.verify:
        print(f"\n  Verifying {args.output}...")
        try:
            v_data, v_flags = decode_video(args.output, workers=workers)
            if args.password:
                v_data = decrypt_data(v_data, args.password)
            if v_flags & COMPRESSED_FLAG:
                import gzip
                v_data = gzip.decompress(v_data)
            v_crc = zlib.crc32(v_data)
            if v_crc == full_crc and len(v_data) == full_len:
                print(f"  Verify OK ({len(v_data)} bytes, CRC: {v_crc:08x})")
            else:
                print(f"  Verify FAILED: CRC {v_crc:08x} (expected {full_crc:08x})")
        except RuntimeError as e:
            print(f"  Verify FAILED: {e}")


# ---------------------------------------------------------------------------
# COMMAND: dec
# ---------------------------------------------------------------------------

def ensure_video_file(input_arg):
    if input_arg.startswith(('http://', 'https://', 'www.')):
        print(f"Downloading video from: {input_arg}")
        import yt_dlp
        tmp = tempfile.mktemp(suffix='.mp4')
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'merge_output_format': 'mp4',
            'outtmpl': tmp,
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([input_arg])
        print(f"Downloaded to: {tmp}")
        return tmp
    return input_arg


def resolve_video_files(input_arg):
    if input_arg.startswith(('http://', 'https://', 'www.')):
        dl = ensure_video_file(input_arg)
        return [dl], [dl]

    if os.path.isdir(input_arg):
        files = sorted(glob.glob(os.path.join(input_arg, '*.mp4')))
        if not files:
            raise RuntimeError(f"No .mp4 files in {input_arg}")
        return files, []

    if '*' in input_arg or '?' in input_arg:
        files = sorted(glob.glob(input_arg))
        if not files:
            raise RuntimeError(f"No files match: {input_arg}")
        return files, []

    video_path = ensure_video_file(input_arg)
    is_temp = video_path != input_arg

    base = os.path.splitext(video_path)[0]
    dirname = os.path.dirname(video_path) or '.'
    basename = os.path.basename(base)

    m = re.match(r'^(.+)\.part\d+$', basename)
    stem = m.group(1) if m else basename

    pattern = os.path.join(dirname, f'{stem}.part*.mp4')
    parts = sorted(glob.glob(pattern))

    if len(parts) > 1:
        return parts, []

    return [video_path], [video_path] if is_temp else []


def _decode_frame_range(args):
    video_path, start, end = args
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    found = []
    fi = start
    while fi < end:
        ret, frame = cap.read()
        if not ret:
            break
        if not is_black_frame(frame):
            for raw in decode_qr_from_frame(frame):
                found.append(raw)
        fi += 1
    cap.release()
    return found


def decode_video(video_path, workers=None):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    chunks = {}
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()

    dur = total_frames / fps
    print(f"\nVideo: {video_path}")
    print(f"Video: {total_frames} frames, {fmt_dur(dur)} @ {fps:.0f} FPS")

    if workers is None:
        workers = max(1, multiprocessing.cpu_count() - 1)

    chk_path = video_path + '.qrvid_chk'
    start_frame = 0
    seen_payloads = set()
    if os.path.exists(chk_path):
        with open(chk_path) as _f:
            saved = json.load(_f)
            start_frame = saved.get('frame_idx', 0)
            for raw in saved.get('found', []):
                raw_bytes = bytes.fromhex(raw)
                seen_payloads.add(raw_bytes)
                info = parse_header(raw_bytes)
                if info and info['chunk_index'] not in chunks:
                    chunks[info['chunk_index']] = info
        if start_frame > 0:
            print(f"  Resuming from frame {start_frame} ({len(chunks)} chunks cached)...")

    remaining = total_frames - start_frame
    all_payloads = []
    t0 = time.time()

    if workers > 1 and remaining > 100:
        chunk_size = max(1, remaining // workers)
        ranges = [(video_path, i, min(i + chunk_size, total_frames))
                  for i in range(start_frame, total_frames, chunk_size)]
        done_ranges = 0
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for result in pool.map(_decode_frame_range, ranges):
                all_payloads.extend(result)
                done_ranges += 1
                pct = done_ranges * 100 // len(ranges)
                elapsed = time.time() - t0
                print(f"  Progress: ~{pct}% ({len(all_payloads)} payloads)  "
                      f"[{fmt_dur(elapsed)} elapsed]")
    else:
        cap = cv2.VideoCapture(video_path)
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        frame_idx = start_frame
        last_progress = 0
        new_chunks_in_window = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if not is_black_frame(frame):
                for raw in decode_qr_from_frame(frame):
                    if raw not in seen_payloads:
                        all_payloads.append(raw)
                        seen_payloads.add(raw)
                        new_chunks_in_window += 1
            frame_idx += 1
            pct = frame_idx * 100 // total_frames if total_frames > 0 else 0
            if pct >= last_progress + 10:
                last_progress = pct
                elapsed = time.time() - t0
                rate = (frame_idx - start_frame) / elapsed if elapsed > 0 else 0
                remaining_frames = total_frames - frame_idx
                eta = remaining_frames / rate if rate > 0 else 0
                print(f"  Progress: {pct}% ({len(all_payloads)} payloads)  "
                      f"[{fmt_dur(elapsed)} elapsed, ETA {fmt_dur(eta)}]")
            if new_chunks_in_window > 0 and frame_idx % 100 == 0:
                with open(chk_path, 'w') as _f:
                    json.dump({
                        'frame_idx': frame_idx,
                        'found': [p.hex() for p in seen_payloads],
                    }, _f)
                new_chunks_in_window = 0
        cap.release()
        if os.path.exists(chk_path):
            os.unlink(chk_path)

    parsed = set()
    for raw in all_payloads:
        if raw in parsed:
            continue
        parsed.add(raw)
        info = parse_header(raw)
        if info and info['chunk_index'] not in chunks:
            chunks[info['chunk_index']] = info

    if not chunks:
        raise RuntimeError("No valid QR codes found in video")

    sample = next(iter(chunks.values()))
    total = sample['total_chunks']
    total_data_len = sample['total_data_len']
    expected_crc = sample['crc']
    flags = sample.get('flags', 0)

    if len(chunks) != total:
        missing = sorted(set(range(total)) - set(chunks.keys()))
        print(f"  Found {len(chunks)}/{total} chunks")
        raise RuntimeError(f"Missing {len(missing)} chunks: "
                           f"{missing[:20]}{'...' if len(missing) > 20 else ''}")

    ordered = [chunks[i] for i in range(total)]
    for ch in ordered:
        if ch['total_chunks'] != total:
            raise RuntimeError("Inconsistent total_chunks across chunks")

    data = b''.join(ch['data'] for ch in ordered)
    data = data[:total_data_len]

    actual_crc = zlib.crc32(data)
    if actual_crc != expected_crc:
        raise RuntimeError(
            f"CRC32 mismatch: expected {expected_crc:08x}, got {actual_crc:08x}")

    print(f"  Reconstructed: {len(data)} bytes (CRC32: {expected_crc:08x} ✓)")
    return data, flags


def cmd_dec(args):
    seen = set()
    files = []
    temp_files = []
    for inp in args.input:
        f, tf = resolve_video_files(inp)
        for path in f:
            abspath = os.path.abspath(path)
            if abspath not in seen:
                seen.add(abspath)
                files.append(path)
        temp_files.extend(tf)

    if not files:
        raise RuntimeError("No input video files found")

    print(f"Files to decode: {len(files)}")

    all_data = b''
    data_flags = 0
    workers = args.workers if args.workers is not None else max(1, multiprocessing.cpu_count() - 1)

    for fi, f in enumerate(files):
        if len(files) > 1:
            print(f"[{fi + 1}/{len(files)}]", end="")
        segment, seg_flags = decode_video(f, workers=workers)
        all_data += segment
        data_flags |= seg_flags

    for tf in temp_files:
        if os.path.exists(tf):
            os.unlink(tf)

    if args.password:
        print(f"\nDecrypting ({len(all_data)} bytes)...")
        all_data = decrypt_data(all_data, args.password)

    if data_flags & COMPRESSED_FLAG:
        import gzip
        all_data = gzip.decompress(all_data)
        print(f"Decompressed: {len(all_data)} bytes")

    print(f"\nReconstructed: {len(all_data)} bytes ✓")
    if args.password:
        print("Decryption: OK (AES-256-GCM)")

    if args.output:
        with open(args.output, 'wb') as f:
            f.write(all_data)
        print(f"Written to {args.output}")
    else:
        sys.stdout.buffer.write(all_data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description='QRVid — encode/decode binary data as QR code videos')
    sub = parser.add_subparsers(dest='command', required=True)

    enc = sub.add_parser('enc', help='Encode file into QR video')
    enc.add_argument('input', help='Input file path (use - for stdin)')
    enc.add_argument('-o', '--output', default='output.mp4',
                     help='Output video path')
    enc.add_argument('-p', '--password', help='Encrypt with this password')
    enc.add_argument('--fps', type=int, default=30,
                     help='Video frame rate')
    enc.add_argument('--hold', type=int, default=3,
                     help='Video frames to display each QR layout (default 3 @30fps = 0.1s)')
    enc.add_argument('--gap', type=int, default=0,
                     help='White separator frames between QR layouts')
    enc.add_argument('--box-size', type=int, default=8,
                     help='QR code module size in pixels (smaller = more dense)')
    enc.add_argument('--cols', type=int, default=6,
                     help='QR code columns per frame (default 6)')
    enc.add_argument('--rows', type=int, default=5,
                     help='QR code rows per frame (default 5)')
    enc.add_argument('--max-duration', type=float, default=None,
                     help='Split output into segments of this many minutes each')
    enc.add_argument('--workers', type=int, default=None,
                     help='Parallel workers for frame generation '
                          '(default: cpu_count - 1)')
    enc.add_argument('--verify', action='store_true',
                     help='Verify output by decoding after encode')
    enc.add_argument('--compress', action='store_true',
                     help='Gzip compress data before encoding')
    dec = sub.add_parser('dec', help='Decode QR video back to file')
    dec.add_argument('input', nargs='+',
                     help='Video file path(s), glob, directory, or YouTube URL(s)')
    dec.add_argument('-o', '--output', help='Output file path (default: stdout)')
    dec.add_argument('-p', '--password', help='Password for decryption')
    dec.add_argument('--workers', type=int, default=None,
                     help='Parallel workers (default: cpu_count - 1)')

    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == 'enc':
        cmd_enc(args)
    elif args.command == 'dec':
        cmd_dec(args)


if __name__ == '__main__':
    main()
