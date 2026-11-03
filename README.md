# claude-move

Move live or saved **Claude Code** sessions seamlessly between machines (`workstation ↔ server`, Linux & macOS) so they keep running uninterrupted on the other side.

At any turn boundary, Claude Code reconstructs its entire conversation, tool history, thinking blocks, subagent contexts, and file-history checkpoints from files under `~/.claude/`. `claude-move` syncs the project working tree (including uncommitted edits), transfers all session artifacts, rewrites machine-specific paths (`/Users/alice/dev/app` or `/home/alice/Projects/app` ↔ `/home/ubuntu/app`) losslessly across transcripts and subagent logs, marks the destination folder trusted in `~/.claude.json`, and resumes `claude --resume <UUID>` in **[Herdr](https://herdr.dev)**, **tmux**, or an interactive SSH shell.

Zero third-party Python dependencies (Python 3.8+ standard library + `ssh` and `rsync`). Works **out of the box with zero configuration** against any SSH host.

---

## Features

- **Zero-Config Ready:** Run `claude-move user@myserver` right after cloning — no config file, no remote installation, and no multiplexer setup required.
- **Works With Herdr, tmux, or Plain SSH (`--launcher auto|herdr|tmux|none`):**
  1. **Herdr** (upstream `herdr` over plain SSH, saved `--machine` profile, or forwarded Unix socket): opens a new tab and starts `claude --resume <UUID>`.
  2. **tmux:** automatically opens a new named `tmux` window if `tmux` is active on the target (or creates a `claude` tmux session with `--launcher tmux`).
  3. **Plain SSH / Terminal:** prints the exact `ssh -t ...` resume command, or jumps straight into the resumed remote session interactively with `--attach` (`-a`).
- **Lossless Turn-Boundary Migration:** Moves `<UUID>.jsonl`, companion `<UUID>/` (`subagents/`, `tool-results/`), `file-history/<UUID>/`, `session-env/<UUID>/`, project `memory/`, and optional [`cproj`](https://github.com/legifx/cproj) session badges.
- **Symmetric Path Rewriting (`workstation ↔ server`):** Rewrites `cwd`, encoded `~/.claude/projects/<encoded-cwd>` directory names, tool input/output paths, tilde shortcuts (`~/Projects/app` ↔ `~/app`), `trackedFileBackups` dictionary keys, and relative `file-history-delta` `trackingPath` entries — without touching similarly named usernames or binary snapshots.
- **Smart Project Detection:** Auto-detects the Git/project root whether you start Claude inside `~/dev/app`, `~/Projects/app`, or `$HOME` (`~`), while refusing to ever `rsync` `$HOME` or `/` itself.
- **Bidirectional — Even Behind NAT:**
  - **Push (`local → remote`):** `claude-move user@server <session>`
  - **Pull (`remote → local`):** `claude-move --from user@server <session>`
  - **Optional Socket Relay:** If a workstation has no inbound SSH, a forwarded Herdr Unix socket (`reverse_herdr_socket`) allows initiating moves directly from the server terminal too.

---

## Installation

```bash
git clone https://github.com/legifx/claude-move.git
cd claude-move
./install.sh

# Optional: also install onto a remote server in one step
./install.sh --remote user@server
```

---

## Quickstart (Zero Config)

```bash
# List live and recent Claude sessions locally or on any SSH host
claude-move ls
claude-move ls user@myserver

# Interactive picker (fzf or numbered menu) -> move chosen session to user@myserver
claude-move user@myserver

# Move a specific session and immediately attach over SSH (no tmux/Herdr needed)
claude-move user@myserver a30a98df --attach

# Move a session and start it inside a remote tmux window
claude-move user@myserver a30a98df --launcher tmux --continue

# Pull a session back from the server to your local machine
claude-move --from user@myserver

# Preview everything without modifying either machine
claude-move --dry-run user@myserver a30a98df
```

### Key Flags

| Flag | Description |
|---|---|
| `-f, --from MACHINE` | Pull a session from `MACHINE` onto the current machine |
| `-t, --to MACHINE` | Push a local session to `MACHINE` |
| `-n, --dry-run` | Print exact project sync pairs, path rewrites, and launch plan without changes |
| `--launcher MODE` | `auto` (default: Herdr → active tmux → manual hint), `herdr`, `tmux`, or `none` |
| `-a, --attach` | Attach interactively (`ssh -t ... claude --resume`) right after migrating |
| `-c, --continue` | Send a continuation prompt as soon as the resumed session starts |
| `-p, --prompt TEXT` | Send a custom prompt upon resume on the destination machine |
| `--project DIR` | Explicit project directory to sync (repeatable; overrides auto-detection) |
| `--all-projects` | Sync all project directories referenced in the transcript (default: primary project only) |
| `--no-project` | Move Claude transcript & state only, skip project `rsync` |
| `--include-deps` | Include `node_modules/`, `.venv/`, `target/`, etc. in `rsync` |
| `--stop` | Interrupt an actively working turn (`Esc`) and stop cleanly before moving |
| `--keep-running` | Fork/clone the session onto target without stopping the source process |
| `--close-tab` | Close the source Herdr tab after stopping the source Claude process |
| `--no-launch` | Sync and rewrite files only; print manual `claude --resume` command |
| `--focus` | Focus the newly created Herdr tab on the destination machine |
| `--skip-permissions` | Pass `--dangerously-skip-permissions` to the resumed Claude process |

---

## Optional Configuration (`~/.config/claude-move/config.json`)

For instant single-word commands (`claude-move`, `claude-move --from server`) or differing directory layouts (e.g. `~/Projects/<repo>` on workstation vs `~/<repo>` on server), create `~/.config/claude-move/config.json` from [`config.example.json`](config.example.json):

```json
{
  "default_target": "server",
  "launcher": "auto",
  "machines": {
    "workstation": {
      "hostnames": ["my-workstation"],
      "home": "/home/alice",
      "projects_dir": "/home/alice/Projects"
    },
    "server": {
      "hostnames": ["my-homeserver"],
      "home": "/home/server",
      "projects_dir": "/home/server",
      "ssh": "server",
      "claude_args": ["--dangerously-skip-permissions"]
    }
  }
}
```

### Herdr Connection Modes (Optional)

If you use [Herdr](https://herdr.dev), `claude-move` supports all setups automatically:
- **Standard Herdr over SSH (zero config):** Leave `herdr_machine` unset (`null`) — `claude-move` invokes `herdr` over `ssh <host>` automatically.
- **Saved Herdr Machine Profile:** Set `"herdr_machine": "Server"` to route via `herdr --machine Server`.
- **Forwarded Unix Socket (NAT traversal):** Set `"reverse_herdr_socket": "/path/to/forwarded.sock"` on the server if the workstation has no inbound SSH.

---

## Development & Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
