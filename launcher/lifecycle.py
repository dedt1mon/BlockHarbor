"""Cancellation owned by one open launcher window.

Scopes never enumerate or terminate unrelated processes. A game is deliberately
not an owned preparation process: it may keep running after its launcher closes.
"""

from __future__ import annotations

import contextlib
import concurrent.futures
import os
import queue
import signal
import socket
import subprocess
import threading
import time

_LOCAL = threading.local()


class OperationCancelled(ValueError):
    pass


class CancellationScope:
    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.RLock()
        self._callbacks = set()
        self._threads = {}
        self._threads_lock = threading.Lock()

    def active_threads(self):
        with self._threads_lock:
            return set(self._threads)

    def check(self):
        if self.event.is_set():
            raise OperationCancelled("Лаунчер закрыт. Операция остановлена.")

    @contextlib.contextmanager
    def guard(self):
        """Serialize a short commit/spawn with cancellation, never a network wait."""
        self.check()
        with self.lock:
            self.check()
            yield

    def on_cancel(self, callback):
        with self.lock:
            cancelled = self.event.is_set()
            if not cancelled:
                self._callbacks.add(callback)
        if cancelled:
            callback()

        def remove():
            with self.lock:
                self._callbacks.discard(callback)

        return remove

    def cancel(self):
        # The event is visible before callbacks run, including to readers whose
        # socket has not finished connecting yet. Callbacks must not wait/join.
        if self.event.is_set():
            return
        self.event.set()

        def dispatch():
            with self.lock:
                callbacks, self._callbacks = tuple(self._callbacks), set()
            for callback in callbacks:
                try:
                    callback()
                except (OSError, ValueError, RuntimeError):
                    pass

        if self.lock.acquire(blocking=False):
            self.lock.release()
            dispatch()
        else:
            # Finish an in-flight atomic rename/SQLite commit without delaying
            # X. The event already blocks new work; no future commit may start.
            threading.Thread(target=dispatch, name="bh-cancel-resources", daemon=True).start()


def current_scope():
    return getattr(_LOCAL, "scope", None)


@contextlib.contextmanager
def bind(scope):
    previous = current_scope()
    _LOCAL.scope = scope
    thread = threading.current_thread()
    if scope:
        with scope._threads_lock:
            scope._threads[thread] = scope._threads.get(thread, 0) + 1
    try:
        if scope:
            scope.check()
        yield scope
    finally:
        if scope:
            with scope._threads_lock:
                count = scope._threads[thread] - 1
                if count:
                    scope._threads[thread] = count
                else:
                    del scope._threads[thread]
        _LOCAL.scope = previous


def check():
    scope = current_scope()
    if scope:
        scope.check()


def wait(seconds):
    scope = current_scope()
    if scope:
        if scope.event.wait(seconds):
            scope.check()
    else:
        time.sleep(seconds)


@contextlib.contextmanager
def guard():
    scope = current_scope()
    with scope.guard() if scope else contextlib.nullcontext():
        yield


def abort_response(opened):
    """Wake a blocked read without waiting for BufferedReader's read lock."""
    candidates = (
        ("fp", "raw", "_sock"),                 # urllib / http.client
        ("raw", "_fp", "fp", "raw", "_sock"), # requests / urllib3
        ("raw", "_connection", "sock"),
        ("_sock",),
    )
    for path in candidates:
        value = opened
        try:
            for name in path:
                value = getattr(value, name)
            if value is not None and callable(getattr(value, "shutdown", None)):
                try:
                    value.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
        except (AttributeError, ValueError):
            continue
    # Some providers/fakes have no exposed socket. close() may itself wait for a
    # read lock, so it must not hold up the window-close request.
    def close():
        try:
            opened.close()
        except (OSError, ValueError):
            pass
    threading.Thread(target=close, name="bh-close-response", daemon=True).start()


class _Response:
    def __init__(self, opened, scope):
        self._opened, self._scope = opened, scope

    def __getattr__(self, name):
        return getattr(self._opened, name)

    def _read(self, name, *args, **kwargs):
        self._scope.check()
        try:
            value = getattr(self._opened, name)(*args, **kwargs)
        except (OSError, ValueError):
            self._scope.check()
            raise
        self._scope.check()
        return value

    def read(self, *args, **kwargs):
        return self._read("read", *args, **kwargs)

    def read1(self, *args, **kwargs):
        return self._read("read1" if hasattr(self._opened, "read1") else "read", *args, **kwargs)

    def readline(self, *args, **kwargs):
        return self._read("readline", *args, **kwargs)

    def readinto(self, *args, **kwargs):
        return self._read("readinto", *args, **kwargs)

    def iter_content(self, *args, **kwargs):
        self._scope.check()
        for block in self._opened.iter_content(*args, **kwargs):
            self._scope.check()
            yield block


@contextlib.contextmanager
def response(opened):
    scope = current_scope()
    remove = scope.on_cancel(lambda: abort_response(opened)) if scope else lambda: None
    try:
        if scope:
            scope.check()
        yield _Response(opened, scope) if scope else opened
    finally:
        remove()
        opened.close()


