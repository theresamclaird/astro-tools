#!/bin/bash
# astro-stack: stack every night of one target from the archive with Siril.
#
#   astro-stack TARGET [--nights GLOB] [--min-frames N] [--batch-max N] [--work DIR]
#               [--name NAME] [--keep] [--dry-run]
#
#   TARGET        target folder name, or a unique prefix of it (e.g. M31)
#   --nights      only raw/ folders matching GLOB (default: all), e.g. '2026-10-1*'
#   --min-frames  skip nights with fewer frames than this (default 50; drops
#                 framing test shots)
#   --batch-max   most frames per batch (default 9000; Siril refuses a sequence of
#                 more than 10,000 files)
#   --work DIR    scratch space for the intermediate files (default $ASTRO_WORK,
#                 else /data/astro-work). Use a fast local disk, never the archive.
#   --name NAME   name of the run (default <today>_siril-all); the result goes to
#                 <archive>/Targets/<target>/processing/NAME/
#   --keep        keep the registered frames of each batch
#   --dry-run     list the nights, batches and space estimate, then stop
#
# Pipeline. Nights are grouped, in date order, into batches of at most
# --batch-max frames. For each batch:
#   copy the batch's raw frames to the scratch disk, one file at a time (Siril
#   reads with many threads at once; on a USB hard drive those scattered reads
#   are several times slower than one sequential copy). The archive is only read.
#   -> debayer to 16-bit files -> plate-solve every frame (install Siril's local
#      Gaia catalogue: much faster, and immune to online-catalogue outages)
#   -> check the solve produced registration data (stop if not; nothing deleted)
#   -> register onto a canvas covering every pointing (-framing=max), dropping
#      the worst 10% by star sharpness and by sky background
#   -> stack: winsorized sigma clipping, add+scale normalization, noise weights
#   -> keep the batch master, delete the batch's intermediate frames
# With more than one batch, the masters are plate-solved, registered onto one
# canvas and averaged, each weighted by the number of frames in it.
#
# Re-running the same command resumes: finished batches are kept and skipped.
# Separate files only: Siril 1.4 doesn't record registration data when
# plate-solving a single-file (FITSEQ) sequence.
#
# All nights must share one scope and filter (read from the folder names,
# <date>_<S50|S30>[_mosaic]_<FILTER>); mixing them is refused.
# The machine is kept awake for the run (systemd-inhibit / caffeinate).
set -euo pipefail

usage() { sed -n '2,41p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

ARCHIVE="${ASTRO_ARCHIVE:-/Volumes/Astro}"
WORK_ROOT="${ASTRO_WORK:-/data/astro-work}"
NIGHTS='*'; MIN=50; BMAX=9000; NAME="$(date +%F)_siril-all"; KEEP=0; DRY=0; TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --nights) NIGHTS="$2"; shift 2 ;;
    --min-frames) MIN="$2"; shift 2 ;;
    --batch-max) BMAX="$2"; shift 2 ;;
    --work) WORK_ROOT="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --dry-run|-n) DRY=1; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "unknown option: $1"; usage 1 ;;
    *) [ -z "$TARGET" ] && TARGET="$1" || { echo "only one TARGET"; exit 1; }; shift ;;
  esac
done
[ -n "$TARGET" ] || usage 1
[ "$BMAX" -le 9900 ] || { echo "--batch-max must be at most 9900 (Siril's 10,000-file limit)"; exit 1; }
[ -d "$ARCHIVE/Targets" ] || { echo "Archive not found: $ARCHIVE/Targets (set ASTRO_ARCHIVE)"; exit 1; }

# ---- resolve the target folder ----
TDIR=""; n=0
for d in "$ARCHIVE/Targets/$TARGET"*/; do
  [ -d "$d/raw" ] || continue
  TDIR="${d%/}"; n=$((n+1))
  [ "$(basename "$TDIR")" = "$TARGET" ] && { n=1; break; }
done
[ "$n" -eq 1 ] || { echo "No unique target matching '$TARGET' with a raw/ folder in $ARCHIVE/Targets"; exit 1; }
TNAME="$(basename "$TDIR")"

# ---- pick the nights and group them into batches ----
SEL=(); CNT=(); BATCH=(); TOTAL=0; KEY=""; BYTES=0; nb=1; inb=0; BIGGEST=0
echo "Target: $TNAME"
for d in "$TDIR/raw/"$NIGHTS/; do
  [ -d "$d" ] || continue
  night="$(basename "$d")"
  cnt=$(find "$d" -maxdepth 1 -name '*.fit' | wc -l | tr -d ' ')
  if [ "$cnt" -lt "$MIN" ]; then printf '  skip  %-34s %6s frames (fewer than %s)\n' "$night" "$cnt" "$MIN"; continue; fi
  [ "$cnt" -le "$BMAX" ] || { echo "$night alone has $cnt frames, more than --batch-max $BMAX"; exit 1; }
  k="$(echo "$night" | sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2}_//; s/_mosaic//')"   # e.g. S50_IRCUT
  if [ -n "$KEY" ] && [ "$k" != "$KEY" ]; then
    echo "Mixed scope/filter: $night is $k, earlier nights are $KEY. Use --nights to pick one set."; exit 1
  fi
  if [ $((inb + cnt)) -gt "$BMAX" ]; then nb=$((nb+1)); inb=0; fi
  inb=$((inb + cnt)); [ "$inb" -gt "$BIGGEST" ] && BIGGEST=$inb
  KEY="$k"; SEL+=("${d%/}"); CNT+=("$cnt"); BATCH+=("$nb"); TOTAL=$((TOTAL+cnt))
  b=$(du -sk "$d" | cut -f1); BYTES=$((BYTES+b))
  printf '  use   %-34s %6s frames  (batch %s)\n' "$night" "$cnt" "$nb"
