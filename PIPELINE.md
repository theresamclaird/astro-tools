# Astrophotography Pipeline Manual

From a night on the Seestar to a finished image, step by step. Command details
are in the [README](README.md); this is the order to use them in.

## Overview

Every session goes through the same six steps, in order. Raw data never changes once it's archived; everything after step 2 can be redone at any time from `raw/`.

1. **Ingest:** `astro-ingest` copies the night off the Seestar into the Astro drive and verifies every file.
2. **Back up:** `astro-backup` mirrors the archive to a second drive and the NAS. Clear the Seestar only after this.
3. **Check frame quality** (optional, when trying new settings): `astro-frame-stats` measures every frame of one night and compares filter cut-offs.
4. **Stack:** `astro-stack` stacks every night of a target into one 32-bit linear image. It reuses earlier batches, so only new nights are processed.
5. **Process in Siril:** crop, background extraction, plate-solve, colour calibration (SPCC), GHS stretch, export a 16-bit TIFF.
6. **Finish in Photoshop** on the Mac: StarXTerminator, then final adjustments on the starless layer.

All commands run on the Linux workstation (Ubuntu) unless noted; on macOS the drives are under `/Volumes/` instead. Every command takes `--help`, and every one that changes something takes `--dry-run`.

### Drives and folders

| What | Example path (Linux) | Notes |
| --- | --- | --- |
| Archive drive (named Astro) | `/run/media/$USER/Astro` | The one true copy. `raw/`, `final/`, `notes.md` are irreplaceable |
| Scratch | `/data/astro-work` | Intermediates from stacking; safe to delete after a run |
| Mirror drive | `/run/media/$USER/T9/Astro-mirror` | Second copy; must be exFAT to move between Mac and Linux |
| NAS | `nas:/volume1/homes/youruser/Astrophotography` | Third copy, over SSH |
| Seestar | Auto-detected `MyWorks` folder | Plug into a rear-panel USB port, not a hub |
| Tools | `~/Projects/theresamclaird/astro-tools` | `git pull` updates every command |

Inside the archive, each target has `raw/<night>/` (light frames), `seestar-stacks/`, `processing/` (rebuildable working files, including `batch-masters/`) and `final/` (finished images).

### One-time setup

- [ ] Clone and install: `git clone https://github.com/theresamclaird/astro-tools.git ~/Projects/theresamclaird/astro-tools && cd ~/Projects/theresamclaird/astro-tools && ./install.sh`
- [ ] Make sure `~/.local/bin` is on the PATH in both `~/.bashrc` and `~/.zshrc`.
- [ ] Edit `~/.config/astro-tools/config`: `ASTRO_ARCHIVE=/run/media/$USER/Astro`, `ASTRO_WORK=/data/astro-work`, `ASTRO_NAS=nas:/volume1/homes/youruser/Astrophotography`. Leave `ASTRO_SOURCE` commented out so the Seestar is found automatically.
- [ ] Install Siril 1.4+ from Flatpak (`flatpak install flathub org.siril.Siril`), not apt. Ubuntu's package is a release candidate the scripts reject.
- [ ] Let the Flatpak see the drives: `flatpak override --user --filesystem=/run/media/$USER --filesystem=/data org.siril.Siril`
- [ ] Install the local Gaia catalogue: `siril_cat_healpix8_astro.dat` in `~/.var/app/org.siril.Siril/data/siril/`, set in Siril under Preferences → Astrometry. Plate-solving then takes minutes instead of hours and doesn't depend on the online server.

## Step 1: Ingest a night

`astro-ingest` files everything new on the Seestar into the archive by target and night, then reads each copy back and checks it by SHA-256. It never changes the Seestar and never overwrites a different file in the archive.

1. Connect the Seestar by USB to a **rear-panel port** on the computer. Through a USB hub it drops out with I/O errors; on the rear port it copies at about 25–28 MB/s.
2. Plug in the Astro drive. Both should appear on their own under `/run/media/$USER/`.
3. Preview what will be copied and where:

   ```bash
   astro-ingest --dry-run
   ```

4. Check the list. Each night lands in `Targets/<target>/raw/<evening-date>_<scope>[_mosaic]_<filter>/`. Frames taken after midnight belong to the evening the session started.
5. Copy for real:

   ```bash
   astro-ingest
   ```

