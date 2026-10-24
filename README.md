# astro-tools

Command-line utilities for a Seestar (S50 Pro / S30) astrophotography workflow
on macOS and Linux: getting each night's data off the telescope into a well-organized
archive, backing that archive up, and turning mosaics into finished stacks with
[Siril](https://siril.org).

> **About this repo.** I took up astrophotography recently with a Seestar S50
> smart telescope, and quickly found that the hard part wasn't capturing data,
> it was keeping track of it: a few weeks of clear nights produced tens of
> thousands of frames across a handful of targets. These are the tools I put
> together to turn that into a simple pipeline: get each night off the telescope
> into an organized archive, back it up and verify it, stack every night of a
> target into one image with Siril, and process the result. If you've recently
> fallen into this hobby too, I hope they save you some time, or at least give you
> ideas for your own setup. They're built around my gear (a Seestar, Linux and
> macOS, a Synology NAS), so expect to adjust paths and settings.
> [PIPELINE.md](PIPELINE.md) walks through the whole process step by step.

The basic idea is that **raw data is collected over many nights and never
changes**, while processing can be repeated whenever you like. The tools keep
those two things separate.

**New here, or coming back after a while?** [PIPELINE.md](PIPELINE.md) is the
step-by-step manual: ingest → back up → check frame quality → stack → process
in Siril → finish in Photoshop, with the exact commands and settings for each
step. This README is the reference for each command.

## Install

