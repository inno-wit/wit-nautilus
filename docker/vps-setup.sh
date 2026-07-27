#!/usr/bin/env bash
# One-time VPS bootstrap for Phase N8 (docs/BUILD_PLAN.md's Phase N8 section):
# Docker Engine + Compose plugin, ufw allowing only 22, a non-root user in the docker
# group. Assumes SSH key-only login is already configured (out of scope here) and this
# is a fresh Ubuntu/Debian VPS with systemd. Idempotent - safe to re-run.
#
# Usage: sudo ./vps-setup.sh <deploy-user>
#   <deploy-user> must already exist (the SSH-key-login account) - this script does not
#   create it, only adds it to the docker group and its own environment stays yours to
#   own; running unattended service management as an account this script also creates
#   would be one more credential this script would need to hand you back safely.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run as root (sudo ./vps-setup.sh <deploy-user>)." >&2
    exit 1
fi

DEPLOY_USER="${1:-}"
if [[ -z "$DEPLOY_USER" ]]; then
    echo "Usage: sudo ./vps-setup.sh <deploy-user>" >&2
    exit 1
fi
if ! id "$DEPLOY_USER" &>/dev/null; then
    echo "User '$DEPLOY_USER' does not exist - create it (with SSH key auth already" >&2
    echo "configured) before running this script." >&2
    exit 1
fi

echo "== Docker Engine + Compose plugin =="
if ! command -v docker &>/dev/null; then
    # Official convenience script (get.docker.com) installs Docker Engine, CLI,
    # containerd, and the compose plugin (`docker compose`, not standalone
    # docker-compose) in one pass - the same install path Docker's own docs recommend
    # for a fresh server.
    curl -fsSL https://get.docker.com | sh
else
    echo "docker already installed ($(docker --version)); skipping."
fi
systemctl enable --now docker

echo "== Non-root docker access for '$DEPLOY_USER' =="
if ! getent group docker &>/dev/null; then
    groupadd docker
fi
if id -nG "$DEPLOY_USER" | grep -qw docker; then
    echo "'$DEPLOY_USER' is already in the docker group; skipping."
else
    usermod -aG docker "$DEPLOY_USER"
    echo "Added '$DEPLOY_USER' to the docker group - they must log out and back in" \
         "(or run 'newgrp docker') for it to take effect in their current session."
fi

echo "== ufw: allow only 22 =="
if ! command -v ufw &>/dev/null; then
    apt-get update -qq && apt-get install -y -qq ufw
fi
# Set the policy and the one allow rule before enabling - never enable first, which
# would (briefly, but really) lock out anything not yet permitted, including this
# SSH session itself if it raced the reset below.
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
# --force skips the interactive "this may disrupt existing connections" prompt - safe
# here specifically because 22/tcp was just allowed above, in this same invocation,
# before enable runs.
ufw --force enable
ufw status verbose

echo
echo "Done. Log out and back in as '$DEPLOY_USER' (or run 'newgrp docker'), then:"
echo "  cd /path/to/wit-nautilus"
echo "  cp .env.example .env   # fill in real values - see docker/compose.yml's own comments"
echo "  docker compose -f docker/compose.yml up -d"
