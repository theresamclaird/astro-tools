#!/usr/bin/env python3
"""Bookkeeping for astro-stack's per-target store of batch masters.

A batch master is the stack of one group of nights. Each is kept in the
target's store (Targets/<target>/processing/batch-masters/) as
    <first-date>_<last-date>_<N>n_<id>.fit   the 32-bit linear master
    <same name>.txt                          what went into it:
        astro-stack batch master
        settings: <pipeline settings string>
        frames: <frames stacked, after filtering>
        night: <night folder name> <raw frame count>
        ...

Subcommands (night lists are read from stdin as "<night>\t<count>" lines):

  plan --store DIR --settings S --batch-max N [--rebuild]
      Prints how to cover the given nights:
        REUSE\t<master path>\t<frames stacked>\t<night,night,...>
        NEW\t<batch number>\t<night>\t<count>
      A stored master is reused only if it was made with the same settings and
      every night in it is in the selection with the same frame count (a night
      that has since gained frames invalidates the master). Reused masters never
      overlap; larger ones win. Remaining nights are grouped, in date order, into
      new batches of at most --batch-max frames.

  add --store DIR --settings S --file F --frames N [--move]
      Puts master F into the store, recording the nights on stdin. Prints the
      stored path.
"""
import argparse, hashlib, os, shutil, sys

HEADER = "astro-stack batch master"


def read_nights(stream):
    nights = []
    for line in stream:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        name, count = line.rsplit("\t", 1)
        nights.append((name, int(count)))
    return nights


def read_sidecar(path):
    info = {"nights": []}
    with open(path, encoding="utf-8") as f:
        lines = [l.rstrip("\n") for l in f]
    if not lines or lines[0] != HEADER:
        return None
    for l in lines[1:]:
        key, _, val = l.partition(": ")
        if key == "settings":
            info["settings"] = val
        elif key == "frames":
            info["frames"] = int(val or 0)
        elif key == "night":
            name, count = val.rsplit(" ", 1)
            info["nights"].append((name, int(count)))
    return info


def stored_masters(store):
    if not os.path.isdir(store):
        return []
    out = []
    for fn in sorted(os.listdir(store)):
        if not fn.endswith(".txt"):
            continue
        fit = os.path.join(store, fn[:-4] + ".fit")
        if not os.path.isfile(fit):
            continue
        info = read_sidecar(os.path.join(store, fn))
        if info and info["nights"]:
            info["file"] = fit
            out.append(info)
    return out


def plan(nights, store, settings, batch_max, rebuild):
    have = dict(nights)
    covered, reuse = set(), []
    if not rebuild:
        cands = [m for m in stored_masters(store)
                 if m.get("settings") == settings
                 and all(have.get(n) == c for n, c in m["nights"])]
        cands.sort(key=lambda m: -sum(c for _, c in m["nights"]))
        for m in cands:
            names = {n for n, _ in m["nights"]}
            if names & covered:
                continue
            covered |= names
            reuse.append(m)
    new, b, inb = [], 1, 0
    for name, count in sorted(nights):
        if name in covered:
            continue
        if inb and inb + count > batch_max:
            b, inb = b + 1, 0
        inb += count
        new.append((b, name, count))
    return reuse, new


def master_name(nights, settings):
    names = sorted(n for n, _ in nights)
    ident = hashlib.sha1((settings + "\n" + "\n".join("%s %d" % nc for nc in sorted(nights))).encode()).hexdigest()[:8]
    first, last = names[0][:10], names[-1][:10]
    return "%s_%s_%dn_%s" % (first, last, len(names), ident)


def add(nights, store, settings, src, frames, move):
    if not nights:
        sys.exit("add: no nights given on stdin")
    os.makedirs(store, exist_ok=True)
    base = os.path.join(store, master_name(nights, settings))
    tmp = base + ".fit.partial"
    (shutil.move if move else shutil.copyfile)(src, tmp)
    os.replace(tmp, base + ".fit")
    with open(base + ".txt.partial", "w", encoding="utf-8") as f:
        f.write("%s\nsettings: %s\nframes: %d\n" % (HEADER, settings, frames))
        for n, c in sorted(nights):
            f.write("night: %s %d\n" % (n, c))
    os.replace(base + ".txt.partial", base + ".txt")
    print(base + ".fit")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--store", required=True)
    p.add_argument("--settings", required=True)
    p.add_argument("--batch-max", type=int, required=True)
    p.add_argument("--rebuild", action="store_true")
    a2 = sub.add_parser("add")
    a2.add_argument("--store", required=True)
    a2.add_argument("--settings", required=True)
    a2.add_argument("--file", required=True)
    a2.add_argument("--frames", type=int, default=0)
    a2.add_argument("--move", action="store_true")
    a = ap.parse_args()
    nights = read_nights(sys.stdin)
    if a.cmd == "plan":
        reuse, new = plan(nights, a.store, a.settings, a.batch_max, a.rebuild)
        for m in reuse:
            print("REUSE\t%s\t%d\t%s" % (m["file"], m.get("frames", 0), ",".join(n for n, _ in m["nights"])))
        for b, n, c in new:
            print("NEW\t%d\t%s\t%d" % (b, n, c))
    else:
        add(nights, a.store, a.settings, a.file, a.frames, a.move)


if __name__ == "__main__":
    main()
