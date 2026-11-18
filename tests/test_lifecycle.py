import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from launcher import lifecycle, preparation, window
from launcher.sessions import SessionBusy, process_identity
from launcher.transfers import TransferCancelled, TransferManager
from main import App, Handler, Server


PROJECT = Path(__file__).resolve().parents[1]


def eventually(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for lifecycle state")


def stop_process(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


class ScopeTests(unittest.TestCase):
    def test_windows_probe_has_no_console_without_a_cancellation_scope(self):
        capture = Mock()
        with patch.object(lifecycle.os, "name", "nt"), patch.object(lifecycle.subprocess, "run") as run:
            lifecycle.run(["java.exe", "-version"], creationflags=0x200 | 0x10 | 0x08,
                          stdout=capture, stderr=subprocess.STDOUT, timeout=8)
        self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000 | 0x200)
        self.assertIs(run.call_args.kwargs["stdout"], capture)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(run.call_args.kwargs["timeout"], 8)

    def test_windows_owned_installer_keeps_cancellation_and_output_without_console(self):
        scope = lifecycle.CancellationScope()
        process = Mock(returncode=0)
        process.communicate.return_value = (b"installed", b"diagnostic output")
        startup = object()
        with (lifecycle.bind(scope), patch.object(lifecycle.os, "name", "nt"),
              patch.object(lifecycle.subprocess, "Popen", return_value=process) as spawn,
              patch.object(lifecycle, "_ProcessOwner") as owner):
            result = lifecycle.run(["java.exe", "-jar", "installer.jar"],
                                   check=True, capture_output=True, startupinfo=startup)
        self.assertEqual(spawn.call_args.kwargs["creationflags"], 0x08000000)
        self.assertIs(spawn.call_args.kwargs["startupinfo"], startup)
        self.assertEqual(spawn.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(spawn.call_args.kwargs["stderr"], subprocess.PIPE)
        owner.assert_called_once_with(process, group=False)
        owner.return_value.close.assert_called_once()
        self.assertEqual(result.stdout, b"installed")
        self.assertEqual(result.stderr, b"diagnostic output")

    def test_cancel_is_immediate_while_atomic_commit_finishes_and_callbacks_run_once(self):
        scope = lifecycle.CancellationScope()
        entered, release, called = threading.Event(), threading.Event(), threading.Event()
        calls = []
        scope.on_cancel(lambda: (calls.append("cancel"), called.set()))

        def commit():
            with scope.guard():
                entered.set()
                release.wait(3)

        thread = threading.Thread(target=commit)
        thread.start()
        self.assertTrue(entered.wait(2))
        began = time.monotonic()
        scope.cancel()
        scope.cancel()
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertTrue(scope.event.is_set())
        self.assertEqual(calls, [])
        with self.assertRaises(lifecycle.OperationCancelled):
            scope.check()
        with self.assertRaises(lifecycle.OperationCancelled):
            with scope.guard():
                self.fail("closed scope admitted a new commit")
        release.set()
        thread.join(2)
        self.assertTrue(called.wait(2))
        self.assertEqual(calls, ["cancel"])

    def test_response_cancellation_wakes_a_read_before_bytes_can_be_published(self):
        scope = lifecycle.CancellationScope()
        reading, stopped = threading.Event(), threading.Event()
        published, errors = [], []

        class Response:
            def read(self, *_):
                reading.set()
                stopped.wait(3)
                return b"must not be saved"

            def close(self):
                stopped.set()

        def download():
            try:
                with lifecycle.bind(scope), lifecycle.response(Response()) as response:
                    published.append(response.read(1))
            except lifecycle.OperationCancelled as exc:
                errors.append(exc)

        thread = threading.Thread(target=download)
        thread.start()
        self.assertTrue(reading.wait(2))
        scope.cancel()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(published, [])
        self.assertEqual(len(errors), 1)

    def test_pool_cancels_queued_work_and_retains_scope_until_running_task_finishes(self):
        scope = lifecycle.CancellationScope()
        entered, release = threading.Event(), threading.Event()
        started = []
        with lifecycle.bind(scope):
            pool = lifecycle.Executor(max_workers=1)
            first = pool.submit(lambda: (entered.set(), release.wait(3), lifecycle.check()))
            second = pool.submit(lambda: started.append("unexpected"))
        self.assertTrue(entered.wait(2))
        scope.cancel()
        self.assertTrue(second.cancelled())
        self.assertFalse(first.done())
        release.set()
        pool.shutdown()
        with self.assertRaises(lifecycle.OperationCancelled):
            first.result()
        self.assertEqual(started, [])
        self.assertFalse(any(thread.is_alive() for thread in pool._threads))

    def test_owned_processor_dies_but_unrelated_child_survives(self):
        with tempfile.TemporaryDirectory() as temp:
            pidfile = Path(temp) / "processor.pid"
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(stop_process, unrelated)
            scope, errors = lifecycle.CancellationScope(), []
            code = "import os,time,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"

            def work():
                try:
                    with lifecycle.bind(scope):
                        lifecycle.run([sys.executable, "-c", code, str(pidfile)], timeout=90)
                except lifecycle.OperationCancelled as exc:
                    errors.append(exc)

            thread = threading.Thread(target=work)
            thread.start()
            eventually(pidfile.exists)
            owned = int(pidfile.read_text())
            scope.cancel()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertIsNone(process_identity(owned))
            self.assertIsNone(unrelated.poll())
            self.assertEqual(len(errors), 1)

    def test_vendor_swallowed_download_cancel_cannot_extract_cached_natives(self):
        from minecraft_launcher_lib import install

        scope = lifecycle.CancellationScope()

        def failed(*_args, **_kwargs):
            scope.cancel()
            raise lifecycle.OperationCancelled("closed")

        with tempfile.TemporaryDirectory() as temp, patch.object(install, "extract_natives_file") as extract:
            with self.assertRaises(lifecycle.OperationCancelled):
                with lifecycle.bind(scope), preparation.environment(preparation.Progress(lambda _: None)):
                    with patch.object(install, "download_file", side_effect=failed):
                        install.install_libraries("fixture", [{"name": "fixture:fixture:1", "extract": {"exclude": []}, "natives": {"linux": "natives-linux", "windows": "natives-windows", "osx": "natives-osx"}}], temp, {})
            extract.assert_not_called()


class TransferLifecycleTests(unittest.TestCase):
    def test_close_cancels_active_and_queued_downloads_without_callbacks_or_commit(self):
        import hashlib

        with tempfile.TemporaryDirectory() as temp:
            manager = TransferManager(Path(temp))
            entered, stopped = threading.Event(), threading.Event()
            calls = []

            class Response:
                headers = {"Content-Length": "1"}

                def getcode(self):
                    return 200

                def read(self, *_):
                    entered.set()
                    stopped.wait(3)
                    return b"x"

                def close(self):
                    stopped.set()

            options = {"hashes": {"sha256": hashlib.sha256(b"x").hexdigest()}, "size": 1, "hosts": {"example.test"}, "on_complete": lambda result: calls.append(result)}
            with patch("launcher.transfers.open_https", return_value=Response()):
                first = manager.submit("https://example.test/a", "a.jar", **options)
                self.assertTrue(entered.wait(2))
                second = manager.submit("https://example.test/b", "b.jar", **options)
                manager.close(timeout=2)
            self.assertFalse(manager.worker.is_alive())
            self.assertFalse(manager.active())
            self.assertEqual(manager.tasks[first]["state"], "cancelled")
            self.assertEqual(manager.tasks[second]["state"], "cancelled")
            self.assertEqual(calls, [])
            self.assertFalse((Path(temp) / "a.jar").exists())
            self.assertFalse((Path(temp) / "b.jar").exists())
            with self.assertRaises(lifecycle.OperationCancelled):
                manager.submit("https://example.test/c", "c.jar", **options)
            with self.assertRaises(lifecycle.OperationCancelled):
                manager.retry(second)
            manager.close()


class WindowLifecycleTests(unittest.TestCase):
    def test_early_x_is_seen_without_page_registration_and_game_hide_is_alive(self):
        with tempfile.TemporaryDirectory() as temp:
            host = window.DesktopWindow(temp)
            native = Mock()
            native.hwnd = None
            native.alive.return_value = True
            native.register_host.side_effect = lambda *_: setattr(native, "hwnd", 84) or True
            host.native, host.marker = native, "a" * 24
            host.host_process = SimpleNamespace(pid=123, poll=lambda: None)
            host.awaiting_registration = True
            self.assertFalse(host.closed())
            native.register_host.assert_called_once_with(123, "a" * 24)
            host.url = "http://127.0.0.1:9000/#token"
            host.game_started()
            self.assertFalse(host.closed())
            native.alive.return_value = False
            self.assertTrue(host.closed())
            with patch.object(host, "show") as show:
                host.game_ended()
                show.assert_not_called()

    def test_close_terminates_only_retained_app_host_and_prevents_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            host = window.DesktopWindow(temp)
            native, process = Mock(), Mock()
            native.hwnd = 84
            process.wait.side_effect = [subprocess.TimeoutExpired("browser", 0.1), 0]
            host.native, host.host_process = native, process
            host.release_host = Mock()
            host.restore_after_game = True
            with patch.object(window.webbrowser, "open") as browser:
                host.close(timeout=0)
                host.game_ended()
                host.show("http://127.0.0.1/")
                host.close()
                browser.assert_not_called()
            process.kill.assert_called_once()
            native.close.assert_called_once()
            self.assertTrue(host.stopped)


class AppLifecycleTests(unittest.TestCase):
    def new_app(self, root):
        app = App(root)
        self.addCleanup(app.close)
        return app

    def test_close_cancels_jobs_timers_exports_and_rejects_new_jobs(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        app = self.new_app(root)
        started = threading.Event()
        deferred = Mock()
        archive = tempfile.TemporaryFile(dir=root)
        app.downloads.issue(archive, "fixture.zip")
        app.later(0.4, deferred)

        def job(status):
            started.set()
            lifecycle.wait(30)
            (root / "late.txt").write_text("unexpected")

        app.start_job(job)
        self.assertTrue(started.wait(2))
        app.close()
        app.close()
        deferred.assert_not_called()
        self.assertFalse(app.job["busy"])
        self.assertTrue(app.job["cancelled"])
        self.assertTrue(archive.closed)
        self.assertFalse(app.transfers.worker.is_alive())
        self.assertEqual(app.timers, set())
        self.assertFalse((root / "late.txt").exists())
        with self.assertRaises(lifecycle.OperationCancelled):
            app.start_job(lambda _: None)
        with self.assertRaises(lifecycle.OperationCancelled):
            app.action("create", {"name": "Late", "version": "1.20.1", "loader": "vanilla"})

    def test_bounded_close_keeps_shared_lease_until_uninterruptible_worker_returns(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        first = self.new_app(root)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocked(status):
            with lifecycle.guard():
                entered.set()
                release.wait(4)
            lifecycle.check()
            (root / "late.txt").write_text("unexpected")

        first.start_job(blocked)
        self.assertTrue(entered.wait(2))
        began = time.monotonic()
        first.close(timeout=0.04)
        self.assertLess(time.monotonic() - began, 1)
        self.assertFalse(first.coordinator.closed)
        second = self.new_app(root)
        with self.assertRaises(SessionBusy):
            second.action("create", {"name": "Peer", "version": "1.21.1", "loader": "vanilla"})
        release.set()
        eventually(lambda: first.coordinator.closed)
        second.action("create", {"name": "Peer", "version": "1.21.1", "loader": "vanilla"})
        self.assertFalse((root / "late.txt").exists())

    def test_manual_close_leaves_game_and_peer_alive_and_cancels_metrics(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        first, peer = self.new_app(root), self.new_app(root)
        pid = first.store.create("Game", "1.21.1", "vanilla")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(stop_process, child)
        first._track_game(child, first.store.profile(pid))
        first.close()
        self.assertIsNone(child.poll())
        self.assertFalse(peer.closing.is_set())
        self.assertFalse(peer.transfers.scope.event.is_set())
        self.assertFalse(first.metrics.threads)
        with self.assertRaises(ValueError):
            peer.require_profile_idle(pid)
        peer.action("create", {"name": "Other", "version": "1.21.1", "loader": "vanilla"})

    def test_page_beacon_requires_token_and_reload_revokes_pending_close(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        app = self.new_app(root)
        server = Server(("127.0.0.1", 0), Handler)
        server.app = app
        app.shutdown = server.shutdown
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        serving.start()
        self.addCleanup(server.server_close)
        page = "a" * 24

        def send(event, token, origin=None):
            url = f"http://127.0.0.1:{server.server_port}"
            request = Request(url + "/api/window", data=json.dumps({"event": event, "page": page, "token": token}).encode(), headers={"Content-Type": "application/json", "Origin": origin or url})
            with urlopen(request, timeout=2) as reply:
                return json.load(reply)

        with self.assertRaises(HTTPError) as error:
            send("closed", "wrong")
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError):
            send("opened", app.token, "https://example.test")
        send("opened", app.token)
        send("closed", app.token)
        app.page_close = (page, time.monotonic() - 1)
        self.assertTrue(app.window_closed())
        page = "b" * 24
        send("opened", app.token)
        self.assertFalse(app.window_closed())
        app.close()
        serving.join(2)
        self.assertFalse(serving.is_alive())

    def test_manual_x_wins_over_acknowledged_update_handoff_timer(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        app = self.new_app(root)
        app.shutdown = Mock()
        with (patch.object(type(app.updates), "handoff_ready", new_callable=PropertyMock, return_value=True),
              patch.object(app.window, "closed", return_value=True),
              patch.object(app, "request_close") as request):
            app._shutdown_for_update()
            eventually(lambda: request.called)
            request.assert_called_once_with(reason="user")

    def test_fallback_close_beacon_defers_update_until_close_or_reload_is_resolved(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        app = self.new_app(root)
        app.shutdown = Mock()
        page = "d" * 24
        app.page_event("opened", page)
        app.page_event("closed", page)
        callbacks = []
        with (patch.object(type(app.updates), "handoff_ready", new_callable=PropertyMock, return_value=True),
              patch.object(app.window, "closed", return_value=False),
              patch.object(app, "later", side_effect=lambda delay, callback: callbacks.append(callback)),
              patch.object(app, "request_close") as request):
            app._shutdown_for_update()
            callbacks.pop(0)()
            request.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            app.page_event("opened", "e" * 24)
            callbacks.pop(0)()
            request.assert_called_once_with(reason="update")


_PROCESS_FIXTURE = r'''
import json, pathlib, subprocess, sys, time
import main
from launcher import lifecycle

root = pathlib.Path(sys.argv[1])
kind = sys.argv[2]
class TestWindow:
    native = None
    def __init__(self, *a, **k): pass
    def show(self, url): (root / "ready.json").write_text(json.dumps({"url": url}))
    def closed(self): return False
    def page_closed(self): return True
    def game_started(self): pass
    def game_ended(self): (root / "reopened").write_text("unexpected")
    def close(self, **kwargs): pass

OriginalApp = main.App
class FixtureApp(OriginalApp):
    def _initialize(self, root):
        super()._initialize(root)
        self.updates.start_auto = lambda: None
        self.test_profile = self.store.create("Test", "1.21.1", "vanilla")
    def action(self, action, params):
        if action != "fixtureStart": return super().action(action, params)
        def run(status):
            script = "import pathlib,os,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(90)"
            command = [sys.executable, "-c", script, str(root / "child.pid")]
            if kind == "preparation":
                return lifecycle.run(command, timeout=100)
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._track_game(child, self.store.profile(self.test_profile))
            return "started"
        return self.start_job(run, profile=self.test_profile)
main.App, main.DesktopWindow = FixtureApp, TestWindow
sys.argv = ["main.py", "--data", str(root)]
main.run()
'''


class RealProcessClosureTests(unittest.TestCase):
    def exercise(self, kind):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            owner = subprocess.Popen([sys.executable, "-c", _PROCESS_FIXTURE, str(root), kind], cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(stop_process, owner)
            eventually(lambda: (root / "ready.json").exists(), timeout=5)
            url = json.loads((root / "ready.json").read_text())["url"]
            address, token = url.split("#", 1)

            def post(path, body, bearer=False):
                headers = {"Content-Type": "application/json", "Origin": address.rstrip("/")}
                if bearer:
                    headers["Authorization"] = "Bearer " + token
                request = Request(address.rstrip("/") + path, data=json.dumps(body).encode(), headers=headers)
                with urlopen(request, timeout=3) as response:
                    return json.load(response)

            page = "c" * 24
            post("/api/window", {"token": token, "event": "opened", "page": page})
            post("/api/action", {"action": "fixtureStart"}, bearer=True)
            eventually(lambda: (root / "child.pid").exists())
            pid = int((root / "child.pid").read_text())
            try:
                post("/api/window", {"token": token, "event": "closed", "page": page})
                stdout, stderr = owner.communicate(timeout=7)
                self.assertEqual(owner.returncode, 0, stderr.decode(errors="replace"))
                self.assertFalse((root / "reopened").exists())
                if kind == "preparation":
                    self.assertIsNone(process_identity(pid), "owned processor outlived its launcher")
                else:
                    self.assertIsNotNone(process_identity(pid), "closing the launcher killed the game")
            finally:
                if process_identity(pid) is not None:
                    os.kill(pid, signal.SIGTERM)

    def test_real_http_x_exits_launcher_and_owned_preparation_process(self):
        self.exercise("preparation")

    def test_real_http_x_exits_launcher_but_game_survives(self):
        self.exercise("game")


if __name__ == "__main__":
    unittest.main()
