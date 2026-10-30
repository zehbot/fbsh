#!/usr/bin/env bash
#
# build-image.sh - build the hase10 image for a Raspberry Pi (and amd64).
#
#   ./scripts/build-image.sh                    # all platforms, local only
#   ./scripts/build-image.sh --load             # single platform into the local store
#   PLATFORMS=linux/arm64 ./scripts/build-image.sh --load
#   PUSH=1 ./scripts/build-image.sh             # build and push a manifest list
#
# Native build on the Pi: PLATFORMS=linux/arm64 ./build-image.sh --load
# Multi-arch on a workstation needs buildx plus qemu binfmt
#   (docker run --privileged --rm tonistiigi/binfmt --install arm64,arm).

set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-fbsh}"
TAG="${TAG:-0.1.0}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64,linux/arm/v7}"
PUSH="${PUSH:-0}"
LOAD=0

for arg in "$@"; do
  case "$arg" in
    --push)   PUSH=1 ;;
    --load)   LOAD=1 ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if docker info >/dev/null 2>&1; then
  d() { docker "$@"; }
elif sg docker -c 'docker info' >/dev/null 2>&1; then
  d() { sg docker -c "docker $(printf '%q ' "$@")"; }
else
  echo "error: docker not reachable (not running, or no permission)" >&2
  exit 3
fi

if ! d buildx version >/dev/null 2>&1; then
  echo "error: docker buildx is not available" >&2
  exit 3
fi

args=(buildx build --platform "$PLATFORMS" --provenance=false
      -t "$IMAGE:$TAG" -t "$IMAGE:latest")
[ "$PUSH" = "1" ] && args+=(--push)
[ "$LOAD" = "1" ] && args+=(--load)
args+=(.)

echo "image:     $IMAGE:$TAG"
echo "platforms: $PLATFORMS"
d "${args[@]}"

echo
echo "run it with:"
echo "  docker run --rm --env-file private/.env $IMAGE:$TAG devices"
