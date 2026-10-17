#!/usr/bin/env python3
"""astro-frame-stats: measure every frame of one night, and compare frame filters.

  astro-frame-stats TARGET NIGHT [--compare] [--variants LIST] [--crop X,Y,W,H]
                    [--work DIR] [--dry-run]

  TARGET     target folder name or unique prefix (M31)
  NIGHT      raw/ night folder name or unique prefix (2026-10-15)
  --compare  also register and stack the night once per filter variant, and
             measure each stack (star FWHM, background noise) in one crop
  --variants comma-separated variants to stack (default: all of them):
               all       every plate-solved frame, no filter
               current   astro-stack's filter: best 90% by wFWHM and by background
               wfwhm75   best 75% by wFWHM
               wfwhm60   best 60% by wFWHM (discards ~40%, as some people do)
               outliers  only clear outliers: wFWHM, roundness and star count more
                         than 3 sigma (robust) from the night's median
  --crop     region measured and saved as a JPG for side-by-side viewing, in
             stack pixels (default: the central 2000x2000)
  --work     scratch folder (default $ASTRO_WORK, else /data/astro-work)

What it does:
  1. Copies the night's raw frames to scratch, debayers and plate-solves them
     exactly as astro-stack does. (Skipped when a previous run left them there.)
  2. Reads what Siril measured on every frame -- FWHM, weighted FWHM, roundness,
     sky background, star count -- plus each frame's time and pointing, and
     groups the frames into mosaic panels by pointing.
  3. Writes frames.csv, summary.txt (spread of each measure, robust outliers,
     what each percentage cut would drop, per panel) and plots (if matplotlib
     is installed) to <archive>/Targets/<target>/processing/frame-stats/<night>/.
  4. With --compare: one stack per variant, measured in the same crop, with a
     JPG of each crop (same stretch) and comparison.txt.

The archive is only read. Stacks and intermediates stay in the scratch folder:
<work>/<target>_<night>_frame-stats/ (delete it when done).
"""
import argparse, csv, datetime, glob, math, os, re, shutil, statistics, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "mosaic"))
from seestar_split_panels import read_header, cluster  # noqa: E402

STACK = "rej w 3 3 -norm=addscale -fastnorm -weight=noise -maximize -32b"
VARIANTS = ["all", "current", "wfwhm75", "wfwhm60", "outliers"]
METRICS = [("wfwhm", "weighted FWHM (px)", "high"), ("fwhm", "FWHM (px)", "high"),
           ("roundness", "roundness", "low"), ("bkg", "background", "high"),
           ("nstars", "stars detected", "low")]


def die(msg):
    sys.exit("astro-frame-stats: " + msg)


# ---------------------------------------------------------------- finding things

def find_dir(parent, name, what):
    if not os.path.isdir(parent):
        die("%s folder not found: %s" % (what, parent))
    hits = sorted(d for d in os.listdir(parent) if d.startswith(name) and os.path.isdir(os.path.join(parent, d)))
    if name in hits:
        return name
    if len(hits) != 1:
        die("no unique %s matching '%s' in %s%s" % (what, name, parent,
            (": " + ", ".join(hits)) if hits else ""))
    return hits[0]


def find_siril():
    if shutil.which("siril-cli"):
        cmd = ["siril-cli"]
    elif os.access("/Applications/Siril.app/Contents/MacOS/siril-cli", os.X_OK):
        cmd = ["/Applications/Siril.app/Contents/MacOS/siril-cli"]
    elif shutil.which("flatpak") and subprocess.run(["flatpak", "info", "org.siril.Siril"],
                                                    capture_output=True).returncode == 0:
        cmd = ["flatpak", "run", "--command=siril-cli", "org.siril.Siril"]
    else:
        die("Siril not found (siril-cli, /Applications/Siril.app, or the org.siril.Siril Flatpak)")
    if shutil.which("systemd-inhibit") and subprocess.run(
            ["systemd-inhibit", "--what=sleep", "true"], capture_output=True).returncode == 0:
        cmd = ["systemd-inhibit", "--what=sleep:idle", "--who=astro-frame-stats", "--why=measuring frames"] + cmd
    elif shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i"] + cmd
    return cmd


