#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "$script_dir/.." && pwd)"

push_git=false
push_images=false
skip_tests=false
skip_build=false

usage() {
    cat <<'EOF'
Usage: ./scripts/release.sh [OPTIONS]

Build and tag a release using the version in pyproject.toml.

Options:
  --push          Push multi-platform images and the Git commit/tag.
  --push-git      Push the Git commit and tag only.
  --push-images   Build and push multi-platform Docker images only.
  --skip-tests    Skip the Python unit tests.
  --skip-build    Reuse local latest images (incompatible with --push-images).
  -h, --help      Show this help.

Environment:
  SOURCE_IMAGE_PREFIX     Local Compose image prefix.
                          Default: cumulus-ztp-server
  ZTP_RELEASE_IMAGE       ZTP release repository.
                          Default: ghcr.io/derrickhayashi/cumulus-ztp-server
  DNSMASQ_RELEASE_IMAGE   dnsmasq release repository.
                          Default: ghcr.io/derrickhayashi/cumulus-ztp-server-dnsmasq
  NGINX_RELEASE_IMAGE     nginx release repository.
                          Default: ghcr.io/derrickhayashi/cumulus-ztp-server-nginx
  RELEASE_PLATFORMS       Comma-separated release platforms.
                          Default: linux/amd64,linux/arm64
  RELEASE_GIT_REMOTE      Git remote used for pushes. Default: origin
  RELEASE_GIT_BRANCH      Git branch used for pushes. Default: main
EOF
}

while (($#)); do
    case "$1" in
        --push)
            push_git=true
            push_images=true
            ;;
        --push-git)
            push_git=true
            ;;
        --push-images)
            push_images=true
            ;;
        --skip-tests)
            skip_tests=true
            ;;
        --skip-build)
            skip_build=true
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

cd "$project_dir"

for command_name in git docker uv; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Error: required command is not available: $command_name" >&2
        exit 1
    fi
done

if ! docker compose version >/dev/null 2>&1; then
    echo "Error: Docker Compose is not available." >&2
    exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Error: commit or restore tracked changes before creating a release." >&2
    exit 1
fi

version="$(uv run python -c 'import pathlib, tomllib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
    echo "Error: invalid release version in pyproject.toml: $version" >&2
    exit 1
fi

git_tag="v$version"
head_commit="$(git rev-parse HEAD)"
tag_exists=false
if git rev-parse --verify --quiet "refs/tags/$git_tag" >/dev/null; then
    tag_commit="$(git rev-list -n 1 "$git_tag")"
    if [[ "$tag_commit" != "$head_commit" ]]; then
        echo "Error: $git_tag already points to a different commit." >&2
        exit 1
    fi
    tag_exists=true
fi

source_image_prefix="${SOURCE_IMAGE_PREFIX:-cumulus-ztp-server}"
ztp_release_image="${ZTP_RELEASE_IMAGE:-ghcr.io/derrickhayashi/cumulus-ztp-server}"
dnsmasq_release_image="${DNSMASQ_RELEASE_IMAGE:-ghcr.io/derrickhayashi/cumulus-ztp-server-dnsmasq}"
nginx_release_image="${NGINX_RELEASE_IMAGE:-ghcr.io/derrickhayashi/cumulus-ztp-server-nginx}"
release_platforms="${RELEASE_PLATFORMS:-linux/amd64,linux/arm64}"
git_remote="${RELEASE_GIT_REMOTE:-origin}"
git_branch="${RELEASE_GIT_BRANCH:-main}"

if [[ "$push_images" == true && "$skip_build" == true ]]; then
    echo "Error: --skip-build cannot be used with --push-images." >&2
    exit 1
fi

if [[ "$skip_tests" == false ]]; then
    uv run python -m unittest discover -s ztp/app -p 'test_*.py' -v
fi

docker compose config --quiet

services=(ztp nginx dnsmasq)
contexts=(./ztp ./nginx ./dnsmasq)
release_images=("$ztp_release_image" "$nginx_release_image" "$dnsmasq_release_image")

if [[ "$push_images" == true ]]; then
    if ! docker buildx version >/dev/null 2>&1; then
        echo "Error: Docker Buildx is required to publish release images." >&2
        exit 1
    fi

    for index in "${!services[@]}"; do
        release_image="${release_images[$index]}"
        docker buildx build \
            --platform "$release_platforms" \
            --tag "$release_image:$version" \
            --tag "$release_image:latest" \
            --label "org.opencontainers.image.source=https://github.com/derrickhayashi/cumulus-ztp-server" \
            --label "org.opencontainers.image.revision=$head_commit" \
            --label "org.opencontainers.image.version=$version" \
            --push \
            "${contexts[$index]}"
    done
else
    if [[ "$skip_build" == false ]]; then
        "$script_dir/build-images.sh"
    fi

    for service in "${services[@]}"; do
        source_image="$source_image_prefix-$service:latest"
        release_image="$source_image_prefix-$service:$version"
        docker image inspect "$source_image" >/dev/null
        docker tag "$source_image" "$release_image"
    done
fi

if [[ "$tag_exists" == false ]]; then
    git tag -a "$git_tag" -m "Release $git_tag"
fi

if [[ "$push_git" == true ]]; then
    git push "$git_remote" "HEAD:refs/heads/$git_branch" "refs/tags/$git_tag"
fi

echo "Release $git_tag completed."
echo "Git commit: $head_commit"
echo "Docker images:"
if [[ "$push_images" == true ]]; then
    for release_image in "${release_images[@]}"; do
        echo "  $release_image:$version"
        echo "  $release_image:latest"
    done
    echo "Platforms: $release_platforms"
else
    for service in "${services[@]}"; do
        echo "  $source_image_prefix-$service:$version"
    done
fi