done
[ "${#SEL[@]}" -gt 0 ] || { echo "No nights selected."; exit 1; }
NBATCH=$nb

# Space, per batch (batches run one after another and clean up after
# themselves): 16-bit debayered frames ~50 MB each, plus registered frames on
# the mosaic canvas (larger, ~100+ MB; ~80% survive the filters).
NEED_GB=$(( BIGGEST * 19 / 1024 + BIGGEST * 50 / 1024 + BIGGEST * 80 / 100 * 110 / 1024 ))
mkdir -p "$WORK_ROOT" 2>/dev/null || true
FREE_GB=$(( $(df -Pk "$WORK_ROOT" | awk 'NR==2{print $4}') / 1024 / 1024 ))
echo
echo "$TOTAL frames ($(( TOTAL * 10 / 3600 )) h at 10 s), $(( BYTES / 1024 / 1024 )) GB raw, scope/filter $KEY, $NBATCH batch(es)"
echo "Scratch: $WORK_ROOT  (~${NEED_GB} GB needed at peak for the largest batch, ${FREE_GB} GB free)"
[ "$FREE_GB" -gt "$NEED_GB" ] || echo "WARNING: scratch space looks tight; the canvas size decides the real figure."
[ "$DRY" -eq 1 ] && { echo "(dry run: nothing done)"; exit 0; }

# ---- find Siril (1.4+) ----
SIRIL=()
if command -v siril-cli >/dev/null; then SIRIL=(siril-cli)
elif [ -x /Applications/Siril.app/Contents/MacOS/siril-cli ]; then SIRIL=(/Applications/Siril.app/Contents/MacOS/siril-cli)
elif command -v flatpak >/dev/null && flatpak info org.siril.Siril >/dev/null 2>&1; then
  SIRIL=(flatpak run --command=siril-cli org.siril.Siril)
  echo "Using the Siril Flatpak. It needs access to the archive and the scratch disk:"
  echo "  flatpak override --user --filesystem=$ARCHIVE --filesystem=$WORK_ROOT org.siril.Siril"
else echo "Siril not found (siril-cli, /Applications/Siril.app, or the org.siril.Siril Flatpak)"; exit 1; fi

HARD=$(ulimit -Hn); [ "$HARD" = unlimited ] && HARD=1048576
ulimit -n "$HARD" 2>/dev/null || true

AWAKE=()
if command -v systemd-inhibit >/dev/null && systemd-inhibit --what=sleep true >/dev/null 2>&1; then AWAKE=(systemd-inhibit --what=sleep:idle --who=astro-stack --why="stacking $TNAME")
elif command -v caffeinate >/dev/null; then AWAKE=(caffeinate -i); fi
siril() { ${AWAKE[@]+"${AWAKE[@]}"} "${SIRIL[@]}" "$@"; }

_t=$(mktemp -d); : > "$_t/a"
if cp --update=none "$_t/a" "$_t/b" 2>/dev/null; then NOCLOBBER=--update=none; else NOCLOBBER=-n; fi
rm -rf "$_t"

# Number of frames with registration data in a Siril .seq file.
reg_count() { local f n=0; f=$(ls "$1"/$2*.seq 2>/dev/null | head -1 || true); [ -n "$f" ] && n=$(grep -c '^R' "$f" || true); echo "${n:-0}"; }
# Number of frames a sequence holds, from its .seq header.
seq_count() { local f n=0; f=$(ls "$1"/$2*.seq 2>/dev/null | head -1 || true); [ -n "$f" ] && n=$(awk '/^S /{print $4; exit}' "$f"); echo "${n:-0}"; }

# ---- run folder (re-running resumes) ----
RUN="$WORK_ROOT/${TNAME}_$NAME"
OUT="$TDIR/processing/$NAME"
MASTERS="$RUN/masters"
mkdir -p "$MASTERS"
LOG="$RUN/astro-stack.log"
exec > >(tee -a "$LOG") 2>&1
echo "=== astro-stack $TNAME $(date) ==="
t0=$(date +%s)
REG_TOTAL=0

