#!/usr/bin/env python3
"""
Ingest new Seestar data into the Astro archive.

    astro-ingest [SOURCE ...] [--dry-run] [--yes] [--no-previews] [--archive PATH]

SOURCE is a Seestar "MyWorks" folder, or any folder containing Seestar output
(the telescope's storage, a copy on another drive, ...). Subfolders are searched.
With no SOURCE, $ASTRO_SOURCE is used, else the first MyWorks folder found on a
mounted drive or network share (macOS /Volumes, Linux /run/media/$USER, /media/$USER,
or a GNOME network mount). The archive likewise defaults to $ASTRO_ARCHIVE, else a
mounted drive named Astro.

What it does
  * Light frames   -> Targets/<target>/raw/<evening-date>_<scope>[_mosaic]_<filter>/
    per-frame JPGs -> .../previews/            (skip with --no-previews)
  * On-scope stacks-> Targets/<target>/seestar-stacks/
  * Moon/Sun video & photos -> Targets/Moon|Sun/raw/<evening-date>_<scope>/
  The "evening date" is the date the night started: frames taken before noon
  belong to the previous date. The scope (S50/S30) is read from the FITS header.

Safety
  * Copies only. Never deletes or modifies anything in SOURCE.
  * Never overwrites a different file in the archive (reported as a conflict).
  * Every copied file is read back and compared (SHA-256) with the source,
    then appended to MANIFEST.sha256.
  * Re-running is safe: files already in the archive are skipped.
  * Each target's notes.md gets its "Raw sessions" table refreshed; everything
    else in notes.md (your own notes) is left untouched.

Target names
  Seestar names are mapped to archive folders: Caldwell / star-name aliases
  below, then _tools/targets.tsv (optional, "Seestar name<TAB>folder name" per
  line), then any existing Targets/ folder whose name starts with the catalog
  id (e.g. "NGC 7000" -> NGC7000_North-America-Nebula). Otherwise a new folder
  named after the catalog id is created; you can rename it to add a common name
  (keep the catalog prefix, e.g. NGC7331 -> NGC7331_Deer-Lick-Galaxy).
"""
import argparse, collections, datetime, getpass, glob, hashlib, os, re, shutil, sys


def _mount_roots():
    """Where removable drives appear: macOS, then Linux (udisks, then older /media)."""
    user = os.environ.get("USER") or getpass.getuser()
    return ["/Volumes", "/run/media/%s" % user, "/media/%s" % user]


def _find_archive():
    for root in _mount_roots():
        p = os.path.join(root, "Astro")
        if os.path.isfile(os.path.join(p, "MANIFEST.sha256")):
            return p
    return "/Volumes/Astro"


def _find_source():
    """The Seestar's MyWorks folder: over USB it mounts like a drive; over the
    network GNOME mounts its share under /run/user/<uid>/gvfs/."""
    pats = [os.path.join(r, "*", "MyWorks") for r in _mount_roots()]
    pats.append("/run/user/%d/gvfs/*/MyWorks" % os.getuid())
    for pat in pats:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return "/Volumes/Seestar/MyWorks"


ARCHIVE_DEFAULT = os.environ.get("ASTRO_ARCHIVE") or _find_archive()
SOURCE_DEFAULT = os.environ.get("ASTRO_SOURCE") or _find_source()

ALIASES = {  # Seestar object name -> catalog id used for folder matching
    "C 12": "NGC 6946", "C 19": "IC 5146", "C 20": "NGC 7000", "C 23": "NGC 891",
    "Sadr": "IC 1318", "Lunar": "Moon", "Solar": "Sun",
}
JUNK_DIRS = {"__pycache__", "_to_delete", ".Spotlight-V100", ".fseventsd", ".Trashes", "cache"}

LIGHT = re.compile(r"^Light_(?P<mosaic>mosaic_)?(?P<obj>.+?)_(?P<exp>[\d.]+s)_(?P<filt>[A-Za-z0-9]+)_"
                   r"(?P<d>\d{8})-(?P<t>\d{6})(?P<thn>_thn)?\.(?P<ext>fit|fits|jpg)$")
STACK = re.compile(r"^Stacked_\d+_(?P<mosaic>mosaic_)?(?P<obj>.+?)_(?P<exp>[\d.]+s)_(?P<filt>[A-Za-z0-9]+)_"
                   r"(?P<d>\d{8})-(?P<t>\d{6})(?P<thn>_thn)?\.(?P<ext>fit|fits|jpg)$")
