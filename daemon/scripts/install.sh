#!/bin/bash
# Installation script for helixd daemon

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${SCRIPT_DIR}/build"
MODE="${1:-install}"

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Error: Installation requires root privileges"
    echo "Please run: sudo ./scripts/install.sh"
    exit 1
fi

if [ "$MODE" = "uninstall" ]; then
    echo "=== Uninstalling helixd ==="

    if systemctl is-active --quiet helixd 2>/dev/null; then
        echo "Stopping helixd service..."
        systemctl stop helixd
    fi

    if systemctl is-enabled --quiet helixd 2>/dev/null; then
        echo "Disabling helixd service..."
        systemctl disable helixd
    fi

    echo "Removing systemd service file..."
    rm -f /etc/systemd/system/helixd.service

    echo "Removing installed binary..."
    rm -f /usr/local/bin/helixd

    echo "Reloading systemd daemon..."
    systemctl daemon-reload

    echo "=== Uninstall Complete ==="
    exit 0
fi

echo "=== Installing helixd ==="

# Check if built
if [ ! -f "$BUILD_DIR/helixd" ]; then
    echo "Error: helixd binary not found."
    echo "Run: ./scripts/build.sh"
    exit 1
fi

# Get the actual user who invoked sudo (not root)
INSTALL_USER="${SUDO_USER:-$USER}"
if [ "$INSTALL_USER" = "root" ]; then
    # Try to get the user from logname if SUDO_USER is not set
    INSTALL_USER=$(logname 2>/dev/null || echo "root")
fi

# Stop existing service if running
if systemctl is-active --quiet helixd 2>/dev/null; then
    echo "Stopping existing helixd service..."
    systemctl stop helixd
fi

# Install binary
echo "Installing binary to /usr/local/bin..."
install -m 0755 "$BUILD_DIR/helixd" /usr/local/bin/helixd

# Install systemd files
# Note: We only install the service file, not a socket file.
# The daemon manages its own socket to avoid conflicts with systemd socket activation.
echo "Installing systemd service files..."
install -m 0644 "$SCRIPT_DIR/systemd/helixd.service" /etc/systemd/system/

# Create config directory
echo "Creating configuration directory..."
mkdir -p /etc/helix
if [ ! -f /etc/helix/daemon.yaml ]; then
    # SCRIPT_DIR points to daemon/, so config is at daemon/config/
    install -m 0644 "$SCRIPT_DIR/config/helixd.yaml.example" /etc/helix/daemon.yaml
    echo "  Created default config: /etc/helix/daemon.yaml"
fi

# Create helix group for socket access
echo "Setting up helix group for socket access..."
if ! getent group helix >/dev/null 2>&1; then
    groupadd helix
    echo "  Created 'helix' group"
else
    echo "  Group 'helix' already exists"
fi

# Add the installing user to the helix group
if [ "$INSTALL_USER" != "root" ]; then
    if id -nG "$INSTALL_USER" | grep -qw helix; then
        echo "  User '$INSTALL_USER' is already in 'helix' group"
    else
        usermod -aG helix "$INSTALL_USER"
        echo "  Added user '$INSTALL_USER' to 'helix' group"
        GROUP_ADDED=1
    fi
fi

# Create state directories
echo "Creating state directories..."
mkdir -p /var/lib/helix
chown root:helix /var/lib/helix
chmod 0750 /var/lib/helix

mkdir -p /run/helix
chown root:helix /run/helix
chmod 0755 /run/helix

# Create user config directory for installing user
if [ "$INSTALL_USER" != "root" ]; then
    INSTALL_USER_HOME=$(getent passwd "$INSTALL_USER" | cut -d: -f6)
    if [ -n "$INSTALL_USER_HOME" ]; then
        mkdir -p "$INSTALL_USER_HOME/.helix"
        chown "$INSTALL_USER:$INSTALL_USER" "$INSTALL_USER_HOME/.helix"
        chmod 0700 "$INSTALL_USER_HOME/.helix"
    fi
fi

# Also create root's config directory
mkdir -p /root/.helix
chmod 0700 /root/.helix

# Reload systemd
echo "Reloading systemd daemon..."
systemctl daemon-reload

# Enable service
echo "Enabling helixd service..."
systemctl enable helixd

# Start service
echo "Starting helixd service..."
if systemctl start helixd; then
    echo ""
    echo "=== Installation Complete ==="
    echo ""
    systemctl status helixd --no-pager || true
    echo ""
    echo "Commands:"
    echo "  Status:   systemctl status helixd"
    echo "  Logs:     journalctl -u helixd -f"
    echo "  Stop:     systemctl stop helixd"
    echo "  Config:   /etc/helix/daemon.yaml"
    
else
    echo ""
    echo "=== Installation Complete (service failed to start) ==="
    echo ""
    echo "Troubleshooting:"
    echo "  1. Check logs: journalctl -xeu helixd -n 50"
    echo "  2. Verify binary: /usr/local/bin/helixd --version"
    echo "  3. Check config: cat /etc/helix/daemon.yaml"
    echo ""
    exit 1
fi
