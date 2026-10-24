#!/bin/bash
# Back up the Astro archive to a destination and verify the copy.
# Copies only: never deletes anything at the destination. Safe to re-run; unchanged files are skipped.
# Usage: astro-backup [--full] [destination]
#   destination (default $ASTRO_NAS) is either
#     host:/path   over SSH (host is an ~/.ssh/config alias, key login required), or
#     /local/path  a mounted drive or share, e.g. /run/media/$USER/AstroBackup
#   Verification: by default only the files copied in this run are checked (each is read
#   back from the destination and compared with the archive). --full checks every file in
#   MANIFEST.sha256 at the destination, which catches files that went bad on the backup
#   drive since they were copied; run it about once a month (you'll be reminded).
set -u
FULL=0; DST_ARG=""
for a in "$@"; do
  case "$a" in
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --full) FULL=1 ;;
    -*) echo "unknown option: $a (try --help)"; exit 1 ;;
    *) [ -z "$DST_ARG" ] || { echo "only one destination"; exit 1; }; DST_ARG="$a" ;;
  esac
done

# Keep the machine awake for the whole run (a sleeping machine drops network connections).
if [ -z "${ASTRO_CAFFEINATED:-}" ]; then
  export ASTRO_CAFFEINATED=1
  if command -v caffeinate >/dev/null 2>&1; then exec caffeinate -i /bin/bash "$0" "$@"
  elif command -v systemd-inhibit >/dev/null 2>&1 && systemd-inhibit --what=sleep true >/dev/null 2>&1; then
    exec systemd-inhibit --what=sleep:idle --who=astro-backup --why="backing up the Astro archive" /bin/bash "$0" "$@"
  fi
fi

# The archive: $ASTRO_ARCHIVE, else wherever a drive named Astro is mounted (macOS or Linux).
if [ -n "${ASTRO_ARCHIVE:-}" ]; then SRC="$ASTRO_ARCHIVE"
else SRC=/Volumes/Astro
  for c in "/run/media/$USER/Astro" "/media/$USER/Astro"; do [ -f "$c/MANIFEST.sha256" ] && { SRC="$c"; break; }; done
fi
DST="${DST_ARG:-${ASTRO_NAS:-/Volumes/home/Astrophotography}}"
[ -f "$SRC/MANIFEST.sha256" ] || { echo "Astro drive not mounted (no $SRC/MANIFEST.sha256)"; exit 1; }
mkdir -p "$SRC/_tools/logs"
exec 3>&1   # the terminal, kept aside so rsync's progress line reaches it but not the log
LOG="$SRC/_tools/logs/backup-$(date +%Y%m%d-%H%M).log"
exec > >(tee -a "$LOG") 2>&1
echo "=== $(date) backup $SRC -> $DST ($([ "$FULL" -eq 1 ] && echo "full verification" || echo "verifying new files only"))"
# When this destination last had a full verification (kept on the archive, per destination)
MARK="$SRC/_tools/verified/$(printf '%s' "$DST" | tr '/: ' '___')"
COPIED="$(mktemp)"   # files copied in this run, one per line

EXCLUDES=(.Spotlight-V100 .fseventsd .Trashes .DS_Store '._*' '*.partial' 'System Volume Information' '$RECYCLE.BIN' lost+found _tools/logs _tools/stack-queue.tsv _tools/verified)
NEED=$(du -sk "$SRC/Targets" "$SRC/_tools" | awk '{s+=$1} END{printf "%d", s/1024/1024}')
# GNU (Linux) and BSD (macOS) tools differ; these work on both.
if command -v sha256sum >/dev/null 2>&1; then SHA256=(sha256sum); else SHA256=(shasum -a 256); fi
sha() { "${SHA256[@]}" "$1" 2>/dev/null | cut -c1-64; }
fsize() { stat -c %s "$1" 2>/dev/null || stat -f %z "$1"; }
manifest_sha() { grep -F "  $1" "$SRC/MANIFEST.sha256" | awk -v p="$1" 'substr($0,67)==p{print substr($0,1,64); exit}'; }