6. Read the summary at the end. It must report **0 failed** and **0 conflicts**. Re-running copies only what's still missing, so after an interruption just run it again.

What else it does:

- Per-frame JPGs go to `previews/` beside the raw frames; the Seestar's own stacks go to `seestar-stacks/`. Use `--no-previews` to skip the JPGs.
- Each target's `notes.md` has its "Raw sessions" table rebuilt: frames, hours and size per night, plus a total. Anything else you write in `notes.md` is left alone.
- `MANIFEST.sha256` gains a line per new file. The backups are verified against it.
- A new target gets a folder named by its catalogue id (`NGC7000`). Rename it to add a common name, keeping the prefix: `NGC7000_North-America-Nebula`.
- It prints progress every 30 s and stops after 20 failures in a row, which almost always means the USB connection dropped. Reseat the cable and run it again.

**Don't clear the Seestar yet.** Until step 2 runs, the Seestar holds the only other copy of the night.

## Step 2: Back up

`astro-backup` copies the archive to a destination, never deletes anything there, then verifies the copy against `MANIFEST.sha256` and recopies any file that fails. Run it to the mirror drive and to the NAS after every ingest.

**To a mirror drive** (a mounted drive, here a Samsung T9, verified by reading every file back):

```bash
astro-backup /run/media/$USER/T9/Astro-mirror
```

**To the NAS** (over SSH; the NAS checks its own copy, so nothing is read back over the network):

```bash
astro-backup            # uses ASTRO_NAS from the config
```

- Re-running sends only what's missing, so an interrupted backup is resumed by running it again.
- Over SSH, a dropped connection is retried 6 times, 60 s apart. The run counts as complete only when the NAS confirms its checksum pass finished.
- The machine is kept awake for the run, and logs go to `Astro/_tools/logs/`.
- A file you edited after it was archived (such as `notes.md`) passes if the copy matches the archive's current version.

**When both backups report success, clear the night from the Seestar.** Its storage is not a backup.

If the NAS connection drops partway through a long run, the backup retries; if it gives up, just run it again. Keep a local mirror drive as well, so you're never relying on the network alone.

## Step 3: Check frame quality (optional)

`astro-frame-stats` measures every frame of one night and shows which frames a given filter would drop. Use it when choosing filter settings, not after every night. It only reads the archive.

It was added after the other commands: after `git pull`, run `./install.sh` once in `~/Projects/theresamclaird/astro-tools` to add it and the plotting library.

1. Pick a night. A smaller one is faster for a first try: `2026-10-16` has 1,093 frames.
2. Preview the space and where the report goes:

   ```bash
   astro-frame-stats M31 2026-10-16 --dry-run
   ```

3. Measure the frames. This debayers and plate-solves the night exactly as `astro-stack` does:

   ```bash
   astro-frame-stats M31 2026-10-16
   ```

4. Open the report in `Targets/M31_Andromeda-Galaxy/processing/frame-stats/<night>/`.
5. If the report suggests a change, run the comparison stacks. They reuse the frames from step 3:

   ```bash
   astro-frame-stats M31 2026-10-16 --compare
   ```

6. Delete the scratch folder it names at the end once you've decided.

### Reading the report

| File | What to look for |
| --- | --- |
| `frames_over_time.png` | Bad frames show as a cluster in time (cloud pass, dew building up) or isolated spikes (wind gusts, a bump). An even band with a few spikes means the night was good |
| `histograms.png` | A long tail on the bad side is what a filter should remove. A narrow, symmetric peak means a percentage cut is mostly removing good frames |
| `summary.txt`, "Robust outliers" | Frames more than 3 sigma from the median on the bad side. This is the honest count of clearly bad frames |
| `summary.txt`, "Per panel" | If the background cut drops 20% of one panel and 0% of another, it's removing that panel's brighter sky, not bad frames |
| `frames.csv` | Every frame's numbers, for sorting or plotting yourself |

The measures are weighted FWHM (star size; lower is sharper), roundness (1 is perfectly round; low means trailing), background (sky brightness), and stars detected (drops with cloud or haze).

