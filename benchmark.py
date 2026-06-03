#!/usr/bin/env python3
"""QRVid benchmark: test grid layouts for speed, density, and chunk loss."""
import subprocess, time, os, sys, tempfile, shutil

PY = [sys.executable, '-u', os.path.join(os.path.dirname(__file__), 'qrvid.py')]


def bench_local(data_size, configs, tmp, extra_flags):
    rows = []
    for cfg in configs:
        label = cfg['label']
        cols, rows_n = cfg['cols'], cfg['rows']
        infile = os.path.join(tmp, 'input.bin')
        with open(infile, 'wb') as f:
            f.write(os.urandom(data_size))
        outfile = os.path.join(tmp, 'out.mp4')
        chk = outfile + '.qrvid_chk'
        for p in [outfile, outfile + '.qrvid_frames', chk]:
            if os.path.isfile(p): os.unlink(p)
            if os.path.isdir(p): shutil.rmtree(p)

        t0 = time.time()
        r = subprocess.run(PY + ['enc', infile, '-o', outfile,
                            '--cols', str(cols), '--rows', str(rows_n),
                            '--verify']
                           + extra_flags,
                           capture_output=True, text=True, timeout=600)
        enc_t = time.time() - t0
        if r.returncode != 0:
            rows.append((label, 'ERR', '', '', '', r.stderr.strip()[:120]))
            continue

        lines = r.stdout.strip().splitlines()
        enc_info = {}
        for l in lines:
            if 'Chunks:' in l:
                enc_info['chunks'] = l.split()[1].rstrip(',')
            if 'Saved:' in l:
                parts = l.split()
                for i, p in enumerate(parts):
                    if p.endswith('bytes,'):
                        enc_info['size'] = parts[i - 1].lstrip('(')
                    if p.endswith('s)'):
                        enc_info['dur'] = p[:-1]
            if 'Verify' in l and 'FAILED' in l:
                enc_info['verify'] = 'FAIL'
            if 'Verify OK' in l:
                enc_info['verify'] = 'OK'

        verify_ok = enc_info.get('verify', '?')
        loss = 0 if verify_ok == 'OK' else '?'

        rows.append((label, enc_info.get('size', '?'),
                     enc_info.get('dur', '?'), enc_info.get('chunks', '?'),
                     loss, f"{enc_t:.1f}s"))
    return rows

def bench_youtube(url, tmp):
    outfile = os.path.join(tmp, 'yt.mp4')
    chk = outfile + '.qrvid_chk'
    for p in [outfile, outfile + '.qrvid_frames', chk]:
        if os.path.isfile(p): os.unlink(p)
        if os.path.isdir(p): shutil.rmtree(p)

    print(f"\n  Downloading {url}...")
    t0 = time.time()
    cmd = PY + ['dec', url, '-o', os.path.join(tmp, 'decoded.bin')]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    dl_dec_t = time.time() - t0
    lines = r.stdout.strip().splitlines()
    result = {}
    for l in lines:
        if 'Reconstructed:' in l and 'CRC32' in l:
            parts = l.split()
            result['size'] = parts[1]
        if 'Missing' in l:
            result['missing'] = l.split()[1]
    result['time'] = f"{dl_dec_t:.1f}s"
    result['ok'] = r.returncode == 0
    return result

def main():
    import argparse
    ap = argparse.ArgumentParser(description='QRVid benchmark')
    ap.add_argument('--size', type=int, default=1024*1024,
                    help='Test file size in bytes (default 1 MB)')
    ap.add_argument('--youtube', help='YouTube URL to benchmark decode')
    ap.add_argument('--layouts', default='6x5,5x4,4x4,4x3,3x3,2x2',
                    help='Comma-separated grid layouts (default: all)')
    ap.add_argument('extra', nargs=argparse.REMAINDER,
                    help='Extra flags (after --) passed to qrvid.py enc')
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix='qrvid_bench_')

    configs = []
    for spec in args.layouts.split(','):
        c, r = spec.split('x')
        configs.append({'cols': int(c), 'rows': int(r), 'label': spec})

    if args.youtube:
        print(f"YouTube decode benchmark: {args.youtube}")
        r = bench_youtube(args.youtube, tmp)
        status = "OK" if r.get('ok') else "FAIL"
        print(f"  Result: {status}  |  Decode: {r.get('time', '?')}  |  "
              f"Data: {r.get('size', '?')} bytes  |  "
              f"Missing: {r.get('missing', '0')} chunks")

    if configs:
        data_size = args.size
        extra = [x for x in args.extra if x != '--']
        print(f"\nLocal encode/decode benchmark ({data_size // 1024} KB):"
              f"{'  extra: ' + ' '.join(extra) if extra else ''}")
        results = bench_local(data_size, configs, tmp, extra)
        print(f"\n  {'Layout':>8}  {'Video':>8}  {'Dur':>6}  "
              f"{'Chunks':>7}  {'Loss':>5}  {'Enc':>8}")
        print(f"  {'-'*8}  {'-'*8}  {'-'*6}  {'-'*7}  {'-'*5}  {'-'*8}")
        for r in results:
            print(f"  {r[0]:>8}  {str(r[1]):>8}  {str(r[2]):>6}  "
                  f"{str(r[3]):>7}  {str(r[4]):>5}  {str(r[5]):>8}")

    shutil.rmtree(tmp)

if __name__ == '__main__':
    main()