```bash
git clone git@github.com:theresamclaird/astro-tools.git ~/Projects/theresamclaird/astro-tools
cd ~/Projects/theresamclaird/astro-tools
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

**Requirements:** macOS or Linux (developed on Ubuntu 26.04), `python3`
(macOS: `xcode-select --install`; Ubuntu: `sudo apt install python3 python3-venv`),
and for the stacking and mosaic tools Siril 1.4+. Siril is found as `siril-cli`
on the PATH, `/Applications/Siril.app` on macOS, or the Flatpak
(`org.siril.Siril`) on Linux. Ubuntu's own Siril package may be too old; use the
Flatpak.

## Configuration

`~/.config/astro-tools/config`:

```bash
ASTRO_ARCHIVE=/Volumes/Astro                            # the archive drive
ASTRO_NAS=nas:/volume1/homes/youruser/Astrophotography # where astro-backup copies it (host:/path over SSH, or a mounted folder)
ASTRO_SOURCE=/Volumes/Seestar/MyWorks                   # where astro-ingest looks when no SOURCE is given
```

Environment variables of the same names override the file. If `ASTRO_ARCHIVE` /
`ASTRO_SOURCE` aren't set, the tools look for a drive named `Astro` and a
`MyWorks` folder under `/Volumes` (macOS), `/run/media/$USER` or `/media/$USER`
(Linux), and GNOME network mounts (`/run/user/<uid>/gvfs`).

## The nightly routine

```bash
astro-ingest --dry-run   # see what's new on the Seestar and where it will go
astro-ingest             # copy, verify, update notes.md, queue the new nights
astro-backup             # mirror the archive to the NAS and verify
astro-stack --changed    # stack every target that got a new night
```

or all of it in one go, stopping at the first step that fails:

```bash
astro-nightly            # ingest -> backup -> stack --changed
```

## Commands

| Command | What it does |
|---|---|
| `astro-ingest` | Files new Seestar output into the archive by target and night, verifies every copy, updates `MANIFEST.sha256` and each target's `notes.md`, and queues the new nights for stacking |
| `astro-backup` | Copies the archive to the NAS over SSH (or to a mounted share), never deleting there, then verifies what it copied (or, with `--full`, the whole copy against `MANIFEST.sha256`) and repairs bad copies from the archive |
| `astro-stack` | Stacks every night of one target from the archive with Siril: plate-solves each frame, registers onto the full mosaic canvas, stacks, and writes the result to the target's `processing/` folder. `--changed` does this for every target with newly queued nights |
| `astro-nightly` | Runs `astro-ingest`, `astro-backup` and `astro-stack --changed` in order, stopping at the first failure |
| `astro-frame-stats` | Measures every frame of one night (sharpness, roundness, background, star count, by mosaic panel), shows what each filter cut would drop, and optionally stacks the night with several filters to compare |
| `seestar-split-panels` | Groups a mosaic's subs into one folder per panel by clustering their RA/Dec |
| `seestar-reject-frames` | Finds and sets aside frames spoiled by an obstruction (e.g. a lit tree branch) |
| `seestar-stack-panels` | Stacks each panel into a master with Siril |
| `seestar-prep-panels` | Crops each master to its low-noise area and removes its background gradient |
| `seestar-mosaic` | Plate-solves the panel masters and assembles the mosaic with Siril |

Every command takes `--help`.

### astro-ingest

```
astro-ingest [SOURCE ...] [--dry-run] [--yes] [--no-previews] [--archive PATH]
```

`SOURCE` is a Seestar `MyWorks` folder (or anything containing Seestar output);
subfolders are searched. With no `SOURCE`, `ASTRO_SOURCE` is used
(default `/Volumes/Seestar/MyWorks`).

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
- **Stacking queue:** each night that received light frames is appended to
  `<archive>/_tools/stack-queue.tsv` for `astro-stack --changed`. It isn't
  backed up (it's working state, not data).
- Exit status: 0 done or nothing new, 1 failures or conflicts, 2 cancelled at
  the confirmation prompt.

### astro-backup

```
astro-backup [--full] [DESTINATION]
```

`DESTINATION` (default `ASTRO_NAS`) is either:

- **`host:/path` — over SSH (recommended).** rsync sends only new or changed
  files, then the NAS checksums its own copy against `MANIFEST.sha256` and
  reports only mismatches, so nothing is read back over the network. `host` is
  an alias from `~/.ssh/config`, and key-based login must work
  (see [Setting up SSH to the NAS](#setting-up-ssh-to-the-nas)).
- **`/local/path` — a mounted share**, e.g. `/Volumes/home/Astrophotography`.
  Copies with `cp` (macOS's built-in rsync fails on SMB shares) and verifies by
  reading every file back. Much slower, and sensitive to the share dropping.

**What gets verified.** A routine run checks only the files it copied in that
run: each one is checksummed at the destination and compared with the archive.
That keeps a backup after one night's ingest down to minutes. `--full` checks
every file in `MANIFEST.sha256` at the destination, which reads the whole
backup and catches a copy that has gone bad since it was written. Run `--full`
about once a month for each destination; a routine run reminds you when it has
been 30 days or more (the date of the last full check is kept in
`_tools/verified/`).

Either way:

- it never deletes anything on the NAS, and re-running only sends what's missing;
- a NAS file that fails verification is recopied from the archive and checked again;
- a file edited after the manifest was written (e.g. `notes.md`) passes if the
  NAS copy matches the archive's current version;
- the Mac is kept awake for the run (`caffeinate`);
- over SSH in a terminal, rsync shows a live progress line (percent of the total,
  rate, time left). It needs rsync 3.1+ (macOS: `brew install rsync`); without it
  the copy step runs silently. The progress line isn't written to the log;
- over SSH, a dropped connection is retried automatically (6 attempts, 60 s apart;
  `ASTRO_BACKUP_TRIES` / `ASTRO_BACKUP_RETRY_WAIT` to change), and the backup is only
  reported complete if the NAS confirms its checksum pass finished;
- logs go to `<archive>/_tools/logs/`.

#### Setting up SSH to the NAS

On a Synology (DSM 7): enable **Control Panel → Terminal & SNMP → SSH** and
**Control Panel → File Services → rsync**. Only accounts in the *administrators*
group can log in over SSH, and the account needs the rsync privilege
(**Application Privileges → rsync**).

> The SSH port in Terminal & SNMP and the "SSH encryption port" on the rsync tab
> must match. If they differ, plain `ssh` works but rsync over SSH fails with
> "Permission denied, please try again". Port 22 is fine on a home network that
> doesn't forward SSH from the internet.

Then, on the Mac:

```bash
[ -f ~/.ssh/id_ed25519.pub ] || ssh-keygen -t ed25519
ssh-copy-id youruser@nas.local
# DSM home folders are often too permissive for sshd to accept keys:
ssh youruser@nas.local 'chmod 755 ~ && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys'
printf '\nHost nas\n  HostName nas.local\n  User youruser\n' >> ~/.ssh/config
ssh nas echo ok          # must print "ok" without asking for a password
```

and set `ASTRO_NAS=nas:/volume1/homes/youruser/Astrophotography` in
`~/.config/astro-tools/config`.

If the copy step misbehaves with macOS's built-in rsync (openrsync), install the
standard one with `brew install rsync`; `astro-backup` uses it automatically.

### astro-stack

```
astro-stack TARGET [--nights GLOB] [--min-frames N] [--batch-max N] [--work DIR] [--name NAME]
                   [--rebuild] [--keep] [--dry-run]
