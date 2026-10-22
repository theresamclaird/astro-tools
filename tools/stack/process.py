#!/usr/bin/env python3
"""astro-process: take a stacked linear image to a stretched 16-bit TIFF with Siril,
choosing every stretch setting from the measured sky level.

  astro-process TARGET [RUN] [--crop X,Y,W,H] [--sensor S] [--filter F]
                [--whiteref W] [--bg-degree N] [--sky TARGET] [--saturation A]
                [--dry-run]

  TARGET   target folder or unique prefix (M31)
  RUN      processing/ run folder or unique prefix (default: the newest one that
           holds a *_linear.fit, e.g. 2026-10-18_siril-all)
  --crop   crop rectangle in stack pixels (default: trim the empty canvas edges)
  --sensor / --filter / --whiteref
           SPCC settings (defaults: "ZWO Seestar S50 Pro", "UV/IR Block",
           "Average Spiral Galaxy"). Must match Siril's names exactly.
  --bg-degree  background-extraction polynomial degree (default 2; 0 = skip)
  --sky    final sky brightness, 0-1 (default 0.10)
  --saturation  colour saturation amount (default 0.5; 0 = skip)

Steps, each saved as its own FITS in processing/<run>/auto/ so you can pick up
from any of them in Siril:
  01_crop        crop (given rectangle, or trimmed to the area with data)
  02_bg          background extraction (polynomial; samples placed automatically)
  03_spcc        plate-solve + spectrophotometric colour calibration
  04_offset      black point per channel, just below the sky (removes the grey
                 offset background extraction leaves; without it colour washes out)
  05_stretch1    GHS, luminance: SP just above the sky, D 3, b 6, HP 0.95
  06_stretch2    GHS, luminance: SP above the new sky, LP at the sky, D 2, b 4
  07_stretched   black point so the sky sits at --sky, clipping <= 0.1%
  08_final       saturation; also written as a 16-bit TIFF for Photoshop
The sky level is measured after every step (median of the dimmest half of the
image, which is sky on these fields), and the values used are written to
auto/README.txt. It's a consistent starting point, not a replacement for judging
the result: open 07 or 08 in Siril and adjust from there.
"""
import argparse, glob, os, re, shutil, subprocess, sys, datetime
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from frame_stats import find_siril, find_dir, die  # noqa: E402


# ---------------------------------------------------------------- FITS I/O

def read_fits(path):
    """Return a float32 array (channels, height, width) from a Siril FITS."""
    with open(path, "rb") as f:
        hdr, n = {}, 0
        while True:
            block = f.read(2880)
            if len(block) < 2880:
                die("truncated FITS header: " + path)
            n += 1
            done = False
            for i in range(0, 2880, 80):
                card = block[i:i + 80].decode("ascii", "replace")
                key = card[:8].strip()
                if key == "END":
                    done = True
                    break
                if card[8:10] == "= ":
                    v = card[10:].split("/")[0].strip().strip("'").strip()
                    hdr[key] = v
            if done:
                break
        bitpix = int(hdr["BITPIX"])
        nx, ny = int(hdr["NAXIS1"]), int(hdr["NAXIS2"])
        nc = int(hdr.get("NAXIS3", 1)) if int(hdr["NAXIS"]) >= 3 else 1
        dt = {-32: ">f4", -64: ">f8", 16: ">i2", 8: "u1", 32: ">i4"}[bitpix]
        data = np.fromfile(f, dtype=dt, count=nx * ny * nc).astype(np.float32)
    data = data.reshape(nc, ny, nx)
    bscale, bzero = float(hdr.get("BSCALE", 1)), float(hdr.get("BZERO", 0))
    if bscale != 1 or bzero != 0:
        data = data * bscale + bzero
    if bitpix > 0:  # integer data: normalise to 0-1
        data = data / (65535.0 if bitpix == 16 else 255.0 if bitpix == 8 else 4294967295.0)
    return data


def sky_stats(data):
    """Per-image sky level and noise from a subsample of the dimmest half."""
    sub = data[:, ::4, ::4]
    lum = sub.mean(axis=0).ravel()
    lum = lum[np.isfinite(lum) & (lum != 0)]
    med = np.median(lum)
    low = lum[lum <= med]
    sky = float(np.median(low))
    sigma = float(1.4826 * np.median(np.abs(low - sky))) or 1e-4
    chans = [float(np.median(c[np.isfinite(c) & (c != 0)])) for c in sub.reshape(sub.shape[0], -1)]
    return sky, sigma, chans, lum


