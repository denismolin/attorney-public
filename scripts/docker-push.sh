#!/usr/bin/env bash
# Build + push de l'image app sur un registry (ex. Docker Hub).
#
#   IMAGE=<votre-namespace>/avocat-app ./scripts/docker-push.sh   # tag latest
#   IMAGE=... TAG=v1.0 ./scripts/docker-push.sh                   # tag versionné (+ latest)
#   IMAGE=... MULTIARCH=1 ./scripts/docker-push.sh                # build multi-arch amd64+arm64
#
# Nécessite : `docker login` (avec vos identifiants registry).
# Définissez IMAGE sur votre propre namespace.
set -euo pipefail

IMAGE="${IMAGE:-your-namespace/avocat-app}"
TAG="${TAG:-latest}"
cd "$(dirname "$0")/.."

echo "=== Build ${IMAGE}:${TAG} ==="

if [ "${MULTIARCH:-0}" = "1" ]; then
  docker buildx build --platform linux/amd64,linux/arm64 \
    -t "${IMAGE}:${TAG}" -t "${IMAGE}:latest" --push .
  echo "Image multi-arch poussée : ${IMAGE}:${TAG} (+ latest)"
  exit 0
fi

docker build -t "${IMAGE}:${TAG}" -t "${IMAGE}:latest" .

echo "=== Push ==="
docker push "${IMAGE}:${TAG}"
docker push "${IMAGE}:latest"
echo "✓ Poussé : ${IMAGE}:${TAG} (+ latest)"
