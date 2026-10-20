#!/bin/bash
# astro-stack: stack every night of one target from the archive with Siril.
#
#   astro-stack TARGET [--nights GLOB] [--min-frames N] [--batch-max N] [--work DIR]
#               [--name NAME] [--rebuild] [--keep] [--dry-run]
#   astro-stack TARGET --nights GLOB --adopt MASTER.fit [--frames N]
#   astro-stack --changed [--min-frames N] [--batch-max N] [--work DIR] [--rebuild]
#               [--keep] [--dry-run]
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
#   --rebuild     ignore stored batch masters and restack every night
#   --keep        keep the registered frames of each batch
#   --dry-run     show which stored masters would be reused and which nights
#                 would be stacked, with the space estimate, then stop
#   --adopt F     record an existing batch master F (made by this pipeline) as
#                 covering exactly the nights selected by --nights; --frames N
#                 is how many frames it stacked (for the README). Then stops.
#   --changed     stack every target that astro-ingest queued in
#                 <archive>/_tools/stack-queue.tsv, instead of one TARGET (see below)
#
# --changed. astro-ingest queues each night it adds. For every queued target,
# the queued nights are grouped by scope and filter (e.g. S50_IRCUT), and each
# group is stacked with all of that target's nights of the same scope and filter,
# so the result is the target's full stack including the new night. The run is
# named <today>_siril-all_<scope>_<filter>. Queued nights are dropped with a
# note, not stacked, when they're Moon/Sun data, have fewer than --min-frames
# frames, or their folder no longer exists. A group's nights leave the queue
# once its stack succeeds (or it's dropped); a failed group stays queued for the
# next run, and the other groups still run. --dry-run shows each group's plan and
# leaves the queue alone.
#
# Incremental. Each batch master is kept in the target's store,
# Targets/<target>/processing/batch-masters/, with a note of exactly which
# nights (and frame counts) went into it. A run reuses every stored master
# whose nights are all still selected with unchanged frame counts, stacks only
# the remaining nights (grouped in date order into batches of at most
# --batch-max frames), then combines all the masters. Adding a night therefore
# costs one small batch plus the combine, not a full restack. Re-running after
# a failure resumes the same way, since each batch is stored as soon as it's done.
#
# Each new batch:
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
# With more than one master, they're plate-solved, registered onto one canvas
# and averaged, each weighted by the number of frames in it.
#
# Separate files only: Siril 1.4 doesn't record registration data when
# plate-solving a single-file (FITSEQ) sequence.
#
# All nights must share one scope and filter (read from the folder names,
# <date>_<S50|S30>[_mosaic]_<FILTER>); mixing them is refused.
# The machine is kept awake for the run (systemd-inhibit / caffeinate).
set -euo pipefail

usage() { awk 'NR>1 && /^set -euo/{exit} NR>1' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

ARCHIVE="${ASTRO_ARCHIVE:-/Volumes/Astro}"
WORK_ROOT="${ASTRO_WORK:-/data/astro-work}"
NIGHTS='*'; MIN=50; BMAX=9000; NAME="$(date +%F)_siril-all"; KEEP=0; DRY=0; TARGET=""
REBUILD=0; ADOPT=""; ADOPT_FRAMES=0; CHANGED=0; NIGHTS_SET=0; NAME_SET=0
HERE="$(cd "$(dirname "$0")" && pwd)"
BM_PY=(python3 "$HERE/batch_masters.py")
# Anything that changes how a batch master is made goes in here, so masters made
# differently are never mixed into one result.
SETTINGS="v1 debayer16 platesolve framing=max filter-wfwhm=90% filter-bkg=90% rej=w3,3 norm=addscale weight=noise"
while [ $# -gt 0 ]; do
  case "$1" in
    --nights) NIGHTS="$2"; NIGHTS_SET=1; shift 2 ;;
    --min-frames) MIN="$2"; shift 2 ;;
    --batch-max) BMAX="$2"; shift 2 ;;
    --work) WORK_ROOT="$2"; shift 2 ;;
    --name) NAME="$2"; NAME_SET=1; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --rebuild) REBUILD=1; shift ;;
    --adopt) ADOPT="$2"; shift 2 ;;
    --frames) ADOPT_FRAMES="$2"; shift 2 ;;
    --changed) CHANGED=1; shift ;;
    --dry-run|-n) DRY=1; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "unknown option: $1"; usage 1 ;;
    *) [ -z "$TARGET" ] && TARGET="$1" || { echo "only one TARGET"; exit 1; }; shift ;;
  esac
