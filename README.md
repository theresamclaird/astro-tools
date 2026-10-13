# astro-tools

Command-line utilities for a Seestar (S50 Pro / S30) astrophotography workflow
on macOS: getting each night's data off the telescope into a well-organized
archive, backing that archive up, and turning mosaics into finished stacks with
[Siril](https://siril.org).

The basic idea is that **raw data is collected over many nights and never
changes**, while processing can be repeated whenever you like. The tools keep
those two things separate.

## Install

```bash
git clone git@github.com:theresamclaird/astro-tools.git ~/src/astro-tools
cd ~/src/astro-tools
./install.sh
```

`install.sh`:

- creates a private Python environment in `.venv/` and installs numpy into it
  (only the mosaic tools need numpy);
- writes a small wrapper for each command into `~/.local/bin` (use
  `--bin DIR` to put them elsewhere). The wrappers point back at this
  checkout, so a `git pull` updates the tools without reinstalling;
- creates `~/.config/astro-tools/config` from `config.example` if it doesn't
  exist;
- reports whether Siril was found and whether `~/.local/bin` is on your `PATH`.

`./install.sh --uninstall` removes the wrappers.

**Requirements:** macOS, `python3` (from the Xcode command-line tools:
`xcode-select --install`), and for the stacking/mosaic tools Siril 1.4+
(`/Applications/Siril.app` is detected automatically).

## Configuration

`~/.config/astro-tools/config`:

```bash
ASTRO_ARCHIVE=/Volumes/Astro                     # the archive drive
ASTRO_NAS=/Volumes/home/Astrophotography         # where astro-backup copies it
```

Environment variables of the same names override the file.

## The nightly routine

```bash
astro-ingest "/path/to/MyWorks" --dry-run   # see what's new and where it will go
astro-ingest "/path/to/MyWorks"             # copy, verify, update notes.md
astro-backup                                # mirror the archive to the NAS and verify
```

## Commands

| Command | What it does |
|---|---|
| `astro-ingest` | Files new Seestar output into the archive by target and night, verifies every copy, updates `MANIFEST.sha256` and each target's `notes.md` |
| `astro-backup` | Copies the archive to the NAS (never deletes there), then verifies the NAS copy against `MANIFEST.sha256`, repairing bad copies from the archive |
| `seestar-split-panels` | Groups a mosaic's subs into one folder per panel by clustering their RA/Dec |
| `seestar-reject-frames` | Finds and sets aside frames spoiled by an obstruction (e.g. a lit tree branch) |
| `seestar-stack-panels` | Stacks each panel into a master with Siril |
| `seestar-prep-panels` | Crops each master to its low-noise area and removes its background gradient |
| `seestar-mosaic` | Plate-solves the panel masters and assembles the mosaic with Siril |

Every command takes `--help`.

### astro-ingest

```
astro-ingest SOURCE [SOURCE ...] [--dry-run] [--yes] [--no-previews] [--archive PATH]
```

`SOURCE` is a Seestar `MyWorks` folder (or anything containing Seestar output);
subfolders are searched.

- Light frames → `Targets/<target>/raw/<evening-date>_<scope>[_mosaic]_<filter>/`,
  with the per-frame JPGs in `previews/`.
- The **evening date** is the date the night started: frames taken before noon
  belong to the previous date, so one night is one folder.
- The **scope** (S50 / S30) is read from each frame's FITS header.
- On-scope stacks → `seestar-stacks/`. Moon/Sun video and photos →
  `Targets/Moon|Sun/raw/<evening-date>_<scope>/`.
- **Target names:** Seestar names are matched to existing folders by catalog id
  (`NGC 7000` → `NGC7000_North-America-Nebula`), with Caldwell and star-name
  aliases (`C 12` → NGC 6946, `Sadr` → IC 1318). An unknown target gets a new
  folder named by its catalog id; rename it to add a common name, keeping the
  prefix. Extra mappings can go in `<archive>/_tools/targets.tsv`
  (`Seestar name<TAB>folder name`).
- **Safety:** never modifies the source and never overwrites a different file
  in the archive (reported as a conflict). Each copy is read back and compared
  by SHA-256 before it's added to the manifest. Re-running copies only what's new.

### astro-backup

```
astro-backup [DESTINATION]
```

Copies everything on the archive drive (except macOS housekeeping files) to
`ASTRO_NAS`, skipping files already there with the same size that aren't newer.
Then it checks every file on the NAS against `MANIFEST.sha256`:

- a file that doesn't match is recopied from the archive and checked again;
- a file edited after the manifest was written (e.g. `notes.md`) passes if the
  NAS copy matches the archive's current version.

Logs go to `<archive>/_tools/logs/`. It never deletes anything on the NAS.

### Mosaics

There are two ways to turn a Seestar mosaic into an image:

**1. Per-panel (the `seestar-*` tools).** Best for large mosaics with many
panels (Sadr, NGC 7000):

```bash
seestar-split-panels "Sadr_mosaic_sub"            # dry run: shows the panels it found
seestar-split-panels "Sadr_mosaic_sub" --apply
seestar-reject-frames Sadr_mosaic_panels --scan   # optional: look for obstructed frames
seestar-stack-panels  Sadr_mosaic_panels --only 2 # try one panel first
seestar-stack-panels  Sadr_mosaic_panels
seestar-prep-panels   Sadr_mosaic_panels
seestar-mosaic        Sadr_mosaic_panels
```

Each script's docstring explains its choices in detail (why the panels are
plate-solved rather than star-matched, why background extraction is degree-1,
how the crop is measured, and so on).

**2. One Siril run over every sub.** Plate-solve every frame, register onto
the full mosaic canvas and stack in one pass. This is how the full-week M31
stack was made; see `siril/examples/m31_week.ssf` and `run_m31_week.sh`.

> **Note:** the mosaic tools and the M31 example still expect the original
> Seestar folder layout (`<Target>_mosaic_sub`) and paths on the old T9 drive.
> Updating them to read from the archive's `raw/<night>/` folders and write to
> `processing/` is the next planned change.

## The archive layout

```
<archive>/
  README.md
  MANIFEST.sha256             SHA-256 of every archived file
  Targets/
    M31_Andromeda-Galaxy/
      raw/2026-10-11_S50_mosaic_IRCUT/       light frames, untouched
          previews/                          Seestar per-frame JPGs
      seestar-stacks/                        the scope's own stacks
      processing/<date>_<description>/       working files; can be regenerated
      final/<date>_<description>/            finished images
      notes.md                               sessions table + your notes
  _tools/
    targets.tsv                              optional extra name mappings
    logs/                                    astro-backup logs
```

`raw/`, `seestar-stacks/`, `final/` and `notes.md` are irreplaceable;
`processing/` can always be rebuilt from `raw/`.

## Tests

```bash
python3 -m unittest discover tests
```
