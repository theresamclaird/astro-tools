#!/usr/bin/env python3
"""
seestar_reject_frames.py — pull obstructed frames out of the panel folders.

Written after a tree got into a Sadr session. Two things about that are worth
recording, because they are what made it hard to find:

1. An out-of-focus branch a few metres away, seen through a 50mm aperture
   focused at infinity, is not a silhouette. It is a broad, soft glow, and if
   it is lit from the ground it is BRIGHTER than the sky, not darker.

2. In linear ADU the contrast is tiny. A branch that dominates a stretched
   preview measured 1.0% above sky here - 383 ADU on a 38,310 ADU background.
   Any detector tuned to how it looks after stretching will miss it entirely.

So the test is: divide the frame into ~240px blocks, take each block's median,
and compare the brightest block to the frame's own typical block. Clean frames
sit near +0.2%. Obstructed frames jump to +2-3%. The separation is clean.

Frames are MOVED to rejected/panel_NN/, not deleted, and the panel folders hold
hardlinks, so the originals in <Target>_mosaic_sub are untouched either way.
Use --restore to put them back.

Usage:
    python3 seestar_reject_frames.py Sadr_mosaic_panels --scan
    python3 seestar_reject_frames.py Sadr_mosaic_panels --after 20260921-040759 --apply
    python3 seestar_reject_frames.py Sadr_mosaic_panels --threshold 0.008 --apply
    python3 seestar_reject_frames.py Sadr_mosaic_panels --restore
"""

import argparse, glob, os, re, shutil, sys
import numpy as np

S = 8
NY, NX = 16, 9          # 240 x 240 raw pixels per block
TS = re.compile(r'_(\d{8}-\d{6})\.fits?$')


def peak_excess(fp):
    """Brightest 240px block relative to the frame's own typical block."""
    a = np.memmap(fp, dtype='>u2', mode='r', offset=5760, shape=(3840, 2160))
    v = np.array(a[::S, ::S], dtype=np.float32)
    by, bx = v.shape[0] // NY, v.shape[1] // NX
    bm = np.empty((NY, NX))
    for i in range(NY):
        for j in range(NX):
            bm[i, j] = np.median(v[i*by:(i+1)*by, j*bx:(j+1)*bx])
    rel = bm / np.median(bm)
    return float(rel.max() - 1.0), float(np.mean(rel > 1.008))


def collect(root):
    out = []
    for pd in sorted(glob.glob(os.path.join(root, 'panel_*'))):
        if not os.path.isdir(pd):
            continue
        m = re.search(r'panel_(\d+)$', pd)
        if not m:
            continue
        for f in sorted(os.listdir(pd)):
            if not f.startswith('Light_'):
                continue
            t = TS.search(f)
            out.append((t.group(1) if t else '', int(m.group(1)), os.path.join(pd, f)))
    out.sort()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('root')
    ap.add_argument('--after', help='reject frames with timestamp >= this (YYYYMMDD-HHMMSS)')
    ap.add_argument('--before', help='reject frames with timestamp <= this')
    ap.add_argument('--threshold', type=float, default=None,
                    help='also reject any frame whose peak block exceeds this '
                         'fraction above its own sky (0.008 works for a lit branch)')
    ap.add_argument('--scan', action='store_true', help='measure and report only')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--restore', action='store_true', help='move rejected frames back')
    args = ap.parse_args()

    root = os.path.abspath(args.root.rstrip('/'))
    rej = os.path.join(root, 'rejected')

    if args.restore:
        n = 0
        for pd in sorted(glob.glob(os.path.join(rej, 'panel_*'))):
            dest = os.path.join(root, os.path.basename(pd))
            for f in os.listdir(pd):
                shutil.move(os.path.join(pd, f), os.path.join(dest, f)); n += 1
        print(f'restored {n} frames')
        return

    frames = collect(root)
    if not frames:
        sys.exit(f'no panel_NN/Light_*.fit under {root}')

    need_scan = args.scan or args.threshold is not None
    scores = {}
    if need_scan:
        print(f'measuring {len(frames)} frames...')
        for ts, pn, fp in frames:
            scores[fp] = peak_excess(fp)
        mx = np.array([scores[f[2]][0] for f in frames])
        print('peak block excess over own sky:')
        for q in (50, 90, 95, 99, 100):
            print(f'   p{q:<4} {np.percentile(mx, q)*100:6.2f}%')

    sel = []
    for ts, pn, fp in frames:
        why = []
        if args.after and ts >= args.after:
            why.append('after')
        if args.before and ts <= args.before:
            why.append('before')
        if args.threshold is not None and scores.get(fp, (0, 0))[0] > args.threshold:
            why.append(f'+{scores[fp][0]*100:.2f}%')
        if why:
            sel.append((ts, pn, fp, ','.join(why)))

    if args.scan and not sel:
        flag = sorted(frames, key=lambda f: -scores[f[2]][0])[:15]
        print('\nmost affected frames:')
        for ts, pn, fp in flag:
            print(f'   {ts}  p{pn:02d}  peak +{scores[fp][0]*100:5.2f}%  area {scores[fp][1]*100:4.1f}%')
        return

    from collections import Counter
    print(f'\nselected {len(sel)} of {len(frames)} frames')
    if sel:
        print(f'   {sel[0][0]} .. {sel[-1][0]}')
        print('   by panel: ' + ', '.join(f'p{k:02d}:{v}' for k, v in sorted(Counter(s[1] for s in sel).items())))
        remaining = Counter(f[1] for f in frames)
        for k, v in sorted(Counter(s[1] for s in sel).items()):
            left = remaining[k] - v
            note = '   <-- thin' if left < 15 else ''
            print(f'      panel_{k:02d}: {remaining[k]} -> {left} frames{note}')

    if not args.apply:
        print('\nDRY RUN - nothing moved. Add --apply.')
        return

    moved = 0
    for ts, pn, fp, why in sel:
        d = os.path.join(rej, f'panel_{pn:02d}')
        os.makedirs(d, exist_ok=True)
        shutil.move(fp, os.path.join(d, os.path.basename(fp))); moved += 1
    print(f'\nmoved {moved} frames to {rej}')
    print('originals in <Target>_mosaic_sub are untouched; --restore undoes this')


if __name__ == '__main__':
    main()
