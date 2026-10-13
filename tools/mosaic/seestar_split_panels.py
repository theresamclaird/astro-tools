#!/usr/bin/env python3
"""
seestar_split_panels.py — group Seestar mosaic sub-frames into per-panel folders.

The Seestar writes every sub of a mosaic into one flat "<Target>_mosaic_sub"
directory with no panel identifier in the filename or the FITS header. The only
clue is the RA/DEC keywords, which record the *measured* pointing of each frame,
so every revisit to a panel lands a few arcminutes from the last one.

This script reads the header of every .fit file, clusters the pointings by
angular separation, and populates <Target>_mosaic_panels/panel_NN/ so each
panel can be stacked independently before the panels are mosaicked.

Default action is a dry run. Pass --apply to actually create anything.
Originals are never modified, moved, or deleted.

Usage:
    python3 seestar_split_panels.py "Sadr_mosaic_sub"
    python3 seestar_split_panels.py "Sadr_mosaic_sub" --apply
    python3 seestar_split_panels.py "NGC 7000_mosaic_sub" --apply --mode copy
"""

import argparse
import csv
import datetime
import math
import os
import re
import sys
from collections import Counter

FITS_BLOCK = 2880
HEADER_READ = FITS_BLOCK * 8   # plenty for a Seestar header (~44 cards)


# ---------------------------------------------------------------- FITS header

def read_header(path):
    """Return the FITS primary header as a dict of str -> str.

    Only the first few 2880-byte blocks are read, which is all a Seestar
    header occupies. Stops at the END card.
    """
    with open(path, "rb") as fh:
        raw = fh.read(HEADER_READ)
    hdr = {}
    for off in range(0, len(raw), 80):
        card = raw[off:off + 80].decode("ascii", "replace")
        key = card[:8].strip()
        if key == "END":
            break
        if card[8:10] != "= ":
            continue
        value = card[10:]
        # strip an inline comment, but not a "/" inside a quoted string
        if value.lstrip().startswith("'"):
            end = value.find("'", value.find("'") + 1)
            value = value[:end + 1] if end != -1 else value
        else:
            value = value.split("/")[0]
        hdr[key] = value.strip().strip("'").strip()
    return hdr


def night_of(name):
    """Observing night a filename belongs to.

    The Seestar timestamps filenames in local time, so a session that runs past
    midnight spans two calendar dates. Anything before noon belongs to the
    previous evening's session.
    """
    m = re.search(r"_(\d{8})-(\d{6})\.fits?$", name)
    if not m:
        return None
    d, t = m.group(1), m.group(2)
    dt = datetime.date(int(d[:4]), int(d[4:6]), int(d[6:]))
    if int(t[:2]) < 12:
        dt -= datetime.timedelta(days=1)
    return dt.isoformat()


def collect(directory, want_nights=None, want_filter=None):
    """Read every .fit in `directory`. Returns (frames, unreadable).

    want_nights / want_filter exist because the Seestar files every session of
    a target into one folder. Two sessions are NOT interchangeable: the mosaic
    grid is re-chosen each time (offsets of half a panel are normal), and the
    filter may differ. Frames taken through different filters must never be
    stacked together, and frames from different nights should be split into
    their own panel sets and merged later at the mosaic stage, which places
    panels by sky coordinate and does not care about the grid.
    """
    names = sorted(n for n in os.listdir(directory) if n.lower().endswith(".fit"))
    frames, bad = [], []
    skipped_night = skipped_filter = 0
    for name in names:
        path = os.path.join(directory, name)
        if want_nights and night_of(name) not in want_nights:
            skipped_night += 1
            continue
        try:
            hdr = read_header(path)
            ra = float(hdr["RA"])
            dec = float(hdr["DEC"])
        except Exception as exc:
            bad.append((name, str(exc)))
            continue
        if want_filter and hdr.get("FILTER", "").upper() != want_filter.upper():
            skipped_filter += 1
            continue
        frames.append({
            "name": name,
            "ra": ra,
            "dec": dec,
            "date_obs": hdr.get("DATE-OBS", ""),
            "filter": hdr.get("FILTER", ""),
            "object": hdr.get("OBJECT", ""),
            "exptime": hdr.get("EXPTIME", ""),
            "night": night_of(name),
        })
    if skipped_night:
        print(f"  skipped {skipped_night} frames from other nights")
    if skipped_filter:
        print(f"  skipped {skipped_filter} frames with a different filter")
    return frames, bad


# ---------------------------------------------------------------- clustering

