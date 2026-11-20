"""Tests for self-move detection and detaching (claude-move run inside the moved session)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from claude_move import cli, selfdetach
from claude_move.config import AppConfig, MachineConfig
from claude_move.discovery import SessionRecord

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestAncestry(unittest.TestCase):
    def test_parent_pid_matches_os(self) -> None:
        self.assertEqual(selfdetach._parent_pid(os.getpid()), os.getppid())

    def test_parent_pid_ps_fallback(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError("no /proc")):
            self.assertEqual(selfdetach._parent_pid(os.getpid()), os.getppid())

    def test_is_descendant_of_parent_and_self(self) -> None:
        self.assertTrue(selfdetach.is_descendant_of(os.getppid()))
        self.assertTrue(selfdetach.is_descendant_of(os.getpid()))

    def test_not_descendant_of_unrelated_process(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            self.assertFalse(selfdetach.is_descendant_of(proc.pid))
        finally:
            proc.kill()
            proc.wait()

    def test_init_and_invalid_pids_never_match(self) -> None:
        self.assertFalse(selfdetach.is_descendant_of(1))
        self.assertFalse(selfdetach.is_descendant_of(0))

    def test_needs_detach_guards(self) -> None:
        ppid = os.getppid()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(selfdetach.DETACHED_ENV, None)
            self.assertTrue(selfdetach.needs_detach(ppid, True, False, False))
            self.assertFalse(selfdetach.needs_detach(ppid, True, True, False))  # dry-run
            self.assertFalse(selfdetach.needs_detach(ppid, True, False, True))  # keep-running
            self.assertFalse(selfdetach.needs_detach(ppid, False, False, False))  # not alive
            self.assertFalse(selfdetach.needs_detach(None, True, False, False))
            os.environ[selfdetach.DETACHED_ENV] = "1"  # loop guard
            self.assertFalse(selfdetach.needs_detach(ppid, True, False, False))


class TestDetachProcess(unittest.TestCase):
    def test_detach_returns_fast_and_worker_runs_in_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "last-move.log"
            script = textwrap.dedent(
                f"""
                import os, sys
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from pathlib import Path
                from claude_move.selfdetach import detach
                parent_sid = os.getsid(0)
                if detach(Path({str(log)!r})):
                    sys.exit(0)
                print("WORKER", os.getsid(0) != parent_sid, os.environ.get("CLAUDE_MOVE_DETACHED"))
                """
            )
            env = dict(os.environ, CLAUDE_MOVE_DETACH_DELAY="3")
            env.pop("CLAUDE_MOVE_DETACHED", None)
            start = time.time()
            # capture_output only returns once *every* holder of the pipe exits, so a
            # fast return proves the worker no longer holds the caller's stdout/stderr.
            proc = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=10
            )
            self.assertLess(time.time() - start, 2.5)  # returned before the worker delay ended
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("detached", proc.stdout)
            self.assertIn(str(log), proc.stdout)
            self.assertNotIn("WORKER", proc.stdout)

            for _ in range(80):
                if log.exists() and "WORKER" in log.read_text():
                    break
                time.sleep(0.1)
            content = log.read_text()
            self.assertIn("WORKER True 1", content)


class TestCliSelfMove(unittest.TestCase):
    def _run_main(self, argv: list[str], descendant: bool, in_worker: bool = False):
        local = MachineConfig(name="local", home="/home/u", projects_dir="/home/u")
        remote = MachineConfig(name="box", home="/home/u", projects_dir="/home/u", ssh="u@box")
        cfg = AppConfig(machines={"local": local, "box": remote}, here="local")
        session = SessionRecord(
            session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            transcript_path="/x.jsonl",
            encoded_cwd="-home-u",
            cwd="/home/u",
            mtime=time.time(),
            size_bytes=1,
            alive=True,
            pid=4242,
        )
        with mock.patch.object(cli, "load_config", return_value=cfg), mock.patch.object(
            cli, "resolve_machine", return_value=remote
        ), mock.patch.object(cli, "discover_sessions", return_value=[session]), mock.patch.object(
            cli, "resolve_session", return_value=session
        ), mock.patch.object(
            cli, "move_session"
        ) as move, mock.patch.object(
            cli, "detach", return_value=not in_worker
        ) as det, mock.patch.object(
            selfdetach, "is_descendant_of", return_value=descendant
        ), mock.patch.dict(
            os.environ, {}, clear=False
        ):
            os.environ.pop(selfdetach.DETACHED_ENV, None)
            rc = cli.main(argv)
        return rc, move, det

    def test_self_move_detaches_and_returns_zero(self) -> None:
        rc, move, det = self._run_main(["push", "box", "aaaaaaaa"], descendant=True)
        self.assertEqual(rc, 0)
        det.assert_called_once()
        move.assert_not_called()

    def test_detached_worker_continues_move_with_implicit_stop(self) -> None:
        rc, move, det = self._run_main(["push", "box", "aaaaaaaa"], descendant=True, in_worker=True)
        self.assertEqual(rc, 0)
        det.assert_called_once()
        move.assert_called_once()
        self.assertTrue(move.call_args.kwargs["opts"].stop_working)

    def test_dry_run_never_detaches(self) -> None:
        rc, move, det = self._run_main(["-n", "push", "box", "aaaaaaaa"], descendant=True)
        self.assertEqual(rc, 0)
        det.assert_not_called()
        move.assert_called_once()

    def test_regular_move_does_not_detach(self) -> None:
        rc, move, det = self._run_main(["push", "box", "aaaaaaaa"], descendant=False)
        self.assertEqual(rc, 0)
        det.assert_not_called()
        move.assert_called_once()
        self.assertFalse(move.call_args.kwargs["opts"].stop_working)


if __name__ == "__main__":
    unittest.main()
