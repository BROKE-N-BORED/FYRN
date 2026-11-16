"""Tests for launch speed: the pre-Qt second-launch fast path
(fyrn/__main__.py) and the lazy imports that keep the cold start
down. The fast-path tests spawn real subprocesses; the lazy
import test inspects a fresh interpreter's modules."""

import fcntl
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent


def _python_env(home):
    env = dict(os.environ)
    env["FYRN_HOME"] = str(home)
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


class FastSecondLaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.lock_file = None
        self.received = []
        self.server = None
        self.server_thread = None

    def tearDown(self):
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass
        if self.server_thread is not None:
            self.server_thread.join(timeout=5)
        if self.lock_file is not None:
            self.lock_file.close()
        self.tmp.cleanup()

    def _hold_lock_and_listen(self):
        """Pretend to be the running first instance: hold the
        fcntl lock and answer the raise socket."""
        self.lock_file = open(self.home / "instance.lock", "a+")
        fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.server = socket.socket(socket.AF_UNIX,
                                    socket.SOCK_STREAM)
        self.server.bind(str(self.home / "raise.sock"))
        self.server.listen(1)
        self.server.settimeout(10)

        def serve():
            try:
                conn, _ = self.server.accept()
                self.received.append(conn.recv(16))
                conn.close()
            except OSError:
                pass

        self.server_thread = threading.Thread(target=serve,
                                              daemon=True)
        self.server_thread.start()

    def test_second_launch_raises_without_qt(self):
        # A second launch exits quickly after asking the running
        # instance to raise — without importing Qt. If the fast
        # path regressed, the child would import Qt (seconds),
        # take the free QLockFile, and open a window instead:
        # the timeout would fire and the raise would never arrive.
        self._hold_lock_and_listen()
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "fyrn"],
            cwd=str(_ROOT), env=_python_env(self.home),
            capture_output=True, text=True, timeout=15)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.received, [b"raise"])
        # The fast path is stdlib-only: well under the seconds
        # Qt's import alone costs, even under sandbox load.
        self.assertLess(elapsed, 8.0)

    def test_try_raise_running_first_then_second(self):
        from fyrn.__main__ import _try_raise_running
        old_home = os.environ.get("FYRN_HOME")
        os.environ["FYRN_HOME"] = str(self.home)
        try:
            # First instance: takes the lock, returns False.
            self.assertIs(_try_raise_running(), False)
            # Second call in this process: the lock is held, and
            # the listener answers, so it returns True fast.
            self._listen_only()
            started = time.monotonic()
            self.assertIs(_try_raise_running(), True)
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            if old_home is None:
                os.environ.pop("FYRN_HOME", None)
            else:
                os.environ["FYRN_HOME"] = old_home

    def _listen_only(self):
        """Listen on the raise socket without holding the lock
        (the lock is already held by _try_raise_running above)."""
        self.server = socket.socket(socket.AF_UNIX,
                                    socket.SOCK_STREAM)
        self.server.bind(str(self.home / "raise.sock"))
        self.server.listen(1)
        self.server.settimeout(10)

        def serve():
            try:
                conn, _ = self.server.accept()
                self.received.append(conn.recv(16))
                conn.close()
            except OSError:
                pass

        self.server_thread = threading.Thread(target=serve,
                                              daemon=True)
        self.server_thread.start()


class LazyImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_gui_import_stays_light(self):
        # The cold-start win: importing fyrn.gui must not pull in
        # the pull module, the debug logger (importlib.metadata),
        # or QtNetwork. They are imported lazily, where used.
        script = ("import sys; import fyrn.gui; "
                  "print('pull' if 'fyrn.pull' in sys.modules "
                  "else 'no-pull'); "
                  "print('debug' if 'fyrn.debug_log' in sys.modules "
                  "else 'no-debug'); "
                  "print('net' if 'PySide6.QtNetwork' in sys.modules "
                  "else 'no-net')")
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(_ROOT), env=_python_env(Path(self.tmp.name)),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(lines, ["no-pull", "no-debug", "no-net"])

    def test_main_module_imports_no_qt(self):
        # The second-launch path is stdlib-only: importing
        # fyrn.__main__ must not import any PySide6 module.
        script = ("import sys; import fyrn.__main__; "
                  "print([m for m in sys.modules "
                  "if m.startswith('PySide6')])")
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(_ROOT), env=_python_env(Path(self.tmp.name)),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
