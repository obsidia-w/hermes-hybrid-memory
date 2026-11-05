#!/usr/bin/env bash
set -euo pipefail
ROOT='/home/hermes/Obsidian/Macidian/hermes저장소/엘리자베스/로컬 메모리 트리'
SCRIPT="$ROOT/_system/memory_index.py"
LOG="$ROOT/_system/last-index.log"
cd "$ROOT/_system"
python3 "$SCRIPT" index > "$LOG.tmp"
mv "$LOG.tmp" "$LOG"
# Stay quiet on success so cron does not send routine noise.
