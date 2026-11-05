#!/usr/bin/env python3
"""
Merge one target's data into another target in the Astro archive.

    astro-merge-targets SOURCE DEST [--dry-run] [--yes] [--alias NAME ...] [--archive PATH]

Use it when two targets turn out to be one field of view (e.g. IC 5070, the
Pelican Nebula, inside the NGC 7000 North America Nebula mosaic) and should be
stacked together. SOURCE and DEST are target folder names or unique prefixes
(IC5070, NGC7000).

What it does
  * Moves SOURCE's raw/ and seestar-stacks/ into DEST at the same relative
    paths. A night folder that exists in both (same date, scope and filter) is
    merged: its files go into DEST's folder. Moves are renames on the archive
    drive, so file contents and checksums never change.
  * Rewrites the moved files' paths in MANIFEST.sha256 (a copy of the old
    manifest is kept in _tools/) and in the stacking queue.
  * Rebuilds the "Raw sessions" table in both targets' notes.md.
  * Adds "<Seestar name><TAB>DEST" to _tools/targets.tsv, so astro-ingest
    files future sessions of that object under DEST. The name is derived from
    SOURCE's catalog id (IC5070 -> "IC 5070"); add others with --alias.

Safety
  * Nothing is moved if any file would land on a different existing file.
    A file already at DEST with the same SHA-256 counts as merged: the SOURCE
    duplicate is removed.
  * SOURCE's notes.md, processing/ and final/ are left where they are, and the
    SOURCE folder itself is not deleted. Look through what's left and remove it
    by hand when you're happy.
  * Re-running after an interruption finishes the job: moved files are
    skipped and the manifest is rewritten from where the files actually are.
  * Backups never delete, so the NAS and mirror drive keep the old SOURCE
    paths as well as the new ones after the next astro-backup. Remove the old
    SOURCE/raw and SOURCE/seestar-stacks there by hand to reclaim the space.

After merging, stack DEST again (astro-stack DEST --dry-run first). Nights
whose frame count changed are restacked; the other stored masters are reused.
"""
import argparse, datetime, hashlib, os, re, shutil, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ingest import ARCHIVE_DEFAULT, QUEUE, refresh_notes  # noqa: E402

MOVED_DIRS = ("raw", "seestar-stacks")


