#!/usr/bin/env python3
"""
seestar_prep_panels.py — crop and background-extract Seestar panel masters.

Stage 2.5, between per-panel stacking and mosaicking. Two jobs:

1. CROP. The Seestar is alt-az and does not derotate, so a panel revisited
   across a night accumulates large field rotation. Registration aligns the
   frames on the sky, which means the stack's outer regions were built from
   only a handful of subs while the middle got all of them. Those regions are
   several times noisier and will produce seams once feathering blends a clean
   panel into a neighbour's ragged corner.

   Rather than guess a fixed margin, this measures it. Each master is divided
   into a 32x18 grid and per-block noise is estimated from the second spatial
   difference, which cancels smooth nebulosity and leaves per-pixel noise. The
   map is normalised by its own median, and the largest axis-aligned rectangle
   of blocks below the threshold is kept. Well-covered panels lose almost
   nothing; badly-covered ones lose a lot, which is correct.

   Cropping costs no sky coverage here: panel pitch is about 0.5 degrees
   against a 1.38 x 2.45 degree field, so even the most aggressive crop still
   spans well over the distance to the next panel centre.

2. BACKGROUND EXTRACTION. Each panel was shot at a different altitude over
   many hours and carries its own light-pollution gradient. Removing it per
   panel is far easier than removing 45 differently-angled gradients baked
   into one large mosaic.

   Default is a degree-1 (planar) fit. That is deliberate. The Sadr region is
   wall-to-wall extended hydrogen-alpha, and a higher-degree or RBF fit cannot
   tell faint nebulosity from sky gradient - it will flatten the thing you
   spent the night collecting. Degree 1 removes the ramp and leaves structure.

Originals in masters/ are never modified. Output goes to masters_prepped/.

Usage:
    python3 seestar_prep_panels.py Sadr_mosaic_panels --dry-run
    python3 seestar_prep_panels.py Sadr_mosaic_panels
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys

import numpy as np

NY, NX = 32, 18          # noise-map grid
ANALYSIS_STRIDE = 2      # subsample factor when measuring noise

SIRIL_CANDIDATES = [
    "siril-cli",
    "/Applications/Siril.app/Contents/MacOS/siril-cli",
    "/opt/homebrew/bin/siril-cli",
    "/usr/local/bin/siril-cli",
]


# ---------------------------------------------------------------- FITS I/O

def read_cards(path):
    """Return (cards, data_offset). cards is a list of 80-char strings."""
    cards = []
    with open(path, "rb") as fh:
        while True:
            block = fh.read(2880)
            if not block:
                raise ValueError(f"no END card in {path}")
            for i in range(0, 2880, 80):
                card = block[i:i + 80].decode("ascii", "replace")
                if card[:8].strip() == "END":
                    return cards, fh.tell()
                cards.append(card)


def card_value(cards, key):
    for c in cards:
        if c[:8].strip() == key and c[8:10] == "= ":
            return c[10:].split("/")[0].strip().strip("'").strip()
    return None


def make_card(key, value, comment=""):
    body = f"{key:<8}= {str(value):>20}"
    if comment:
        body += f" / {comment}"
    return f"{body:<80}"[:80]


def open_cube(path):
    """memmap the data as (planes, rows, cols) without loading it."""
    cards, off = read_cards(path)
    n1 = int(card_value(cards, "NAXIS1"))
    n2 = int(card_value(cards, "NAXIS2"))
    n3 = int(card_value(cards, "NAXIS3") or 1)
    bitpix = int(card_value(cards, "BITPIX"))
    dt = {-32: ">f4", -64: ">f8", 16: ">u2"}.get(bitpix)
    if dt is None:
        raise ValueError(f"unhandled BITPIX {bitpix} in {path}")
    cube = np.memmap(path, dtype=np.dtype(dt), mode="r", offset=off,
                     shape=(n3, n2, n1))
    return cards, cube


def write_cropped(src, dst, x, y, w, h):
    cards, cube = open_cube(src)
    out = np.array(cube[:, y:y + h, x:x + w])

    new = []
    for c in cards:
        k = c[:8].strip()
        if k == "NAXIS1":
            new.append(make_card("NAXIS1", w, "length of data axis 1"))
        elif k == "NAXIS2":
            new.append(make_card("NAXIS2", h, "length of data axis 2"))
        else:
            new.append(c)
    note = f"HISTORY cropped to x={x} y={y} w={w} h={h} (rotation coverage)"
    new.append(f"{note:<80}"[:80])
    new.append(f"{'END':<80}")

    hdr = "".join(new)
    hdr += " " * ((2880 - len(hdr) % 2880) % 2880)

    with open(dst, "wb") as fh:
        fh.write(hdr.encode("ascii"))
        raw = out.tobytes()
        fh.write(raw)
        pad = (2880 - len(raw) % 2880) % 2880
        fh.write(b"\0" * pad)


# ---------------------------------------------------------------- crop logic

def noise_map(plane):
    """Per-block noise from the second spatial difference.

    A smooth gradient - nebulosity, light pollution - has a near-zero second
    difference, so this responds to per-pixel noise rather than to signal.
    """
    H, W = plane.shape
    by, bx = H // NY, W // NX
    m = np.zeros((NY, NX))
    for i in range(NY):
        for j in range(NX):
            b = plane[i * by:(i + 1) * by, j * bx:(j + 1) * bx]
            d2 = b[2:, :] - 2 * b[1:-1, :] + b[:-2, :]
            m[i, j] = np.median(np.abs(d2))
    return m


def largest_rect(mask):
    """Largest all-True axis-aligned rectangle. Histogram method, O(rows*cols)."""
    ny, nx = mask.shape
    best_area, best = 0, None
    heights = np.zeros(nx, dtype=int)
    for i in range(ny):
        heights = np.where(mask[i], heights + 1, 0)
        stack = []
        for j in range(nx + 1):
            cur = heights[j] if j < nx else 0
            start = j
            while stack and stack[-1][1] >= cur:
                s, hh = stack.pop()
                area = hh * (j - s)
                if area > best_area:
                    best_area, best = area, (i - hh + 1, s, i + 1, j)
                start = s
            stack.append((start, cur))
    return best


def plan_crop(path, threshold):
    cards, cube = open_cube(path)
    n2, n1 = cube.shape[1], cube.shape[2]
    plane = np.array(cube[1 if cube.shape[0] > 1 else 0,
                          ::ANALYSIS_STRIDE, ::ANALYSIS_STRIDE], dtype=np.float32)
    m = noise_map(plane)
    rel = m / np.median(m)
    rect = largest_rect(rel <= threshold)
    if rect is None:
        return None
    r0, c0, r1, c1 = rect
    y = int(r0 / NY * n2)
    h = int((r1 - r0) / NY * n2)
    x = int(c0 / NX * n1)
    w = int((c1 - c0) / NX * n1)
    return x, y, w, h, (w * h) / (n1 * n2)


# ---------------------------------------------------------------- siril

def find_siril(explicit=None):
    if explicit:
        return explicit
    for cand in SIRIL_CANDIDATES:
        found = shutil.which(cand) if os.sep not in cand else (
            cand if os.access(cand, os.X_OK) else None)
        if found:
            return found
    return None


def run_subsky(binary, workdir, names, args):
    method = "-rbf" if args.rbf else str(args.degree)
    lines = ["requires 1.4.0", "setext fit", f'cd "{workdir}"']
    for n in names:
        lines += [f"load {n}", f"subsky {method}", f"save {n}"]
    lines += ["close", ""]
    script = "\n".join(lines)
    if args.dry_run:
        return script, None
    proc = subprocess.run([binary, "-s", "-"], input=script,
                          capture_output=True, text=True)
    return script, proc


def count_subsky_successes(stdout):
    """Siril writes no HISTORY card for subsky, so verify from its own log.

    It emits one "Background with ... computed" line per successful image.
    Checking the header instead reports a false failure on every panel.
    """
    return stdout.count("Background with")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help='the "<Target>_mosaic_panels" directory')
    ap.add_argument("--threshold", type=float, default=1.6,
                    help="keep blocks whose noise is at most this multiple of "
                         "the panel's median (default 1.6)")
    ap.add_argument("--min-keep", type=float, default=0.30,
                    help="warn if a crop keeps less than this fraction (default 0.30)")
    ap.add_argument("--degree", type=int, default=1,
                    help="subsky polynomial degree (default 1 - see the note at "
                         "the top about why higher is dangerous here)")
    ap.add_argument("--rbf", action="store_true",
                    help="use RBF background extraction instead of polynomial. "
                         "Will eat faint nebulosity in fields like Sadr.")
    ap.add_argument("--no-crop", action="store_true")
    ap.add_argument("--no-subsky", action="store_true")
    ap.add_argument("--only", type=int)
    ap.add_argument("--siril")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(args.root.rstrip("/"))
    masters = os.path.join(root, "masters")
    out = os.path.join(root, "masters_prepped")
    if not os.path.isdir(masters):
        sys.exit(f"no masters directory under {root}")

    files = sorted(glob.glob(os.path.join(masters, "panel_*.fit")))
    if args.only is not None:
        files = [f for f in files
                 if int(re.search(r"panel_(\d+)", f).group(1)) == args.only]
    if not files:
        sys.exit("no panel masters found")

    print(f"masters  : {masters}")
    print(f"output   : {out}")
    print(f"panels   : {len(files)}")
    print(f"crop     : {'disabled' if args.no_crop else f'noise threshold {args.threshold}x median'}")
    print(f"subsky   : {'disabled' if args.no_subsky else ('RBF' if args.rbf else f'polynomial degree {args.degree}')}")
    print()

    if not args.dry_run:
        os.makedirs(out, exist_ok=True)

    plans, warn = [], []
    for f in files:
        n = int(re.search(r"panel_(\d+)", f).group(1))
        name = f"panel_{n:02d}"
        if args.no_crop:
            plans.append((name, f, None, 1.0))
            print(f"  {name}  (crop disabled)")
            continue
        p = plan_crop(f, args.threshold)
        if p is None:
            print(f"  {name}  NO REGION BELOW THRESHOLD - skipped")
            warn.append(name)
            continue
        x, y, w, h, kept = p
        flag = "  <-- heavy" if kept < args.min_keep else ""
        if kept < args.min_keep:
            warn.append(name)
        print(f"  {name}  x={x:<5d} y={y:<5d} {w}x{h}   keeps {100*kept:5.1f}%{flag}")
        plans.append((name, f, (x, y, w, h), kept))

    if plans:
        print(f"\nmean kept: {100*np.mean([k for *_, k in plans]):.1f}%")

    if args.dry_run:
        print("\nDRY RUN - nothing written.")
        if not args.no_subsky:
            binary = find_siril(args.siril)
            script, _ = run_subsky(binary or "siril-cli", out,
                                   [n for n, *_ in plans], args)
            print("\nSiril script that would run:\n" + "\n".join(script.splitlines()[:8]) + "\n  ...")
        return

    for name, src, rect, _ in plans:
        dst = os.path.join(out, name + ".fit")
        if rect is None:
            shutil.copy2(src, dst)
        else:
            write_cropped(src, dst, *rect)
    print(f"\nwrote {len(plans)} cropped masters")

    if args.no_subsky:
        return

    binary = find_siril(args.siril)
    if not binary:
        sys.exit("could not find siril-cli; pass --siril /path/to/siril-cli")
    print(f"running background extraction via {binary} ...")
    script, proc = run_subsky(binary, out, [n for n, *_ in plans], args)
    log = os.path.join(out, "subsky.log")
    with open(log, "w") as fh:
        fh.write(script + "\n" + "=" * 60 + "\n" + proc.stdout + proc.stderr)

    ok = count_subsky_successes(proc.stdout + proc.stderr)
    print(f"background extracted on {ok}/{len(plans)} panels")
    if ok < len(plans):
        print(f"  {len(plans) - ok} panel(s) did not complete - check {log}")
    if warn:
        print(f"\nworth eyeballing before Stage 3: {', '.join(warn)}")


if __name__ == "__main__":
    main()
