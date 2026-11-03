#!/usr/bin/env bash
# claude-move installer — links bin/claude-move into ~/.local/bin (and optionally onto a remote SSH host).
#   ./install.sh                   install locally
#   ./install.sh --remote <host>   also sync & link onto <host>:~/claude-move
#   ./install.sh --uninstall       remove local symlink
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${CLAUDE_MOVE_BIN_DIR:-$HOME/.local/bin}"

command -v python3 >/dev/null || { echo "claude-move requires python3 >= 3.10" >&2; exit 1; }
command -v rsync >/dev/null || { echo "claude-move requires rsync" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$BIN_DIR/claude-move"
  echo "Removed $BIN_DIR/claude-move"
  exit 0
fi

chmod +x "$REPO/bin/claude-move"
mkdir -p "$BIN_DIR"
ln -sfn "$REPO/bin/claude-move" "$BIN_DIR/claude-move"
echo "Installed $BIN_DIR/claude-move -> $REPO/bin/claude-move"

if [ "${1:-}" = "--remote" ] && [ -n "${2:-}" ]; then
  REMOTE_HOST="$2"
  REMOTE_DIR="${3:-claude-move}"
  rsync -az --exclude '.git/' --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.venv/' \
    "$REPO/" "${REMOTE_HOST}:${REMOTE_DIR}/"
  ssh -o BatchMode=yes "$REMOTE_HOST" \
    "chmod +x ~/${REMOTE_DIR}/bin/claude-move && mkdir -p ~/.local/bin && ln -sfn \"\$HOME/${REMOTE_DIR}/bin/claude-move\" ~/.local/bin/claude-move"
  echo "Installed on ${REMOTE_HOST}:~/.local/bin/claude-move"
fi
