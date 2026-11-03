# claude-move – Hinweise für Claude Code

Move Claude Code sessions between machines — sync working tree, rewrite transcript paths, resume in remote Herdr.

## Einstieg

1. `docs/PROGRESS.md` — aktueller Stand und Änderungsprotokoll.
2. `docs/decisions/0001-session-migration-architecture.md` — Architektur, Pfad-Mapping und NAT-Relay.

## Projektstruktur

- `claude_move/config.py` — Laden von `~/.config/claude-move/config.json` & Host-Auflösung
- `claude_move/discovery.py` — lokale & ein-Roundtrip-SSH-Session-Erkennung + `/proc/<pid>/environ`-Herdr-Zuordnung + Picker
- `claude_move/rewrite.py` — verlustfreies, symmetrisches JSONL-/Companion-Pfad-Rewriting
- `claude_move/herdr_ctl.py` — `herdr`-Steuerung (`local`, `--machine`, Socket oder SSH) + NAT-Relay
- `claude_move/transfer.py` — Orchestrierung (`stop` → `rsync` → `rewrite` → `.claude.json`-Trust → `herdr` Resume)
- `claude_move/cli.py` & `bin/claude-move` — CLI-Einstiegspunkt (reine Python-Stdlib)

## Tests & Installation

```bash
python3 -m unittest discover -s tests -v
./install.sh
```

## Regeln

- **Öffentliches GitHub-Repo (`legifx/claude-move`):** Niemals persönliche Hostnamen, IP-Adressen, private Projektnamen oder Secrets ins Repo committen — persönliche Maschinenkonfiguration gehört ausschließlich nach `~/.config/claude-move/config.json`.
- Wer Code ändert, trägt das oben im Log von `docs/PROGRESS.md` ein.
- Entscheidungen mit Alternativen kurz in `docs/decisions/NNNN-titel.md`.