def separation(ra1, dec1, ra2, dec2):
    """Angular separation in degrees, small-angle approximation.

    Adequate here: panels are ~0.5 deg apart and well away from the pole.
    """
    dra = (ra1 - ra2) * math.cos(math.radians((dec1 + dec2) / 2.0))
    return math.hypot(dra, dec1 - dec2)


def cluster(frames, tol, passes=8):
    """Greedy seeding by angular separation, then Lloyd refinement.

    Frames are seeded in sky order rather than file order so the result does
    not depend on capture sequence. Refinement fixes frames that seeded into
    a neighbouring panel because they arrived early.
    """
    order = sorted(range(len(frames)), key=lambda i: (frames[i]["dec"], frames[i]["ra"]))
    centers = []
    for i in order:
        f = frames[i]
        best, best_d = None, float("inf")
        for c, (cra, cdec, _) in enumerate(centers):
            d = separation(f["ra"], f["dec"], cra, cdec)
            if d < best_d:
                best, best_d = c, d
        if best is None or best_d > tol:
            centers.append([f["ra"], f["dec"], 1])
        else:
            cra, cdec, n = centers[best]
            centers[best] = [(cra * n + f["ra"]) / (n + 1),
                             (cdec * n + f["dec"]) / (n + 1), n + 1]

    labels = [None] * len(frames)
    for _ in range(passes):
        changed = False
        for i, f in enumerate(frames):
            best, best_d = None, float("inf")
            for c, (cra, cdec, _) in enumerate(centers):
                d = separation(f["ra"], f["dec"], cra, cdec)
                if d < best_d:
                    best, best_d = c, d
            if labels[i] != best:
                labels[i] = best
                changed = True
        sums = [[0.0, 0.0, 0] for _ in centers]
        for i, f in enumerate(frames):
            s = sums[labels[i]]
            s[0] += f["ra"]
            s[1] += f["dec"]
            s[2] += 1
        for c, (sra, sdec, n) in enumerate(sums):
            if n:
                centers[c] = [sra / n, sdec / n, n]
        if not changed:
            break
    return labels, centers


# ---------------------------------------------------------------- output

