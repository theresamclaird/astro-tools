#!/bin/bash
# Back up the Astro archive to the NAS, then verify the NAS copy against MANIFEST.sha256.
# Copies only: never deletes anything on the NAS. Safe to re-run; unchanged files are skipped.
# Usage: astro-backup [destination]
#   destination (default $ASTRO_NAS) is either
#     host:/path   over SSH (recommended; host is an ~/.ssh/config alias, key login required), or
#     /local/path  a mounted share, e.g. /Volumes/home/Astrophotography
set -u
case "${1:-}" in -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; -*) echo "unknown option: $1 (try --help)"; exit 1 ;; esac

# Keep the Mac awake for the whole run (a sleeping Mac drops network connections).
if [ -z "${ASTRO_CAFFEINATED:-}" ] && command -v caffeinate >/dev/null 2>&1; then
  export ASTRO_CAFFEINATED=1; exec caffeinate -i /bin/bash "$0" "$@"
fi

SRC="${ASTRO_ARCHIVE:-/Volumes/Astro}"
DST="${1:-${ASTRO_NAS:-/Volumes/home/Astrophotography}}"
[ -f "$SRC/MANIFEST.sha256" ] || { echo "Astro drive not mounted (no $SRC/MANIFEST.sha256)"; exit 1; }
mkdir -p "$SRC/_tools/logs"
LOG="$SRC/_tools/logs/backup-$(date +%Y%m%d-%H%M).log"
exec > >(tee -a "$LOG") 2>&1
echo "=== $(date) backup $SRC -> $DST"

EXCLUDES=(.Spotlight-V100 .fseventsd .Trashes .DS_Store '._*' '*.partial' _tools/logs)
NEED=$(du -sk "$SRC/Targets" "$SRC/_tools" | awk '{s+=$1} END{printf "%d", s/1024/1024}')
sha() { shasum -a 256 "$1" 2>/dev/null | cut -c1-64; }
manifest_sha() { grep -F "  $1" "$SRC/MANIFEST.sha256" | awk -v p="$1" 'substr($0,67)==p{print substr($0,1,64); exit}'; }

case "$DST" in
  /*) MODE=local ;;
  *:/*) MODE=ssh; HOST="${DST%%:*}"; RPATH="${DST#*:}" ;;
  *) echo "Destination must be host:/path or an absolute folder: $DST"; exit 1 ;;
esac

if [ "$MODE" = ssh ]; then
  # ------------------------------------------------------------------ SSH mode
  q() { printf '%q' "$1"; }                                   # quote for the remote shell
  remote() { ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" "$@"; }
  remote true 2>/dev/null || { echo "Can't log in to '$HOST' without a password. Set up an SSH key (see the README)."; exit 1; }
  case "$RPATH" in *' '*) echo "The NAS path must not contain spaces: $RPATH"; exit 1 ;; esac
  remote "mkdir -p $(q "$RPATH")" || exit 1
  FREE=$(remote "df -k $(q "$RPATH")" | awk 'NR==2{printf "%d", $4/1024/1024}')
  echo "Archive: ~${NEED} GiB   Free on NAS: ${FREE:-?} GiB"
  [ -n "$FREE" ] && [ "$FREE" -le "$NEED" ] && echo "WARNING: NAS free space may be short (files already on the NAS don't need space again)."

  RSYNC=rsync
  for r in /opt/homebrew/bin/rsync /usr/local/bin/rsync; do [ -x "$r" ] && { RSYNC="$r"; break; }; done
  EX=(); for e in "${EXCLUDES[@]}"; do EX+=(--exclude "$e"); done

  echo "=== $(date +%H:%M:%S) copying with $RSYNC (only new or changed files are sent)"
  "$RSYNC" -rt -e "ssh -o BatchMode=yes" "${EX[@]}" "$SRC/" "$HOST:$RPATH/" | grep -v '/$'
  rc=${PIPESTATUS[0]}
  [ "$rc" -eq 0 ] || { echo "rsync exited with code $rc. Re-run to resume; only what's missing is sent."; exit 1; }
  echo "=== $(date +%H:%M:%S) copy complete"

  TOTAL=$(wc -l < "$SRC/MANIFEST.sha256" | tr -d ' ')
  echo "=== $(date +%H:%M:%S) verifying $TOTAL files (the NAS checksums its own disks; only mismatches come back)"
  FAILED="$(mktemp)"
  remote "cd $(q "$RPATH") && sha256sum -c MANIFEST.sha256 2>/dev/null | grep -v ': OK\$'" > "$FAILED"
  nas_sha() { remote "sha256sum $(q "$RPATH/$1") 2>/dev/null" | cut -c1-64; }
  put() { remote "mkdir -p $(q "$(dirname "$RPATH/$1")") && cat > $(q "$RPATH/$1.partial") && mv -f $(q "$RPATH/$1.partial") $(q "$RPATH/$1")" < "$SRC/$1"; }
else
  # ---------------------------------------------------------------- local mode
  [ -d "$DST" ] || { echo "NAS folder not found: $DST  (is the share mounted?)"; exit 1; }
  [ -w "$DST" ] || { echo "NAS folder not writable: $DST"; exit 1; }
  FREE=$(df -g "$DST" | awk 'NR==2{print $4}')
  echo "Archive: ~${NEED} GiB   Free on NAS: ${FREE} GiB"
  [ "$FREE" -gt "$NEED" ] || echo "WARNING: NAS free space may be short (files already on the NAS don't need space again)."

  # Plain cp to a .partial name, then rename (macOS's built-in rsync fails on SMB shares).
  # A file is skipped when the NAS already has it with the same size and it isn't newer on Astro.
  echo "=== $(date +%H:%M:%S) listing files"
  LIST="$(mktemp)"
  FIND_EX=(); for e in "${EXCLUDES[@]}"; do case "$e" in */*) FIND_EX+=(! -path "./$e/*") ;; *) FIND_EX+=(! -name "$e" ! -path "./$e/*") ;; esac; done
  ( cd "$SRC" && find . -type f "${FIND_EX[@]}" -print0 ) > "$LIST"
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

  TOTAL=$(wc -l < "$DST/MANIFEST.sha256" | tr -d ' ')
  echo "=== $(date +%H:%M:%S) verifying $TOTAL files on the NAS (reads the whole archive over the network)"
  FAILED="$(mktemp)"
  ( cd "$DST" && shasum -a 256 -c MANIFEST.sha256 2>/dev/null | grep -v ': OK$' ) > "$FAILED"
  nas_sha() { sha "$DST/$1"; }
  put() { mkdir -p "$(dirname "$DST/$1")" && cp -p "$SRC/$1" "$DST/$1.partial" && mv -f "$DST/$1.partial" "$DST/$1"; }
