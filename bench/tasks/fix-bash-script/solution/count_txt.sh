#!/usr/bin/env bash
set -euo pipefail
dir="$1"
find "$dir" -maxdepth 1 -type f -name '*.txt' | wc -l | tr -d ' '
