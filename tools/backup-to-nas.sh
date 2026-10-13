#!/bin/bash
# Back up the Astro archive to the NAS, then verify the NAS copy against MANIFEST.sha256.
# Copies only: never deletes anything on the NAS. Safe to re-run; unchanged files are skipped.
# Usage: astro-backup [destination]   (defaults: ASTRO_ARCHIVE=/Volumes/Astro, ASTRO_NAS=/Volumes/home/Astrophotography)
set -u
case "${1:-}" in -h|--help) sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; -*) echo "unknown option: $1 (try --help)"; exit 1 ;; esac
SRC="${ASTRO_ARCHIVE:-/Volumes/Astro}"
DST="${1:-${ASTRO_NAS:-/Volumes/home/Astrophotography}}"
mkdir -p "$SRC/_tools/logs"
LOG="$SRC/_tools/logs/backup-$(date +%Y%m%d-%H%M).log"
exec > >(tee -a "$LOG") 2>&1
echo "=== $(date) backup $SRC -> $DST"

[ -f "$SRC/MANIFEST.sha256" ] || { echo "Astro drive not mounted (no $SRC/MANIFEST.sha256)"; exit 1; }
[ -d "$DST" ] || { echo "NAS folder not found: $DST  (is the share mounted?)"; exit 1; }
[ -w "$DST" ] || { echo "NAS folder not writable: $DST"; exit 1; }

NEED=$(du -sk "$SRC/Targets" "$SRC/_tools" | awk '{s+=$1} END{printf "%d", s/1024/1024}')
FREE=$(df -g "$DST" | awk 'NR==2{print $4}')
echo "Archive: ~${NEED} GiB   Free on NAS: ${FREE} GiB"
[ "$FREE" -gt "$NEED" ] || echo "WARNING: NAS free space may be short (files already on the NAS don't need space again)."

# ---- 1. copy ----
# Plain cp (to a .partial name, then rename). macOS's built-in rsync (openrsync) fails on SMB shares.
# A file is skipped when the NAS already has it with the same size and it isn't newer on Astro.
echo "=== $(date +%H:%M:%S) listing files"
LIST="$(mktemp)"
( cd "$SRC" && find . -type f ! -name '.DS_Store' ! -name '._*' ! -name '*.partial' \
    ! -path './.Spotlight-V100/*' ! -path './.fseventsd/*' ! -path './.Trashes/*' ! -path './_tools/logs/*' -print0 ) > "$LIST"
N=$(tr -cd '\0' < "$LIST" | wc -c | tr -d ' ')
echo "=== $(date +%H:%M:%S) copying ($N files)"
i=0; copied=0; skipped=0; failed=0
while IFS= read -r -d '' f; do
  f="${f#./}"; s="$SRC/$f"; d="$DST/$f"; i=$((i+1))
  if [ -f "$d" ] && [ "$(stat -f %z "$s")" = "$(stat -f %z "$d")" ] && ! [ "$s" -nt "$d" ]; then
    skipped=$((skipped+1))
  else
    mkdir -p "$(dirname "$d")"
    if { cp -p "$s" "$d.partial" 2>/dev/null || cp "$s" "$d.partial"; } && \
       [ "$(stat -f %z "$s")" = "$(stat -f %z "$d.partial")" ] && mv -f "$d.partial" "$d"; then
      copied=$((copied+1))
    else
      echo "COPY FAILED: $f"; failed=$((failed+1))
    fi
  fi
  [ $((i % 500)) -eq 0 ] && echo "  $(date +%H:%M:%S)  $i / $N  (copied $copied, already on NAS $skipped, failed $failed)"
done < "$LIST"
rm -f "$LIST"
echo "Copy done: copied $copied, already on NAS $skipped, failed $failed"
[ "$failed" -eq 0 ] || { echo "Some files failed to copy (see above). Re-run the script to retry them."; exit 1; }
echo "=== $(date +%H:%M:%S) copy complete"

# ---- 2. verify the NAS copy against the archive's checksums ----
TOTAL=$(wc -l < "$DST/MANIFEST.sha256" | tr -d ' ')
echo "=== $(date +%H:%M:%S) verifying $TOTAL files on the NAS (reads the whole archive over the network)"
cd "$DST" || exit 1
shasum -a 256 -c MANIFEST.sha256 2>/dev/null | grep -v ': OK$' > "$LOG.failed"
# A file that fails only because it was edited after the manifest was written (e.g. notes.md)
# is fine as long as the NAS copy is identical to the drive's current copy.
: > "$LOG.problems"; EDITED=0; REPAIRED=0
while IFS= read -r line; do
  f="${line%: FAILED*}"
  if [ -f "$SRC/$f" ] && [ -f "$DST/$f" ] && cmp -s "$SRC/$f" "$DST/$f"; then EDITED=$((EDITED+1))
  elif [ -f "$SRC/$f" ] && grep -qF "  $f" "$SRC/MANIFEST.sha256" && \
       [ "$(shasum -a 256 "$SRC/$f" | cut -c1-64)" = "$(grep -F "  $f" "$SRC/MANIFEST.sha256" | head -1 | cut -c1-64)" ] && \
       cp -p "$SRC/$f" "$DST/$f" && cmp -s "$SRC/$f" "$DST/$f"; then
    echo "Repaired: $f (NAS copy was bad; recopied from Astro and re-checked)"; REPAIRED=$((REPAIRED+1))
  else echo "$line" >> "$LOG.problems"; fi
done < "$LOG.failed"
rm -f "$LOG.failed"
BAD=$(wc -l < "$LOG.problems" | tr -d ' ')

# Files on the drive that aren't in the manifest yet (e.g. added later) were copied but not hash-checked
UNLISTED=$(cd "$SRC" && find Targets _tools -type f ! -name '._*' ! -name '.DS_Store' ! -path '_tools/logs/*' ! -name 'backup-to-nas.sh' | \
  awk 'NR==FNR{ sub(/^[0-9a-f]+  /,""); m[$0]=1; next } !($0 in m)' MANIFEST.sha256 - | wc -l | tr -d ' ')

if [ "$BAD" -eq 0 ]; then
  echo "Verified: all $TOTAL files on the NAS match the archive."
  [ "$EDITED" -eq 0 ] || echo "($EDITED of them were edited after the manifest was written, e.g. notes.md; the NAS copy matches the drive's current version.)"
  [ "$REPAIRED" -eq 0 ] || echo "($REPAIRED file(s) were repaired from the Astro drive; see 'Repaired:' lines above.)"
  rm -f "$LOG.problems"
  [ "$UNLISTED" -eq 0 ] || echo "Note: $UNLISTED file(s) on Astro aren't in MANIFEST.sha256 yet; they were copied but not hash-checked."
  echo "=== $(date) BACKUP COMPLETE"
else
  echo "PROBLEMS: $BAD file(s) failed verification:"; cat "$LOG.problems"
  echo "These could not be repaired automatically (the Astro copy may itself be bad). Tell Claude."
  echo "=== $(date) BACKUP FINISHED WITH ERRORS"; exit 1
fi
