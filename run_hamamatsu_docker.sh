#!/usr/bin/env bash
# ===============================================
# Hamamatsu Detector Docker Build & Launch Script
# ===============================================
# This script builds the Docker image and launches
# the GUI with USB and X11 forwarding support.
#
# Usage:
#   ./run_hamamatsu_docker.sh
# ===============================================

set -e  # Exit on error

IMAGE_NAME="hamamatsu-controller"
CONTAINER_NAME="hamamatsu_gui"
DOCKERFILE="Dockerfile"

# ---- 1. Check for X11 access ----
echo "🔍 Checking X11 access permissions..."
if command -v xhost >/dev/null 2>&1; then
    xhost +local:docker >/dev/null
else
    echo "⚠️  'xhost' not found — X11 GUI may not display."
fi

# ---- 2. Build the image ----
echo "🐳 Building Docker image: $IMAGE_NAME"
docker build -t "$IMAGE_NAME" -f "$DOCKERFILE" .

# ---- 3. Remove old container if exists ----
if docker ps -a --format '{{.Names}}' | grep -Eq "^${CONTAINER_NAME}\$"; then
    echo "🧹 Removing old container: $CONTAINER_NAME"
    docker stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
fi

# ---- 4. Launch container ----
echo "🚀 Launching Hamamatsu Detector GUI..."
docker run -it \
  --device /dev/bus/usb:/dev/bus/usb \
  -e DISPLAY=${DISPLAY:-:0} \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  --name "$CONTAINER_NAME" \
  "$IMAGE_NAME"


# ---- 5. Cleanup (optional) ----
echo "🧼 Cleaning up after exit..."
docker rm "$CONTAINER_NAME" >/dev/null 2>&1 || true

echo "✅ Done. The Hamamatsu GUI container has exited."
