#!/bin/bash
# SSH setup script for environments without internet access (e.g., H100/H200 clusters)
# Uses pre-cached dropbear deb packages and rtunnel binary.
#
# Usage: setup_ssh_dropbear.sh <dropbear_deb_dir> <rtunnel_bin> <ssh_port> <rtunnel_port>
#
# Environment variables used:
#   INSPIRE_DROPBEAR_DEB_DIR - Directory containing dropbear*.deb files
#   INSPIRE_RTUNNEL_BIN - Path to rtunnel binary (optional, uses arg if not set)
#
# To use this script:
# 1. Copy it to a shared filesystem accessible from the cluster
# 2. Set INSPIRE_SETUP_SCRIPT to the script's path on the cluster
# 3. Set INSPIRE_DROPBEAR_DEB_DIR to the directory with dropbear debs
# 4. Optionally set INSPIRE_RTUNNEL_BIN to a cached rtunnel binary

set -e

DROPBEAR_DEB_DIR="${1:-${INSPIRE_DROPBEAR_DEB_DIR}}"
RTUNNEL_BIN="${2:-${INSPIRE_RTUNNEL_BIN:-/tmp/rtunnel}}"
SSH_PORT="${3:-22222}"
RTUNNEL_PORT="${4:-31337}"

echo "=== SSH Setup Script ==="
echo "Dropbear deb dir: $DROPBEAR_DEB_DIR"
echo "Rtunnel bin: $RTUNNEL_BIN"
echo "SSH port: $SSH_PORT"
echo "Rtunnel port: $RTUNNEL_PORT"

# Install dropbear from local debs
if [ -d "$DROPBEAR_DEB_DIR" ]; then
    echo ">>> Installing dropbear from local debs..."
    dpkg -i "$DROPBEAR_DEB_DIR"/*.deb 2>/dev/null || true
else
    echo ">>> Warning: Dropbear deb dir not found: $DROPBEAR_DEB_DIR"
    echo ">>> Attempting to install from apt..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq dropbear || true
fi

# Verify dropbear is installed
if ! command -v dropbear &>/dev/null; then
    echo ">>> ERROR: dropbear not found after installation"
    exit 1
fi

# Generate host keys if they don't exist
mkdir -p /etc/dropbear
if [ ! -f /etc/dropbear/dropbear_rsa_host_key ]; then
    echo ">>> Generating RSA host key..."
    dropbearkey -t rsa -f /etc/dropbear/dropbear_rsa_host_key
fi
if [ ! -f /etc/dropbear/dropbear_ed25519_host_key ]; then
    echo ">>> Generating ED25519 host key..."
    dropbearkey -t ed25519 -f /etc/dropbear/dropbear_ed25519_host_key 2>/dev/null || true
fi

# Kill any existing dropbear on our port
pkill -f "dropbear.*-p $SSH_PORT" 2>/dev/null || true
sleep 1

# Start dropbear
echo ">>> Starting dropbear on port $SSH_PORT..."
# -F: do not daemonize (so $! is the actual dropbear PID)
dropbear -R -E -F -p "$SSH_PORT" >/tmp/dropbear.log 2>&1 &
DROPBEAR_PID=$!
sleep 2

if ! kill -0 $DROPBEAR_PID 2>/dev/null; then
    echo ">>> ERROR: dropbear failed to start"
    cat /tmp/dropbear.log
    exit 1
fi
echo ">>> Dropbear started (PID: $DROPBEAR_PID)"

# Copy rtunnel if needed
if [ -f "$RTUNNEL_BIN" ] && [ "$RTUNNEL_BIN" != "/tmp/rtunnel" ]; then
    cp "$RTUNNEL_BIN" /tmp/rtunnel
    chmod +x /tmp/rtunnel
fi
RTUNNEL_BIN="/tmp/rtunnel"

if [ ! -x "$RTUNNEL_BIN" ]; then
    echo ">>> ERROR: rtunnel binary not found or not executable: $RTUNNEL_BIN"
    exit 1
fi

# Kill any existing rtunnel
pkill -f "rtunnel.*:$RTUNNEL_PORT" 2>/dev/null || true
sleep 1

# Start rtunnel server (forward websocket connections on RTUNNEL_PORT to dropbear on SSH_PORT)
echo ">>> Starting rtunnel: 127.0.0.1:$SSH_PORT -> 0.0.0.0:$RTUNNEL_PORT..."
nohup "$RTUNNEL_BIN" "127.0.0.1:$SSH_PORT" "0.0.0.0:$RTUNNEL_PORT" >/tmp/rtunnel-server.log 2>&1 &
RTUNNEL_PID=$!
sleep 2

if ! kill -0 $RTUNNEL_PID 2>/dev/null; then
    echo ">>> ERROR: rtunnel failed to start"
    cat /tmp/rtunnel-server.log
    exit 1
fi
echo ">>> Rtunnel started (PID: $RTUNNEL_PID)"

echo "=== SSH Setup Complete ==="
echo "SSH: localhost:$SSH_PORT"
echo "Rtunnel: localhost:$RTUNNEL_PORT"
