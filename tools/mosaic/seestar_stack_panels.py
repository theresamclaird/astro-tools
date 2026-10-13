#!/usr/bin/env python3
"""
seestar_stack_panels.py — batch-stack Seestar mosaic panels with Siril.

Runs the same four-step recipe on every panel_NN directory produced by
seestar_split_panels.py:

    convert (with debayer) -> register -> stack -> move master to masters/

and deletes the intermediates once a panel's master is verified, so peak disk
stays around one panel instead of ~5 GB x N.

The recipe matches what the Siril GUI produces with:
    Conversion : Debayer ticked
    Register   : Global registration
    Stacking   : Average with rejection, Winsorized sigma clip 3/3,
                 Additive with scaling, output NOT normalised

Output normalisation and RGB equalisation are deliberately left off. Each panel
must stay on the same linear scale as its neighbours, or the mosaic stage's
overlap normalisation has to undo per-panel scaling that never should have been
applied.

Resumable: panels whose master already exists are skipped unless --force.

Usage:
    python3 seestar_stack_panels.py Sadr_mosaic_panels --only 2      # try one
    python3 seestar_stack_panels.py Sadr_mosaic_panels               # all of them
    python3 seestar_stack_panels.py Sadr_mosaic_panels --dry-run     # print scripts
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

ORIGINAL_GLOB = re.compile(r"^Light_.*\.fits?$", re.I)

# Files Siril leaves behind in a panel directory. Only these are ever removed.
ARTIFACT_PATTERNS = [
    re.compile(r"^p\d+_\d+\.fits?$", re.I),
    re.compile(r"^r_p\d+_\d+\.fits?$", re.I),
    re.compile(r"^r_p\d+_stacked\.fits?$", re.I),
    re.compile(r"^.*\.seq$", re.I),
    re.compile(r"^.*_conversion\.txt$", re.I),
]
ARTIFACT_DIRS = ["cache", "process"]

SIRIL_CANDIDATES = [
    "siril-cli",
    "/Applications/Siril.app/Contents/MacOS/siril-cli",
    "/Applications/SiriL.app/Contents/MacOS/siril-cli",
    "/opt/homebrew/bin/siril-cli",
    "/usr/local/bin/siril-cli",
    "/usr/bin/siril-cli",
]


# ---------------------------------------------------------------- FITS header

def read_header(path):
    with open(path, "rb") as fh:
        raw = fh.read(2880 * 12)
    hdr = {}
    for off in range(0, len(raw), 80):
        card = raw[off:off + 80].decode("ascii", "replace")
        key = card[:8].strip()
        if key == "END":
            break
        if card[8:10] == "= ":
            hdr[key] = card[10:].split("/")[0].strip().strip("'").strip()
    return hdr


# ---------------------------------------------------------------- environment

def find_siril(explicit=None):
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        sys.exit(f"not an executable: {explicit}")
    for cand in SIRIL_CANDIDATES:
        found = shutil.which(cand) if os.sep not in cand else (
            cand if os.access(cand, os.X_OK) else None)
        if found:
            return found
    sys.exit("could not find siril-cli. Pass --siril /path/to/siril-cli")


def siril_version(binary):
    try:
        out = subprocess.run([binary, "--version"], capture_output=True,
                             text=True, timeout=30).stdout.strip()
        return out or "unknown"
    except Exception as exc:
        return f"unknown ({exc})"


# ---------------------------------------------------------------- per panel

def panel_dirs(root):
    found = []
    for name in sorted(os.listdir(root)):
        m = re.fullmatch(r"panel_(\d+)", name)
        if m and os.path.isdir(os.path.join(root, name)):
            found.append((int(m.group(1)), name))
    return found


def originals(panel_path):
    return sorted(n for n in os.listdir(panel_path) if ORIGINAL_GLOB.match(n))


def sweep_artifacts(panel_path, apply=True):
    """Remove Siril leftovers. Never touches Light_*.fit."""
    removed = 0
    for name in os.listdir(panel_path):
        full = os.path.join(panel_path, name)
        if os.path.isdir(full):
            if name in ARTIFACT_DIRS:
                if apply:
                    shutil.rmtree(full, ignore_errors=True)
                removed += 1
            continue
        if ORIGINAL_GLOB.match(name):
            continue
        if any(p.match(name) for p in ARTIFACT_PATTERNS):
            if apply:
                try:
                    os.remove(full)
                except OSError:
                    pass
            removed += 1
    return removed


def build_script(panel_path, tag, master_rel, args):
    """The Siril command sequence for one panel.

    Two details here are not obvious and both were learned the hard way:

    1. -out= must NOT be quoted. Siril's argument parser does not strip quotes,
       so -out="/abs/path/x" creates a literal directory named '"' and buries
       the result inside it. `cd` does accept quotes, which is what makes paths
       with spaces (e.g. "NGC 7000_mosaic_panels") work at all. So we cd with a
       quoted absolute path and write the output via an unquoted RELATIVE path,
       which contains no spaces regardless of where the tree lives.

    2. The Seestar is alt-az (EQMODE = 0) and does not derotate. A panel
       revisited across a whole night accumulates well over 100 degrees of field
       rotation. Registering against frame #1 leaves the far end of that range
       at an extreme angle and star matching fails on those frames. -2pass picks
       the reference from star statistics across the sequence instead, which
       puts it near the middle of the rotation range and roughly halves the
       worst-case angle any frame has to be matched through.
    """
    rej = f"rej {args.rejection} {args.siglow} {args.sighigh}"
    extra = ""
    if args.filter_round:
        extra += f" -filter-round={args.filter_round}"

    reg = [f"register {tag}"]
    if args.register == "2pass":
        reg = [f"register {tag} -2pass", f"seqapplyreg {tag}"]
    if args.transf:
        reg[0] += f" -transf={args.transf}"
    if args.minpairs:
        reg[0] += f" -minpairs={args.minpairs}"

    return "\n".join([
        "requires 1.4.0",
        "setext fit",
        f'cd "{panel_path}"',
        f"convert {tag} -debayer -out=process",
        "cd process",
        *reg,
        f"stack r_{tag} {rej} -norm=addscale{extra} -out={master_rel}",
        "close",
        "",
    ])


def run_panel(num, name, root, masters, binary, args):
    panel_path = os.path.join(root, name)
    tag = f"p{num:02d}"
    master = os.path.join(masters, f"panel_{num:02d}")
    master_fit = master + ".fit"

    result = {"panel": name, "inputs": 0, "stacked": None,
              "status": "", "seconds": 0.0}

    src = originals(panel_path)
    result["inputs"] = len(src)
    if not src:
        result["status"] = "no Light_*.fit found"
        return result

    if os.path.exists(master_fit) and not args.force:
        hdr = read_header(master_fit)
        result["stacked"] = hdr.get("STACKCNT")
        # The master is done, so any intermediates still sitting in the panel
        # directory are dead weight - reclaim them even though we skip the work.
        n = sweep_artifacts(panel_path, apply=not (args.keep or args.dry_run))
        result["status"] = ("skipped (master exists)" if not n
                            else f"skipped, swept {n} leftovers")
        return result

    # cwd at stack time is <panel dir>/process, so ../../masters/panel_NN
    master_rel = f"../../masters/panel_{num:02d}"
    script = build_script(panel_path, tag, master_rel, args)

    if args.dry_run:
        print(f"\n----- {name} -----\n{script}", end="")
        result["status"] = "dry-run"
        return result

    # A previous aborted run would leave files that `convert` would pick up as
    # if they were subs, so always start from a clean panel directory.
    sweep_artifacts(panel_path)

    log_path = os.path.join(masters, f"panel_{num:02d}.log")
    start = time.time()
    proc = subprocess.run([binary, "-s", "-"], input=script,
                          capture_output=True, text=True)
    result["seconds"] = time.time() - start

    with open(log_path, "w") as fh:
        fh.write(script + "\n" + "=" * 60 + "\n" + proc.stdout + proc.stderr)

    if not os.path.exists(master_fit):
        result["status"] = f"FAILED (rc={proc.returncode}, see {log_path})"
        return result

    hdr = read_header(master_fit)
    result["stacked"] = hdr.get("STACKCNT")
    if hdr.get("NAXIS3") != "3":
        result["status"] = "FAILED (not RGB - debayer did not run)"
        return result

    if not args.keep:
        sweep_artifacts(panel_path)
    result["status"] = "ok"
    return result


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help='the "<Target>_mosaic_panels" directory')
    ap.add_argument("--only", type=int, help="process just this panel number")
    ap.add_argument("--from", dest="first", type=int, default=0)
    ap.add_argument("--to", dest="last", type=int, default=10**6)
    ap.add_argument("--siril", help="path to siril-cli if not auto-detected")
    ap.add_argument("--rejection", default="w",
                    help="rejection type letter: w winsorized, s sigma, "
                         "g GESDT, l linear fit, m MAD (default w)")
    ap.add_argument("--siglow", default="3")
    ap.add_argument("--sighigh", default="3")
    ap.add_argument("--register", choices=["2pass", "1pass"], default="2pass",
                    help="2pass picks the reference frame from star statistics "
                         "across the sequence, which matters a lot for alt-az "
                         "field rotation (default). 1pass is the plain "
                         "single-command form.")
    ap.add_argument("--transf", default=None,
                    help="transformation model: shift, similarity, affine, "
                         "homography (Siril default is homography)")
    ap.add_argument("--minpairs", default=None,
                    help="minimum star pairs for a match (Siril default 10)")
    ap.add_argument("--filter-round", default=None,
                    help="drop frames below this star roundness, e.g. 0.65. "
                         "Off by default so every panel keeps every frame.")
    ap.add_argument("--keep", action="store_true",
                    help="keep intermediates (about 5 GB per panel)")
    ap.add_argument("--force", action="store_true",
                    help="restack panels that already have a master")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the Siril script for each panel, run nothing")
    args = ap.parse_args()

    root = os.path.abspath(args.root.rstrip("/"))
    if not os.path.isdir(root):
        sys.exit(f"not a directory: {root}")
    masters = os.path.join(root, "masters")
    os.makedirs(masters, exist_ok=True)

    binary = None if args.dry_run else find_siril(args.siril)
    panels = [(n, nm) for n, nm in panel_dirs(root)
              if args.first <= n <= args.last and (args.only is None or n == args.only)]
    if not panels:
        sys.exit("no matching panel_NN directories")

    print(f"root    : {root}")
    print(f"masters : {masters}")
    if binary:
        print(f"siril   : {binary}")
        print(f"version : {siril_version(binary)}")
    print(f"panels  : {len(panels)}")
    print(f"recipe  : convert -debayer | register | stack rej {args.rejection} "
          f"{args.siglow} {args.sighigh} -norm=addscale"
          + (f" -filter-round={args.filter_round}" if args.filter_round else ""))
    print()

    results = []
    t0 = time.time()
    for i, (num, name) in enumerate(panels, 1):
        if not args.dry_run:
            print(f"[{i}/{len(panels)}] {name} ... ", end="", flush=True)
        r = run_panel(num, name, root, masters, binary, args)
        results.append(r)
        if not args.dry_run:
            took = f"{r['seconds']:.0f}s" if r["seconds"] else ""
            print(f"{r['status']}  ({r['stacked'] or '?'}/{r['inputs']} frames) {took}")

    if args.dry_run:
        return

    print(f"\n{'panel':<10} {'in':>4} {'stacked':>8}  status")
    for r in results:
        print(f"{r['panel']:<10} {r['inputs']:>4} {str(r['stacked'] or '-'):>8}  {r['status']}")

    ok = [r for r in results if r["status"] == "ok"]
    bad = [r for r in results if r["status"].startswith("FAILED")]
    dropped = [r for r in ok if r["stacked"] and int(r["stacked"]) < r["inputs"]]

    print(f"\n{len(ok)} stacked, {len(results) - len(ok) - len(bad)} skipped, "
          f"{len(bad)} failed, {time.time() - t0:.0f}s total")
    if dropped:
        print("\npanels where registration dropped frames (worth a look):")
        for r in dropped:
            print(f"  {r['panel']}: {r['stacked']}/{r['inputs']}")
    if bad:
        print("\nfailures — read the .log next to the master:")
        for r in bad:
            print(f"  {r['panel']}: {r['status']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
