"""One launcher process per data directory; local activation requests contain no credentials."""

from __future__ import annotations

import errno
import os
import tempfile
import threading
import time
from pathlib import Path


class SingleInstance:
    """Keep a lifetime OS file lock. An additional launch asks the owner to show its UI."""

    def __init__(self, data_dir, timeout=15.0):
        self.directory = Path(data_dir) / ".session"
        self.timeout = timeout
        self.primary = False
        self._handle = None
        self._stop = threading.Event()
        self._thread = None

    def _acquire(self):
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                # Windows can lock a byte beyond EOF. The file never needs to be replaced.
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        self.primary = True
        return True

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.directory / "owner.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self._handle = os.fdopen(fd, "r+b", buffering=0)
        try:
            if not self._acquire():
                self._request_window()
            if self.primary:
                # Remove messages left by callers that were themselves terminated.
                cutoff = time.time() - max(60, 2 * self.timeout)
                for request in self.directory.glob("show-*"):
                    try:
                        if (
                            request.suffix in (".request", ".failed")
                            and request.stat().st_mtime < cutoff
                        ):
                            request.unlink()
                    except FileNotFoundError:
                        pass
            return self
        except BaseException:
            self._handle.close()
            raise

    def _request_window(self):
        fd, filename = tempfile.mkstemp(
            dir=self.directory, prefix="show-", suffix=".request"
        )
        os.close(fd)
        request = Path(filename)
        failed = request.with_suffix(".failed")
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                if not request.exists():
                    if failed.exists():
                        raise RuntimeError(
                            "Первое окно BlockHarbor не удалось открыть. Проверьте браузер по умолчанию."
                        )
                    return
                if self._acquire():
                    return  # The former owner exited while this caller was waiting.
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Запущенный BlockHarbor не ответил. Попробуйте открыть его ещё раз."
                    )
                time.sleep(0.12)
        finally:
            request.unlink(missing_ok=True)
            failed.unlink(missing_ok=True)

    def monitor(self, reopen, idle, shutdown):
        """Call reopen() in this owner's process, or shutdown() when idle() is true."""
        if not self.primary or self._thread is not None:
            raise RuntimeError(
                "Монитор окна может быть запущен только владельцем сессии."
            )

        def watch():
            while not self._stop.wait(0.25):
                requests = list(self.directory.glob("show-*.request"))
                if requests:
                    try:
                        reopen()
                    except Exception:
                        for request in requests:
                            try:
                                request.replace(request.with_suffix(".failed"))
                            except FileNotFoundError:
                                pass  # The waiting process already gave up.
                    else:
                        for request in requests:
                            request.unlink(missing_ok=True)
                elif idle():
                    shutdown()
                    return

        self._thread = threading.Thread(
            target=watch, name="blockharbor-window", daemon=True
        )
        self._thread.start()

    def __exit__(self, *_exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._handle.close()  # The kernel also releases the lock on abrupt process termination.
