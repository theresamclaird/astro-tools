#!/bin/bash
# Install the astro-tools commands.
#
#   ./install.sh               install (or update) the commands into ~/.local/bin
#   ./install.sh --bin DIR     install into DIR instead
#   ./install.sh --uninstall   remove the commands this script installed
#
# Each command is a tiny wrapper that points back at this checkout, so after a
# `git pull` there is nothing to reinstall unless a new command was added.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
CONF_DIR="$HOME/.config/astro-tools"
MARK="# astro-tools wrapper"
UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --bin) BIN="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done

# command name | interpreter (py = the repo's Python, bash) | script path relative to the repo
COMMANDS="
astro-ingest|py|tools/ingest.py
astro-backup|bash|tools/backup-to-nas.sh
astro-stack|bash|tools/stack/astro-stack.sh
astro-nightly|bash|tools/nightly.sh
astro-frame-stats|py|tools/stack/frame_stats.py
astro-process|py|tools/stack/process.py
seestar-split-panels|py|tools/mosaic/seestar_split_panels.py
seestar-stack-panels|py|tools/mosaic/seestar_stack_panels.py
seestar-prep-panels|py|tools/mosaic/seestar_prep_panels.py
seestar-mosaic|py|tools/mosaic/seestar_mosaic.py
seestar-reject-frames|py|tools/mosaic/seestar_reject_frames.py
"

if [ "$UNINSTALL" -eq 1 ]; then
  echo "$COMMANDS" | while IFS='|' read -r name _ _; do
    [ -n "$name" ] || continue
    f="$BIN/$name"
    if [ -f "$f" ] && grep -q "$MARK" "$f"; then rm -f "$f"; echo "removed $f"; fi
  done
  echo "Left in place: $REPO/.venv and $CONF_DIR (delete them by hand if you like)."
  exit 0
fi

# ---- Python: a private venv with numpy (mosaic tools) and matplotlib (astro-frame-stats plots) ----
command -v python3 >/dev/null || { echo "python3 not found. On macOS: xcode-select --install; on Ubuntu: sudo apt install python3 python3-venv"; exit 1; }
PY="$REPO/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "Creating Python environment in $REPO/.venv"
  python3 -m venv "$REPO/.venv" || { echo "Could not create the venv. On Ubuntu: sudo apt install python3-venv"; exit 1; }
fi
if ! "$PY" -c 'import numpy, matplotlib' 2>/dev/null; then
  echo "Installing numpy and matplotlib into the environment"
  "$PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  if ! "$PY" -m pip install --quiet -r "$REPO/requirements.txt"; then
    echo "WARNING: could not install numpy/matplotlib. astro-ingest and astro-backup will work;"
    echo "         the seestar-* mosaic tools need numpy; astro-frame-stats needs matplotlib for its plots. Re-run ./install.sh when online."
  fi
fi

# ---- config ----
mkdir -p "$CONF_DIR"
if [ ! -f "$CONF_DIR/config" ]; then
  cp "$REPO/config.example" "$CONF_DIR/config"
  echo "Created $CONF_DIR/config (edit it to change the archive or NAS paths)"
fi

# ---- wrappers ----
mkdir -p "$BIN"
echo "$COMMANDS" | while IFS='|' read -r name kind path; do
  [ -n "$name" ] || continue
  f="$BIN/$name"
  if [ -e "$f" ] && ! grep -q "$MARK" "$f" 2>/dev/null; then
    echo "SKIPPED $f: a different file with that name already exists"; continue
  fi
  case "$kind" in
    py)   run="exec \"$PY\" \"$REPO/$path\" \"\$@\"" ;;
    bash) run="exec /bin/bash \"$REPO/$path\" \"\$@\"" ;;
  esac
  cat > "$f" <<EOF
#!/bin/bash
$MARK -> $REPO/$path
[ -f "$CONF_DIR/config" ] && { set -a; . "$CONF_DIR/config"; set +a; }
$run
EOF
  chmod +x "$f"
  echo "installed $name"
done

# ---- checks ----
SIRIL=""
for c in "$(command -v siril-cli 2>/dev/null || true)" /Applications/Siril.app/Contents/MacOS/siril-cli \
         /opt/homebrew/bin/siril-cli /usr/local/bin/siril-cli; do
  [ -n "$c" ] && [ -x "$c" ] && { SIRIL="$c"; break; }
done
[ -z "$SIRIL" ] && command -v flatpak >/dev/null && flatpak info org.siril.Siril >/dev/null 2>&1 && SIRIL="Flatpak org.siril.Siril"
echo
[ -n "$SIRIL" ] && echo "Siril: $SIRIL" || echo "Siril not found (needed by astro-stack and the seestar-* mosaic tools): https://siril.org"
case ":$PATH:" in
  *":$BIN:"*) echo "Done. Try: astro-ingest --help" ;;
  *) echo "Done, but $BIN is not on your PATH. Add this to ~/.zshrc and open a new terminal:"
     echo "    export PATH=\"$BIN:\$PATH\"" ;;
esac