SOLSYS = re.compile(r"^(?P<d>\d{4}-\d{2}-\d{2})-(?P<t>\d{6})-(?P<obj>Lunar|Solar|[A-Za-z]+)(?P<raw>-RAW)?"
                    r"(?P<thn>_thn)?\.(?P<ext>jpg|png|mp4|avi|avi\.txt|ser)$")
GiB = 1024 ** 3


def night(d8, t6):
    dt = datetime.datetime.strptime(d8 + t6, "%Y%m%d%H%M%S")
    if dt.hour < 12:
        dt -= datetime.timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def fits_header(path):
    hdr = {}
    try:
        with open(path, "rb") as fh:
            for _ in range(10):  # up to 10 header blocks
                block = fh.read(2880).decode("latin1")
                for i in range(0, len(block), 80):
                    card = block[i:i + 80]
                    if card.startswith("END"):
                        return hdr
                    if "=" in card[:10]:
                        key = card[:8].strip()
                        val = card[10:].split("/")[0].strip().strip("'").strip()
                        hdr[key] = val
    except OSError:
        pass
    return hdr


def scope_from_header(hdr):
    s = (hdr.get("CREATOR", "") + " " + hdr.get("INSTRUME", "") + " " + hdr.get("TELESCOP", "")).upper()
    if "S30" in s:
        return "S30"
    if "S50" in s:
        return "S50"
    return None


def scope_from_path(path):
    p = path.upper()
    if "S30" in p:
        return "S30"
    if "S50" in p:
        return "S50"
    return None


class TargetResolver:
    def __init__(self, archive):
        self.tdir = os.path.join(archive, "Targets")
        self.existing = sorted(os.listdir(self.tdir)) if os.path.isdir(self.tdir) else []
        self.user = {}
        tsv = os.path.join(archive, "_tools", "targets.tsv")
        if os.path.isfile(tsv):
            for line in open(tsv, encoding="utf-8"):
                if line.strip() and not line.startswith("#") and "\t" in line:
                    k, v = line.rstrip("\n").split("\t", 1)
                    self.user[k.strip()] = v.strip()
        self.new = set()

    def folder(self, obj):
        if obj in self.user:
            return self.user[obj]
        obj = obj.replace("_", " ").strip()
        if obj in self.user:
            return self.user[obj]
        cat = ALIASES.get(obj, obj)
        key = re.sub(r"[^A-Za-z0-9]+", "", cat)
        for name in self.existing:
            if name == key or name.startswith(key + "_"):
                return name
        self.new.add(key)
        return key


def plan(sources, archive, previews):
    res = TargetResolver(archive)
    items = {}          # dst(rel) -> src
    conflicts, unknown, thumbs = [], [], 0
    scope_cache = {}

    def dir_scope(dirpath, fitpath=None):
        if dirpath not in scope_cache or (scope_cache[dirpath] is None and fitpath):
            sc = scope_from_header(fits_header(fitpath)) if fitpath else None
            scope_cache[dirpath] = sc or scope_from_path(dirpath)
        return scope_cache[dirpath]

    for src_root in sources:
        for dp, dns, fns in os.walk(src_root):
            dns[:] = [d for d in dns if d not in JUNK_DIRS and not d.startswith(".")]
            fits_here = sorted(f for f in fns if f.lower().endswith((".fit", ".fits")) and not f.startswith("._"))
            first_fit = os.path.join(dp, fits_here[0]) if fits_here else None
            for n in fns:
                if n.startswith("._") or n == ".DS_Store" or n.endswith(".partial"):
                    continue
                src = os.path.join(dp, n)
                m = LIGHT.match(n)
                if m:
                    if m.group("thn") and not previews:
                        continue
                    if m.group("ext") == "jpg" and not previews:
                        continue
                    sc = dir_scope(dp, first_fit) or "Seestar"
                    sess = "%s_%s%s_%s" % (night(m.group("d"), m.group("t")), sc,
                                           "_mosaic" if m.group("mosaic") else "", m.group("filt").upper())
                    tgt = res.folder(m.group("obj"))
                    sub = "previews/" if m.group("ext") == "jpg" else ""
                    dst = "Targets/%s/raw/%s/%s%s" % (tgt, sess, sub, n)
                else:
                    m = STACK.match(n)
                    if m:
                        if m.group("thn"):
                            thumbs += 1
                            continue
                        dst = "Targets/%s/seestar-stacks/%s" % (res.folder(m.group("obj")), n)
                    else:
                        m = SOLSYS.match(n)
                        if m:
                            if m.group("thn"):
                                thumbs += 1
                                continue
                            d8 = m.group("d").replace("-", "")
                            sc = scope_from_path(dp) or "Seestar"
                            dst = "Targets/%s/raw/%s_%s/%s" % (res.folder(m.group("obj")),
                                                               night(d8, m.group("t")), sc, n)
                        else:
                            unknown.append(src)
                            continue
                if dst in items:
                    if os.path.getsize(items[dst]) != os.path.getsize(src):
                        conflicts.append((src, dst, "two different source files map to the same name"))
                    continue
                items[dst] = src

    todo, already = [], 0
    for dst, src in sorted(items.items()):
        full = os.path.join(archive, dst)
        if os.path.exists(full):
            if os.path.getsize(full) == os.path.getsize(src):
                already += 1
            else:
                conflicts.append((src, dst, "a different file with this name is already in the archive"))
        else:
            todo.append((src, dst))
    return todo, already, conflicts, unknown, thumbs, res.new


