#!/usr/bin/env python3
"""
seestar_mosaic.py — Stage 3: plate solve, astrometrically register and stack
the prepped panels into one mosaic.

Why astrometric registration rather than star matching: adjacent panels share
too little sky for pattern matching to be reliable, and the Seestar is alt-az
so every panel sits at its own rotation. Plate solving gives each panel real
sky coordinates, and registration then places them by those coordinates. That
is the whole reason this works.

Pipeline:
    link <seq>                      build a sequence from the prepped panels
    seqplatesolve <seq>             solve every panel, storing registration data
    seqapplyreg <seq> -framing=max  place each panel on the common sky canvas
    stack r_<seq> ... -feather=N    combine with blended seams

Notes on the choices:

  -framing=max keeps the union of all panels. min would crop to their common
  area, which for a 45-panel mosaic is nothing at all.

  Rejection is deliberately wide (default 5/5). These are already-stacked
  masters, so there are no cosmic rays or satellites left to reject, and with
  only a handful of panels overlapping any given pixel, tight rejection throws
  away good data at the seams.

  Feathering blends the tile borders. Too little leaves visible edges, too much
  smears real detail across them. 150 px is a starting point, not an answer -
  expect to try a few values.

  Overlap normalisation matters here: panel brightness varies from wall-to-wall
  hydrogen-alpha to near-empty field, so whole-image statistics would make the
  panels disagree about what "background" means.

Siril's documentation is inconsistent about some option names, so every run
first asks Siril itself for the usage lines of the commands it is about to use
and writes them to the log. If a command is rejected, the correct syntax is
sitting right above the error.

Usage:
    python3 seestar_mosaic.py Sadr_mosaic_panels --probe      # just ask Siril
    python3 seestar_mosaic.py Sadr_mosaic_panels --dry-run
    python3 seestar_mosaic.py Sadr_mosaic_panels
"""

import argparse
import glob
import importlib.util
import os
import re
import shutil
import subprocess
import sys

SIRIL_CANDIDATES = [
    "siril-cli",
    "/Applications/Siril.app/Contents/MacOS/siril-cli",
    "/opt/homebrew/bin/siril-cli",
    "/usr/local/bin/siril-cli",
]
PROBE_COMMANDS = ["link", "seqplatesolve", "seqapplyreg", "stack"]


def find_siril(explicit=None):
    if explicit:
        return explicit
    for c in SIRIL_CANDIDATES:
        found = shutil.which(c) if os.sep not in c else (c if os.access(c, os.X_OK) else None)
        if found:
            return found
    sys.exit("could not find siril-cli; pass --siril /path/to/siril-cli")


def run_siril(binary, script):
    return subprocess.run([binary, "-s", "-"], input=script,
                          capture_output=True, text=True)