def auto_crop(data):
    """Largest-ish rectangle without empty (zero) canvas: trim the worst edge until clean."""
    empty = (data == 0).all(axis=0) | ~np.isfinite(data).all(axis=0)
    y0, y1, x0, x1 = 0, empty.shape[0], 0, empty.shape[1]
    step = max(4, min(empty.shape) // 400)
    while y1 - y0 > 100 and x1 - x0 > 100:
        box = empty[y0:y1, x0:x1]
        if box.mean() < 1e-5:
            break
        edges = {"top": box[:step].mean(), "bottom": box[-step:].mean(),
                 "left": box[:, :step].mean(), "right": box[:, -step:].mean()}
        worst = max(edges, key=edges.get)
        if worst == "top": y0 += step
        elif worst == "bottom": y1 -= step
        elif worst == "left": x0 += step
        else: x1 -= step
    # a small margin past the hard edge, where few frames overlapped
    m = int(0.01 * min(y1 - y0, x1 - x0))
    return x0 + m, y0 + m, (x1 - x0) - 2 * m, (y1 - y0) - 2 * m


# ---------------------------------------------------------------- Siril

def siril_step(siril, workdir, name, lines, log):
    script = os.path.join(workdir, name + ".ssf")
    with open(script, "w") as f:
        f.write("requires 1.4.0\nset32bits\nsetext fit\n" + "\n".join(lines) + "\nclose\n")
    cmd = siril + ["-d", workdir, "-s", script]
    with open(log, "a") as lf:
        lf.write("\n=== %s %s\n" % (datetime.datetime.now().strftime("%F %T"), name))
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        lf.write(p.stdout)
    out = os.path.join(workdir, name + ".fit")
    if p.returncode != 0 or not os.path.isfile(out):
        tail = "\n".join(p.stdout.strip().splitlines()[-15:])
        die("Siril step %s failed; last lines:\n%s\nFull log: %s" % (name, tail, log))
    return out, p.stdout


def q(s):
    return '"%s"' % s


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target")
    ap.add_argument("run", nargs="?")
    ap.add_argument("--crop")
    ap.add_argument("--sensor", default="ZWO Seestar S50 Pro")
    ap.add_argument("--filter", default="UV/IR Block")
    ap.add_argument("--whiteref", default="Average Spiral Galaxy")
    ap.add_argument("--bg-degree", type=int, default=2)
    ap.add_argument("--sky", type=float, default=0.10)
    ap.add_argument("--saturation", type=float, default=0.5)
    ap.add_argument("--archive", default=os.environ.get("ASTRO_ARCHIVE") or "/Volumes/Astro")
    ap.add_argument("--dry-run", "-n", action="store_true")
    a = ap.parse_args()

    tdir = os.path.join(a.archive, "Targets", find_dir(os.path.join(a.archive, "Targets"), a.target, "target"))
    proc = os.path.join(tdir, "processing")
    if a.run:
        run = find_dir(proc, a.run, "run")
    else:
        runs = sorted(d for d in os.listdir(proc) if glob.glob(os.path.join(proc, d, "*_linear.fit")))
        if not runs:
            die("no processing/<run>/*_linear.fit under " + proc + " (run astro-stack first)")
        run = runs[-1]
    rdir = os.path.join(proc, run)
    lin = sorted(glob.glob(os.path.join(rdir, "*_linear.fit")))
    if not lin:
        die("no *_linear.fit in " + rdir)
    lin = lin[0]
    auto = os.path.join(rdir, "auto")
    log = os.path.join(auto, "astro-process.log")
    tname = os.path.basename(tdir)
    print("Target: %s   run: %s\nInput:  %s\nOutput: %s/" % (tname, run, lin, auto))
    if a.dry_run:
        print("(dry run: nothing done)")
        return
    siril = find_siril()
    os.makedirs(auto, exist_ok=True)
    notes = ["astro-process %s %s (%s)" % (tname, run, datetime.datetime.now().strftime("%F %T")),
             "input: " + lin, ""]

    def note(s):
        print(s)
        notes.append(s)

    # 01 crop
    data = read_fits(lin)
    if a.crop:
        x, y, w, h = (int(v) for v in a.crop.split(","))
    else:
        x, y, w, h = auto_crop(data)
    note("01 crop: x=%d y=%d w=%d h=%d (from %dx%d)" % (x, y, w, h, data.shape[2], data.shape[1]))
    H = data.shape[1]
    out = None
    # Siril's crop y can count from the top or the bottom of the array depending on
    # the file's row order; take the one that leaves no empty canvas.
    best = None
    for yy in ([y] if a.crop or y == H - y - h else [y, H - y - h]):
        out, _ = siril_step(siril, auto, "01_crop", ["load " + q(lin), "crop %d %d %d %d" % (x, yy, w, h),
                                                      "save " + q(os.path.join(auto, "01_crop"))], log)
        empty = float((read_fits(out) == 0).all(axis=0).mean())
        if empty < 1e-4:
            best = None
            break
        if best is None or empty < best[1]:
            best = (yy, empty)
    if best is not None and not a.crop:
        out, _ = siril_step(siril, auto, "01_crop", ["load " + q(lin), "crop %d %d %d %d" % (x, best[0], w, h),
                                                      "save " + q(os.path.join(auto, "01_crop"))], log)
        if best[1] > 0.001:
            note("    note: %.1f%% of the crop is still empty canvas; pass --crop X,Y,W,H" % (100 * best[1]))
    del data
    prev = out
    # 02 background
    if a.bg_degree > 0:
        out, _ = siril_step(siril, auto, "02_bg", ["load " + q(prev), "subsky %d -samples=20 -tolerance=2.0" % a.bg_degree,
                                                    "save " + q(os.path.join(auto, "02_bg"))], log)
        note("02 background extraction: polynomial degree %d, automatic samples" % a.bg_degree)
        prev = out
    # 03 plate-solve + SPCC
    out, txt = siril_step(siril, auto, "03_spcc", [
        "load " + q(prev), "platesolve",
        "spcc %s %s %s" % (q("-oscsensor=" + a.sensor), q("-oscfilter=" + a.filter), q("-whiteref=" + a.whiteref)),
        "save " + q(os.path.join(auto, "03_spcc"))], log)
    m = re.findall(r"K([012]):\s*([0-9.]+)", txt)
    note("03 plate-solve + SPCC (%s / %s / %s)%s" % (a.sensor, a.filter, a.whiteref,
         ("; factors " + " ".join(v for _, v in m[:3])) if m else ""))
    prev = out

    # 04 offset: per-channel black point just below the sky
    sky, sig, chans, _ = sky_stats(read_fits(prev))
    bp = max(0.0, min(chans) - 5 * sig)
    note("    sky after SPCC: %.5f (noise %.5f; R/G/B %s)" % (sky, sig, " ".join("%.5f" % c for c in chans)))
    if bp > 0:
        out, _ = siril_step(siril, auto, "04_offset", ["load " + q(prev), "linstretch -BP=%.6f" % bp,
                                                       "save " + q(os.path.join(auto, "04_offset"))], log)
        note("04 black point (per channel): %.5f" % bp)
        prev = out
    else:
        note("04 no offset to remove")

    # 05 first stretch
    sky, sig, _, _ = sky_stats(read_fits(prev))
    sp = max(0.0, sky) + max(4 * sig, 0.002)
    out, _ = siril_step(siril, auto, "05_stretch1", ["load " + q(prev),
                        "ght -D=3 -B=6 -SP=%.6f -LP=0 -HP=0.95 -human" % sp,
                        "save " + q(os.path.join(auto, "05_stretch1"))], log)
    note("05 stretch 1: sky %.5f -> GHS D=3 b=6 SP=%.5f LP=0 HP=0.95" % (sky, sp))
    prev = out

    # 06 second stretch
    sky, sig, _, _ = sky_stats(read_fits(prev))
    sp2, lp2 = sky * 1.3, sky
    out, _ = siril_step(siril, auto, "06_stretch2", ["load " + q(prev),
                        "ght -D=2 -B=4 -SP=%.6f -LP=%.6f -HP=0.95 -human" % (sp2, lp2),
                        "save " + q(os.path.join(auto, "06_stretch2"))], log)
    note("06 stretch 2: sky %.5f -> GHS D=2 b=4 SP=%.5f LP=%.5f HP=0.95" % (sky, sp2, lp2))
    prev = out

    # 07 final black point
    data = read_fits(prev)
    sky, sig, _, lum = sky_stats(data)
    del data
    if sky > a.sky:
        bp = (sky - a.sky) / (1 - a.sky)
        limit = float(np.quantile(lum, 0.001))  # never clip more than 0.1%
        if bp > limit:
            note("    (black point limited by clipping: %.5f -> %.5f)" % (bp, limit))
            bp = limit
        out, _ = siril_step(siril, auto, "07_stretched", ["load " + q(prev), "linstretch -BP=%.6f" % bp,
                                                          "save " + q(os.path.join(auto, "07_stretched"))], log)
        note("07 sky %.5f -> black point %.5f (target sky %.2f)" % (sky, bp, a.sky))
        prev = out
    else:
        note("07 sky %.5f already at or below %.2f; no black point" % (sky, a.sky))
        shutil.copyfile(prev, os.path.join(auto, "07_stretched.fit"))
        prev = os.path.join(auto, "07_stretched.fit")

    # 08 saturation + TIFF
    tif = os.path.join(auto, "%s_%s_auto" % (tname.split("_")[0], run))
    lines = ["load " + q(prev)]
    if a.saturation > 0:
        lines.append("satu %.3f 1.0" % a.saturation)
    lines += ["save " + q(os.path.join(auto, "08_final")), "savetif " + q(tif)]
    siril_step(siril, auto, "08_final", lines, log)
    sky, _, chans, _ = sky_stats(read_fits(os.path.join(auto, "08_final.fit")))
    note("08 saturation %.2f; final sky %.4f (R/G/B %s)" % (a.saturation, sky, " ".join("%.4f" % c for c in chans)))
    note("")
    note("TIFF (16-bit) for Photoshop: %s.tif" % tif)
    with open(os.path.join(auto, "README.txt"), "w") as f:
        f.write("\n".join(notes) + "\n")
    print("Steps and settings: %s/README.txt   Log: %s" % (auto, log))


if __name__ == "__main__":
    main()
