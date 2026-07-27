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
# Probed independently of `docker` itself (Phase N8 audit finding L3): the CLI can be
# present (e.g. from a distro package) with no `compose` subcommand at all, in which
# case `command -v docker` alone would wrongly skip the get.docker.com install below
# and `docker compose up -d` would fail on the very last line of this script's own
# instructions.
if ! command -v docker &>/dev/null || ! docker compose version &>/dev/null; then
    # Official convenience script (get.docker.com) installs Docker Engine, CLI,
    # containerd, and the compose plugin (`docker compose`, not standalone
    # docker-compose) in one pass - the same install path Docker's own docs recommend
    # for a fresh server.
    curl -fsSL https://get.docker.com | sh
else
    echo "docker + compose already installed ($(docker --version)); skipping."
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

echo "== ufw: allow only the configured SSH port =="
if ! command -v ufw &>/dev/null; then
    apt-get update -qq && apt-get install -y -qq ufw
fi
# Detect the actual configured sshd port (Phase N8 audit finding M3) rather than
# hardcoding 22 - a box with sshd already moved to a non-standard port (common
# hardening, and exactly the kind of VPS this script gets run on manually after the
# fact) would otherwise have this script firewall off the very session running it.
# `sshd -T` (dump effective config) is preferred; grep of sshd_config is the fallback
# for when `sshd -T` itself isn't runnable. Falls back to 22 if both come up empty.
SSH_PORT="$(sshd -T 2>/dev/null | awk '$1=="port"{print $2; exit}')"
if [[ -z "$SSH_PORT" ]]; then
    SSH_PORT="$(grep -iE '^[[:space:]]*Port[[:space:]]+[0-9]+' /etc/ssh/sshd_config 2>/dev/null \
        | awk '{print $2; exit}')"
fi
SSH_PORT="${SSH_PORT:-22}"
echo "Detected SSH port: $SSH_PORT"
# Set the policy and the one allow rule before enabling - never enable first, which
# would (briefly, but really) lock out anything not yet permitted, including this
# SSH session itself if it raced the reset below.
ufw default deny incoming
ufw default allow outgoing
ufw allow "$SSH_PORT/tcp" comment 'SSH'
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