done
if [ "$CHANGED" -eq 1 ]; then
  [ -z "$TARGET" ] && [ "$NIGHTS_SET" -eq 0 ] && [ "$NAME_SET" -eq 0 ] && [ -z "$ADOPT" ] || {
    echo "--changed takes no TARGET, --nights, --name or --adopt (it picks those itself)"; exit 1; }
else
  [ -n "$TARGET" ] || usage 1
fi
[ "$BMAX" -le 9900 ] || { echo "--batch-max must be at most 9900 (Siril's 10,000-file limit)"; exit 1; }
[ -d "$ARCHIVE/Targets" ] || { echo "Archive not found: $ARCHIVE/Targets (set ASTRO_ARCHIVE)"; exit 1; }

# The target folder for NAME: exact match, else the one folder with a raw/ that starts with it.
resolve_target() {
  local d found="" n=0
  for d in "$ARCHIVE/Targets/$1"*/; do
    [ -d "$d/raw" ] || continue
    found="${d%/}"; n=$((n+1))
    [ "$(basename "$found")" = "$1" ] && { n=1; break; }
  done
  [ "$n" -eq 1 ] && echo "$found"
}

# ---- --changed: stack every target astro-ingest queued ----
if [ "$CHANGED" -eq 1 ]; then
  QUEUE="$ARCHIVE/_tools/stack-queue.tsv"
  if ! [ -s "$QUEUE" ]; then echo "Nothing queued for stacking ($QUEUE is empty or missing)."; exit 0; fi
  Q="$(mktemp -d)"; trap 'rm -rf "$Q"' EXIT
  : > "$Q/cand"; : > "$Q/drop"
  echo "Queued nights ($QUEUE):"
  while IFS=$'\t' read -r qt qn; do
    [ -n "$qt" ] && [ -n "$qn" ] || continue
    line="$qt"$'\t'"$qn"
    td="$(resolve_target "$qt" || true)"
    why=""
    case "$qt" in Moon|Moon_*|Sun|Sun_*) why="Moon/Sun data isn't stacked by astro-stack" ;; esac
    if [ -z "$why" ] && [ -z "$td" ]; then why="no unique target folder matching '$qt'"; fi
    if [ -z "$why" ] && ! [ -d "$td/raw/$qn" ]; then why="night folder not found"; fi
    if [ -z "$why" ]; then
      cnt=$(find "$td/raw/$qn" -maxdepth 1 -name '*.fit' | wc -l | tr -d ' ')
      [ "$cnt" -ge "$MIN" ] || why="$cnt frames (fewer than --min-frames $MIN)"
    fi
    if [ -n "$why" ]; then
      printf '  drop   %s/%s: %s\n' "$qt" "$qn" "$why"
      printf '%s\n' "$line" >> "$Q/drop"; continue
    fi
    key="$(echo "$qn" | sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2}_//; s/_mosaic//')"   # e.g. S50_IRCUT
    printf '  stack  %s/%s (%s frames, %s)\n' "$(basename "$td")" "$qn" "$cnt" "$key"
    printf '%s\t%s\t%s\n' "$(basename "$td")" "$key" "$line" >> "$Q/cand"
  done < <(sort -u "$QUEUE")

  # Take lines out of the queue, re-reading it so anything queued meanwhile is kept.
  unqueue() {
    [ -s "$1" ] || return 0
    grep -v -x -F -f "$1" "$QUEUE" > "$Q/rest" || true
    cat "$Q/rest" > "$QUEUE"
  }
  [ "$DRY" -eq 1 ] || unqueue "$Q/drop"

  PASS=(--min-frames "$MIN" --batch-max "$BMAX" --work "$WORK_ROOT")
  [ "$REBUILD" -eq 1 ] && PASS+=(--rebuild)
  [ "$KEEP" -eq 1 ] && PASS+=(--keep)
  [ "$DRY" -eq 1 ] && PASS+=(--dry-run)
  OK=(); FAILED=()
  # (fd 4, so the stack runs can't read the group list from stdin)
  while IFS=$'\t' read -r tname key <&4; do
    scope="${key%%_*}"; filt="${key#*_}"
    awk -F'\t' -v t="$tname" -v k="$key" '$1==t && $2==k {print $3 "\t" $4}' "$Q/cand" > "$Q/group"
    echo; echo "=== $tname, $key ==="
    # All of this target's nights with the same scope and filter, mosaic or not.
    if /bin/bash "$0" "$tname" --nights "????-??-??_${scope}_*${filt}" \
         --name "$(date +%F)_siril-all_$key" ${PASS[@]+"${PASS[@]}"}; then
      OK+=("$tname $key"); [ "$DRY" -eq 1 ] || unqueue "$Q/group"
    else
      FAILED+=("$tname $key"); echo "*** $tname $key failed; its nights stay queued."
    fi
  done 4< <(cut -f1,2 "$Q/cand" | sort -u)
  echo
  [ "$DRY" -eq 1 ] && { echo "(dry run: nothing stacked, queue unchanged)"; exit 0; }
  [ "${#OK[@]}" -eq 0 ] || { echo "Stacked:"; printf '  %s\n' "${OK[@]}"; }
  if [ "${#FAILED[@]}" -gt 0 ]; then
    echo "FAILED (still queued; fix and re-run astro-stack --changed):"; printf '  %s\n' "${FAILED[@]}"; exit 1
  fi
  [ "${#OK[@]}" -gt 0 ] || echo "Nothing to stack."
  exit 0
