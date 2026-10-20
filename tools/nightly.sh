#!/bin/bash
# astro-nightly: the whole routine after a night out, in one command:
#   astro-ingest  ->  astro-backup  ->  astro-stack --changed
# Each step runs only if the one before it succeeded, so new data is safely on
# the NAS before any stacking starts.
#
#   astro-nightly [SOURCE ...] [--yes] [--no-previews] [--no-backup] [--no-stack]
#                 [--dry-run]
#
#   SOURCE         Seestar folder(s) for astro-ingest (default: as astro-ingest)
#   --yes          don't ask before copying (for fully unattended runs)
#   --no-previews  passed to astro-ingest
#   --no-backup    skip astro-backup (e.g. the NAS is off); stacking still runs
#   --no-stack     stop after the backup
#   --dry-run      show what astro-ingest would copy and what is already queued
#                  for stacking; change nothing
#
# Without --yes, astro-ingest asks once before copying; answer and walk away,
# the rest runs unattended. Each step writes its own log (backup logs in
# <archive>/_tools/logs/, stack logs in each result folder).
# Exit status: 0 if every step succeeded, else that of the step that failed
# (astro-ingest uses 2 for "cancelled at the prompt").
set -u
usage() { awk 'NR>1 && /^set -u/{exit} NR>1' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/../.venv/bin/python"; [ -x "$PY" ] || PY=python3
INGEST=(); BACKUP=1; STACK=1; DRY=0
ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y|--no-previews) INGEST+=("$1"); shift ;;
    --no-backup) BACKUP=0; shift ;;
    --no-stack) STACK=0; shift ;;
    --dry-run|-n) DRY=1; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "unknown option: $1"; usage 1 ;;
    *) INGEST+=("$1"); shift ;;
  esac
done

# Keep the machine awake for the whole run (the backup and stack steps see this
# and don't wrap themselves a second time).
if [ "$DRY" -eq 0 ] && [ -z "${ASTRO_CAFFEINATED:-}" ]; then
  export ASTRO_CAFFEINATED=1
  if command -v caffeinate >/dev/null 2>&1; then exec caffeinate -i /bin/bash "$0" ${ARGS[@]+"${ARGS[@]}"}
  elif command -v systemd-inhibit >/dev/null 2>&1 && systemd-inhibit --what=sleep true >/dev/null 2>&1; then
    exec systemd-inhibit --what=sleep:idle --who=astro-nightly --why="ingest, backup and stack" /bin/bash "$0" ${ARGS[@]+"${ARGS[@]}"}
  fi
fi

step() { echo; echo "######## $(date +%H:%M:%S)  $1"; }
stop() { echo; echo "######## astro-nightly stopped: $1"; exit "$2"; }

if [ "$DRY" -eq 1 ]; then
  step "1/2 astro-ingest --dry-run"
  "$PY" "$HERE/ingest.py" ${INGEST[@]+"${INGEST[@]}"} --dry-run || exit $?
  step "2/2 astro-stack --changed --dry-run (nights queued so far; tonight's are added by the real run)"
  /bin/bash "$HERE/stack/astro-stack.sh" --changed --dry-run
  exit $?
fi

t0=$(date +%s)
step "1/3 astro-ingest"
"$PY" "$HERE/ingest.py" ${INGEST[@]+"${INGEST[@]}"}
rc=$?
[ "$rc" -eq 2 ] && stop "ingest cancelled; nothing backed up or stacked" 2
[ "$rc" -eq 0 ] || stop "astro-ingest failed (exit $rc); fix that first, nothing was backed up or stacked" "$rc"

if [ "$BACKUP" -eq 1 ]; then
  step "2/3 astro-backup"
  /bin/bash "$HERE/backup-to-nas.sh"
  rc=$?
  [ "$rc" -eq 0 ] || stop "astro-backup failed (exit $rc); not stacking until the data is backed up" "$rc"
else
  step "2/3 astro-backup skipped (--no-backup)"
fi

if [ "$STACK" -eq 1 ]; then
  step "3/3 astro-stack --changed"
  /bin/bash "$HERE/stack/astro-stack.sh" --changed
  rc=$?
  [ "$rc" -eq 0 ] || stop "astro-stack --changed failed for some targets (exit $rc); see above" "$rc"
else
  step "3/3 astro-stack skipped (--no-stack); run astro-stack --changed later"
fi
echo; echo "######## astro-nightly done in $(( ($(date +%s) - t0) / 60 )) min."
