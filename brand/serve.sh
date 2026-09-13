#!/usr/bin/env bash
# Serve the chosen brand page on 7101 and every exploration on 7111+. Ctrl-C stops all.
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
pids=()
python3 -m http.server 7101 --bind 127.0.0.1 --directory "$root/polyphony" >/dev/null 2>&1 & pids+=($!)
echo "http://localhost:7101  ->  polyphony (the chosen direction)"
port=7111
for d in "$root"/explorations/*/; do
  python3 -m http.server "$port" --bind 127.0.0.1 --directory "$d" >/dev/null 2>&1 & pids+=($!)
  echo "http://localhost:$port  ->  explorations/$(basename "$d")"
  port=$((port+1))
done
trap 'kill "${pids[@]}" 2>/dev/null' EXIT INT TERM
wait