fi

# ---- resolve the target folder ----
TDIR="$(resolve_target "$TARGET" || true)"
[ -n "$TDIR" ] || { echo "No unique target matching '$TARGET' with a raw/ folder in $ARCHIVE/Targets"; exit 1; }
TNAME="$(basename "$TDIR")"

# ---- pick the nights ----
SEL=(); CNT=(); TOTAL=0; KEY=""; BYTES=0
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
  KEY="$k"; SEL+=("${d%/}"); CNT+=("$cnt"); TOTAL=$((TOTAL+cnt))
  b=$(du -sk "$d" | cut -f1); BYTES=$((BYTES+b))
done
[ "${#SEL[@]}" -gt 0 ] || { echo "No nights selected."; exit 1; }
night_list() { for i in "${!SEL[@]}"; do printf '%s\t%s\n' "$(basename "${SEL[$i]}")" "${CNT[$i]}"; done; }
STORE="$TDIR/processing/batch-masters"

if [ -n "$ADOPT" ]; then
  [ -f "$ADOPT" ] || { echo "No such file: $ADOPT"; exit 1; }
  echo "Adopting $ADOPT as the master for:"; night_list | sed 's/^/  /'
  stored=$(night_list | "${BM_PY[@]}" add --store "$STORE" --settings "$SETTINGS" --file "$ADOPT" --frames "$ADOPT_FRAMES")
  echo "Stored: $stored"
  exit 0
fi

# ---- plan: reuse stored masters, batch the rest ----
RB=(); [ "$REBUILD" -eq 1 ] && RB=(--rebuild)
PLAN=$(night_list | "${BM_PY[@]}" plan --store "$STORE" --settings "$SETTINGS" --batch-max "$BMAX" ${RB[@]+"${RB[@]}"})
REUSE=(); REUSE_FRAMES=0; BATCH=(); NBATCH=0
while IFS=$'\t' read -r kind a b c; do
  case "$kind" in
    REUSE) REUSE+=("$a"); REUSE_FRAMES=$((REUSE_FRAMES + b))
           echo "  reuse stored master ($b frames): $(echo "$c" | tr ',' ' ')" ;;
    NEW)   for i in "${!SEL[@]}"; do if [ "$(basename "${SEL[$i]}")" = "$b" ]; then BATCH[$i]=$a; fi; done
           if [ "$a" -gt "$NBATCH" ]; then NBATCH=$a; fi ;;
  esac
done <<< "$PLAN"
BIGGEST=0
for bn in $(seq 1 "$NBATCH"); do
  sum=0
  for i in "${!SEL[@]}"; do
    if [ "${BATCH[$i]:-0}" = "$bn" ]; then
      sum=$((sum + CNT[i]))
      printf '  stack %-34s %6s frames  (new batch %s)\n' "$(basename "${SEL[$i]}")" "${CNT[$i]}" "$bn"
    fi
  done
  if [ "$sum" -gt "$BIGGEST" ]; then BIGGEST=$sum; fi