class Executor(concurrent.futures.Executor):
    """Small daemon pool whose queued work belongs to its initiating scope.

    The standard pool registers an interpreter-wide join for every worker. A
    stalled DNS lookup would therefore leave an invisible launcher alive after
    X. These workers cooperate with cancellation and never prevent process exit.
    """

    def __init__(self, max_workers=None, thread_name_prefix="bh-work", initializer=None, initargs=()):
        self._scope = current_scope()
        self._lock = threading.RLock()
        self._queue = queue.Queue()
        self._closed = False
        count = max_workers or 4
        if count < 1:
            raise ValueError("max_workers must be positive")
        self._max_workers = count
        self._threads = [threading.Thread(target=self._worker, args=(initializer, initargs),
                          daemon=True, name=f"{thread_name_prefix}-{i}") for i in range(count)]
        for thread in self._threads:
            thread.start()
        self._remove_cancel = self._scope.on_cancel(lambda: self.shutdown(wait=False, cancel_futures=True)) if self._scope else lambda: None

    def _worker(self, initializer, initargs):
        if initializer:
            initializer(*initargs)
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                future, fn, args, kwargs = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    with bind(self._scope):
                        result = fn(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                self._queue.task_done()

    def submit(self, fn, /, *args, **kwargs):
        with self._lock:
            if self._scope:
                self._scope.check()
            if self._closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
            future = concurrent.futures.Future()
            self._queue.put((future, fn, args, kwargs))
            return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._lock:
            if cancel_futures:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        item[0].cancel()
                    self._queue.task_done()
            first = not self._closed
            self._closed = True
            # Cancellation may have drained a prior set of sentinels.
            if first or cancel_futures:
                for _ in self._threads:
                    self._queue.put(None)
        if wait:
            for thread in self._threads:
                if thread is threading.current_thread():
                    continue
                # The caller is also owned daemon work. Keep its transaction /
                # temporary provider patches alive until running tasks return.
                # App.close has its own bounded join; process exit never waits
                # for this pool through concurrent.futures' atexit registry.
                thread.join()
        if hasattr(self, "_remove_cancel"):
            self._remove_cancel()


class _ProcessOwner:
    """A handle/group created for preparation, never for a Minecraft session."""
    def __init__(self, process, *, group=False):
        self.process, self.group, self.job = process, group, None
        self.lock = threading.RLock()
        if os.name == "nt":
            self._windows_job()

    def _windows_job(self):
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Counters), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            for name, result, args in (
                ("CreateJobObjectW", wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
                ("SetInformationJobObject", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]),
                ("AssignProcessToJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
                ("TerminateJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
                ("CloseHandle", wintypes.BOOL, [wintypes.HANDLE]),
            ):
                method = getattr(kernel, name)
                method.restype, method.argtypes = result, args
            handle = kernel.CreateJobObjectW(None, None)
            if not handle:
                return
            limits = Extended()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if (kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                    and kernel.AssignProcessToJobObject(handle, int(self.process._handle))):
                self.job = (kernel, handle)
            else:
                kernel.CloseHandle(handle)
        except (OSError, AttributeError, TypeError):
            pass

    def stop(self):
        with self.lock:
            if self.job:
                self.job[0].TerminateJobObject(self.job[1], 1)
            elif self.group and os.name != "nt":
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif self.process.poll() is None:
                try:
                    self.process.kill()
                except OSError:
                    pass

    def close(self):
        with self.lock:
            if self.job:
                self.job[0].CloseHandle(self.job[1])
                self.job = None


def own_process(process, *, group=False):
    """Register a known helper; unregistering never terminates a detached game."""
    scope = current_scope()
    owner = _ProcessOwner(process, group=group)
    remove = scope.on_cancel(owner.stop) if scope else lambda: None

    def release():
        remove()
        owner.close()

    return release


def run(command, *, timeout=None, check=False, capture_output=False, input=None, **kwargs):
    """Like subprocess.run for cancelable installer/probe children only."""
    if os.name == "nt":
        # CREATE_NO_WINDOW is ignored when NEW_CONSOLE or DETACHED_PROCESS is
        # present. Installer/probe children always stay in the background while
        # their stdout/stderr retain the caller's existing log/capture handles.
        # Games, Prism and Steam use their own Popen paths and are unaffected.
        flags = kwargs.get("creationflags", 0)
        kwargs["creationflags"] = (flags & ~0x00000018) | 0x08000000
    scope = current_scope()
    if not scope:
        return subprocess.run(command, timeout=timeout, check=check, capture_output=capture_output, input=input, **kwargs)
    if capture_output:
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("stdout/stderr cannot be used with capture_output")
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input is not None:
        if "stdin" in kwargs:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    group = os.name != "nt" and not kwargs.get("start_new_session") and "process_group" not in kwargs
    if group:
        kwargs["start_new_session"] = True
    with scope.guard():
        process = subprocess.Popen(command, **kwargs)
        owner = _ProcessOwner(process, group=group)
        remove = scope.on_cancel(owner.stop)
    began = time.monotonic()
    try:
        first = True
        while True:
            scope.check()
            if timeout is not None and time.monotonic() - began >= timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(input=input if first else None, timeout=0.15)
                break
            except subprocess.TimeoutExpired:
                first = False
        scope.check()
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
    except BaseException:
        owner.stop()
        try:
            process.communicate(timeout=1)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        raise
    finally:
        remove()
        owner.close()