def run_siril(siril, script_path, lines, log, cwd_dir=None):
    with open(script_path, "w") as f:
        f.write("requires 1.4.0\n" + "\n".join(lines) + "\nclose\n")
    cmd = siril + (["-d", cwd_dir] if cwd_dir else []) + ["-s", script_path]
    out = []
    with open(log, "a") as lf:
        lf.write("\n=== %s %s\n" % (datetime.datetime.now().strftime("%F %T"), " ".join(cmd)))
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        for line in p.stdout:
            lf.write(line)
            out.append(line)
        p.wait()
    if p.returncode != 0:
        die("Siril failed (exit %d); see %s" % (p.returncode, log))
    return out


# ---------------------------------------------------------------- the .seq file

def read_seq(path):
    """Images and per-frame registration data from a Siril .seq file.

    I lines: "I <filenum> <included> ...". R lines, one per image per registered
    layer, in image order: "R<layer> fwhm wfwhm roundness quality background
    nstars H h00 ... h22". Frames that failed to plate-solve have an all-zero H.
    """
    images, regs = [], {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            t = line.split()
            if not t:
                continue
            if t[0] == "I" and len(t) >= 3:
                images.append((int(t[1]), t[2] == "1"))
            elif t[0].startswith("R") and len(t[0]) == 2:
                try:
                    h = t.index("H")
                except ValueError:
                    h = len(t)
                vals = t[1:h]
                if len(vals) < 6:
                    continue
                hom = [float(x) for x in t[h + 1:h + 10]] if h < len(t) else []
                regs.setdefault(t[0][1], []).append({
                    "fwhm": float(vals[0]), "wfwhm": float(vals[1]), "roundness": float(vals[2]),
                    "quality": float(vals[3]), "bkg": float(vals[4]), "nstars": int(float(vals[5])),
                    "solved": any(abs(x) > 0 for x in hom) if hom else float(vals[0]) > 0})
    if not regs:
        return images, []
    layer = max(regs, key=lambda k: sum(1 for r in regs[k] if r["fwhm"] > 0))
    return images, regs[layer]


# ---------------------------------------------------------------- statistics

def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def robust(xs):
    med = statistics.median(xs)
    mad = statistics.median([abs(x - med) for x in xs])
    return med, 1.4826 * mad


def fmt(x):
    if isinstance(x, int):
        return str(x)
    if x != x:
        return "-"
    return ("%.4g" % x)


def cut_value(xs, keep, bad):
    """Threshold that keeps the best `keep`% (bad = which end is worse)."""
    return pct(xs, keep) if bad == "high" else pct(xs, 100 - keep)


def kept(v, thr, bad):
    return v <= thr if bad == "high" else v >= thr


def summarize(frames, out):
    sol = [f for f in frames if f["solved"] and f["fwhm"] > 0]
    L = []
    w = L.append
    w("Frames: %d, plate-solved with star measurements: %d" % (len(frames), len(sol)))
    if not sol:
        return "\n".join(L), {}
    w("")
    w("Spread of each measure (solved frames):")
    w("  %-20s %8s %8s %8s %8s %8s %8s" % ("", "min", "p10", "median", "p90", "max", "sigma*"))
    stats = {}
    for key, label, bad in METRICS:
        xs = [f[key] for f in sol]
        med, sig = robust(xs)
        stats[key] = (med, sig)
        w("  %-20s %8s %8s %8s %8s %8s %8s" % (label, fmt(min(xs)), fmt(pct(xs, 10)), fmt(med),
                                               fmt(pct(xs, 90)), fmt(max(xs)), fmt(sig)))
    w("  * robust sigma = 1.4826 x median absolute deviation")
    w("")
    w("Robust outliers (more than 3 sigma from the median, on the bad side):")
    any_out = set()
    for key, label, bad in METRICS:
        med, sig = stats[key]
        if sig == 0:
            continue
        lim = med + 3 * sig if bad == "high" else med - 3 * sig
        n = [f for f in sol if not kept(f[key], lim, bad)]
        any_out |= {f["idx"] for f in n}
        w("  %-20s %5d frames (%4.1f%%)   limit %s" % (label, len(n), 100.0 * len(n) / len(sol), fmt(lim)))
    w("  any of these         %5d frames (%4.1f%%)" % (len(any_out), 100.0 * len(any_out) / len(sol)))
    w("")
    w("Percentage cuts: frames dropped (each cut keeps the best N% of that measure):")
    for keep in (90, 75, 60):
        a = cut_value([f["wfwhm"] for f in sol], keep, "high")
        b = cut_value([f["bkg"] for f in sol], keep, "high")
        n_w = sum(1 for f in sol if f["wfwhm"] > a)
        n_wb = sum(1 for f in sol if f["wfwhm"] > a or f["bkg"] > b)
        w("  keep %d%%: wFWHM alone drops %d (%.0f%%); wFWHM and background together drop %d (%.0f%%)"
          % (keep, n_w, 100.0 * n_w / len(sol), n_wb, 100.0 * n_wb / len(sol)))
    panels = sorted({f["panel"] for f in sol})
    if len(panels) > 1:
        w("")
        w("Per panel (mosaic pointing). A percentage cut on background that falls")
        w("mostly on one panel is removing that panel's sky, not bad frames:")
        tw = cut_value([f["wfwhm"] for f in sol], 90, "high")
        tb = cut_value([f["bkg"] for f in sol], 90, "high")
        w("  %-6s %7s %10s %10s %12s %12s" % ("panel", "frames", "med wFWHM", "med bkg", "wFWHM 90% cut", "bkg 90% cut"))
        for p in panels:
            fs = [f for f in sol if f["panel"] == p]
            dw = sum(1 for f in fs if f["wfwhm"] > tw)
            db = sum(1 for f in fs if f["bkg"] > tb)
            w("  %-6s %7d %10s %10s %7d (%2.0f%%) %7d (%2.0f%%)" % (
                p, len(fs), fmt(statistics.median([f["wfwhm"] for f in fs])),
                fmt(statistics.median([f["bkg"] for f in fs])), dw, 100.0 * dw / len(fs), db, 100.0 * db / len(fs)))
    return "\n".join(L), stats


def plots(frames, outdir):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return "matplotlib not installed; skipped the plots (re-run ./install.sh to add it)"
    sol = [f for f in frames if f["solved"] and f["fwhm"] > 0]
    if not sol:
        return "no measured frames to plot"
    panels = sorted({f["panel"] for f in sol})
    cmap = plt.get_cmap("tab10")
    t0 = min(f["t"] for f in sol if f["t"]) if any(f["t"] for f in sol) else None
    keys = [m for m in METRICS if m[0] != "fwhm"]
    fig, axes = plt.subplots(len(keys), 1, figsize=(12, 2.6 * len(keys)), sharex=True)
    for ax, (key, label, bad) in zip(axes, keys):
        for i, p in enumerate(panels):
            fs = [f for f in sol if f["panel"] == p]
            xs = [((f["t"] - t0).total_seconds() / 3600 if t0 and f["t"] else f["idx"]) for f in fs]
            ax.scatter(xs, [f[key] for f in fs], s=3, color=cmap(i % 10), label="panel %s" % p if len(panels) > 1 else None)
        xs = [f[key] for f in sol]
        for keep, ls in ((90, "-"), (75, "--"), (60, ":")):
            ax.axhline(cut_value(xs, keep, bad), color="k", lw=0.8, ls=ls)
        ax.set_ylabel(label)
    axes[0].set_title("Per-frame measurements; lines = keep-best 90% (solid), 75% (dashed), 60% (dotted)")
    axes[-1].set_xlabel("hours since first frame" if t0 else "frame")
    if len(panels) > 1:
        axes[0].legend(markerscale=4, fontsize=8, ncol=min(len(panels), 8), loc="upper right")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "frames_over_time.png"), dpi=110)
    plt.close(fig)
    fig, axes = plt.subplots(1, len(keys), figsize=(4 * len(keys), 3.2))
    for ax, (key, label, bad) in zip(axes, keys):
        xs = [f[key] for f in sol]
        ax.hist(xs, bins=60, color="0.4")
        for keep, ls in ((90, "-"), (75, "--"), (60, ":")):
            ax.axvline(cut_value(xs, keep, bad), color="C3", lw=0.9, ls=ls)
        ax.set_title(label, fontsize=10)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "histograms.png"), dpi=110)
    plt.close(fig)
    return "plots: frames_over_time.png, histograms.png"