done
NMASTERS=$(( ${#REUSE[@]} + NBATCH ))

# Space, per batch (batches run one after another and clean up after
# themselves): raw copies ~19 MB, 16-bit debayered frames ~50 MB, plus
# registered frames on the mosaic canvas (larger, ~110 MB; ~80% survive).
NEED_GB=$(( BIGGEST * 19 / 1024 + BIGGEST * 50 / 1024 + BIGGEST * 80 / 100 * 110 / 1024 ))
mkdir -p "$WORK_ROOT" 2>/dev/null || true
FREE_GB=$(( $(df -Pk "$WORK_ROOT" | awk 'NR==2{print $4}') / 1024 / 1024 ))
echo
echo "$TOTAL frames ($(( TOTAL * 10 / 3600 )) h at 10 s), $(( BYTES / 1024 / 1024 )) GB raw, scope/filter $KEY"
echo "${#REUSE[@]} stored master(s) reused, $NBATCH new batch(es) to stack, $NMASTERS master(s) in the result"
[ "$NBATCH" -gt 0 ] && echo "Scratch: $WORK_ROOT  (~${NEED_GB} GB needed at peak for the largest new batch, ${FREE_GB} GB free)"
[ "$NBATCH" -eq 0 ] || [ "$FREE_GB" -gt "$NEED_GB" ] || echo "WARNING: scratch space looks tight; the canvas size decides the real figure."
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

# ---- run folder ----
RUN="$WORK_ROOT/${TNAME}_$NAME"
OUT="$TDIR/processing/$NAME"
MASTERS="$RUN/masters"
mkdir -p "$MASTERS"
USED=(${REUSE[@]+"${REUSE[@]}"})          # stored masters that go into the result
LOG="$RUN/astro-stack.log"
exec > >(tee -a "$LOG") 2>&1
echo "=== astro-stack $TNAME $(date) ==="
t0=$(date +%s)
REG_TOTAL=$REUSE_FRAMES

for b in $(seq 1 "$NBATCH"); do
  BM="$MASTERS/batch$b.fit"
  B="$RUN/batch$b"
  rm -rf "$B"; mkdir -p "$B/links" "$B/work"
  bcount=0
  echo; echo "--- batch $b/$NBATCH ($(date +%T)) ---"
  for i in "${!SEL[@]}"; do
    [ "${BATCH[$i]:-0}" = "$b" ] || continue
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
  # Store it at once, so a later failure (or a later run) reuses it.
  stored=$(for i in "${!SEL[@]}"; do if [ "${BATCH[$i]:-0}" = "$b" ]; then printf '%s\t%s\n' "$(basename "${SEL[$i]}")" "${CNT[$i]}"; fi; done \
           | "${BM_PY[@]}" add --store "$STORE" --settings "$SETTINGS" --file "$BM" --frames "$reg" --move)
  USED+=("$stored"); REG_TOTAL=$((REG_TOTAL + reg))
  [ "$KEEP" -eq 1 ] || rm -rf "$B"
  echo "  batch $b done: $stored"
done

RES="$RUN/result_linear.fit"
if [ "${#USED[@]}" -eq 1 ]; then
  cp "${USED[0]}" "$RES"
else
  # Combine the masters: plate-solve, register onto one canvas, then a plain
  # average weighted by how many frames went into each master.
  C="$RUN/combine"; rm -rf "$C" "$RES"; mkdir -p "$C/in"
  k=0; for m in "${USED[@]}"; do k=$((k+1)); ln -s "$m" "$C/in/$(printf 'm_%03d.fit' "$k")"; done
  cat > "$C/combine.ssf" <<EOF
requires 1.4.0
set32bits
setext fit
link m -out=$C
cd $C
seqplatesolve m
seqapplyreg m -framing=max
stack r_m rej n -norm=addscale -weight=nbstack -maximize -32b -out=${RES%.fit}
close
EOF
  echo; echo "--- combining ${#USED[@]} masters ($(date +%T)) ---"
  siril -d "$C/in" -s "$C/combine.ssf"
fi
[ -f "$RES" ] || { echo "No final result; see the log above. The batch masters are in $STORE."; exit 1; }

mkdir -p "$OUT"
cp "$RES" "$OUT/${TNAME}_${NAME}_linear.fit"
{ echo "astro-stack run $NAME"
  echo "frames: $TOTAL selected, $REG_TOTAL stacked, from ${#USED[@]} batch master(s) (${#REUSE[@]} reused, $NBATCH new)"
  echo "settings: $SETTINGS"
  echo "nights:"; night_list | sed 's/^/  /'
  echo "masters:"; for m in "${USED[@]}"; do echo "  $(basename "$m")"; done; } > "$OUT/README.txt"
echo; echo "Done in $(( ($(date +%s) - t0) / 60 )) min."
echo "Result: $OUT/${TNAME}_${NAME}_linear.fit"
echo "(Batch masters are kept in $STORE for the next run; $RUN can be deleted.)"
cp "$LOG" "$OUT/astro-stack.log"
