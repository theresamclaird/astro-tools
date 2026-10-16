#!/bin/bash
# astro-stack: stack every night of one target from the archive with Siril.
#
#   astro-stack TARGET [--nights GLOB] [--min-frames N] [--work DIR] [--name NAME]
#               [--keep] [--dry-run]
#
#   TARGET        target folder name, or a unique prefix of it (e.g. M31)
#   --nights      only raw/ folders matching GLOB (default: all), e.g. '2026-10-1*'
#   --min-frames  skip nights with fewer frames than this (default 50; drops
#                 framing test shots)
#   --work DIR    scratch space for the intermediate files (default $ASTRO_WORK,
#                 else /data/astro-work). Use a fast local disk, never the archive.
#   --name NAME   name of the run (default <today>_siril-all); the result goes to
#                 <archive>/Targets/<target>/processing/NAME/
#   --keep        keep the registered frames after a successful stack
#   --dry-run     list the nights, frame counts and space estimate, then stop
#
# Pipeline (the one that made the full-week M31 stack; see siril/examples/):
#   link every raw .fit (read in place, never modified) and convert them, debayered
#   (16-bit), into one FITS-sequence file -> plate-solve each frame from its header RA/Dec
#   -> register onto a canvas covering every pointing (-framing=max),
#      dropping the worst 10% by star sharpness and by sky background
#   -> delete the debayered frames (they're no longer needed)
#   -> stack: winsorized sigma clipping, add+scale normalization, noise weights
#   -> copy the 32-bit linear result and the log into the archive
#
# All nights must share one scope and filter (read from the folder names,
# <date>_<S50|S30>[_mosaic]_<FILTER>); mixing them is refused.
# The machine is kept awake for the run (systemd-inhibit / caffeinate).
set -euo pipefail

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

ARCHIVE="${ASTRO_ARCHIVE:-/Volumes/Astro}"
WORK_ROOT="${ASTRO_WORK:-/data/astro-work}"
NIGHTS='*'; MIN=50; NAME="$(date +%F)_siril-all"; KEEP=0; DRY=0; TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --nights) NIGHTS="$2"; shift 2 ;;
    --min-frames) MIN="$2"; shift 2 ;;
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

# ---- pick the nights ----
SEL=(); TOTAL=0; KEY=""; BYTES=0
echo "Target: $TNAME"
for d in "$TDIR/raw/"$NIGHTS/; do
  [ -d "$d" ] || continue
  night="$(basename "$d")"
  cnt=$(find "$d" -maxdepth 1 -name '*.fit' | wc -l | tr -d ' ')
  if [ "$cnt" -lt "$MIN" ]; then printf '  skip  %-34s %6s frames (fewer than %s)\n' "$night" "$cnt" "$MIN"; continue; fi
  k="$(echo "$night" | sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2}_//; s/_mosaic//')"   # e.g. S50_IRCUT
  if [ -n "$KEY" ] && [ "$k" != "$KEY" ]; then
    echo "Mixed scope/filter: $night is $k, earlier nights are $KEY. Use --nights to pick one set."; exit 1
  fi
  KEY="$k"; SEL+=("$d"); TOTAL=$((TOTAL+cnt))
  b=$(du -sk "$d" | cut -f1); BYTES=$((BYTES+b))
  printf '  use   %-34s %6s frames\n' "$night" "$cnt"
done
[ "${#SEL[@]}" -gt 0 ] || { echo "No nights selected."; exit 1; }

# Space: 16-bit debayered frames ~50 MB each; registered frames on the full
# mosaic canvas are larger (~1.5-2x), and ~80% survive the filters. The
# debayered set is deleted before stacking, but both exist at the peak.
NEED_GB=$(( TOTAL * 50 / 1024 + TOTAL * 80 / 100 * 100 / 1024 ))
mkdir -p "$WORK_ROOT" 2>/dev/null || true
FREE_GB=$(( $(df -Pk "$WORK_ROOT" | awk 'NR==2{print $4}') / 1024 / 1024 ))
echo
echo "$TOTAL frames ($(( TOTAL * 10 / 3600 )) h at 10 s), $(( BYTES / 1024 / 1024 )) GB raw, scope/filter $KEY"
echo "Scratch: $WORK_ROOT  (~${NEED_GB} GB needed at peak, ${FREE_GB} GB free)"
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

