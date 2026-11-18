"""Entry point: python -m fyrn opens the Fyrn window.

A second launch raises the running window WITHOUT importing Qt.
The first instance holds an fcntl lock on ~/.fyrn/instance.lock
and listens on ~/.fyrn/raise.sock, so a second process only has
to try the lock (it fails), connect to the socket, send "raise",
and exit. On a 2-core CPU that turns an ~11 s second launch
(the review's measurement: most of it is importing Qt) into a
fraction of a second, because Qt's import alone costs seconds.

Nothing in this module imports Qt, or anything that imports Qt:
the whole point is that the second-launch path stays stdlib-only.
"""

import fcntl
import os
import socket
import time
from pathlib import Path

# The first instance's lock file, kept open for the process
# lifetime: closing it releases the lock. A module-level list so
# the file object is never garbage-collected.
_FIRST_INSTANCE_LOCK = []


def _home_dir():
    """Fyrn's home directory (~/.fyrn, or FYRN_HOME in tests)."""
    return Path(os.environ.get("FYRN_HOME", Path.home() / ".fyrn"))


def _try_raise_running():
    """Raise the running instance, if there is one.

    Returns True when another instance is already running (this
    process should exit: it asked the running one to raise, or
    the running one is mid-startup and will appear on its own).
    Returns False when this process is the first instance: it
    keeps the lock and must serve the socket for its lifetime
    (RaiseListener in fyrn.gui, started by main()).
    """
    home = _home_dir()
    home.mkdir(parents=True, exist_ok=True)
    lock_file = open(home / "instance.lock", "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another instance holds the lock: ask it to raise. It
        # may still be starting (its listener starts after the
        # window exists), so retry briefly before giving up.
        lock_file.close()
        sock_path = str(home / "raise.sock")
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                client = socket.socket(socket.AF_UNIX,
                                       socket.SOCK_STREAM)
                client.settimeout(0.2)
                client.connect(sock_path)
                client.sendall(b"raise")
                client.close()
                return True
            except OSError:
                time.sleep(0.1)
        # The first instance holds the lock but never answered:
        # it is mid-startup and will appear on its own. Exit
        # anyway — a second window is worse than a missed raise,
        # and the in-Qt guard is the backstop for the slow case.
        return True
    _FIRST_INSTANCE_LOCK.append(lock_file)
    return False


def main_entry():
    """The real entry point, split out so tests can call the
    fast-path check without launching the GUI."""
    if _try_raise_running():
        return 0
    # A staged update from the last run applies now, before Qt
    # (or anything else) is imported, so the new version is what
    # actually launches. A failed apply leaves the old version
    # in place and is noted for the debug log, never fatal: the
    # next launch's update check will offer the update again.
    # updates.py is stdlib-only, so this import costs the fast
    # path nothing (and the fast path already returned above).
    try:
        from .updates import apply_pending_update, pending_update
        if pending_update(_home_dir()) is not None:
            ok, message = apply_pending_update(_home_dir())
            if not ok:
                error_path = _home_dir() / "updates" / "last-error.txt"
                try:
                    error_path.parent.mkdir(parents=True,
                                            exist_ok=True)
                    error_path.write_text(
                        "Staged update did not apply: %s\n" % message,
                        encoding="utf-8")
                except OSError:
                    pass
    except Exception:
        pass
    # First instance: only now import Qt (the slow part).
    from .gui import main
    return main()


if __name__ == "__main__":
    raise SystemExit(main_entry())
