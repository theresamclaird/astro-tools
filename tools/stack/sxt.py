#!/usr/bin/env python3
"""astro-sxt: split a stretched image into starless and stars layers with RC Astro's
StarXTerminator (the rc-astro command-line tool), ready to stack in Photoshop.

  astro-sxt IMAGE... [--depth 16U] [--no-unscreen] [--device auto] [--overwrite]
            [--dry-run]

  IMAGE         stretched TIFF (or FITS/XISF/PNG); several, or a wildcard, are fine
  --depth       output bit depth: 8U, 16U, 32F or 64F (default 16U, for Photoshop)
  --no-unscreen write stars as original minus starless instead of "unscreened".
                Unscreened stars (the default) put back with blend mode Screen
                rebuild the original exactly, which is how the Photoshop layer stack
                in PIPELINE.md uses them.
  --device      compute device for rc-astro: auto, cpu, gpu, gpuN (default auto)
  --overwrite   replace existing outputs (default: skip images already split)

For each IMAGE it writes, beside it:
  <name>_starless.tif   the starless image (the `sxt` layer)
  <name>_stars.tif      the stars (the `stars` layer, blend mode Screen)

Needs rc-astro installed and StarXTerminator activated (`rc-astro license`).
Set RC_ASTRO to its path if it isn't on your PATH.
"""
import argparse, glob, os, shutil, subprocess, sys, tempfile


def find_rc_astro():
    """Path to the rc-astro command, or None."""
    p = os.environ.get("RC_ASTRO")
    if p:
        return p if os.access(p, os.X_OK) else None
    return shutil.which("rc-astro")


def outputs_for(image):
    stem, _ = os.path.splitext(image)
    return stem + "_starless.tif", stem + "_stars.tif"


def split(image, rc, depth="16U", unscreen=True, device="auto", overwrite=False, log=None):
    """Run StarXTerminator on one image. Returns (starless, stars) paths.

    rc-astro writes into a private folder beside the image; the two results are
    then renamed to <name>_starless.tif and <name>_stars.tif. Raises RuntimeError
    if rc-astro fails or doesn't produce both files.
    """
    starless, stars = outputs_for(image)
    if not overwrite and os.path.exists(starless) and os.path.exists(stars):
        raise FileExistsError(starless)
    folder = os.path.dirname(os.path.abspath(image))
    tmp = tempfile.mkdtemp(prefix=".sxt-", dir=folder)
    try:
        cmd = [rc, "sxt", image, "--output", tmp + os.sep, "--stars", "--depth", depth, "--device", device]
        if unscreen:
            cmd.append("--unscreen")
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if log:
            with open(log, "a") as f:
                f.write("$ " + " ".join(cmd) + "\n" + res.stdout + "\n")
        if res.returncode != 0:
            tail = "\n".join(res.stdout.strip().splitlines()[-15:])
            raise RuntimeError("rc-astro sxt failed (exit %d):\n%s" % (res.returncode, tail))
        made = sorted(os.path.join(tmp, f) for f in os.listdir(tmp) if not f.startswith("."))
        star_files = [f for f in made if "star" in os.path.basename(f).lower()]
        other = [f for f in made if f not in star_files]
        if len(star_files) != 1 or len(other) != 1:
            raise RuntimeError("expected a starless and a stars image from rc-astro, got: %s"
                               % (", ".join(os.path.basename(f) for f in made) or "nothing"))
        for src, dst in ((other[0], starless), (star_files[0], stars)):
            if not dst.lower().endswith(os.path.splitext(src)[1].lower()):
                dst = os.path.splitext(dst)[0] + os.path.splitext(src)[1]
            os.replace(src, dst)
        return starless, stars
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="+", metavar="IMAGE")
    ap.add_argument("--depth", default="16U", choices=["8U", "16U", "32F", "64F"])
    ap.add_argument("--no-unscreen", dest="unscreen", action="store_false")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--dry-run", "-n", action="store_true")
    a = ap.parse_args()

    images = []
    for pat in a.images:
        hits = sorted(glob.glob(pat)) or [pat]
        images += [h for h in hits if not h.endswith(("_starless.tif", "_stars.tif"))]
    missing = [i for i in images if not os.path.isfile(i)]
    if missing:
        sys.exit("astro-sxt: no such file: " + ", ".join(missing))
    rc = find_rc_astro()
    if not rc:
        sys.exit("astro-sxt: rc-astro not found. Install RC Astro's command-line tools from your\n"
                 "rc-astro.com account page, then run `rc-astro license` (or set RC_ASTRO).")

    failed = 0
    for img in images:
        starless, stars = outputs_for(img)
        if a.dry_run:
            print("would split %s\n  -> %s\n  -> %s" % (img, starless, stars))
            continue
        print("StarXTerminator: %s" % img, flush=True)
        try:
            s, t = split(img, rc, a.depth, a.unscreen, a.device, a.overwrite)
            print("  starless: %s\n  stars:    %s" % (s, t))
        except FileExistsError:
            print("  already split (use --overwrite to redo)")
        except RuntimeError as e:
            failed += 1
            print("  FAILED: %s" % e)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
