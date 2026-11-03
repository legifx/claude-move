# Fortschritt

Wer etwas ändert, trägt es **oben im Log** ein.

## Stand

`claude-move` v0.1.0 ist implementiert, getestet und als öffentliches Repo veröffentlicht. Zero-Config-Betrieb, Linux-/macOS-Portabilität sowie Unterstützung für Herdr, `tmux` und direktes interaktives SSH (`--attach`) stehen bereit.

## Log

### 2026-11-03 · v0.1.0 implementiert & getestet
- Gemacht:
  - Komplettes Python-CLI-Paket (`claude_move/`: `config.py`, `rewrite.py`, `discovery.py`, `herdr_ctl.py`, `transfer.py`, `cli.py`) ohne Drittanbieter-Abhängigkeiten gebaut.
  - Symmetrisches Pfad-Rewriting (`workstation ↔ server`), portable POSIX-Prozessprüfung (Linux & macOS), automatische Git-Root-Erkennung für beliebige Arbeitsverzeichnisse, `.claude.json`-Trust-Freigabe sowie Multiplexer-Kaskade (`Herdr → tmux → SSH / --attach`) umgesetzt.
  - Unit- und End-to-End-Tests (`tests/`), Installer (`install.sh`), `config.example.json`, `README.md` und ADR `0001` erstellt.
- Offen / nächster Schritt: Im Alltag erproben.

### 2026-11-03 · angelegt
- Gemacht: Projekt angelegt.
- Offen / nächster Schritt: Ziel und ersten Meilenstein festlegen.