case "$DST" in
  /*) MODE=local ;;
  *:/*) MODE=ssh; HOST="${DST%%:*}"; RPATH="${DST#*:}" ;;
  *) echo "Destination must be host:/path or an absolute folder: $DST"; exit 1 ;;
esac

if [ "$MODE" = ssh ]; then
  # ------------------------------------------------------------------ SSH mode
  q() { printf '%q' "$1"; }                                   # quote for the remote shell
  # Keepalives: a quiet-but-alive connection stays up during long remote work (the NAS-side
  # checksum pass), and a dead one is noticed in ~2 minutes instead of hanging.
  SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=8"
  remote() { ssh $SSH_OPTS "$HOST" "$@"; }
  if ! err=$(remote true 2>&1); then
    echo "Can't log in to '$HOST' over SSH. ssh said:"; echo "    ${err:-(no message)}"
    case "$err" in
      *ermission\ denied*|*assword*) echo "Key login isn't working; see 'Setting up SSH to the NAS' in the README." ;;
      *efused*|*closed\ by*|*reset\ by*) echo "The NAS refused the connection. Check that SSH is enabled, and DSM's auto-block list (Control Panel > Security > Protection)." ;;
      *imed\ out*|*o\ route*|*resolve*|*nreachable*) echo "The NAS isn't reachable from this Mac right now (network, or the NAS is busy/asleep)." ;;
    esac
    exit 1
  fi
  case "$RPATH" in *' '*) echo "The NAS path must not contain spaces: $RPATH"; exit 1 ;; esac
  remote "mkdir -p $(q "$RPATH")" || exit 1
  FREE=$(remote "df -k $(q "$RPATH")" | awk 'NR==2{printf "%d", $4/1024/1024}')
  echo "Archive: ~${NEED} GiB   Free at destination: ${FREE:-?} GiB"
  [ -n "$FREE" ] && [ "$FREE" -le "$NEED" ] && echo "WARNING: free space may be short (files already there don't need space again)."

  RSYNC=rsync
  for r in /opt/homebrew/bin/rsync /usr/local/bin/rsync; do [ -x "$r" ] && { RSYNC="$r"; break; }; done
  EX=(); for e in "${EXCLUDES[@]}"; do EX+=(--exclude "$e"); done
  # A live progress line (total %, rate, time left) when run in a terminal with rsync 3.1+.
  # macOS's built-in rsync lacks --info; `brew install rsync` adds it. --no-inc-recursive
  # scans the whole tree first so the percentage is of the real total.
  PROGRESS=0
  [ -t 3 ] && "$RSYNC" --help 2>&1 | grep -q -- '--info=' && PROGRESS=1
  # rsync's own log of what it sent, so only those files need verifying. macOS's built-in
  # rsync can't write it; then every run does a full verification.
  XFER="$(mktemp)"; LOGOPT=()
  if "$RSYNC" --help 2>&1 | grep -q -- '--log-file-format'; then
    LOGOPT=(--log-file="$XFER" --log-file-format='%i %n')
  elif [ "$FULL" -eq 0 ]; then
    echo "(This rsync can't list what it sent, so every file will be verified. 'brew install rsync' fixes that.)"; FULL=1
  fi

  # rsync skips what's already on the NAS, so after a dropped connection we just run it again.
  TRIES="${ASTRO_BACKUP_TRIES:-6}"; WAIT="${ASTRO_BACKUP_RETRY_WAIT:-60}"; try=1
  while :; do
    echo "=== $(date +%H:%M:%S) copying with $RSYNC (only new or changed files are sent; attempt $try/$TRIES)"
    if [ "$PROGRESS" -eq 1 ]; then
      "$RSYNC" -rt --info=progress2 --no-inc-recursive ${LOGOPT[@]+"${LOGOPT[@]}"} -e "ssh $SSH_OPTS" "${EX[@]}" "$SRC/" "$HOST:$RPATH/" >&3
      rc=$?
      echo >&3   # end the progress line
    else
      "$RSYNC" -rt ${LOGOPT[@]+"${LOGOPT[@]}"} -e "ssh $SSH_OPTS" "${EX[@]}" "$SRC/" "$HOST:$RPATH/" | grep -v '/$'
      rc=${PIPESTATUS[0]}
    fi
    [ "$rc" -eq 0 ] && break
    if [ "$try" -ge "$TRIES" ]; then
      echo "rsync failed $try times (last exit code $rc). Re-run later; only what's missing will be sent."; exit 1
    fi
    echo "--- rsync stopped (exit code $rc), probably a dropped connection. Retrying in ${WAIT}s."
    sleep "$WAIT"
    until remote true 2>/dev/null; do
      try=$((try+1)); [ "$try" -le "$TRIES" ] || { echo "The destination is not reachable. Re-run later."; exit 1; }
      echo "--- destination not reachable yet; waiting ${WAIT}s"; sleep "$WAIT"
    done
    try=$((try+1))
  done
  echo "=== $(date +%H:%M:%S) copy complete"
  # Files actually sent ("YYYY/MM/DD HH:MM:SS [pid] <f......... path"; < sent, > local copy)
  awk '$4 ~ /^[<>]f/ { sub(/^[^ ]+ [^ ]+ [^ ]+ [^ ]+ /, ""); print }' "$XFER" | sort -u > "$COPIED"; rm -f "$XFER"

  FAILED="$(mktemp)"
  DONE_MARK="__ASTRO_VERIFY_DONE__"
  if [ "$FULL" -eq 1 ]; then
    TOTAL=$(wc -l < "$SRC/MANIFEST.sha256" | tr -d ' ')
    echo "=== $(date +%H:%M:%S) verifying all $TOTAL files (the destination checksums its own disks; only mismatches come back)"
    remote "cd $(q "$RPATH") && { sha256sum -c MANIFEST.sha256 2>/dev/null | grep -v ': OK\$'; echo $DONE_MARK; }" > "$FAILED"
  else
    TOTAL=$(wc -l < "$COPIED" | tr -d ' ')
    echo "=== $(date +%H:%M:%S) verifying the $TOTAL file(s) copied in this run"
    # Checksum them here, then have the destination check its copies against that list.
    SUMS="$(mktemp)"
    while IFS= read -r f; do printf '%s  %s\n' "$(sha "$SRC/$f")" "$f"; done < "$COPIED" > "$SUMS"
    remote "cd $(q "$RPATH") && { sha256sum -c - 2>/dev/null | grep -v ': OK\$'; echo $DONE_MARK; }" < "$SUMS" > "$FAILED"
    rm -f "$SUMS"
  fi
  # No end marker = the connection dropped mid-check; never report success on a partial check.
  grep -qx "$DONE_MARK" "$FAILED" || { echo "The checksum pass at the destination was interrupted. Re-run with --full (nothing will be recopied)."; exit 1; }
  grep -vx "$DONE_MARK" "$FAILED" > "$FAILED.x"; mv "$FAILED.x" "$FAILED"
  nas_sha() { remote "sha256sum $(q "$RPATH/$1") 2>/dev/null" | cut -c1-64; }
  put() { remote "mkdir -p $(q "$(dirname "$RPATH/$1")") && cat > $(q "$RPATH/$1.partial") && mv -f $(q "$RPATH/$1.partial") $(q "$RPATH/$1")" < "$SRC/$1"; }
else
  # ---------------------------------------------------------------- local mode
  [ -d "$DST" ] || { echo "Destination folder not found: $DST  (is the drive or share mounted?)"; exit 1; }
  [ -w "$DST" ] || { echo "Destination folder not writable: $DST"; exit 1; }
  FREE=$(df -Pk "$DST" | awk 'NR==2{printf "%d", $4/1024/1024}')
  echo "Archive: ~${NEED} GiB   Free at destination: ${FREE} GiB"
  [ "$FREE" -gt "$NEED" ] || echo "WARNING: free space may be short (files already there don't need space again)."

  # Plain cp to a .partial name, then rename (macOS's built-in rsync fails on SMB shares).
  # A file is skipped when the destination already has it with the same size and it isn't newer on Astro.
  echo "=== $(date +%H:%M:%S) listing files"
  LIST="$(mktemp)"
  FIND_EX=(); for e in "${EXCLUDES[@]}"; do case "$e" in */*) FIND_EX+=(! -path "./$e" ! -path "./$e/*") ;; *) FIND_EX+=(! -name "$e" ! -path "./$e/*") ;; esac; done
  ( cd "$SRC" && find . -type f "${FIND_EX[@]}" -print0 ) > "$LIST"
  N=$(tr -cd '\0' < "$LIST" | wc -c | tr -d ' ')
  echo "=== $(date +%H:%M:%S) copying ($N files)"
  i=0; copied=0; skipped=0; failed=0
  while IFS= read -r -d '' f; do
    f="${f#./}"; s="$SRC/$f"; d="$DST/$f"; i=$((i+1))
    if [ -f "$d" ] && [ "$(fsize "$s")" = "$(fsize "$d")" ] && ! [ "$s" -nt "$d" ]; then
      skipped=$((skipped+1))
    else
      mkdir -p "$(dirname "$d")"
      if { cp -p "$s" "$d.partial" 2>/dev/null || cp "$s" "$d.partial"; } && \
         [ "$(fsize "$s")" = "$(fsize "$d.partial")" ] && mv -f "$d.partial" "$d"; then
        copied=$((copied+1)); printf '%s\n' "$f" >> "$COPIED"
      else
        echo "COPY FAILED: $f"; failed=$((failed+1))
      fi
    fi
    [ $((i % 500)) -eq 0 ] && echo "  $(date +%H:%M:%S)  $i / $N  (copied $copied, already there $skipped, failed $failed)"
  done < "$LIST"
  rm -f "$LIST"
  echo "Copy done: copied $copied, already there $skipped, failed $failed"
  [ "$failed" -eq 0 ] || { echo "Some files failed to copy (see above). Re-run the script to retry them."; exit 1; }
  echo "=== $(date +%H:%M:%S) copy complete"

  # Make the read-back come from the disk, not from memory still holding what was just
  # written (Linux; elsewhere this is skipped and the check reads what the OS has cached).
  evict() { command -v python3 >/dev/null 2>&1 || return 0
    python3 - "$DST" "$@" <<'EOF' 2>/dev/null || true
import os, sys
root = sys.argv[1]
for f in sys.argv[2:]:
    try:
        fd = os.open(os.path.join(root, f), os.O_RDONLY); os.fsync(fd)
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED); os.close(fd)
    except Exception: pass