def copy_verify(src, dst_full):
    os.makedirs(os.path.dirname(dst_full), exist_ok=True)
    tmp = dst_full + ".partial"
    h = hashlib.sha256()
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        while True:
            b = fi.read(8 * 1024 * 1024)
            if not b:
                break
            h.update(b)
            fo.write(b)
        fo.flush()
        os.fsync(fo.fileno())
    st = os.stat(src)
    os.utime(tmp, (st.st_atime, st.st_mtime))
    h2 = hashlib.sha256()
    with open(tmp, "rb") as fr:
        while True:
            b = fr.read(8 * 1024 * 1024)
            if not b:
                break
            h2.update(b)
    if h.hexdigest() != h2.hexdigest():
        os.remove(tmp)
        raise IOError("checksum mismatch after copy")
    os.replace(tmp, dst_full)
    return h.hexdigest()


NOTES_TEMPLATE = "# {title}\n\n## Raw sessions\n\n## Notes\n\n- \n"


def refresh_notes(archive, target):
    tdir = os.path.join(archive, "Targets", target)
    raw = os.path.join(tdir, "raw")
    rows, tot_n, tot_b = [], 0, 0
    if os.path.isdir(raw):
        for sess in sorted(os.listdir(raw)):
            p = os.path.join(raw, sess)
            if not os.path.isdir(p):
                continue
            fits = [f for f in os.listdir(p) if f.lower().endswith((".fit", ".fits")) and not f.startswith("._")]
            if fits:
                n = len(fits)
                b = sum(os.path.getsize(os.path.join(p, f)) for f in fits)
                exp = 10.0
                m = LIGHT.match(fits[0])
                if m:
                    exp = float(m.group("exp").rstrip("s"))
                hours = n * exp / 3600
                rows.append("| `%s` | %d | %.1f h | %.1f GiB |" % (sess, n, hours, b / GiB))
                tot_n += n
                tot_b += b
            else:
                rows.append("| `%s` | video/photos | | |" % sess)
    # total integration (assumes per-file exposure from filenames)
    total_h = 0.0
    if os.path.isdir(raw):
        for sess in os.listdir(raw):
            p = os.path.join(raw, sess)
            if os.path.isdir(p):
                for f in os.listdir(p):
                    m = LIGHT.match(f)
                    if m and m.group("ext") != "jpg":
                        total_h += float(m.group("exp").rstrip("s")) / 3600
    table = ["## Raw sessions", "",
             "| Session (evening start) | Light frames | Integration | Size |", "|---|---|---|---|"] + rows
    if tot_n:
        table += ["", "**Total:** %d frames, %.1f h, %.1f GiB" % (tot_n, total_h, tot_b / GiB)]
    block = "\n".join(table) + "\n"

    path = os.path.join(tdir, "notes.md")
    if os.path.isfile(path):
        text = open(path, encoding="utf-8").read()
    else:
        title = target.replace("_", " — ", 1).replace("-", " ")
        text = NOTES_TEMPLATE.format(title=title)
    m = re.search(r"^## Raw sessions\s*$", text, re.M)
    if m:
        nxt = re.search(r"^## ", text[m.end():], re.M)
        end = m.end() + nxt.start() if nxt else len(text)
        text = text[:m.start()] + block + "\n" + text[end:]
    else:
        text = text.rstrip("\n") + "\n\n" + block
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def main():
    ap = argparse.ArgumentParser(prog="astro-ingest", description="Ingest Seestar data into the Astro archive.")
    ap.add_argument("sources", nargs="*", help="Seestar MyWorks folder(s) (default: $ASTRO_SOURCE, %s)" % SOURCE_DEFAULT)
    ap.add_argument("--archive", default=ARCHIVE_DEFAULT)
    ap.add_argument("--dry-run", action="store_true", help="show what would be copied, change nothing")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--no-previews", action="store_true", help="skip the per-frame JPG previews")
    a = ap.parse_args()

    if not a.sources:
        a.sources = [SOURCE_DEFAULT]

    if not os.path.isfile(os.path.join(a.archive, "MANIFEST.sha256")):
        sys.exit("Archive not found at %s (no MANIFEST.sha256). Is the Astro drive mounted?" % a.archive)
    for s in a.sources:
        if not os.path.isdir(s):
            sys.exit("Source folder not found: %s (is the Seestar connected and mounted?)" % s)

    print("Scanning %s ..." % ", ".join(a.sources))
    todo, already, conflicts, unknown, thumbs, new_targets = plan(a.sources, a.archive, not a.no_previews)

    by = collections.OrderedDict()
    for src, dst in todo:
        parts = dst.split("/")
        key = "/".join(parts[1:4]) if parts[2] == "raw" else "/".join(parts[1:3])
        c = by.setdefault(key, [0, 0, 0])
        c[0] += 1
        c[1] += os.path.getsize(src)
        if dst.lower().endswith((".fit", ".fits")) and parts[2] == "raw":
            c[2] += 1
    total = sum(os.path.getsize(s) for s, _ in todo)
    print("\nNew files to add: %d (%.1f GiB)   already in archive: %d" % (len(todo), total / GiB, already))
    for k, (n, b, nf) in by.items():
        extra = "  (%d light frames, %.1f h at 10 s)" % (nf, nf * 10 / 3600) if nf else ""
        print("  %6d  %7.2f GiB  %s%s" % (n, b / GiB, k, extra))
    for t in sorted(new_targets):
        print("  NEW TARGET FOLDER: Targets/%s  (rename later to add a common name, keeping the '%s' prefix)" % (t, t))
    if thumbs:
        print("  (skipping %d Seestar thumbnails)" % thumbs)
    if unknown:
        print("\nNot recognized as Seestar output (left alone): %d file(s)" % len(unknown))
        for u in unknown[:15]:
            print("   ", u)
        if len(unknown) > 15:
            print("    ...")
    if conflicts:
        print("\nCONFLICTS (not copied):")
        for s, d, why in conflicts:
            print("   %s\n     -> %s: %s" % (s, d, why))
    if not todo:
        print("\nNothing new to ingest.")
        return 0 if not conflicts else 1
    if a.dry_run:
        print("\nDry run: nothing was copied.")
        return 0
    free = shutil.disk_usage(a.archive).free
    if free < total * 1.05:
        sys.exit("Not enough free space on the archive (%.1f GiB free)." % (free / GiB))
    if not a.yes:
        try:
            if input("\nCopy these into %s? [y/N] " % a.archive).strip().lower() not in ("y", "yes"):
                print("Cancelled; nothing copied.")
                return 0
        except EOFError:
            return 0

    manifest = os.path.join(a.archive, "MANIFEST.sha256")
    failed, done, done_b = [], 0, 0
    touched = set()
    start = last_report = datetime.datetime.now()
    streak = 0  # consecutive failures; a long run means the source has gone away
    with open(manifest, "a", encoding="utf-8") as mf:
        for src, dst in todo:
            try:
                digest = copy_verify(src, os.path.join(a.archive, dst))
                mf.write("%s  %s\n" % (digest, dst))
                mf.flush()
                done += 1
                done_b += os.path.getsize(src)
                touched.add(dst.split("/")[1])
                streak = 0
            except Exception as e:  # keep going; report at the end
                failed.append((src, str(e)))
                streak += 1
                if streak >= 20:
                    print("\n20 files in a row failed; the source seems to be disconnected. Stopping.", flush=True)
                    break
            now = datetime.datetime.now()
            if (now - last_report).total_seconds() >= 30 or done == len(todo):
                last_report = now
                el = (now - start).total_seconds()
                print("  %s  %d / %d  (%.1f GiB, %.0f MB/s%s)" % (now.strftime("%H:%M:%S"),
                      done, len(todo), done_b / GiB, done_b / 1e6 / max(el, 1),
                      ", %d failed so far" % len(failed) if failed else ""), flush=True)
    for t in sorted(touched):
        try:
            refresh_notes(a.archive, t)
        except Exception as e:
            print("  could not update notes.md for %s: %s" % (t, e))

    print("\nCopied and verified %d file(s), %.1f GiB." % (done, done_b / GiB))
    if touched:
        print("Updated notes.md for: %s" % ", ".join(sorted(touched)))
    if failed:
        print("\nFAILED (%d) - re-run to retry:" % len(failed))
        for s, e in failed:
            print("   %s: %s" % (s, e))
        return 1
    print("Source files were not touched. Back up with:  astro-backup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