def link(src, dst, mode):
    """Place `src` at `dst`. Falls back hardlink -> symlink -> copy."""
    if mode in ("hardlink", "auto"):
        try:
            os.link(src, dst)
            return "hardlink"
        except OSError:
            if mode == "hardlink":
                raise
    if mode in ("symlink", "auto"):
        try:
            os.symlink(os.path.abspath(src), dst)
            return "symlink"
        except OSError:
            if mode == "symlink":
                raise
    import shutil
    shutil.copy2(src, dst)
    return "copy"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("subdir", help='the "<Target>_mosaic_sub" directory')
    ap.add_argument("--out", help="output directory (default: <subdir minus _sub>_panels)")
    ap.add_argument("--tol", type=float, default=0.35,
                    help="max angular separation in degrees for two frames to share "
                         "a panel (default 0.35; Seestar panel pitch is ~0.5-0.9)")
    ap.add_argument("--min-frames", type=int, default=10,
                    help="clusters smaller than this are treated as strays "
                         "(mid-slew frames), not panels (default 10)")
    ap.add_argument("--mode", choices=["auto", "hardlink", "symlink", "copy"],
                    default="auto",
                    help="how to place files (default auto: hardlink, else symlink, "
                         "else copy). Hardlinks cost no disk space and leave the "
                         "originals in place.")
    ap.add_argument("--nights", default=None,
                    help="comma-separated observing nights to include, "
                         "e.g. 2026-09-22. A night runs from noon to noon, so "
                         "after-midnight frames belong to the evening they "
                         "started in. Use --list-nights to see what is there.")
    ap.add_argument("--filter", dest="want_filter", default=None,
                    help="only include frames taken through this filter "
                         "(LP, IRCUT). Frames through different filters must "
                         "never be stacked together.")
    ap.add_argument("--list-nights", action="store_true",
                    help="report what nights and filters the directory holds, "
                         "then exit")
    ap.add_argument("--apply", action="store_true",
                    help="actually create directories and files (default: dry run)")
    args = ap.parse_args()

    subdir = args.subdir.rstrip("/")
    if not os.path.isdir(subdir):
        sys.exit(f"not a directory: {subdir}")

    base = subdir[:-4] if subdir.endswith("_sub") else subdir
    outdir = args.out or base + "_panels"

    if args.list_nights:
        names = sorted(n for n in os.listdir(subdir) if n.lower().endswith(".fit"))
        tally = {}
        for n in names:
            try:
                f = read_header(os.path.join(subdir, n)).get("FILTER", "?")
            except Exception:
                f = "?"
            tally.setdefault((night_of(n), f), 0)
            tally[(night_of(n), f)] += 1
        print(f"{subdir}\n")
        print(f"  {'night':<14} {'filter':<8} frames")
        for (night, filt), c in sorted(tally.items()):
            print(f"  {str(night):<14} {filt:<8} {c}")
        print("\nProcess each night/filter separately, then merge the panel "
              "masters at the mosaic stage.")
        return

    nights = set(x.strip() for x in args.nights.split(",")) if args.nights else None
    frames, bad = collect(subdir, nights, args.want_filter)
    if not frames:
        sys.exit(f"no readable .fit files in {subdir}")

    print(f"source      : {subdir}")
    print(f"destination : {outdir}")
    print(f"frames read : {len(frames)}")
    if bad:
        print(f"unreadable  : {len(bad)}")
        for name, err in bad[:10]:
            print(f"    {name}: {err}")

    filters = Counter(f["filter"] for f in frames)
    objects = Counter(f["object"] for f in frames)
    exps = Counter(f["exptime"] for f in frames)
    print(f"object      : {', '.join(f'{k} x{v}' for k, v in objects.most_common())}")
    print(f"filter      : {', '.join(f'{k} x{v}' for k, v in filters.most_common())}")
    print(f"exposure    : {', '.join(f'{k}s x{v}' for k, v in exps.most_common())}")
    if len(filters) > 1:
        print("  WARNING: mixed filters in one directory - do not stack these together.")

    labels, centers = cluster(frames, args.tol)
    counts = Counter(labels)

    panels = sorted((c for c in counts if counts[c] >= args.min_frames),
                    key=lambda c: (-centers[c][1], centers[c][0]))
    strays = [c for c in counts if counts[c] < args.min_frames]
    panel_no = {c: i + 1 for i, c in enumerate(panels)}

    n_stray = sum(counts[c] for c in strays)
    per = sorted(counts[c] for c in panels)
    print(f"\npanels      : {len(panels)}  "
          f"(frames/panel min {per[0]}, median {per[len(per) // 2]}, max {per[-1]})")
    print(f"assigned    : {len(frames) - n_stray} frames")
    print(f"strays      : {n_stray} frames in {len(strays)} off-grid clusters "
          f"(< {args.min_frames} frames)")
    print(f"integration : {(len(frames) - n_stray) * float(frames[0]['exptime'] or 0) / 3600:.2f} h")

    print("\n  panel      RA        DEC    frames")
    for c in panels:
        print(f"  panel_{panel_no[c]:02d}  {centers[c][0]:8.3f}  {centers[c][1]:8.3f}  {counts[c]:5d}")
    if strays:
        print(f"  strays    {'':8}  {'':8}  {n_stray:5d}")

    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply to create the panel folders.")
        return

    if os.path.exists(outdir):
        sys.exit(f"\n{outdir} already exists; remove it or pass --out to write elsewhere.")

    dirs = {c: os.path.join(outdir, f"panel_{panel_no[c]:02d}") for c in panels}
    for c in strays:
        dirs[c] = os.path.join(outdir, "strays")
    for d in set(dirs.values()):
        os.makedirs(d, exist_ok=True)

    modes = Counter()
    rows = []
    for f, c in zip(frames, labels):
        target = "strays" if c in strays else f"panel_{panel_no[c]:02d}"
        src = os.path.join(subdir, f["name"])
        dst = os.path.join(dirs[c], f["name"])
        modes[link(src, dst, args.mode)] += 1
        rows.append({
            "file": f["name"], "panel": target,
            "ra": f"{f['ra']:.6f}", "dec": f"{f['dec']:.6f}",
            "date_obs": f["date_obs"], "filter": f["filter"],
        })

    manifest = os.path.join(outdir, "panels.csv")
    with open(manifest, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "panel", "ra", "dec", "date_obs", "filter"])
        w.writeheader()
        w.writerows(rows)

    print(f"\nwrote {len(rows)} entries into {outdir}")
    print("placement   : " + ", ".join(f"{v} by {k}" for k, v in modes.items()))
    print(f"manifest    : {manifest}")
    print(f"\nTo undo, delete {outdir} - the originals in {subdir} were not touched.")


if __name__ == "__main__":
    main()