# ---------------------------------------------------------------- comparisons

def variant_filters(name, sol, stats):
    if name == "all":
        return []
    if name == "current":
        return ["-filter-wfwhm=90%", "-filter-bkg=90%"]
    if name == "wfwhm75":
        return ["-filter-wfwhm=75%"]
    if name == "wfwhm60":
        return ["-filter-wfwhm=60%"]
    if name == "outliers":
        out = []
        m, s = stats["wfwhm"]
        out.append("-filter-wfwhm=%.4g" % (m + 3 * s))
        m, s = stats["roundness"]
        if s > 0:
            out.append("-filter-round=%.4g" % max(0.0, m - 3 * s))
        m, s = stats["nstars"]
        if s > 0 and m - 3 * s > 0:
            out.append("-filter-nbstars=%d" % int(m - 3 * s))
        return out
    die("unknown variant: %s (known: %s)" % (name, ", ".join(VARIANTS)))


def image_size(path):
    h = read_header(path)
    return int(h["NAXIS1"]), int(h["NAXIS2"])


def parse_measure(lines, starfile):
    noise = None
    for l in lines:
        m = re.search(r"[Bb]ackground noise value.*?:\s*([0-9.eE+-]+)", l)
        if m:
            noise = float(m.group(1))
    fwhms = []
    if os.path.isfile(starfile):
        cols = None
        with open(starfile, errors="replace") as f:
            for line in f:
                t = line.strip().lstrip("#").split()
                if not t:
                    continue
                if cols is None and any(c.lower().startswith("fwhmx") for c in t):
                    cols = [c.lower() for c in t]
                    continue
                if cols and not line.lstrip().startswith("#"):
                    try:
                        ix = next(i for i, c in enumerate(cols) if c.startswith("fwhmx") and "\"" not in c)
                        iy = next(i for i, c in enumerate(cols) if c.startswith("fwhmy") and "\"" not in c)
                        fwhms.append((float(t[ix]) + float(t[iy])) / 2)
                    except (StopIteration, ValueError, IndexError):
                        pass
    return noise, (statistics.median(fwhms) if fwhms else None), len(fwhms)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target")
    ap.add_argument("night")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--crop")
    ap.add_argument("--work", default=os.environ.get("ASTRO_WORK") or "/data/astro-work")
    ap.add_argument("--archive", default=os.environ.get("ASTRO_ARCHIVE") or "/Volumes/Astro")
    ap.add_argument("--tol", type=float, default=0.35, help="panel grouping distance, degrees (default 0.35)")
    ap.add_argument("--dry-run", "-n", action="store_true")
    a = ap.parse_args()

    targets = os.path.join(a.archive, "Targets")
    tname = find_dir(targets, a.target, "target")
    tdir = os.path.join(targets, tname)
    night = find_dir(os.path.join(tdir, "raw"), a.night, "night")
    ndir = os.path.join(tdir, "raw", night)
    raws = sorted(glob.glob(os.path.join(ndir, "*.fit")))
    if not raws:
        die("no .fit frames in " + ndir)
    variants = [v.strip() for v in a.variants.split(",") if v.strip()]
    run = os.path.join(a.work, "%s_%s_frame-stats" % (tname, night))
    work = os.path.join(run, "work")
    out = os.path.join(tdir, "processing", "frame-stats", night)
    seqf = os.path.join(work, "pp_light_.seq")
    have = os.path.isfile(seqf) and glob.glob(os.path.join(work, "pp_light_*.fit"))

    print("Target: %s   night: %s   %d frames" % (tname, night, len(raws)))
    print("Scratch: %s  (%s)" % (run, "debayered frames already there; reusing them" if have
                                 else "needs ~%d GB" % (len(raws) * 70 // 1024 + 1)))
    if a.compare:
        print("Variants to stack: %s  (each needs ~%d GB while it runs)" % (
            ", ".join(variants), len(raws) * 110 // 1024 + 1))
    print("Report: %s" % out)
    if a.dry_run:
        print("(dry run: nothing done)")
        return
    siril = find_siril()
    os.makedirs(work, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    log = os.path.join(run, "frame-stats.log")

    # 1. debayer + plate-solve, as astro-stack does
    if not have:
        links = os.path.join(run, "links")
        shutil.rmtree(links, ignore_errors=True)
        os.makedirs(links)
        print("Copying %d raw frames to scratch..." % len(raws))
        for r in raws:
            shutil.copyfile(r, os.path.join(links, os.path.basename(r)))
        print("Debayer + plate-solve (%s)..." % datetime.datetime.now().strftime("%T"))
        run_siril(siril, os.path.join(run, "solve.ssf"),
                  ["set16bits", "setext fit", "link light -date -out=" + work, "cd " + work,
                   "calibrate light -debayer", "seqplatesolve pp_light"], log, cwd_dir=links)
        shutil.rmtree(links, ignore_errors=True)
        for f in glob.glob(os.path.join(work, "light_*.fit")):
            os.remove(f)
    images, regs = read_seq(seqf)
    if not regs:
        die("no registration data in %s; see %s" % (seqf, log))

    # 2. per-frame table
    files = {}
    for p in glob.glob(os.path.join(work, "pp_light_*.fit")):
        m = re.search(r"_(\d+)\.fit$", p)
        if m:
            files[int(m.group(1))] = p
    frames = []
    for k, (filenum, incl) in enumerate(images):
        r = regs[k] if k < len(regs) else {"fwhm": 0, "wfwhm": 0, "roundness": 0, "quality": 0, "bkg": 0, "nstars": 0, "solved": False}
        f = dict(r, idx=k + 1, file=os.path.basename(files.get(filenum, "")), t=None, ra=None, dec=None)
        if filenum in files:
            h = read_header(files[filenum])
            try:
                f["t"] = datetime.datetime.fromisoformat(h.get("DATE-OBS", "")[:19])
            except ValueError:
                pass
            for rk, dk in (("CRVAL1", "CRVAL2"), ("RA", "DEC")):
                try:
                    f["ra"], f["dec"] = float(h[rk]), float(h[dk])
                    break
                except (KeyError, ValueError):
                    pass
        frames.append(f)
    pos = [f for f in frames if f["ra"] is not None]
    if pos:
        labels, _ = cluster(pos, a.tol)
        for f, lab in zip(pos, labels):
            f["panel"] = str(lab + 1)
    for f in frames:
        f.setdefault("panel", "?")
    with open(os.path.join(out, "frames.csv"), "w", newline="") as fh:
        cw = csv.writer(fh)
        cw.writerow(["frame", "file", "time", "panel", "ra", "dec", "solved", "fwhm", "wfwhm",
                     "roundness", "bkg", "nstars", "quality"])
        for f in frames:
            cw.writerow([f["idx"], f["file"], f["t"].isoformat() if f["t"] else "", f["panel"],
                         fmt(f["ra"]) if f["ra"] is not None else "", fmt(f["dec"]) if f["dec"] is not None else "",
                         int(f["solved"]), fmt(f["fwhm"]), fmt(f["wfwhm"]), fmt(f["roundness"]),
                         fmt(f["bkg"]), f["nstars"], fmt(f["quality"])])
    text, stats = summarize(frames, out)
    head = "astro-frame-stats %s %s  (%s)\n\n" % (tname, night, datetime.datetime.now().strftime("%F %T"))
    with open(os.path.join(out, "summary.txt"), "w") as fh:
        fh.write(head + text + "\n")
    print()
    print(text)
    print()
    print(plots(frames, out))

    # 3. comparison stacks
    if a.compare and stats:
        sol = [f for f in frames if f["solved"] and f["fwhm"] > 0]
        rows = []
        for v in variants:
            flt = variant_filters(v, sol, stats)
            stack = os.path.join(run, "stack_%s.fit" % v)
            pre = "v%s_" % v
            nreg = None
            if not os.path.isfile(stack):
                print("\n--- variant %s: %s (%s)" % (v, " ".join(flt) or "no filter", datetime.datetime.now().strftime("%T")))
                run_siril(siril, os.path.join(run, "reg_%s.ssf" % v),
                          ["set16bits", "setext fit", "cd " + work,
                           "seqapplyreg pp_light -framing=max -prefix=%s %s" % (pre, " ".join(flt))], log)
                rseq = os.path.join(work, pre + "pp_light_.seq")
                if os.path.isfile(rseq):
                    with open(rseq) as fh:
                        for line in fh:
                            if line.startswith("S "):
                                nreg = int(line.split()[3])
                                break
                run_siril(siril, os.path.join(run, "stack_%s.ssf" % v),
                          ["setext fit", "cd " + work,
                           "stack %spp_light %s -out=%s" % (pre, STACK, stack[:-4])], log)
                for f in glob.glob(os.path.join(work, pre + "pp_light_*")):
                    os.remove(f)
                with open(stack[:-4] + ".frames", "w") as fh:
                    fh.write("%s\n" % (nreg if nreg is not None else ""))
            elif os.path.isfile(stack[:-4] + ".frames"):
                with open(stack[:-4] + ".frames") as fh:
                    s = fh.read().strip()
                    nreg = int(s) if s else None
            if not os.path.isfile(stack):
                die("variant %s produced no stack; see %s" % (v, log))
            W, H = image_size(stack)
            if a.crop:
                x, y, cw_, ch = (int(n) for n in a.crop.split(","))
            else:
                cw_, ch = min(2000, W), min(2000, H)
                x, y = (W - cw_) // 2, (H - ch) // 2
            starfile = os.path.join(run, "stars_%s.lst" % v)
            jpg = os.path.join(out, "crop_%s" % v)
            lines = run_siril(siril, os.path.join(run, "measure_%s.ssf" % v),
                              ["load " + stack[:-4], "crop %d %d %d %d" % (x, y, cw_, ch), "bgnoise",
                               "findstar -out=" + starfile, "autostretch -linked", "savejpg %s 92" % jpg], log)
            noise, fw, nst = parse_measure(lines, starfile)
            rows.append((v, " ".join(flt) or "(none)", nreg, fw, nst, noise))
        base = next((r for r in rows if r[0] == "all"), rows[0] if rows else None)
        L = ["Comparison stacks for %s %s, measured in crop x=%d y=%d %dx%d" % (tname, night, x, y, cw_, ch), "",
             "%-9s %7s %11s %7s %13s %10s  %s" % ("variant", "frames", "star FWHM", "stars", "bg noise", "vs %s" % base[0], "filters")]
        for v, flt, n, fw, nst, noise in rows:
            rel = ("%+.1f%%" % (100.0 * (noise / base[5] - 1))) if noise and base[5] else "-"
            L.append("%-9s %7s %11s %7s %13s %10s  %s" % (v, n if n is not None else "?", fmt(fw) if fw else "?",
                                                         nst, fmt(noise) if noise else "?", rel, flt))
        L += ["", "Lower star FWHM = sharper. Lower background noise = smoother faint detail.",
              "Same-stretch crops: crop_<variant>.jpg. Stacks: %s/stack_<variant>.fit" % run,
              "A '?' means the value couldn't be read from Siril's output; see %s" % log]
        txt = "\n".join(L)
        with open(os.path.join(out, "comparison.txt"), "w") as fh:
            fh.write(txt + "\n")
        print()
        print(txt)
    print("\nReport in %s" % out)
    print("Scratch (delete when done): %s" % run)


if __name__ == "__main__":
    main()