EOF
  }
  FAILED="$(mktemp)"
  if [ "$FULL" -eq 1 ]; then
    TOTAL=$(wc -l < "$DST/MANIFEST.sha256" | tr -d ' ')
    echo "=== $(date +%H:%M:%S) verifying all $TOTAL files at the destination (reads the whole backup)"
    ( cd "$DST" && "${SHA256[@]}" -c MANIFEST.sha256 2>/dev/null | grep -v ': OK$' ) > "$FAILED"
  else
    TOTAL=$(wc -l < "$COPIED" | tr -d ' ')
    echo "=== $(date +%H:%M:%S) verifying the $TOTAL file(s) copied in this run"
    sync
    while IFS= read -r f; do
      evict "$f"
      [ "$(sha "$SRC/$f")" = "$(sha "$DST/$f")" ] || echo "$f: FAILED" >> "$FAILED"
    done < "$COPIED"
  fi
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
  if [ "$FULL" -eq 0 ]; then
    # New-files mode: a mismatch is recopied from the archive and checked again.
    if [ -n "$a" ] && put "$f" && [ "$(nas_sha "$f")" = "$a" ]; then
      echo "Repaired: $f (copy didn't match; recopied and re-checked)"; REPAIRED=$((REPAIRED+1))
    else
      echo "$line" >> "$PROBLEMS"
    fi
  elif [ -n "$a" ] && [ "$a" = "$n" ]; then
    EDITED=$((EDITED+1))
  elif [ -n "$a" ] && [ "$a" = "$(manifest_sha "$f")" ] && put "$f" && [ "$(nas_sha "$f")" = "$a" ]; then
    echo "Repaired: $f (backup copy was bad; recopied from the archive and re-checked)"; REPAIRED=$((REPAIRED+1))
  else
    echo "$line" >> "$PROBLEMS"
  fi
