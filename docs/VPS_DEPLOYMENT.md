# VPS Deployment — Phase N8

Step-by-step runbook for deploying `wit-nautilus` (paper trading) on a fresh Ubuntu/Debian
VPS with systemd. Assumes you're starting as `root` with no non-root user yet — adjust step 1
if a deploy user already exists.

Replace `deploy` below with whatever you actually name the account (see step 1) — do **not**
type literal `<angle-brackets>` in any command; that's shell redirection syntax, not a
placeholder convention bash understands.

---

## 1. Create a non-root deploy user (skip if one already exists)

```bash
adduser --disabled-password --gecos "" deploy
```

**Don't assume root already has a key to copy** — many VPS providers hand out root access via
a password, not a key, in which case `cp ~/.ssh/authorized_keys ...` copies nothing and
`deploy` silently ends up with no way in but a password it doesn't have (this is exactly what
happened on first deploy: `ssh deploy@<ip>` prompted for a password that was never set).
Generate a dedicated keypair for `deploy` instead:

**On your local machine (Windows/PowerShell):**
```powershell
ssh-keygen -t ed25519 -C deploy-wit-nautilus -f $env:USERPROFILE\.ssh\wit_deploy
type $env:USERPROFILE\.ssh\wit_deploy.pub
```
Copy the printed public-key line (starts with `ssh-ed25519 AAAA...`).

**Back on the VPS, as root:**
```bash
mkdir -p /home/deploy/.ssh
echo "PASTE_THE_PUBLIC_KEY_LINE_HERE" >> /home/deploy/.ssh/authorized_keys
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys
```

(If root's own `~/.ssh/authorized_keys` genuinely does have real content — check with
`cat /root/.ssh/authorized_keys` before assuming — copying it instead of generating a new key
is fine too. Verify either way before moving on.)

**From your local machine**, verify the new user's SSH access works before continuing — step 3
locks the firewall down to SSH-only:

```powershell
ssh -i $env:USERPROFILE\.ssh\wit_deploy deploy@<your-vps-ip>
```
Should log in with no password prompt.

## 2. Get the repo onto the VPS

```bash
git clone https://github.com/inno-wit/wit-nautilus
cd wit-nautilus
```

## 3. Run the one-time bootstrap (as root)

```bash
sudo ./docker/vps-setup.sh deploy
```

Installs Docker Engine + the Compose plugin, adds `deploy` to the `docker` group, and locks
`ufw` down to inbound SSH only (auto-detects the real configured sshd port; falls back to 22).
Idempotent — safe to re-run.

At the end it prints something like:

```
echo 'FUND_UID=1000' >> .env   # so the fund container can write data/ (uid of 'deploy')
```

**Note the exact `FUND_UID=` value it prints** — needed in step 5.

Log out and back in as `deploy` (or run `newgrp docker`) so the group membership takes effect.

## 4. Pull the repo as `deploy`, if you cloned as root

```bash
# as deploy
cd ~
git clone https://github.com/inno-wit/wit-nautilus
cd wit-nautilus
```

(If you already cloned it under `deploy`'s home directory in step 2, skip this.)

## 5. Configure secrets — two separate files, deliberately not shared

**`.env`** (repo root — `wit`'s own secrets):

```bash
cp .env.example .env
```

Fill in:
- `ANTHROPIC_API_KEY`
- `WIT_DEEP_MODEL` / `WIT_QUICK_MODEL`
- `TWS_ACCOUNT` — your paper account id, starts with `DU`
- `WIT_PAPER_ONLY=true` — leave this alone, it's a hard lock
- `FUND_UID=<value vps-setup.sh printed in step 3>` — append the line it gave you

Leave `IBG_HOST`/`IBG_PORT` at their local-dev defaults; `docker/compose.yml` overrides them to
`ib-gateway`/`4004` internally for the containerized run.

**`docker/ib-gateway.env`** (the third-party gateway container's own IBKR login — a separate
file/blast radius on purpose):

```bash
cp docker/ib-gateway.env.example docker/ib-gateway.env
```

Fill in `TWS_USERID` / `TWS_PASSWORD`. Leave `VNC_SERVER_PASSWORD` commented out except during
step 7.

Both files are gitignored — never commit them.

## 6. (Recommended) Pin the ib-gateway image by digest

`docker/compose.yml` uses the `:stable` floating tag on purpose — pinning is left as a manual
step since no one outside the VPS can verify what digest is current. Before your first real run:

```bash
docker compose -f docker/compose.yml pull ib-gateway
docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/gnzsnz/ib-gateway:stable
```

Replace the `image:` line in `docker/compose.yml` with the printed `@sha256:...` digest.

## 7. Bring it up

```bash
docker compose -f docker/compose.yml up -d
```

Builds the `fund` image, pulls `ib-gateway`, starts `ib-gateway` first — `fund` waits on its
healthcheck (cold start can take up to ~2 minutes).

## 8. One-time paper login / 2FA over VNC

The gateway needs an interactive login the first time (and after any forced re-auth):

```bash
# uncomment VNC_SERVER_PASSWORD in docker/ib-gateway.env, then:
docker compose -f docker/compose.yml up -d ib-gateway

# from your LOCAL machine, tunnel — never expose 5900 publicly:
ssh -L 5900:127.0.0.1:5900 deploy@<vps-ip>
```

Point a VNC client at `localhost:5900`, complete the paper-account login + 2FA. Once in,
comment `VNC_SERVER_PASSWORD` back out in `docker/ib-gateway.env` and restart `ib-gateway`
again to close that door:

```bash
docker compose -f docker/compose.yml up -d ib-gateway
```

## 9. Verify

```bash
docker compose -f docker/compose.yml logs -f fund
docker compose -f docker/compose.yml ps                     # both services should show "healthy"
docker compose -f docker/compose.yml exec fund wit status
docker compose -f docker/compose.yml exec fund wit healthcheck
```

## 10. Nightly backups (cron, as `deploy`, not root)

```bash
crontab -e
```
Add:
```
0 3 * * * /path/to/wit-nautilus/docker/backup-data.sh >> /path/to/wit-nautilus/data/.backup.log 2>&1
```
30-day retention by default; tarballs `data/` (journal, dream state, kill switch) to
`../backups`.

## 11. Day-to-day operations

```bash
docker compose -f docker/compose.yml logs -f fund                          # tail logs
docker compose -f docker/compose.yml exec fund wit halt --reason "..."     # engage kill switch
docker compose -f docker/compose.yml exec fund wit resume                  # release it
docker compose -f docker/compose.yml down                                  # stop (data/ survives)
docker compose -f docker/compose.yml up -d --build                         # redeploy after a code change
```

---

## Before you actually let this run unattended

This deployment plumbing is tested and independently audited (two rounds — see
`docs/BUILD_PLAN.md`'s Phase N8 section for the full findings/fixes record), but nobody has run
`docker compose up` against a live IB Gateway from this repo yet. Phase N9 (the staged
validation gate — backtest → 48h data soak → 24h dry run → first watched paper order →
extended soak) is what actually proves this works end-to-end, and it needs you attended for
the first several runs, not just deployed and left alone.
