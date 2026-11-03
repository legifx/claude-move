# 0001 — Architektur: Turn-Boundary-Migration, symmetrisches Pfad-Rewriting & Multiplexer-Unabhängigkeit

## Kontext

Claude Code hält während eines laufenden API-Streams offene TLS-/MCP-Sockets und Kindprozesse im Speicher, rekonstruiert aber bei `claude --resume <UUID>` den kompletten Gesprächs-, Thinking-, Subagenten- und Datei-Checkpoint-Zustand ausschließlich aus Dateien unter `~/.claude/`. Zwischen Workstation (Linux/macOS, z. B. `~/Projects/<repo>` oder `~/dev/<repo>`) und Server (`/home/<user>/<repo>`) unterscheiden sich `$HOME`, Verzeichnistiefe, Multiplexer (`herdr`, `tmux` oder reines SSH) sowie ggf. die Netzwerktopologie (NAT ohne eingehendes SSH auf der Workstation).

## Entscheidungen

### 1. Migration auf Turn-Grenze statt Prozess-CRIU
- **Gewählt:** Portabler POSIX-Stopp an der Turn-Grenze (`SIGTERM` + `os.kill(pid, 0)` für Linux & macOS), Dateisynchronisation (`rsync` + einzelner komprimierter `tar`-Stream für Session-Metadaten) und Start per `claude --resume <UUID>`.
- **Verworfen:** Prozess-Checkpointing (CRIU) — scheitert an plattformübergreifenden Umzügen (macOS ↔ Linux), unterschiedlichen UIDs/Pfaden, offenen API-TLS-Verbindungen und lokalen MCP-Daemons.

### 2. Struktur-bewusstes, symmetrisches JSONL-Rewriting statt blindem Text-Replace
- **Gewählt:** Zeilenweises JSON-Parsing (`rewrite.py`) mit kombiniertem Single-Pass-Alternation-Regex (längstes Präfix zuerst, strikte Pfadgrenzen) über String-Werte, Pfad-Keys (`trackedFileBackups`), Tilde-Referenzen (`~/Projects/<repo>` ↔ `~/<repo>`) und relative `trackingPath`-Felder in `file-history-delta`.
- **Warum:** Entspricht `projects_dir` auf dem Server direkt `$HOME`, werden beim Rückweg (`server → workstation`) gezielt nur erkannte Projektnamen nach `~/Projects/<repo>` abgebildet, während Dotfiles (`~/.claude/...`) und `cwd` fehlerfrei auf `$HOME` zurückfallen. Binäre Snapshots in `file-history/<UUID>/` bleiben unangetastet.

### 3. Zero-Config & Multiplexer-Kaskade (`Herdr → tmux → SSH`)
- **Gewählt:** `--launcher auto` prüft auf dem Ziel der Reihe nach:
  1. **Herdr** (wahlweise Standard-Upstream-`herdr` über normales SSH, gespeichertes `--machine`-Profil oder weitergeleiteter Unix-Socket),
  2. **tmux** (öffnet ein benanntes Fenster in einer laufenden `tmux`-Session; mit `--launcher tmux` wird bei Bedarf eine neue `tmux`-Session angelegt),
  3. **Manuell / Direkt-Attach** (gibt den fertigen `ssh -t`-Befehl aus bzw. verbindet mit `--attach` direkt interaktiv).
- **Vorteil:** Funktioniert ohne Konfigurationsdatei und unabhängig davon, ob jemand Herdr, tmux oder reines SSH verwendet.