done < "$FAILED"
rm -f "$FAILED" "$COPIED"
BAD=$(wc -l < "$PROBLEMS" | tr -d ' ')

# Files on the drive that aren't in the manifest yet were copied but not hash-checked
UNLISTED=$(cd "$SRC" && find Targets _tools -type f ! -name '._*' ! -name '.DS_Store' ! -path '_tools/logs/*' ! -path '_tools/stack-queue.tsv' | \
  awk 'NR==FNR{ m[substr($0,67)]=1; next } !($0 in m)' MANIFEST.sha256 - | wc -l | tr -d ' ')

if [ "$BAD" -eq 0 ]; then
  if [ "$FULL" -eq 1 ]; then
    echo "Verified: all $TOTAL files in the manifest match the archive."
    [ "$EDITED" -eq 0 ] || echo "($EDITED of them were edited after the manifest was written, e.g. notes.md; the backup matches the archive's current version.)"
    [ "$UNLISTED" -eq 0 ] || echo "Note: $UNLISTED file(s) aren't in MANIFEST.sha256 (processing and final files); a full check covers manifest files, but each of those was verified when it was copied."
    mkdir -p "$(dirname "$MARK")" && date +%s > "$MARK"
  else
    echo "Verified: all $TOTAL file(s) copied in this run match the archive."
    last=$(cat "$MARK" 2>/dev/null || echo 0); age=$(( ($(date +%s) - last) / 86400 ))
    if [ "$last" -eq 0 ]; then echo "Tip: this destination hasn't had a full check yet. Run 'astro-backup --full ${DST_ARG}' when convenient."
    elif [ "$age" -ge 30 ]; then echo "Tip: the last full check of this destination was $age days ago. Run 'astro-backup --full ${DST_ARG}' when convenient."; fi
  fi
  [ "$REPAIRED" -eq 0 ] || echo "($REPAIRED file(s) were repaired from the archive; see 'Repaired:' lines above.)"
  rm -f "$PROBLEMS"
  echo "=== $(date) BACKUP COMPLETE"
else
  echo "PROBLEMS: $BAD file(s) failed verification:"; cat "$PROBLEMS"; rm -f "$PROBLEMS"
  echo "These could not be repaired automatically (the archive copy may itself be bad)."
  echo "=== $(date) BACKUP FINISHED WITH ERRORS"; exit 1
fi
