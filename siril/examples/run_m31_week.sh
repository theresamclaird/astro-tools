#!/bin/zsh
# Runs the full-week M31 stack. Keep the T9 plugged in; caffeinate keeps the Mac awake.
RAW="/Volumes/T9/Astrophotography/Seestar S50 Pro/MyWorks/M 31_mosaic_sub"
WORK="/Volumes/T9/Astrophotography/Processing/M31_2026-10"
SIRIL="/Applications/Siril.app/Contents/MacOS/siril-cli"

ulimit -n 10240 2>/dev/null   # stacking opens every registered frame at once
cd "$WORK" || exit 1
echo "Free space on T9:"; df -h /Volumes/T9 | tail -1
caffeinate -i "$SIRIL" -d "$RAW" -s "$WORK/m31_week.ssf" 2>&1 | tee "$WORK/run_$(date +%Y%m%d-%H%M).log"
