# Fortschritt

Wer etwas ändert, trägt es **oben im Log** ein.

## Stand

`claude-move` v0.1.0 ist implementiert, getestet und als öffentliches Repo veröffentlicht. Zero-Config-Betrieb, Linux-/macOS-Portabilität sowie Unterstützung für Herdr, `tmux` und direktes interaktives SSH (`--attach`) stehen bereit.

## Log

### 2026-11-08 · Fix: Self-Move aus der laufenden Session
- Gemacht:
  - Neues Modul `claude_move/selfdetach.py`: erkennt, ob claude-move ein Nachfahre des zu stoppenden Claude-Prozesses ist (`/proc/<pid>/stat`, Fallback `ps -o ppid=`), und koppelt sich dann per Double-Fork + `setsid` ab (stdin `/dev/null`, Ausgabe an `~/.cache/claude-move/last-move.log`, Startverzögerung `CLAUDE_MOVE_DETACH_DELAY`, Schleifenschutz `CLAUDE_MOVE_DETACHED`).
  - `cli.py`: Push-Pfad koppelt vor Stopp/Relay ab; Self-Move impliziert `--stop`, `--attach` wird abgelehnt, `--dry-run`/`--keep-running` koppeln nie ab.
  - Tests `tests/test_selfdetach.py`, ADR `0002`.
- Offen / nächster Schritt: Self-Move einmal im Alltag (Agent verschiebt sich selbst) erproben.

### 2026-11-03 · v0.1.0 implementiert & getestet
- Gemacht:
  - Komplettes Python-CLI-Paket (`claude_move/`: `config.py`, `rewrite.py`, `discovery.py`, `herdr_ctl.py`, `transfer.py`, `cli.py`) ohne Drittanbieter-Abhängigkeiten gebaut.
  - Symmetrisches Pfad-Rewriting (`workstation ↔ server`), portable POSIX-Prozessprüfung (Linux & macOS), automatische Git-Root-Erkennung für beliebige Arbeitsverzeichnisse, `.claude.json`-Trust-Freigabe sowie Multiplexer-Kaskade (`Herdr → tmux → SSH / --attach`) umgesetzt.
  - Unit- und End-to-End-Tests (`tests/`), Installer (`install.sh`), `config.example.json`, `README.md` und ADR `0001` erstellt.
- Offen / nächster Schritt: Im Alltag erproben.

### 2026-11-03 · angelegt
- Gemacht: Projekt angelegt.
- Offen / nächster Schritt: Ziel und ersten Meilenstein festlegen.