### The comparison stacks

`--compare` stacks the night once per variant and measures each stack in the same central 2000×2000 crop (`--crop X,Y,W,H` to choose another). Each variant is a full register and stack of the night, so this is the slow part.

| Variant | Filter |
| --- | --- |
| `all` | Every plate-solved frame |
| `current` | `astro-stack`'s filter: best 90% by wFWHM and by background |
| `wfwhm75` | Best 75% by wFWHM |
| `wfwhm60` | Best 60% by wFWHM (the ~40% discard) |
| `outliers` | Drops only frames 3 sigma worse than the median in wFWHM, roundness or star count |

`comparison.txt` lists, for each variant, frames stacked, median star FWHM and background noise in the crop. Lower FWHM is sharper; lower noise means smoother faint detail. The `crop_<variant>.jpg` files share one stretch, so you can flip between them. Run a subset with `--variants all,current,outliers`.

If a different filter clearly wins, change the filter in `tools/stack/astro-stack.sh` (the `seqapplyreg` line and the matching `SETTINGS` string). Changing `SETTINGS` marks the stored batch masters as out of date, so the next stack rebuilds everything once.

## Step 4: Stack

`astro-stack` stacks every night of a target into one 32-bit linear FITS. It's incremental: nights already stacked are reused, so after a new night it processes only that night and then combines everything.

1. Start a tmux session so the run survives closing the terminal: `tmux new -s stack` (reattach later with `tmux attach -t stack`).
2. Preview the plan:

   ```bash
   astro-stack M31 --dry-run
   ```

3. Check the plan. Each line says `reuse stored master` or `stack <night> (new batch N)`, followed by the frame totals and the scratch space needed.
4. Run it:

   ```bash
   astro-stack M31
   ```

5. The result is `Targets/M31_Andromeda-Galaxy/processing/<date>_siril-all/M31_Andromeda-Galaxy_<date>_siril-all_linear.fit`, with a `README.txt` (nights, settings, masters used) and the log.
6. Delete the run's scratch folder in `/data/astro-work/` once the result is there. The batch masters are kept in the archive.

### What happens to each new batch

Nights are grouped in date order into batches of at most 9,000 frames, because Siril won't handle more than 10,000 files in one sequence. Each batch:

1. Copies its raw frames to scratch (the archive is only read).
2. Debayers to 16-bit and plate-solves every frame against the local Gaia catalogue.
3. Registers onto a canvas covering every mosaic pointing, dropping the worst 10% by sharpness and by background.
4. Stacks with winsorized sigma clipping and noise weighting.
5. Saves the batch master to `processing/batch-masters/`, then deletes its intermediates.

The masters are then plate-solved, registered together, and averaged, weighted by how many frames each holds. An interrupted run loses at most the batch in progress; run the same command again to continue.

### Options

| Option | Use |
| --- | --- |
| `--nights GLOB` | Only some nights, e.g. `'2026-10-1*'` |
| `--min-frames N` | Skip nights with fewer frames (default 50; drops test shots) |
| `--rebuild` | Ignore stored masters and stack everything again |
| `--name NAME` | Name of the result folder (default `<today>_siril-all`) |
| `--keep` | Keep each batch's registered frames (large) |
| `--adopt FILE --frames N` | Record an existing master as covering the `--nights` selection |

A stored master is reused only if every night in it still has the same frame count and the settings haven't changed. Outdated masters stay in `batch-masters/` until you delete them.

### Example: adopting masters from before the store existed

If you stacked before incremental mode existed, adopt those masters once. In this example, the masters from an October 2026 M31 run predate the store. Adopt them once, so they aren't restacked:

```bash
W=/data/astro-work/M31_Andromeda-Galaxy_2026-10-16_siril-all/masters
astro-stack M31 --nights '2026-10-0*'       --adopt $W/batch1.fit --frames 5855
astro-stack M31 --nights '2026-10-1[124]_*' --adopt $W/batch2.fit --frames 2838
astro-stack M31 --dry-run
```

The first adopt should list Oct 3–8 (7,140 frames), the second Oct 11, 12 and 14 (3,537). The dry run should show 2 masters reused and 1 new batch: Oct 15 and 16, 2,956 frames.