# ---- set up the run folder ----
RUN="$WORK_ROOT/${TNAME}_$NAME"
OUT="$TDIR/processing/$NAME"
# The frames are packed into one FITS-sequence file (Siril caps a sequence of
# separate files at 10,000 frames), but stacking still opens many files at
# once, so lift the soft open-file limit to the hard limit anyway.
HARD=$(ulimit -Hn); [ "$HARD" = unlimited ] && HARD=1048576
ulimit -n "$HARD" 2>/dev/null || true

# A run folder left by a run that failed before producing any frames is
# reused; one with real work in it is not.
if [ -e "$RUN" ]; then
  if find "$RUN/work" -maxdepth 1 \( -name 'light*' -o -name 'r_light*' \) 2>/dev/null | grep -q .; then
    echo "Run folder already exists with work in it: $RUN (remove it or use --name)"; exit 1
  fi
  echo "Removing the empty run folder from a failed attempt: $RUN"; rm -rf "$RUN"
fi
mkdir -p "$RUN/links" "$RUN/work"
LOG="$RUN/astro-stack.log"
exec > >(tee -a "$LOG") 2>&1
echo "=== astro-stack $TNAME $(date) ==="; printf '%s\n' "${SEL[@]}"

# Symlink every frame into one folder (Siril reads one directory). Names are
# Seestar's, which include a timestamp, so they don't collide across nights.
for d in "${SEL[@]}"; do
  find "$d" -maxdepth 1 -name '*.fit' -exec ln -s {} "$RUN/links/" \;
done
echo "linked $(ls "$RUN/links" | wc -l | tr -d ' ') frames"

cat > "$RUN/register.ssf" <<EOF
requires 1.4.0
set16bits
setext fit
convert light -debayer -fitseq -out=$RUN/work
cd $RUN/work
seqplatesolve light -nocache
seqapplyreg light -framing=max -filter-wfwhm=90% -filter-bkg=90%
close
EOF
cat > "$RUN/stack.ssf" <<EOF
requires 1.4.0
setext fit
cd $RUN/work
stack r_light rej w 3 3 -norm=addscale -fastnorm -weight=noise -maximize -32b -out=$RUN/result_linear
close
EOF

# Keep the machine awake; stacking opens every registered frame at once.
AWAKE=()
if command -v systemd-inhibit >/dev/null && systemd-inhibit --what=sleep true >/dev/null 2>&1; then AWAKE=(systemd-inhibit --what=sleep:idle --who=astro-stack --why="stacking $TNAME")
elif command -v caffeinate >/dev/null; then AWAKE=(caffeinate -i); fi

t0=$(date +%s)
echo; echo "--- 1/2: debayer, plate-solve, register ($(date +%T)) ---"
${AWAKE[@]+"${AWAKE[@]}"} "${SIRIL[@]}" -d "$RUN/links" -s "$RUN/register.ssf"
SEQF=$(ls "$RUN/work"/r_light*.seq 2>/dev/null | head -1 || true)
reg=$( [ -n "$SEQF" ] && awk '/^S /{print $4; exit}' "$SEQF" || true ); reg=${reg:-0}
[ "$reg" -gt 0 ] || { echo "Registration produced no frames; see the log above."; exit 1; }
echo "registered $reg of $TOTAL frames; removing the debayered frames"
rm -f "$RUN/work"/light.fit "$RUN/work"/light_*.fit

echo; echo "--- 2/2: stack ($(date +%T)) ---"
${AWAKE[@]+"${AWAKE[@]}"} "${SIRIL[@]}" -s "$RUN/stack.ssf"
RES="$RUN/result_linear.fit"
[ -f "$RES" ] || { echo "Stacking produced no result; see the log above."; exit 1; }

mkdir -p "$OUT"
cp "$RES" "$OUT/${TNAME}_${NAME}_linear.fit"
{ echo "astro-stack run $NAME"; echo "frames: $TOTAL selected, $reg registered"; echo "nights:"
  for d in "${SEL[@]}"; do echo "  $(basename "$d")"; done; } > "$OUT/README.txt"
[ "$KEEP" -eq 1 ] || rm -f "$RUN/work"/r_light.fit "$RUN/work"/r_light_*.fit
echo; echo "Done in $(( ($(date +%s) - t0) / 60 )) min."
echo "Result: $OUT/${TNAME}_${NAME}_linear.fit"
cp "$LOG" "$OUT/astro-stack.log"
