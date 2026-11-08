# 0002 — Self-Move: Abkoppeln, wenn claude-move in der zu verschiebenden Session läuft

## Kontext

Führt Claude Code `claude-move push <maschine> <session>` über sein eigenes Bash-Tool aus, ist claude-move ein Nachfahre genau des Claude-Prozesses, den Schritt 1 (`stop_source_session_if_needed`) per `SIGTERM` beendet. claude-move stirbt mit — kein rsync, kein Transfer, kein Resume. Manueller Workaround war `setsid -f bash -c 'sleep 3; claude-move push …'`.

## Entscheidung

- **Erkennung:** Vor dem Umzug (Push, Quelle lokal) wird die Elternkette ab dem eigenen Prozess durchlaufen — Linux über `/proc/<pid>/stat`, sonst `ps -o ppid= -p <pid>` (macOS/BSD). Liegt die PID der Session darin, ist es ein Self-Move.
- **Abkoppeln per Double-Fork im selben Prozess:** `fork` → `setsid` → `fork`; der Worker hängt stdin an `/dev/null` und hängt stdout/stderr an `~/.cache/claude-move/last-move.log` an (`$XDG_CACHE_HOME` wird beachtet). Der Vordergrundprozess gibt eine Zeile mit Log-Pfad aus und beendet sich mit 0. Der Worker wartet `CLAUDE_MOVE_DETACH_DELAY` Sekunden (Standard 3) und setzt dann den Umzug fort, damit das aufrufende Tool zurückgekehrt ist, bevor die Quelle gestoppt wird.
- **Schleifenschutz:** Der Worker setzt `CLAUDE_MOVE_DETACHED=1`; ist die Variable gesetzt, wird nie erneut abgekoppelt.
- **Kein Abkoppeln** bei `--dry-run`, `--keep-running` (dann wird nichts gestoppt) oder toter Session. `--attach` wird beim Self-Move mit Fehler abgelehnt (kein Terminal mehr vorhanden).
- **Implizites `--stop`:** Die Session gilt beim Self-Move zwangsläufig als „working" (sie führt ja gerade den Befehl aus). Wer claude-move auf die eigene Session anwendet, willigt damit ein, diesen Turn zu beenden — sonst würde der Worker nach dem Abkoppeln nur unsichtbar im Log scheitern.

## Verworfene Alternativen

- **Re-Exec (`subprocess.Popen(..., start_new_session=True)` mit rekonstruierter argv):** Funktioniert, verliert aber eine im interaktiven Picker getroffene Auswahl und müsste die Argumentliste samt Selektor nachbauen. Der In-Prozess-Fork behält die bereits aufgelöste Session und Optionen unverändert.
- **SIGTERM im eigenen Prozess ignorieren:** Hilft nicht — Claude räumt seine Prozessgruppe/Pipes auf, und das Bash-Tool würde bis zum Ende des Umzugs blockieren.
- **Nur dokumentieren (`setsid -f …`):** Zu fehleranfällig für Agenten, die sich selbst verschieben sollen.
