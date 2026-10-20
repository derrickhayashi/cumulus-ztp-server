#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "$script_dir/.." && pwd)"

if ! docker compose version >/dev/null 2>&1; then
    echo "Error: Docker Compose is not available." >&2
    exit 1
fi

cd "$project_dir"
exec docker compose -f compose.build.yaml build "$@"