for b in $(seq 1 "$NBATCH"); do
  BM="$MASTERS/batch$b.fit"
  if [ -f "$BM" ]; then echo; echo "--- batch $b/$NBATCH already stacked: $BM (skipping)"; continue; fi
  B="$RUN/batch$b"
  rm -rf "$B"; mkdir -p "$B/links" "$B/work"
  bcount=0
  echo; echo "--- batch $b/$NBATCH ($(date +%T)) ---"
  for i in "${!SEL[@]}"; do
    [ "${BATCH[$i]}" = "$b" ] || continue
    echo "  $(basename "${SEL[$i]}")  ${CNT[$i]} frames"
    # Never overwrite; the archive copy is only read. (GNU cp 9.x warns about -n
    # and prefers --update=none; BSD/macOS cp only knows -n.)
    find "${SEL[$i]}" -maxdepth 1 -name '*.fit' -print0 | sort -z | xargs -0 sh -c 'opt=$0; dest=$1; shift; cp "$opt" "$@" "$dest"' "$NOCLOBBER" "$B/links/"
    bcount=$((bcount + CNT[i]))
  done

  cat > "$B/solve.ssf" <<EOF
requires 1.4.0
set16bits
setext fit
link light -date -out=$B/work
cd $B/work
calibrate light -debayer
seqplatesolve pp_light
close
EOF
  echo "  copied $(ls "$B/links" | wc -l | tr -d ' ') raw frames to scratch ($(date +%T))"
  echo "  1/3 debayer + plate-solve ($(date +%T))"
  siril -d "$B/links" -s "$B/solve.ssf"
  rm -rf "$B/links"                       # the scratch copies of the raw frames
  solved=$(reg_count "$B/work" pp_light)
  if [ "$solved" -eq 0 ]; then
    echo "Plate-solving left no registration data in $B/work/pp_light_.seq."
    echo "Stopping here; nothing has been deleted. Is the local catalogue installed? See the log above."
    exit 1
  fi
  # (The .seq holds a registration line for every frame, solved or not; Siril's
  # own "N images successfully platesolved" line above is the real solve count.)
  echo "  registration data recorded ($solved entries for $bcount frames)"
  rm -f "$B/work"/light_*.fit

  cat > "$B/register.ssf" <<EOF
requires 1.4.0
set16bits
setext fit
cd $B/work
seqapplyreg pp_light -framing=max -filter-wfwhm=90% -filter-bkg=90%
close
EOF
  echo "  2/3 register ($(date +%T))"
  siril -s "$B/register.ssf"
  reg=$(seq_count "$B/work" r_pp_light)
  [ "$reg" -gt 0 ] || { echo "Registration produced no frames; see the log above. Nothing deleted."; exit 1; }
  echo "  registered $reg frames; removing the debayered frames"
  rm -f "$B/work"/pp_light_*.fit

  cat > "$B/stack.ssf" <<EOF
requires 1.4.0
setext fit
cd $B/work
stack r_pp_light rej w 3 3 -norm=addscale -fastnorm -weight=noise -maximize -32b -out=${BM%.fit}
close
EOF
  echo "  3/3 stack ($(date +%T))"
  siril -s "$B/stack.ssf"
  [ -f "$BM" ] || { echo "Stacking produced no batch master; see the log above."; exit 1; }
  echo "$reg" > "$MASTERS/batch$b.frames"
  [ "$KEEP" -eq 1 ] || rm -rf "$B"
  echo "  batch $b done: $BM"
done

for b in $(seq 1 "$NBATCH"); do REG_TOTAL=$((REG_TOTAL + $(cat "$MASTERS/batch$b.frames" 2>/dev/null || echo 0))); done

RES="$RUN/result_linear.fit"
if [ "$NBATCH" -eq 1 ]; then
  cp "$MASTERS/batch1.fit" "$RES"
else
  # Combine the batch masters: plate-solve, register onto one canvas, then a
  # plain average weighted by how many frames went into each master.
  C="$RUN/combine"; rm -rf "$C"; mkdir -p "$C"
  cat > "$C/combine.ssf" <<EOF
requires 1.4.0
set32bits
setext fit
link batch -out=$C
cd $C
seqplatesolve batch
seqapplyreg batch -framing=max
stack r_batch rej n -norm=addscale -weight=nbstack -maximize -32b -out=${RES%.fit}
close
EOF
  echo; echo "--- combining $NBATCH batch masters ($(date +%T)) ---"
  siril -d "$MASTERS" -s "$C/combine.ssf"
fi
[ -f "$RES" ] || { echo "No final result; see the log above. The batch masters are in $MASTERS."; exit 1; }

mkdir -p "$OUT"
cp "$RES" "$OUT/${TNAME}_${NAME}_linear.fit"
{ echo "astro-stack run $NAME"; echo "frames: $TOTAL selected, $REG_TOTAL stacked, $NBATCH batch(es)"; echo "nights:"
  for i in "${!SEL[@]}"; do echo "  $(basename "${SEL[$i]}")  ${CNT[$i]} frames  batch ${BATCH[$i]}"; done; } > "$OUT/README.txt"
echo; echo "Done in $(( ($(date +%s) - t0) / 60 )) min."
echo "Result: $OUT/${TNAME}_${NAME}_linear.fit"
echo "(Batch masters kept in $MASTERS; delete $RUN when you no longer need them.)"
cp "$LOG" "$OUT/astro-stack.log"