## Step 5: Process in Siril

The stack is linear and looks almost black until it's stretched. Work in this order, and save a new file after each step so you can go back. The numbers below are what worked on the October M31 stack; treat them as starting points.

1. **Open.** Open Siril, click the home icon to set the working folder to the run's `processing/<date>_siril-all/` folder, and open the `_linear.fit`. Set the display mode at the bottom to **Autostretch** so you can see the image. This changes the view only, not the data.
2. **Rotate and crop.** The mosaic has ragged edges. If the framing is tilted, rotate first (Image Processing → Geometry → Rotation; −17.65° with Lanczos for M31). Draw a box inside the fully covered area, right-click → Crop. Save as `M31_crop.fit`.
3. **Remove the background gradient.** Image Processing → Background Extraction. Polynomial, degree 2, correction **Subtraction**. Generate the samples, then right-click to remove any that sit on the galaxy or its faint halo. Compute, check the result, Apply. Save as `M31_bg.fit`.
4. **Plate-solve.** Tools → Astrometry → Image Plate Solver. Search for M31, then Solve; the local catalogue makes this quick. Rotation and cropping discard the solution, so this step is always needed before colour calibration.
5. **Calibrate colour (SPCC).** Image Processing → Colour Calibration → Spectrophotometric Colour Calibration. Sensor: ZWO Seestar S50. Filter: **UV/IR Block**, because the frames were shot through the IR-cut filter. White reference: **Average Spiral Galaxy**. Apply and check the log: about 2,000 stars matched last time. Save as `M31_spcc.fit`.
6. **Stretch with GHS.** Image Processing → Stretches → Generalised Hyperbolic Stretch. Switch the display mode to **Linear** first (close the dialog if the dropdown is greyed out), so the preview isn't auto-stretched on top. Linear data looks black on screen; that's expected.
    1. **Remove the pedestal, per channel.** Type of stretch **Linear stretch (BP shift)**, colour model **Independent channel values**. Zoom the histogram (type 10 in the box above it) and set the black point just left of the sky peak, with shadow clip near 0. Apply. This matters most when the console has said *"Marginally out of range pixels detected: scaling to [0,1]"*: background extraction left pixels below zero, Siril shifted the whole image up, and that grey offset washes the colour out of everything after it. (October 2026 stack: sky at 0.07, black point 0.070.)
    2. **Main stretch.** Type **Generalised hyperbolic transform**, colour model **Human weighted luminance**. Set SP just above the sky by typing it (about 0.005 after step 1; the eyedropper can give nonsense values here). D = 3, b = 6, LP = 0, HP = 0.95. Aim for roughly what the AutoStretch display shows: dark sky, warm core, core not a white blob. Too bright: lower D. Too dim: raise D, or apply and do a second smaller pass. Apply.
    3. **Darken the sky.** Linear stretch (BP shift), Human weighted luminance, black point until the sky sits around 0.08–0.12, shadow clip under about 0.1%. Toggle Preview to check the faint halo doesn't shrink. Apply. (October 2026: BP 0.091.)
    4. Skip extra brightening passes here: they bloat the stars. Stretch the galaxy further in Photoshop after StarXTerminator. Save as `M31_stretched.fit`.
7. **Saturation.** Image Processing → Colour Saturation, amount about 0.5. If even 1.0 barely changes anything, the colour was washed out by a pedestal: go back to `M31_spcc.fit` and redo step 6 from 6.1. Optionally Remove Green Noise (SCNR) if the background looks green. Save as `M31_final_siril.fit`.
8. **Export for Photoshop.** File → Save As, type **TIFF**, **16-bit**, into `Targets/M31_Andromeda-Galaxy/final/<date>_siril-all/`.

When re-processing a newer stack, repeat from step 1 on the new `_linear.fit`. The settings carry over roughly, but use the eyedropper again: SP depends on the data.

## Step 6: Finish in Photoshop

Separating the stars from the galaxy lets you push the galaxy's contrast and colour without bloating the stars. This runs on the Mac, with the RC Astro StarXTerminator plugin.