fi

# ---- classify verification failures (same for both modes) ----
# EDITED:   changed on Astro after the manifest was written (e.g. notes.md) and the NAS matches Astro: fine.
# REPAIRED: NAS copy bad, Astro copy still matches the manifest: recopied and re-checked.
PROBLEMS="$(mktemp)"; EDITED=0; REPAIRED=0
while IFS= read -r line; do
  f="${line%: FAILED*}"
  [ -n "$f" ] || continue
  a=$(sha "$SRC/$f"); n=$(nas_sha "$f")
  if [ -n "$a" ] && [ "$a" = "$n" ]; then
    EDITED=$((EDITED+1))
  elif [ -n "$a" ] && [ "$a" = "$(manifest_sha "$f")" ] && put "$f" && [ "$(nas_sha "$f")" = "$a" ]; then
    echo "Repaired: $f (NAS copy was bad; recopied from Astro and re-checked)"; REPAIRED=$((REPAIRED+1))
  else
    echo "$line" >> "$PROBLEMS"
  fi
done < "$FAILED"
rm -f "$FAILED"
BAD=$(wc -l < "$PROBLEMS" | tr -d ' ')

# Files on the drive that aren't in the manifest yet were copied but not hash-checked
UNLISTED=$(cd "$SRC" && find Targets _tools -type f ! -name '._*' ! -name '.DS_Store' ! -path '_tools/logs/*' | \
  awk 'NR==FNR{ m[substr($0,67)]=1; next } !($0 in m)' MANIFEST.sha256 - | wc -l | tr -d ' ')

if [ "$BAD" -eq 0 ]; then
  echo "Verified: all $TOTAL files on the NAS match the archive."
  [ "$EDITED" -eq 0 ] || echo "($EDITED of them were edited after the manifest was written, e.g. notes.md; the NAS copy matches the drive's current version.)"
  [ "$REPAIRED" -eq 0 ] || echo "($REPAIRED file(s) were repaired from the Astro drive; see 'Repaired:' lines above.)"
  [ "$UNLISTED" -eq 0 ] || echo "Note: $UNLISTED file(s) on Astro aren't in MANIFEST.sha256 yet; they were copied but not hash-checked."
  rm -f "$PROBLEMS"
  echo "=== $(date) BACKUP COMPLETE"
else
  echo "PROBLEMS: $BAD file(s) failed verification:"; cat "$PROBLEMS"; rm -f "$PROBLEMS"
  echo "These could not be repaired automatically (the Astro copy may itself be bad)."
  echo "=== $(date) BACKUP FINISHED WITH ERRORS"; exit 1
fi
