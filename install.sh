#!/bin/sh
# Installs build-agent for this user on macOS, Linux or WSL2. Safe to re-run.
#   ./install.sh            venv + launcher on PATH + config skeleton
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${BUILD_AGENT_HOME:-$HOME/.build-agent}"
CONF_DIR="$HOME/.config/build-agent"
BIN_DIR="$HOME/.local/bin"

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) echo "build-agent runs on macOS, Linux or WSL2 (not native Windows). On Windows: wsl --install -d Ubuntu" >&2; exit 1 ;;
esac

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)" >&2; missing=1; }; }
missing=0
need python3 "3.9+"
need git "2.30+"
need gh "GitHub CLI: https://cli.github.com, then: gh auth login"
need claude "Claude Code CLI: https://docs.claude.com/en/docs/claude-code"
[ "$missing" = 0 ] || exit 1

python3 -m venv "$HERE/.venv"
if ! "$HERE/.venv/bin/python" -c 'import tomllib' 2>/dev/null; then
  "$HERE/.venv/bin/pip" install --quiet 'tomli>=2'
fi

mkdir -p "$HOME_DIR/briefs" "$HOME_DIR/secrets" "$CONF_DIR" "$BIN_DIR"
chmod 700 "$HOME_DIR/secrets"
ln -sf "$HERE/bin/build-agent" "$BIN_DIR/build-agent"
if [ ! -f "$CONF_DIR/config.toml" ]; then
  cp "$HERE/examples/config.example.toml" "$CONF_DIR/config.toml"
  echo "wrote $CONF_DIR/config.toml: edit [[repos]] before the first build"
fi

case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "add $BIN_DIR to PATH" ;; esac
echo "Optional, for unattended runs: claude setup-token, then save the token to $HOME_DIR/secrets/claude-token (chmod 600)"
echo "Then: build-agent --check"
