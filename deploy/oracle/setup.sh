#!/usr/bin/env bash
#
# AutoTube AI - provision an Oracle Cloud Always Free instance.
#
#   curl -fsSL <raw url>/deploy/oracle/setup.sh | bash -s -- --domain your.duckdns.org
#
# or, after cloning the repo:
#
#   sudo bash deploy/oracle/setup.sh --domain your.duckdns.org
#
# Idempotent: safe to re-run. Designed for Ubuntu 22.04/24.04 on
# VM.Standard.A1.Flex (Ampere, aarch64), which is the shape with enough CPU in
# the Always Free tier to actually render video.
#
# What it does NOT do: create your Oracle account, open the VCN Security List
# ingress rules, or register your DNS name. Those are console/web steps and are
# listed in deploy/oracle/README.md.
set -euo pipefail

DOMAIN=""
REPO_URL="https://github.com/ck17nov/auto_vid_app.git"
APP_USER="autotube"
APP_DIR="/opt/autotube"
PORT="8099"
SKIP_TLS="no"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)   DOMAIN="$2"; shift 2 ;;
    --repo)     REPO_URL="$2"; shift 2 ;;
    --no-tls)   SKIP_TLS="yes"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "run with sudo" >&2
  exit 1
fi

say() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*"; }

say "Architecture: $(uname -m)   Cores: $(nproc)   RAM: $(free -h | awk '/^Mem:/{print $2}')"
CORES="$(nproc)"
if [[ "$CORES" -lt 2 ]]; then
  warn "Only $CORES core(s) - almost certainly an AMD E2.1.Micro shape."
  warn "This cannot render video usefully. Use VM.Standard.A1.Flex instead."
elif [[ "$CORES" -le 2 ]]; then
  # Oracle halved the Always Free Ampere allowance to 2 OCPU / 12 GB on
  # 2026-06-15, so 2 cores is now the expected maximum, not a mistake.
  warn "$CORES cores: expect roughly 15-25 min for a 45s Short and 2-3 hours"
  warn "for a 4-minute long-form video. Shorts are fine; long-form is overnight."
fi

# --------------------------------------------------------------------------
say "Installing packages"
# ffmpeg from apt is a full build WITH libass on both Ubuntu 22.04 and 24.04,
# which is what the caption burn-in needs. check_ffmpeg.py verifies it below
# rather than trusting that.
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-pip \
  ffmpeg fonts-dejavu-core \
  git curl ca-certificates \
  debian-keyring debian-archive-keyring apt-transport-https

# --------------------------------------------------------------------------
say "Opening the host firewall"
# THE classic Oracle gotcha: their Ubuntu images ship iptables rules that
# REJECT everything except SSH, on top of the cloud Security List. Opening
# ingress in the console alone is not enough and gives you a silent timeout
# with no log line anywhere. Both layers must be opened.
iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null \
  || iptables -I INPUT 6 -p tcp --dport 80 -j ACCEPT
iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null \
  || iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT
if command -v netfilter-persistent >/dev/null 2>&1; then
  netfilter-persistent save >/dev/null
  echo "iptables rules saved (survive reboot)"
else
  warn "netfilter-persistent missing; rules will NOT survive a reboot"
fi

# --------------------------------------------------------------------------
say "Creating the service account"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home \
  --home-dir "/home/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"

