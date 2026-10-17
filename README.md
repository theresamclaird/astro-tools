# astro-tools

Command-line utilities for a Seestar (S50 Pro / S30) astrophotography workflow
on macOS and Linux: getting each night's data off the telescope into a well-organized
archive, backing that archive up, and turning mosaics into finished stacks with
[Siril](https://siril.org).

The basic idea is that **raw data is collected over many nights and never
changes**, while processing can be repeated whenever you like. The tools keep
those two things separate.

## Install

```bash
git clone git@github.com:theresamclaird/astro-tools.git ~/Projects/astro-tools
cd ~/Projects/astro-tools
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
ASTRO_ARCHIVE=/Volumes/Astro                            # the archive drive
ASTRO_NAS=atlas:/volume1/homes/theresa/Astrophotography # where astro-backup copies it (host:/path over SSH, or a mounted folder)
ASTRO_SOURCE=/Volumes/Seestar/MyWorks                   # where astro-ingest looks when no SOURCE is given
```

Environment variables of the same names override the file.

## The nightly routine

```bash
astro-ingest --dry-run   # see what's new on the Seestar and where it will go
astro-ingest             # copy, verify, update notes.md
astro-backup             # mirror the archive to the NAS and verify
```

## Commands

| Command | What it does |
|---|---|
| `astro-ingest` | Files new Seestar output into the archive by target and night, verifies every copy, updates `MANIFEST.sha256` and each target's `notes.md` |
| `astro-backup` | Copies the archive to the NAS over SSH (or to a mounted share), never deleting there, then verifies the NAS copy against `MANIFEST.sha256` and repairs bad copies from the archive |
| `astro-stack` | Stacks every night of one target from the archive with Siril: plate-solves each frame, registers onto the full mosaic canvas, stacks, and writes the result to the target's `processing/` folder |
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

### astro-backup

```
astro-backup [DESTINATION]
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

Either way:

- it never deletes anything on the NAS, and re-running only sends what's missing;
- a NAS file that fails verification is recopied from the archive and checked again;
- a file edited after the manifest was written (e.g. `notes.md`) passes if the
  NAS copy matches the archive's current version;
- the Mac is kept awake for the run (`caffeinate`);
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
ssh-copy-id theresa@atlas.local
# DSM home folders are often too permissive for sshd to accept keys:
ssh theresa@atlas.local 'chmod 755 ~ && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys'
printf '\nHost atlas\n  HostName atlas.local\n  User theresa\n' >> ~/.ssh/config
ssh atlas echo ok          # must print "ok" without asking for a password
```

and set `ASTRO_NAS=atlas:/volume1/homes/theresa/Astrophotography` in
`~/.config/astro-tools/config`.

If the copy step misbehaves with macOS's built-in rsync (openrsync), install the
standard one with `brew install rsync`; `astro-backup` uses it automatically.

### astro-stack

```
astro-stack TARGET [--nights GLOB] [--min-frames N] [--batch-max N] [--work DIR] [--name NAME] [--keep] [--dry-run]
```

Stacks all of a target's raw nights into one image. `TARGET` is the folder name
or a unique prefix (`M31`). Start with `--dry-run`: it lists the nights, how
they're batched, and the scratch space needed.

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
- Re-running the same command resumes: finished batch masters are skipped.
- Separate files only: Siril 1.4 doesn't record registration data when
  plate-solving a single-file (FITSEQ) sequence.
- Nights with fewer than `--min-frames` (default 50) are skipped; all selected
  nights must share one scope and filter (from the folder names).
- The result (32-bit linear FITS), a README listing the nights, and the log go to
  `Targets/<target>/processing/<date>_siril-all/`.
- Needs Siril 1.4+: `siril-cli` on the PATH, `/Applications/Siril.app`, or the
  Flatpak (`org.siril.Siril`), which must be allowed to see the archive and
  scratch disk: `flatpak override --user --filesystem=/run/media/$USER --filesystem=/data org.siril.Siril`.

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