1. Get the TIFF to the Mac: plug in the Astro drive (it's exFAT, so both machines read it) or copy it from the NAS.
2. Open the 16-bit TIFF in Photoshop and duplicate the background layer.
3. On the copy: Filter → RC Astro → StarXTerminator. Turn on its option to generate a star image, if your version has it, to get the stars on their own layer. This copy becomes the **starless** layer.
4. If there's no star-image option, make the stars layer yourself: duplicate the original again, then Image → Apply Image with the starless layer, blending **Subtract**.
5. Work on the starless layer: Curves or Levels for contrast in the dust lanes and outer arms, then colour and saturation. Watch the sky background; keep it neutral and just above black.
6. Put the stars layer on top with blend mode **Screen**. Lower its opacity to make the stars less prominent.
7. Save the layered file as a PSD in `final/<date>_siril-all/`, then export a JPEG or PNG for sharing.

Keep the PSD: with the layers you can return to the stars-versus-galaxy balance later without starting over.

## Other tools: per-panel mosaics

`astro-stack` handles mosaics in one pass, and it's the path for M31. The `seestar-*` tools are the alternative for large mosaics with many panels (Sadr, NGC 7000): they stack each panel separately, clean it up, then assemble the mosaic.

| Order | Command | What it does |
| --- | --- | --- |
| 1 | `seestar-split-panels DIR [--apply]` | Groups a mosaic's frames into one folder per panel by pointing. Dry run unless `--apply` |
| 2 | `seestar-reject-frames DIR --scan` | Optional: finds frames spoiled by an obstruction, such as a lit tree branch |
| 3 | `seestar-stack-panels DIR [--only N]` | Stacks each panel with Siril. Try one panel first with `--only` |
| 4 | `seestar-prep-panels DIR` | Crops each panel master to its low-noise area and removes its gradient |
| 5 | `seestar-mosaic DIR` | Plate-solves the panel masters and assembles the mosaic |

These still expect the old Seestar folder layout (`<Target>_mosaic_sub`) rather than the archive's `raw/<night>/` folders. Updating them is a planned change.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `command not found` in a new terminal or tmux | `~/.local/bin` isn't on the PATH in that shell | Add the PATH line to `~/.zshrc` and `~/.bashrc`; open a new terminal |
| A new command is missing after `git pull` | Wrappers are made by the installer | Run `./install.sh` in `~/Projects/theresamclaird/astro-tools` |
| Ingest stalls or stops after 20 failures | The Seestar's USB connection reset | Use a rear-panel USB port, not a hub; run `astro-ingest` again |
| Ingest copies from the wrong place, or finds nothing | `ASTRO_SOURCE` set in the config | Comment it out so the Seestar is auto-detected |
| Siril: `requires 1.4.0` fails | Ubuntu's apt Siril (1.4.0-rc2) is running | Remove the apt package; use the Flatpak |
| Siril can't see the drive's files | Flatpak sandbox | `flatpak override --user --filesystem=/run/media/$USER --filesystem=/data org.siril.Siril` |
| Plate-solving takes hours or fails with 503 errors | Using the online catalogue | Install the local Gaia catalogue (Overview → One-time setup) |
| "no registration data" after plate-solving | Solve failed, or a single-file (FITSEQ) sequence | Check the local catalogue path; the tools always use separate files |
| Siril refuses more than 10,000 files | Siril's sequence limit | Keep `--batch-max` at 9,000 or less |
| Plate solution missing after a rotation | Rotating discards the WCS | Plate-solve again before SPCC |
| GHS turns everything white | Symmetry point wrong, or AutoStretch display still on | Display mode Linear; type SP just above the sky peak |
| Stretched image is grey; saturation does nothing | Grey offset from "scaling to [0,1]" after background extraction | Reopen `M31_spcc.fit`; black point with Independent channel values before the main stretch (Step 5, 6.1) |
| Dry run restacks nights you expected reused | A night's frame count changed, or settings changed | Expected; the old master stays in `batch-masters/` until deleted |
| NAS backup drops partway | The network connection to the NAS dropped | Re-run; it resumes. Keep a local mirror drive as well |

Every long run writes a log: `astro-stack.log` in the result folder, `frame-stats.log` in its scratch folder, and the backup logs in `Astro/_tools/logs/`. Start there when something fails.