astro-stack TARGET --nights GLOB --adopt MASTER.fit --frames N
astro-stack --changed [--min-frames N] [--batch-max N] [--work DIR] [--rebuild] [--keep] [--dry-run]
```

Stacks all of a target's raw nights into one image. `TARGET` is the folder name
or a unique prefix (`M31`). Start with `--dry-run`: it lists the nights, which
stored batch masters will be reused, how the rest are batched, and the scratch
space needed.

**Incremental.** Every batch master is kept in
`Targets/<target>/processing/batch-masters/`, with a `.txt` beside it listing
the settings and the nights (with frame counts) that went into it. A later run
reuses any stored master whose nights are all still selected with the same
frame counts, stacks only the nights not covered, and combines all the masters.
So after a new night, re-running `astro-stack M31` stacks just that night. A
master is not reused when one of its nights has gained or lost frames, when the
pipeline settings change, or with `--rebuild` (stack everything again).
Outdated masters stay in the store; delete them when you no longer want them.

`--adopt` puts an existing master (from a run before the store existed) into
the store as covering the nights matched by `--nights`; `--frames` is how many
frames it stacked (for weighting). The file is copied, not moved.

- **Install Siril's local Gaia catalogue first** (Scripts → Siril_Catalog_Installer,
  or download `siril_cat_healpix8_astro.dat` from the Zenodo record named in that
  script and set `core.catalogue_gaia_astro` to it). Plate-solving thousands of
  frames against the online catalogue takes hours and fails whenever the
  server does.
- Frames are read in place from the archive (symlinked, never changed);
  intermediates go to `--work` (default `$ASTRO_WORK`, else `/data/astro-work`).
- Nights are grouped in date order into batches of at most `--batch-max`
  (default 9000) frames, because Siril refuses a sequence of more than 10,000
  files. Each batch: debayer (16-bit) → plate-solve → check that registration
  data was recorded (stops otherwise, deleting nothing) → register onto a canvas
  covering every pointing, dropping the worst 10% by sharpness and by sky
  background → stack (winsorized sigma clipping, noise weighting) → keep the
  batch master and delete the batch's intermediates. With several batches, the
  masters are plate-solved, registered together and averaged, weighted by the
  number of frames in each.
- An interrupted run loses at most the batch in progress: each master goes into
  the store as soon as it's finished.
- Separate files only: Siril 1.4 doesn't record registration data when
  plate-solving a single-file (FITSEQ) sequence.
- Nights with fewer than `--min-frames` (default 50) are skipped; all selected
  nights must share one scope and filter (from the folder names).
- The result (32-bit linear FITS), a README listing the nights, and the log go to
  `Targets/<target>/processing/<date>_siril-all/`.
- Needs Siril 1.4+: `siril-cli` on the PATH, `/Applications/Siril.app`, or the
  Flatpak (`org.siril.Siril`), which must be allowed to see the archive and
  scratch disk: `flatpak override --user --filesystem=/run/media/$USER --filesystem=/data org.siril.Siril`.

**`--changed`** stacks whatever `astro-ingest` queued instead of one target.
For each queued target, the new nights are grouped by scope and filter, and each
group is stacked together with all of that target's nights of the same scope and
filter (mosaic or not), so every result is the target's full stack including the
new night. Thanks to the stored batch masters, that costs one small batch plus the
combine. Runs are named `<today>_siril-all_<scope>_<filter>`, e.g.
`2026-10-20_siril-all_S50_IRCUT`.

- Queued nights are dropped with a note, not stacked, when they're Moon/Sun data,
  have fewer than `--min-frames` frames, or their target or night folder can't be
  found. A target renamed since ingest (e.g. `NGC7331` → `NGC7331_Deer-Lick-Galaxy`)
  is still found by its prefix.
- A group's nights leave the queue once its stack succeeds. A failed group stays
  queued for the next run, the remaining groups still run, and the exit status is 1.
- `--dry-run` shows each group's plan and leaves the queue alone.
- A bad night goes in like any other. To leave it out, restack the target by hand
  with `--nights`; the stored masters for the other nights are reused.

### astro-nightly

```
astro-nightly [SOURCE ...] [--yes] [--no-previews] [--no-backup] [--no-stack] [--dry-run]
```

`astro-ingest`, then `astro-backup`, then `astro-stack --changed`, each only if the
one before succeeded, so new data is on the NAS before any stacking starts. The
machine is kept awake for the whole run. `SOURCE`, `--yes` and `--no-previews`
go to `astro-ingest`; without `--yes` it asks once before copying, and everything
after that runs unattended. `--no-backup` skips the backup (e.g. the NAS is off),
`--no-stack` stops after it, and `--dry-run` shows what would be ingested and what's
already queued. The exit status is that of the step that failed.

### astro-frame-stats

```
astro-frame-stats TARGET NIGHT [--compare] [--variants LIST] [--crop X,Y,W,H] [--work DIR] [--dry-run]
```

Use it to choose frame-filter settings from data instead of a fixed percentage.
It debayers and plate-solves one night exactly as `astro-stack` does, then
writes to `Targets/<target>/processing/frame-stats/<night>/`:

- `frames.csv`: every frame's time, mosaic panel, FWHM, weighted FWHM,
  roundness, sky background and star count, as measured by Siril;
- `summary.txt`: the spread of each measure, robust outliers (more than
  3 sigma from the median), how many frames 90/75/60% cuts drop, and the same
  per panel, so a background cut that only removes one panel's (brighter) sky
  shows up;
- `frames_over_time.png`, `histograms.png`: the measures over the night,
  coloured by panel, with the cut lines.

`--compare` then registers and stacks the night once per variant (`all`,
`current` = astro-stack's filter, `wfwhm75`, `wfwhm60`, `outliers`), measures
star FWHM and background noise in the same crop of each, saves a same-stretch
JPG of each crop, and writes `comparison.txt`. Each variant takes a full
register + stack of the night, so start with a smaller night. Re-running
reuses the debayered frames and finished stacks. The scratch folder is printed
at the end; delete it when you're done.

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

## License

MIT. See [LICENSE](LICENSE).