def resolve(tdir, name):
    names = sorted(n for n in os.listdir(tdir) if os.path.isdir(os.path.join(tdir, n)))
    if name in names:
        return name
    hits = [n for n in names if n.startswith(name + "_")] or [n for n in names if n.startswith(name)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        sys.exit("No target folder matches '%s' in %s" % (name, tdir))
    sys.exit("'%s' matches several targets: %s" % (name, ", ".join(hits)))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def seestar_name(folder):
    """IC5070_Pelican-Nebula -> 'IC 5070' (the name the Seestar uses)."""
    cat = folder.split("_", 1)[0]
    m = re.match(r"^([A-Za-z]+)(\d.*)$", cat)
    return "%s %s" % (m.group(1), m.group(2)) if m else cat


def plan(archive, src, dst):
    """(moves, dups, conflicts): moves = [(old_rel, new_rel)], dups = same file already at DEST."""
    moves, dups, conflicts = [], [], []
    for sub in MOVED_DIRS:
        base = os.path.join(archive, "Targets", src, sub)
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs.sort()
            for f in sorted(files):
                full = os.path.join(root, f)
                inner = os.path.relpath(full, os.path.join(archive, "Targets", src))
                old = "/".join(["Targets", src] + inner.split(os.sep))
                new = "/".join(["Targets", dst] + inner.split(os.sep))
                target = os.path.join(archive, *new.split("/"))
                if os.path.lexists(target):
                    if sha256(full) == sha256(target):
                        dups.append((old, new))
                    else:
                        conflicts.append((old, new))
                else:
                    moves.append((old, new))
    return moves, dups, conflicts


def rewrite_manifest(archive, src, dst):
    """Point manifest lines for SOURCE's moved dirs at DEST wherever the file now lives there.
    Returns (rewritten, dropped_duplicates). Idempotent."""
    path = os.path.join(archive, "MANIFEST.sha256")
    prefixes = tuple("Targets/%s/%s/" % (src, d) for d in MOVED_DIRS)
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    present = {ln[66:] for ln in lines if len(ln) > 66}
    out, changed, dropped = [], 0, 0
    for ln in lines:
        rel = ln[66:] if len(ln) > 66 else ""
        if rel.startswith(prefixes):
            new = "Targets/%s/%s" % (dst, rel[len("Targets/%s/" % src):])
            if not os.path.exists(os.path.join(archive, *rel.split("/"))) and \
                    os.path.exists(os.path.join(archive, *new.split("/"))):
                if new in present:      # DEST already listed (a duplicate we removed)
                    dropped += 1
                    continue
                out.append(ln[:66] + new)
                present.add(new)
                changed += 1
                continue
        out.append(ln)
    if changed or dropped:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        keep = os.path.join(archive, "_tools", "MANIFEST.sha256.before-merge-%s" % stamp)
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        shutil.copy2(path, keep)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("".join(l + "\n" for l in out))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    return changed, dropped


def rewrite_queue(archive, src, dst):
    path = os.path.join(archive, QUEUE)
    if not os.path.isfile(path):
        return 0
    with open(path, encoding="utf-8") as fh:
        lines = [l.rstrip("\n") for l in fh if l.strip()]
    out, n = [], 0
    for l in lines:
        t, _, night = l.partition("\t")
        if t == src:
            l, n = "%s\t%s" % (dst, night), n + 1
        if l not in out:
            out.append(l)
    if n:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("".join(l + "\n" for l in out))
    return n


def add_aliases(archive, names, dst):
    path = os.path.join(archive, "_tools", "targets.tsv")
    have = {}
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            if line.strip() and not line.startswith("#") and "\t" in line:
                k, v = line.rstrip("\n").split("\t", 1)
                have[k.strip()] = v.strip()
    add = [n for n in names if have.get(n) != dst]
    clash = [(n, have[n]) for n in add if n in have]
    if clash:
        sys.exit("targets.tsv already maps %s; edit it by hand." %
                 ", ".join("'%s' -> %s" % c for c in clash))
    if add:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("".join("%s\t%s\n" % (n, dst) for n in add))
    return add


def nights(archive, target):
    raw = os.path.join(archive, "Targets", target, "raw")
    return sorted(os.listdir(raw)) if os.path.isdir(raw) else []


def prune_empty(base):
    for root, dirs, files in os.walk(base, topdown=False):
        if not os.listdir(root):
            os.rmdir(root)


def main():
    ap = argparse.ArgumentParser(prog="astro-merge-targets",
                                 description="Merge one target's raw data into another target.")
    ap.add_argument("source", help="target to merge from (folder name or unique prefix, e.g. IC5070)")
    ap.add_argument("dest", help="target to merge into (e.g. NGC7000)")
    ap.add_argument("--archive", default=ARCHIVE_DEFAULT)
    ap.add_argument("--alias", action="append", default=[],
                    help="extra Seestar object name to route to DEST (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    a = ap.parse_args()

    if not os.path.isfile(os.path.join(a.archive, "MANIFEST.sha256")):
        sys.exit("Archive not found at %s (no MANIFEST.sha256)." % a.archive)
    tdir = os.path.join(a.archive, "Targets")
    src, dst = resolve(tdir, a.source), resolve(tdir, a.dest)
    if src == dst:
        sys.exit("SOURCE and DEST are the same target: %s" % src)

    names = [seestar_name(src)] + [n for n in a.alias if n != seestar_name(src)]
    print("Merge  Targets/%s  ->  Targets/%s\n" % (src, dst))
    print("Hashing files that already exist at DEST, if any ...")
    moves, dups, conflicts = plan(a.archive, src, dst)

    dst_nights = set(nights(a.archive, dst))
    print("\nNights:")
    for n in nights(a.archive, src):
        files = [m for m in moves + dups if m[0].startswith("Targets/%s/raw/%s/" % (src, n))]
        fits = sum(1 for m in files if m[0].lower().endswith((".fit", ".fits")))
        how = "MERGE into existing night" if n in dst_nights else "new night for DEST"
        print("  %-28s %5d light frames, %5d files  (%s)" % (n, fits, len(files), how))
    other = [m for m in moves if m[0].startswith("Targets/%s/seestar-stacks/" % src)]
    if other:
        print("  seestar-stacks/              %5d files" % len(other))
    print("\nTo move: %d files   already at DEST (identical, will drop the duplicate): %d"
          % (len(moves), len(dups)))
    print("targets.tsv: future %s sessions -> %s" % (", ".join("'%s'" % n for n in names), dst))
    left = [d for d in sorted(os.listdir(os.path.join(tdir, src))) if d not in MOVED_DIRS]
    if left:
        print("Left in Targets/%s: %s" % (src, ", ".join(left)))

    if conflicts:
        print("\nCONFLICTS: these would replace a different file at DEST. Nothing was moved.")
        for o, n in conflicts[:20]:
            print("   %s\n     -> %s" % (o, n))
        return 1
    if a.dry_run:
        print("\nDry run: nothing was changed.")
        return 0
    if not a.yes:
        try:
            if input("\nMerge now? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Cancelled; nothing changed.")
                return 2
        except EOFError:
            print("\nCancelled (no answer). Use --yes to skip the question.")
            return 2

    for old, new in moves:
        o = os.path.join(a.archive, *old.split("/"))
        n = os.path.join(a.archive, *new.split("/"))
        os.makedirs(os.path.dirname(n), exist_ok=True)
        os.rename(o, n)              # same drive: a rename, never a copy
    for old, _ in dups:
        os.remove(os.path.join(a.archive, *old.split("/")))
    for sub in MOVED_DIRS:
        p = os.path.join(tdir, src, sub)
        if os.path.isdir(p):
            prune_empty(p)

    changed, dropped = rewrite_manifest(a.archive, src, dst)
    queued = rewrite_queue(a.archive, src, dst)
    added = add_aliases(a.archive, names, dst)
    refresh_notes(a.archive, dst)
    refresh_notes(a.archive, src)

    print("\nMoved %d files, dropped %d duplicates." % (len(moves), len(dups)))
    print("MANIFEST.sha256: %d paths rewritten, %d duplicate lines removed (old copy kept in _tools/)."
          % (changed, dropped))
    if queued:
        print("Stacking queue: %d entries now point at %s." % (queued, dst))
    if added:
        print("targets.tsv: added %s." % ", ".join("'%s'" % n for n in added))
    print("notes.md rebuilt for both targets.")
    print("\nNext: astro-backup to each destination (the old %s paths stay there; remove them by hand),"
          "\nthen astro-stack %s --dry-run (add --nights if it mixes filters)." % (src, dst.split("_")[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