# --------------------------------------------------------------------------
say "Fetching the application"
# This script runs as root under sudo, so root's ~/.ssh is /root/.ssh - but the
# deploy key was generated in the LOGIN user's home. Without this, an SSH clone
# fails with "Permission denied (publickey)" even though the key exists and
# works when you test it by hand, which is a genuinely confusing way to lose an
# hour. Point git at the invoking user's key explicitly.
if [[ "$REPO_URL" == git@* || "$REPO_URL" == ssh://* ]]; then
  KEY_OWNER_HOME="$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)"
  for candidate in "$KEY_OWNER_HOME/.ssh/autotube" "$KEY_OWNER_HOME/.ssh/id_ed25519" \
                   "$KEY_OWNER_HOME/.ssh/id_rsa"; do
    if [[ -f "$candidate" ]]; then
      export GIT_SSH_COMMAND="ssh -i $candidate -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
      echo "using SSH key $candidate"
      break
    fi
  done
  if [[ -z "${GIT_SSH_COMMAND:-}" ]]; then
    warn "No SSH key found under $KEY_OWNER_HOME/.ssh - the clone will likely fail."
    warn "See 'Give the server read access to the repo' in deploy/oracle/README.md"
  fi
fi
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch --quiet origin
  git -C "$APP_DIR" reset --hard --quiet origin/main
else
  mkdir -p "$APP_DIR"
  # The repository is PRIVATE by default, so an anonymous HTTPS clone fails
  # here with an authentication prompt that has no terminal to read from.
  # GIT_TERMINAL_PROMPT=0 turns that hang into an immediate, explainable error.
  if ! GIT_TERMINAL_PROMPT=0 git clone --quiet "$REPO_URL" "$APP_DIR" 2>/tmp/clone.err; then
    echo
    warn "Could not clone $REPO_URL"
    sed 's/^/    /' /tmp/clone.err >&2
    cat <<'CLONEHELP' >&2

  If the repository is private (it is, unless you changed it), pick one:

  1. Deploy key - read-only, scoped to this one repo, and the tidiest option:

       ssh-keygen -t ed25519 -C "oracle-autotube" -f ~/.ssh/autotube -N ""
       cat ~/.ssh/autotube.pub

     Add that as a DEPLOY KEY on GitHub:
       Settings -> Deploy keys -> Add deploy key  (leave write access OFF)

     Then re-run with the SSH URL:
       sudo bash setup.sh --repo git@github.com:ck17nov/auto_vid_app.git --domain ...

  2. Personal access token - quicker, but the token ends up in the git remote
     on disk. Use a fine-grained token limited to this repository, read-only:

       sudo bash setup.sh --repo https://<TOKEN>@github.com/ck17nov/auto_vid_app.git ...

  3. Make the repository public, then no credentials are needed at all.
     Rotate AUTOTUBE_API_TOKEN first if you do.

CLONEHELP
    exit 1
  fi
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

say "Creating the virtualenv"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# --------------------------------------------------------------------------
say "Checking ffmpeg really has what the renderer needs"
# `ffmpeg -version` succeeding proves nothing: a build without libass lists
# `subtitles` and then produces videos with no captions, and it fails at the
# LAST step after the voice and images are already rendered.
if ! sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/check_ffmpeg.py"; then
  echo
  warn "ffmpeg is missing something required. Fix that before going further."
  exit 1
fi

# --------------------------------------------------------------------------
say "Setting up .env"
ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  install -o "$APP_USER" -g "$APP_USER" -m 600 /dev/null "$ENV_FILE"
  {
    echo "# Filled in by deploy/oracle/setup.sh - add your keys below."
    echo "DRY_RUN=true"
    echo "AUTOTUBE_API_TOKEN=$(head -c 32 /dev/urandom | base64 | tr -d '=+/' | cut -c1-43)"
    echo "GEMINI_API_KEY="
    echo "GROQ_API_KEY="
    echo "YOUTUBE_API_KEY="
    echo "YOUTUBE_CLIENT_ID="
    echo "YOUTUBE_CLIENT_SECRET="
    echo "PEXELS_API_KEY="
    echo "PIXABAY_API_KEY="
  } > "$ENV_FILE"
  chown "$APP_USER:$APP_USER" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "created $ENV_FILE with a fresh AUTOTUBE_API_TOKEN"
else
  echo "$ENV_FILE already exists - left untouched"
fi

# --------------------------------------------------------------------------
say "Tuning render concurrency for $CORES cores"
# render_parallel: 0 makes the engine derive this at runtime (cores - 1), which
# is already right. Pinning it in the deployed config makes the value visible
# to whoever looks at this box later instead of implicit.
# The heredoc delimiter is QUOTED and the values arrive through the
# environment. An unquoted heredoc would interpolate, and the regex ends in
# `.*$"` - where bash `$"..."` is locale-translation syntax. Not worth betting
# a first-run deploy on how that parses.
export AUTOTUBE_CORES="$CORES"
export AUTOTUBE_CFG="$APP_DIR/config.yaml"
sudo -u "$APP_USER" --preserve-env=AUTOTUBE_CORES,AUTOTUBE_CFG \
  "$APP_DIR/.venv/bin/python" - <<'PYTUNE'
import os
import pathlib
import re

cores = max(1, int(os.environ["AUTOTUBE_CORES"]) - 1)
path = pathlib.Path(os.environ["AUTOTUBE_CFG"])
text = path.read_text(encoding="utf-8")
text, n = re.subn(r"^(\s*)render_parallel: .*$",
                  r"\g<1>render_parallel: " + str(cores),
                  text, count=1, flags=re.M)
if n:
    path.write_text(text, encoding="utf-8")
    print("  render_parallel =", cores)
else:
    print("  render_parallel key not found; leaving config alone")
PYTUNE

# --------------------------------------------------------------------------
say "Installing the systemd service"
sed -e "s|@APP_DIR@|$APP_DIR|g" \
    -e "s|@APP_USER@|$APP_USER|g" \
    -e "s|@PORT@|$PORT|g" \
    "$APP_DIR/deploy/oracle/autotube.service" > /etc/systemd/system/autotube.service
systemctl daemon-reload
systemctl enable --now autotube.service
sleep 4
systemctl is-active --quiet autotube.service \
  && echo "autotube.service is running" \
  || { warn "service failed to start:"; journalctl -u autotube -n 30 --no-pager; exit 1; }

# --------------------------------------------------------------------------
if [[ "$SKIP_TLS" == "yes" ]]; then
  warn "TLS skipped. The backend is on 127.0.0.1:$PORT only and is NOT reachable"
  warn "from your phone yet. Put something in front of it before exposing it."
elif [[ -z "$DOMAIN" ]]; then
  warn "No --domain given, so no TLS was configured."
  warn "The backend is bound to 127.0.0.1:$PORT and is not publicly reachable."
  warn "Re-run with --domain your.duckdns.org, or use Tailscale (see README)."
else
  say "Installing Caddy for automatic HTTPS on $DOMAIN"
  if ! command -v caddy >/dev/null 2>&1; then
    curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -qq
    apt-get install -y -qq caddy
  fi
  sed -e "s|@DOMAIN@|$DOMAIN|g" -e "s|@PORT@|$PORT|g" \
      "$APP_DIR/deploy/oracle/Caddyfile" > /etc/caddy/Caddyfile
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
  sleep 3
  systemctl is-active --quiet caddy && echo "caddy is running" \
    || { warn "caddy failed:"; journalctl -u caddy -n 20 --no-pager; }
fi

# --------------------------------------------------------------------------
say "Done"
TOKEN=$(grep '^AUTOTUBE_API_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
cat <<SUMMARY

  Backend URL for the app:  $( [[ -n "$DOMAIN" ]] && echo "https://$DOMAIN/" || echo "http://<not exposed yet>" )
  Backend API key:          $TOKEN

  Next:
    1. Add your API keys:   sudo -u $APP_USER nano $ENV_FILE
    2. Restart:             sudo systemctl restart autotube
    3. Verify:              curl -s http://127.0.0.1:$PORT/health
    4. Check readiness:     sudo -u $APP_USER $APP_DIR/.venv/bin/python -m backend.cli doctor

  Logs:      journalctl -u autotube -f
  Disk:      df -h /   (videos are 30-150 MB each; prune with 'backend.cli prune')

SUMMARY
