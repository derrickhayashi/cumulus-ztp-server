#!/usr/bin/env bash
set -Eeuo pipefail

build=false
while (($#)); do
    case "$1" in
        --build) build=true ;;
        -h|--help)
            echo "Usage: ./scripts/deploy.sh [--build]"
            echo "Pull GHCR images and deploy; use --build to build from local source."
            exit 0
            ;;
        *) echo "Error: unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "$script_dir/.." && pwd)"
cd "$project_dir"

if [[ "$build" == true ]]; then
    docker compose -f compose.build.yaml config --quiet
    docker compose -f compose.build.yaml run --build --rm fetch-cumulus-os
    docker compose -f compose.build.yaml up -d --build --wait
else
    docker compose -f compose.yaml config --quiet
    docker compose -f compose.yaml --profile tools pull
    docker compose -f compose.yaml run --pull never --rm fetch-cumulus-os
    docker compose -f compose.yaml up -d --no-build --pull never --wait
fi
