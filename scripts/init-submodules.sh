#!/usr/bin/env bash
# Add and initialize git submodules for the web client and opening data.
# Idempotent: safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."

add_if_missing() {
  local url=$1 path=$2
  if [ ! -d "$path/.git" ] && [ ! -f "$path/.git" ]; then
    git submodule add "$url" "$path"
  fi
}

add_if_missing https://github.com/shaack/cm-chessboard.git web/vendor/cm-chessboard
add_if_missing https://github.com/lichess-org/chess-openings.git web/vendor/chess-openings

git submodule update --init --recursive