def prep_module():
    """Reuse the FITS reader/writer from the Stage 2.5 script."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "seestar_prep_panels.py")
    spec = importlib.util.spec_from_file_location("prep", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SEQ_ARTIFACTS = [
    re.compile(r"^r?_?mosaic.*\.(fit|fits|seq)$", re.I),
    re.compile(r"^.*_conversion\.txt$", re.I),
]


def sweep_sequence_artifacts(d, seq):
    """Remove leftovers from a previous attempt.

    This matters more than it looks. `link` converts every FITS in the working
    directory, and a failed run leaves real mosaic_000NN.fit files behind for
    whichever images did solve - Siril replaces the symlink with a real file
    when it writes the solution. Re-running without cleaning would pull those
    into the new sequence as extra members.
    """
    import shutil as _sh
    removed = 0
    for name in os.listdir(d):
        full = os.path.join(d, name)
        if os.path.isdir(full):
            if name == "cache":
                _sh.rmtree(full, ignore_errors=True)
                removed += 1
            continue
        if name.startswith("panel_"):
            continue
        if any(p.match(name) for p in SEQ_ARTIFACTS) or name.startswith(seq):
            try:
                os.remove(full)
                removed += 1
            except OSError:
                pass
    return removed


def panel_sizes(d):
    m = prep_module()
    out = {}
    for f in sorted(glob.glob(os.path.join(d, "panel_*.fit"))):
        cards, _ = m.read_cards(f)
        out[f] = (int(m.card_value(cards, "NAXIS1")),
                  int(m.card_value(cards, "NAXIS2")))
    return out


def make_uniform(src_dir, dst_dir):
    """Re-crop every panel to the smallest common size, centred on its own
    kept region. Only needed if this build of Siril refuses a sequence whose
    images differ in size."""
    m = prep_module()
    sizes = panel_sizes(src_dir)
    w = min(s[0] for s in sizes.values())
    h = min(s[1] for s in sizes.values())
    os.makedirs(dst_dir, exist_ok=True)
    for f, (sw, sh) in sizes.items():
        x = (sw - w) // 2
        y = (sh - h) // 2
        m.write_cropped(f, os.path.join(dst_dir, os.path.basename(f)), x, y, w, h)
    return w, h, len(sizes)


def autocrop(mosaic_path, out_path):
    """Crop the mosaic to the largest fully-covered rectangle.

    A rotated mosaic has a ragged perimeter of empty canvas. Those zero pixels
    wreck background extraction, SPCC statistics and the stretch, so they have
    to go before any of that.

    CRPIX must move with the crop or the WCS silently points at the wrong sky,
    and SPCC would then calibrate against the wrong stars. That is why this is
    done here rather than with Siril's `crop`: these FITS carry no ROWORDER
    keyword, so there is no safe way to map Siril's top-left origin onto the
    data array, and guessing shifts the crop into empty canvas.
    """
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    m = prep_module()
    cards, cube = m.open_cube(mosaic_path)
    n3, n2, n1 = cube.shape
    S = 8
    g = np.array(cube[1 if n3 > 1 else 0, ::S, ::S], dtype=np.float32)
    bg = np.median(g[g > 0])
    good = g > bg * 0.5
    k = 5
    good = sliding_window_view(np.pad(good, k // 2, constant_values=False),
                               (k, k)).all(axis=(-1, -2))
    rect = m.largest_rect(good)
    if rect is None:
        return None
    r0, c0, r1, c1 = rect
    X, Y, W, H = c0 * S, r0 * S, (c1 - c0) * S, (r1 - r0) * S
    out = np.array(cube[:, Y:Y + H, X:X + W])

    def val(key):
        v = m.card_value(cards, key)
        return float(v) if v is not None else None
    cr1, cr2 = val('CRPIX1'), val('CRPIX2')
    new_cards = []
    for c in cards:
        key = c[:8].strip()
        if key == 'NAXIS1':
            new_cards.append(m.make_card('NAXIS1', W, 'length of data axis 1'))
        elif key == 'NAXIS2':
            new_cards.append(m.make_card('NAXIS2', H, 'length of data axis 2'))
        elif key == 'CRPIX1' and cr1 is not None:
            new_cards.append(m.make_card('CRPIX1', repr(cr1 - X), 'Axis1 reference pixel'))
        elif key == 'CRPIX2' and cr2 is not None:
            new_cards.append(m.make_card('CRPIX2', repr(cr2 - Y), 'Axis2 reference pixel'))
        else:
            new_cards.append(c)
    note = f'HISTORY cropped to covered area x={X} y={Y} w={W} h={H}; CRPIX shifted'
    new_cards.append(f'{note:<80}'[:80])
    new_cards.append(f"{'END':<80}")
    hdr = ''.join(new_cards)
    hdr += ' ' * ((2880 - len(hdr) % 2880) % 2880)
    with open(out_path, 'wb') as fh:
        fh.write(hdr.encode('ascii'))
        raw = out.tobytes()
        fh.write(raw)
        fh.write(b'\0' * ((2880 - len(raw) % 2880) % 2880))
    return X, Y, W, H, (W * H) / (n1 * n2)


def build_script(workdir, seq, args, out_path):
    """Assemble the Siril script.

    Every option here was read off `help` in this build rather than from the
    documentation, which is wrong or incomplete on several of them.

    seqplatesolve -nocache: by default Siril pulls ONE catalogue extraction for
    the whole sequence. Its own help warns that fails "if there is a lot of
    drift or different sampling" - which describes a 45-panel mosaic spanning
    5 degrees exactly. -nocache makes it use each image's own metadata.

    seqplatesolve -radius: the header RA/DEC of each panel is the centre of the
    ORIGINAL master, and the adaptive crop moved some centres by up to about a
    quarter of a degree, so give the solver room to find them.

    stack -maximize: without this the stack covers only the reference image's
    footprint - one panel - no matter what framing registration used. Siril's
    help is explicit that -framing=max must be paired with it.

    stack -overlap_norm: computes normalisation on the overlaps instead of
    whole images, and is only permitted alongside -maximize. Necessary because
    panel brightness ranges from wall-to-wall H-alpha to near-empty field.

    stack rej n: proper no-rejection, which takes no sigma arguments. These are
    already-stacked masters with nothing left to reject.

    stack -weight=nbstack: weights each panel by how many subs built it. Yours
    range from 23 to 36 frames, so this is real information rather than a
    guess.
    """
    if args.rejection == "n":
        rej = "rej n"
    else:
        rej = f"rej {args.rejection} {args.siglow} {args.sighigh}"

    solve = f"seqplatesolve {seq}"
    if args.catalog:
        solve += f" -catalog={args.catalog}"
    if args.limitmag:
        solve += f" -limitmag={args.limitmag}"
    if args.nocache:
        solve += " -nocache"
    if args.radius:
        solve += f" -radius={args.radius}"
    if args.force_solve:
        solve += " -force"

    stack = [f"stack r_{seq}", rej, "-norm=addscale", "-overlap_norm", "-maximize"]
    if args.weight:
        stack.append(f"-weight={args.weight}")
    if args.feather and args.feather != "0":
        stack.append(f"-feather={args.feather}")
    stack += ["-32b", f"-out={out_path}"]

    lines = ["requires 1.4.0", "setext fit"]
    if args.gaia:
        # Set it per-run rather than relying on the preference having been
        # saved. Cheap, and removes a whole class of "did that stick?".
        lines.append(f"set core.catalogue_gaia_astro={args.gaia}")
    lines.append(f'cd "{workdir}"')
    if args.explain:
        lines += [f"help {c}" for c in PROBE_COMMANDS]
    lines += [
        f"link {seq}",
        solve,
        f"seqapplyreg {seq} -framing={args.framing}"
        + (f" -scale={args.scale}" if args.scale else ""),
        " ".join(stack),
        "close",
        "",
    ]
    return "\n".join(lines)


def print_usage_lines(text):
    """Pull the 'Usage: ...' lines Siril prints for each probed command."""
    found = []
    for line in text.splitlines():
        s = line.replace("log: ", "").strip()
        if s.lower().startswith("usage") or re.match(r"^(link|seqplatesolve|seqapplyreg|stack)\s", s):
            found.append(s)
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help='the "<Target>_mosaic_panels" directory')
    ap.add_argument("--input", default="masters_prepped",
                    help="subdirectory holding the panels (default masters_prepped)")
    ap.add_argument("--seq", default="mosaic", help="sequence basename")
    ap.add_argument("--framing", default="max", choices=["max", "min", "cog", "current"])
    ap.add_argument("--scale", default=None,
                    help="downscale factor for the registered canvas, e.g. 0.5. "
                         "Use if the full-resolution mosaic is too large.")
    ap.add_argument("--feather", default="150",
                    help="seam feathering distance in pixels (0 to disable)")
    ap.add_argument("--rejection", default="n",
                    help="n=none (default; correct for already-stacked tiles), "
                         "or w/s/g/l/m/a with --siglow/--sighigh")
    ap.add_argument("--siglow", default="3")
    ap.add_argument("--sighigh", default="3")
    ap.add_argument("--weight", default="nbstack",
                    help="weight panels by noise|wfwhm|nbstars|nbstack "
                         "(default nbstack: frames per panel). Empty to disable.")
    ap.add_argument("--gaia", default=None,
                    help="path to the local Gaia DR3 catalogue "
                         "(siril_cat_healpix8_astro.dat). Auto-detected in "
                         "~/astro-catalogs and a few other usual places.")
    ap.add_argument("--catalog", default=None,
                    help="force a remote catalogue: tycho2, nomad, gaia, "
                         "ppmxl, brightstars, apass. Leave unset to let Siril "
                         "choose (it prefers a local catalogue if installed).")
    ap.add_argument("--limit-mag", dest="limitmag", default=None,
                    help="limit magnitude for plate solving, or a +/- offset. "
                         "Lower values make far lighter catalogue queries.")
    ap.add_argument("--radius", default="2",
                    help="plate-solve near-search radius in degrees (default 2)")
    ap.add_argument("--no-cache", dest="nocache", action="store_false",
                    default=True,
                    help="let Siril reuse one catalogue extraction for the "
                         "whole sequence (not recommended for a wide mosaic)")
    ap.add_argument("--explain", action="store_true",
                    help="also dump Siril's help for each command into the log")
    ap.add_argument("--force-solve", action="store_true",
                    help="re-solve panels that already carry a solution")
    ap.add_argument("--no-crop", action="store_true",
                    help="skip the automatic crop to the fully covered area")
    ap.add_argument("--uniform", action="store_true",
                    help="re-crop all panels to a common size first. Only needed "
                         "if Siril rejects a mixed-size sequence.")
    ap.add_argument("--siril")
    ap.add_argument("--probe", action="store_true",
                    help="only ask Siril for the usage lines of the commands "
                         "this script uses, and print them")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(args.root.rstrip("/"))
    work = os.path.join(root, args.input)
    if not os.path.isdir(work):
        sys.exit(f"no {args.input} directory under {root}")

    binary = find_siril(args.siril)

    if not args.gaia:
        for cand in ("~/astro-catalogs/siril_cat_healpix8_astro.dat",
                     "~/.local/share/kstars/siril_cat_healpix8_astro.dat",
                     "~/Library/Application Support/kstars/siril_cat_healpix8_astro.dat"):
            c = os.path.expanduser(cand)
            if os.path.isfile(c):
                args.gaia = c
                break
    if args.gaia:
        gb = os.path.getsize(args.gaia) / 1e9
        print(f"gaia     : {args.gaia}  ({gb:.2f} GB)")
        if gb < 1.0:
            print("  WARNING: that file looks too small - expected about 1.5 GB. "
                  "Did bunzip2 finish?")
    else:
        print("gaia     : none found - will use the remote catalogue "
              "(this is what failed last time)")

    if args.probe:
        script = "\n".join(["requires 1.4.0"] + [f"help {c}" for c in PROBE_COMMANDS] + ["close", ""])
        proc = run_siril(binary, script)
        print(proc.stdout + proc.stderr)
        return

    sizes = panel_sizes(work)
    if not sizes:
        sys.exit(f"no panel_*.fit in {work}")
    distinct = set(sizes.values())
    print(f"panels   : {len(sizes)} in {os.path.basename(work)}")
    print(f"sizes    : {len(distinct)} distinct"
          + ("" if len(distinct) == 1 else "  (adaptive crops)"))

    if args.uniform and len(distinct) > 1:
        dst = os.path.join(root, "masters_uniform")
        if not args.dry_run:
            w, h, n = make_uniform(work, dst)
            print(f"re-cropped {n} panels to a common {w}x{h}")
        work = dst

    # rough canvas estimate: sky span of the panel centres plus one panel
    m = prep_module()
    ras, decs = [], []
    for f in sorted(glob.glob(os.path.join(work, "panel_*.fit"))) or sorted(sizes):
        cards, _ = m.read_cards(f)
        try:
            ras.append(float(m.card_value(cards, "RA")))
            decs.append(float(m.card_value(cards, "DEC")))
        except (TypeError, ValueError):
            pass
    if ras:
        import math
        span_ra = (max(ras) - min(ras)) * math.cos(math.radians(sum(decs) / len(decs)))
        span_dec = max(decs) - min(decs)
        px = 3600 / 2.30
        print(f"sky span : {span_ra:.2f} x {span_dec:.2f} deg")
        print(f"canvas   : roughly {int(span_ra*px):d} x {int(span_dec*px):d} px at native scale")
        gb = span_ra * px * span_dec * px * 3 * 4 * len(sizes) / 1e9
        print(f"registered sequence will need on the order of {gb:.0f} GB"
              + ("  <-- consider --scale=0.5" if gb > 120 else ""))

    # -out= must be unquoted (Siril does not strip quotes) and this tree can
    # contain spaces, e.g. "NGC 7000_mosaic_panels". A relative path from the
    # working directory has neither problem.
    out_rel = "../mosaic_stacked"
    out_path = os.path.join(root, "mosaic_stacked")
    script = build_script(work, args.seq, args, out_rel)
    print(f"output   : {out_path}.fit")
    print(f"feather  : {args.feather} px")
    print(f"rejection: {'none' if args.rejection=='n' else args.rejection+' '+args.siglow+'/'+args.sighigh}")
    print(f"weight   : {args.weight or 'disabled'}\n")

    if args.dry_run:
        print(script)
        return

    n = sweep_sequence_artifacts(work, args.seq)
    if n:
        print(f"cleared {n} leftover(s) from a previous attempt")
    print("running Siril (this is the long one - plate solving 45 panels then "
          "resampling each onto the full canvas)...\n")
    proc = run_siril(binary, script)
    log = os.path.join(root, "mosaic.log")
    with open(log, "w") as fh:
        fh.write(script + "\n" + "=" * 60 + "\n" + proc.stdout + proc.stderr)

    usage = print_usage_lines(proc.stdout + proc.stderr)
    if usage:
        print("Siril's own usage lines for the commands used:")
        for u in usage:
            print("   " + u)
        print()

    solved = (proc.stdout + proc.stderr).count("Plate solving succeeded") \
        or (proc.stdout + proc.stderr).count("solved")
    print(f"plate solve mentions   : {solved}")
    if os.path.exists(out_path + ".fit"):
        cards, _ = m.read_cards(out_path + ".fit")
        print(f"MOSAIC WRITTEN: {out_path}.fit")
        print(f"  {m.card_value(cards,'NAXIS1')} x {m.card_value(cards,'NAXIS2')}"
              f" x {m.card_value(cards,'NAXIS3')}   "
              f"{os.path.getsize(out_path+'.fit')/1e9:.2f} GB")
        if not args.no_crop:
            cropped = os.path.join(root, "mosaic_cropped.fit")
            r = autocrop(out_path + ".fit", cropped)
            if r:
                X, Y, W, H, frac = r
                print(f"\nCROPPED      : {cropped}")
                print(f"  x={X} y={Y}  {W}x{H}   {W*H/1e6:.0f} Mpx   "
                      f"keeps {100*frac:.1f}% of canvas, WCS preserved")
                print("  ^ this is the file to hand to SPCC")
            else:
                print("  autocrop found no fully covered rectangle")
    else:
        out_text = proc.stdout + proc.stderr
        cat_fail = out_text.count("unable to retrieve the remote catalogue")
        nosolve = out_text.count("did not solve")
        if cat_fail:
            print(f"\nPLATE SOLVE FAILED: {nosolve} panels did not solve, "
                  f"{cat_fail} of them because the remote star catalogue "
                  f"refused the request.")
            print("  The catalogue server is reachable; the problem is the "
                  "volume of queries.")
            print("  Best fix: install the local Gaia DR3 catalogue "
                  "(Siril > Scripts > Catalogue Installer, about 1.5 GB), "
                  "then re-run.")
            print("  Without that, try:  --catalog=tycho2 --limit-mag=11  "
                  "or drop --no-cache")
            sys.exit(1)
        print(f"no mosaic produced - read {log}")
        tail = [l for l in (proc.stdout + proc.stderr).splitlines()
                if re.search(r"error|fail|cannot|invalid|not|wrong", l, re.I)]
        for l in tail[-12:]:
            print("   " + l)
        sys.exit(1)


if __name__ == "__main__":
    main()
