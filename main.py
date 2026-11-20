"""BlockHarbor desktop launcher. Its UI is served exclusively on loopback."""

from __future__ import annotations

import argparse
import hmac
import io
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from launcher import (
    aliases,
    build_archive,
    cards,
    distances,
    e4steam,
    friends,
    lifecycle,
    loader_catalog,
    modrinth,
    preparation,
    room_bridge,
    skin_sync,
    skins,
    steam_network,
    version_ids,
)
from launcher.memory import automatic_heap
from launcher.sessions import SessionBusy, SessionCoordinator, StartupCancelled, WindowStore
from launcher.store import (
    FALLBACK_VERSIONS,
    KINDS,
    MAX_ADDON,
    Store,
    input_profile,
    require_idle_files,
)
from launcher.window import DesktopWindow
from launcher.version import VERSION
from launcher.build_care import BuildCare
from launcher import build_errors
from launcher.diagnostics import SessionMetrics, analyze, report_text
from launcher.experience import Experience
from launcher.performance_profiles import PerformanceProfiles
from launcher.transfers import TransferManager
from launcher.updates import UpdateService, version_key
from launcher.pack_hub import PackHub
from launcher.shared_builds import SharedBuilds

HERE = Path(__file__).resolve().parent
STATIC = {
    "/": "index.html",
    "/app.js": "app.js",
    "/catalog.js": "catalog.js",
    "/compatibility.js": "compatibility.js",
    "/catalog-filters.js": "catalog-filters.js",
    "/style.css": "style.css",
    "/island.js": "island.js",
    "/window.js": "window.js",
    "/icon.png": "icon.png",
    "/icon.svg": "icon.svg",
    "/experience.js": "experience.js",
    "/experience.css": "experience.css",
    "/build-care.js": "build-care.js",
    "/build-care.css": "build-care.css",
    "/updates.js": "updates.js",
    "/updates.css": "updates.css",
    "/pack-hub.js": "pack-hub.js",
    "/pack-hub.css": "pack-hub.css",
}
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


def default_root():
    return (
        Path(os.getenv("LOCALAPPDATA") or (Path.home() / ".local" / "share"))
        / "BlockHarbor"
    )


class App:
    def __init__(self, root):
        self.scope = lifecycle.CancellationScope()
        self.closing = self.scope.event
        self.lifecycle_lock = threading.RLock()
        self.closed = threading.Event()
        self.cleanup_started = False
        self.cleanup_thread = None
        self.close_reason = None
        # A fresh bootstrap passes this only to its own Python child. Consuming
        # it here prevents a later window from replaying an inherited result.
        self.bootstrap_prepared = os.environ.pop("BLOCKHARBOR_BOOTSTRAP_PREPARED", "") == "1"
        self.workers = set()
        self.timers = set()
        self.page = ""
        self.page_close = None
        self.shutdown = None
        self.coordinator = SessionCoordinator(root)
        try:
            self._initialize(root)
            # Opening another window must remain possible during long downloads.
            # Defer filesystem recovery until the current owner releases its lock.
            try:
                with self.coordinator.operation():
                    pass
            except SessionBusy:
                pass
        except BaseException:
            self.close()
            raise

    def _initialize(self, root):
        self.store = WindowStore(root, recover=False)
        self.window = DesktopWindow(root, namespace="window-" + str(self.coordinator.slot))
        # Versions before 1.1 used Microsoft Launcher for the default mode.
        if self.store.get("mode") in ("official", "alias"):
            self.store.set("mode", "local")
        self.downloads = build_archive.Downloads()
        self.token = secrets.token_urlsafe(32)
        self.account = None
        self.auth_generation = 0
        self.lock = threading.RLock()
        self.catalog_lock = threading.Lock()
        self.jobs_lock = threading.Lock()
        self.job = {"id": "", "busy": False, "message": "", "ok": True}
        self.last_seen = time.monotonic()
        self.child = None
        self.games = {}
        self.game_started_at = {}
        self.process_lock = threading.Lock()
        self.game_exit = None
        self.playing_id = ""
        self.playing_name = ""
        self.prism_launches = {}
        self.prism_games = {}
        self.prism_waiting = {}
        self.parked_for_game = False
        self.pending_world_backups = set()
        self.shutdown = None
        self.experience = Experience(self.store)
        self.transfers = TransferManager(self.store.root, namespace=self.coordinator.namespace)
        self.care = BuildCare(self.store, self.transfers, on_clone=self._clone_details, owner=self.coordinator.owner, recover=False)
        self.metrics = SessionMetrics(self.store)
        self.performance = PerformanceProfiles(self.store, self.care.before_change, self.metrics)
        self.packs = PackHub(self.store, self.transfers)
        self.sharing = SharedBuilds(self.store, self.transfers, self.care.before_change, self.profile_running)
        self.updates = UpdateService(self.store, self.transfers, VERSION, can_install=self._can_update, request_shutdown=self._shutdown_for_update,
                                     namespace=self.coordinator.namespace, install_guard=self.coordinator.update_guard, handoff_metadata=self.coordinator.handoff,
                                     cancel_handoff=self.coordinator.cancel_update)
        self.features = (self.care, self.experience, self.performance, self.packs, self.sharing, self.updates)
        self.initial_recovery = False
        self.coordinator.recover = self._recover_shared_state

    def _recover_shared_state(self):
        # Another window could have crashed since this window was opened.
        # Run only after acquiring the cross-process operation lock.
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store._recover_moves(db)
        if self.initial_recovery:
            self.care.recover_pending()
        else:
            self.care._recover()
            self.initial_recovery = True

    def _signal_close(self, reason):
        with self.lifecycle_lock:
            if self.close_reason is not None:
                return False
            self.close_reason = reason if reason in {"user", "update"} else "user"
            for timer in self.timers:
                timer.cancel()
            self.timers.clear()
            self.page_close = None
        self.scope.cancel()
        for component in (getattr(self, "transfers", None), getattr(self, "updates", None)):
            owned = getattr(component, "scope", None)
            if isinstance(owned, lifecycle.CancellationScope):
                owned.cancel()
        return True

    def request_close(self, reason="user"):
        """Immediately stop accepting work; finish cleanup outside HTTP/monitor locks."""
        if not self._signal_close(reason):
            return False
        thread = threading.Thread(target=self.close, name="blockharbor-close", daemon=True)
        thread.start()
        return True

    def close(self, reason="user", timeout=6):
        """Idempotent shutdown of this window's work, leaving live games untouched."""
        self._signal_close(reason)
        with self.lifecycle_lock:
            first = not self.cleanup_started
            if first:
                self.cleanup_started = True
                self.cleanup_thread = threading.current_thread()
            same_thread = self.cleanup_thread is threading.current_thread()
        if not first:
            if not same_thread:
                self.closed.wait(max(0, timeout))
            return
        deadline = time.monotonic() + max(0, timeout)

        def remaining(limit):
            return min(limit, max(0, deadline - time.monotonic()))

        def cleanup(callback):
            try:
                callback()
            except Exception:
                # A vanished temporary directory or failed history write must
                # not skip cancellation of the remaining owned resources.
                pass

        try:
            if self.shutdown:
                # BaseServer.shutdown must run off its serve_forever thread and
                # must never block cleanup when startup failed before serving.
                threading.Thread(target=self.shutdown, name="blockharbor-http-stop", daemon=True).start()
            updates = getattr(self, "updates", None)
            if updates and hasattr(updates, "close"):
                cleanup(lambda: updates.close(reason=self.close_reason, timeout=remaining(2)))
            transfers = getattr(self, "transfers", None)
            if transfers:
                cleanup(lambda: transfers.close(timeout=0))
            metrics = getattr(self, "metrics", None)
            if metrics and hasattr(metrics, "close"):
                cleanup(lambda: metrics.close(timeout=remaining(0.5)))
            downloads = getattr(self, "downloads", None)
            if downloads:
                cleanup(downloads.close)
            window = getattr(self, "window", None)
            if window:
                cleanup(lambda: window.close(timeout=remaining(0.5)))
            getattr(self, "pending_world_backups", set()).clear()
            self.auth_generation = getattr(self, "auth_generation", 0) + 1
            with self.lifecycle_lock:
                workers = tuple(self.workers | self.scope.active_threads())
            for thread in workers:
                if thread is not threading.current_thread():
                    thread.join(remaining(4))
            if transfers:
                cleanup(lambda: transfers.close(timeout=remaining(0.5)))
        finally:
            # An uninterruptible OS DNS/file call may briefly retain a daemon
            # worker. Do not release its file transaction lock under that worker;
            # the process exits normally, or this owner releases it as it returns.
            with self.lifecycle_lock:
                remaining_workers = tuple(t for t in self.workers | self.scope.active_threads() if t is not threading.current_thread() and t.is_alive())
            coordinator = getattr(self, "coordinator", None)
            if coordinator:
                if remaining_workers:
                    def release():
                        for thread in remaining_workers:
                            thread.join()
                        coordinator.close()
                    threading.Thread(target=release, name="blockharbor-session-close", daemon=True).start()
                else:
                    coordinator.close()
            self.closed.set()

    @contextmanager
    def activity(self):
        """Bind HTTP/job work to the current window and track its cleanup."""
        thread = threading.current_thread()
        with self.lifecycle_lock:
            self.scope.check()
            existing = thread in self.workers
            self.workers.add(thread)
        try:
            with lifecycle.bind(self.scope):
                yield
        finally:
            if not existing:
                with self.lifecycle_lock:
                    self.workers.discard(thread)

    def later(self, delay, callback):
        def run():
            try:
                with self.activity():
                    callback()
            except lifecycle.OperationCancelled:
                pass
            finally:
                with self.lifecycle_lock:
                    self.timers.discard(timer)
        timer = threading.Timer(delay, run)
        timer.daemon = True
        with self.lifecycle_lock:
            self.scope.check()
            self.timers.add(timer)
            timer.start()
        return timer

    def page_event(self, event, page):
        if (event not in {"opened", "closed"} or not isinstance(page, str)
                or len(page) != 24 or any(c not in "0123456789abcdef" for c in page)):
            raise ValueError("Некорректное событие окна.")
        with self.lifecycle_lock:
            self.scope.check()
            if event == "opened":
                self.page = page
                self.page_close = None
                self.last_seen = time.monotonic()
            elif page == self.page:
                self.page_close = (page, time.monotonic() + 1.5)
        return {"ok": True}

    def window_closed(self):
        if self.window.closed():
            return True
        with self.lifecycle_lock:
            pending = self.page_close
        return bool(pending and pending[0] == self.page and time.monotonic() >= pending[1]
                    and self.window.page_closed())

    def __del__(self):
        coordinator = getattr(self, "coordinator", None)
        if coordinator:
            coordinator.close()

    def _clone_details(self, source, destination):
        with self.store.connection() as db:
            row = db.execute("SELECT description,group_name,color,cover FROM build_details WHERE profile=?", (source,)).fetchone()
            if row:
                db.execute("INSERT OR REPLACE INTO build_details(profile,description,group_name,color,cover) VALUES(?,?,?,?,?)", (destination, *row))

    def _can_update(self):
        if self.closing.is_set() or self.transfers.active():
            return False
        state = self.coordinator.snapshot()
        return (state["launcherCount"] <= 1 and not state["runningProfiles"] and not self.game_running()
                and not self.job_snapshot()["busy"] and not any(p.poll() is None for p in self.prism_launches.values()))

    def check_updates_in_background(self):
        if self.closing.is_set():
            return
        if self.store.get("checkUpdatesOnStart", "1") != "1" or 0 <= time.time() - self.updates.checked < 24 * 3600:
            return
        try:
            self.updates.check()
            if self.update_available():
                self.experience.notify("Доступен BlockHarbor " + self.update_available() + ". Открой «Настройки → Загрузки и обновления», чтобы посмотреть изменения.", "info")
        except (ValueError, OSError):
            pass  # Details remain in the download centre; offline launch still works.

    def update_available(self):
        release = getattr(getattr(self, "updates", None), "release", None)
        return release["version"] if release and version_key(release["version"]) > version_key(VERSION) else ""

    def _shutdown_for_update(self):
        if not self.shutdown:
            raise ValueError("Перезапусти установленное приложение для обновления.")
        self.experience.notify("Управление передано установщику BlockHarbor.", "info")
        # Give the HTTP response and helper handshake time to finish.
        def finish():
            if self.closing.is_set() or not self.updates.handoff_ready:
                return
            if self.window.closed():
                self.request_close(reason="user")
                return
            with self.lifecycle_lock:
                leaving_page = self.page_close is not None
            if leaving_page:
                # Give reload's new opened event a chance to arrive. A real
                # fallback X is handled by the monitor as a user cancellation.
                self.later(0.2, finish)
                return
            self.request_close(reason="update")
        self.later(0.75, finish)

    def require_profile_idle(self, pid):
        self.store.profile(pid)
        if pid in self.care.recovery_errors:
            raise ValueError(self.care.recovery_errors[pid])
        prism = self.prism_launches.get(pid)
        if self.profile_running(pid) or (prism is not None and prism.poll() is None):
            raise ValueError("Закрой эту сборку в Minecraft или Prism во всех окнах перед изменением файлов.")
        require_idle_files(self.store.folder(pid))

    def require_mutations_allowed(self):
        self.scope.check()
        if self.updates.pending and self.updates.pending.get("stage") in ("preparing", "launching"):
            raise ValueError("Подготавливается обновление BlockHarbor. Дождись установщика или отмени подготовку в центре загрузок.")

    def feature_action(self, action, params, *, read=False):
        self.scope.check()
        service = next((item for item in self.features if action in item.ACTIONS), None)
        reads = getattr(service, "READ_ACTIONS", {"updateState"} if service is self.updates else set())
        # Read-only queries, cancel controls and the atomic favorite flag stay
        # responsive during downloads without acquiring the game-files lock.
        concurrent = {"diagnostics", "packPreview", "packDiscard", "shareCreate", "shareInvite", "shareInspect", "shareCompare",
                      "transferCancel", "transferClear", "updateCancelInstall", "updateAutomatic", "updateState", "experienceFavorite"}
        if read or action in reads | concurrent:
            return self._feature_action(action, params, read=read)
        with self.coordinator.operation():
            return self._feature_action(action, params, read=read)

    def _feature_action(self, action, params, *, read=False):
        if action == "diagnostics":
            failure = self.game_exit if (self.game_exit or {}).get("profile") == params["id"] else None
            result = analyze(self.store, params["id"], (failure or {}).get("code"))
            result["report"] = report_text(self.store, result)
            return result
        service = next((item for item in self.features if action in item.ACTIONS), None)
        if service is None:
            raise ValueError("Действие не найдено.")
        reads = getattr(service, "READ_ACTIONS", {"updateState"} if service is self.updates else set())
        if read and action not in reads:
            raise ValueError("Это действие требует запроса на изменение.")
        # Downloads are independent of Minecraft and must remain cancellable.
        concurrent = reads | {"updateDownload", "updateCancelInstall", "updateAutomatic", "transferCancel", "transferRetry", "transferClear", "updateChannel", "updateInstall", "updateRollback", "packPreview", "packDiscard", "shareCreate", "shareInvite", "shareInspect", "shareCompare", "experiencePreferences", "experienceClearNotifications", "experienceFavorite"}
        if action in reads or action in {"packPreview", "shareCreate", "shareInvite", "shareInspect", "shareCompare"}:
            return service.handle(action, params)
        with self.lock:
            self.scope.check()
            if action not in {"updateCancelInstall", "updateAutomatic", "transferCancel", "transferClear"}:
                self.require_mutations_allowed()
            if self.job_snapshot()["busy"] and action not in concurrent:
                raise ValueError("Дождись завершения текущей операции.")
            pid = params.get("id")
            if service in (self.care, self.performance, self.sharing) and pid and action not in reads | {"careCheckUpdates", "carePin", "careRetention"}:
                self.require_profile_idle(pid)
            if action in service.JOBS:
                guarded = pid if service in (self.care, self.performance, self.sharing) and action != "careCheckUpdates" else None
                return self.start_job(lambda status: service.handle(action, params, status), profile=guarded)
            result = service.handle(action, params)
            if action not in concurrent and not (isinstance(result, dict) and result.get("review")):
                self.experience.notify((result or {}).get("message", "Изменения сохранены."), "success")
            return result

    def state(self):
        s = self.store
        settings = s.settings()
        pid = settings.get("active")
        job = self.ui_job_snapshot()
        profiles = s.all()
        details = self.experience.metadata()
        for profile in profiles:
            profile.update({key: value for key, value in details.get(profile["id"], {}).items() if key != "profile"})
            profile["steamNoticeAcknowledged"] = e4steam.notice_acknowledged(
                settings, profile["id"], profile["version"], profile["loader"]
            )
        active = next(
            (profile for profile in profiles if profile["id"] == pid),
            profiles[0] if profiles else None,
        )
        pid = active["id"] if active else ""
        version, loader = (active["version"], active["loader"]) if active else ("", "")
        supported = bool(active and e4steam.support(version, loader))
        content = s.content(pid) if active else {kind: [] for kind in KINDS}
        folder = s.folder(pid) if active else None
        ready = supported and e4steam.installed(
            folder, version, loader, sizes={row["size"] for row in content["mods"]}
        )
        managed = skin_sync.managed_files(folder, version, loader, content["mods"])
        if ready:
            managed.update(e4steam.protected_names(version, loader))
        if folder and room_bridge.installed(folder):
            managed.add(room_bridge.FILENAME)
        for entry in content["mods"]:
            entry["managed"] = entry["name"] in managed
        configured = settings.get("memory", "auto")
        estimated = automatic_heap()
        return {
            "appVersion": VERSION,
            "experience": {"preferences": self.experience.preferences()},
            "profiles": profiles,
            "deletedProfiles": s.deleted(),
            "active": pid,
            "contents": content,
            "steam": {
                "supported": supported,
                "version": version,
                "loader": loader,
                "canContinue": True,
                "acknowledged": bool(active and active["steamNoticeAcknowledged"]),
                "message": e4steam.compatibility(version, loader)["message"] if active else "Сначала создай или выбери сборку.",
                "installed": ready,
                "canEnable": loader == "vanilla" and e4steam.support(version, "fabric"),
                "reason": ""
                if supported
                else (
                    "Для Steam нужен загрузчик модов."
                    if loader == "vanilla"
                    else "Для этой версии и загрузчика нет совместимого комплекта e4steam и Public Servers."
                ),
            },
            "skinNetwork": skin_sync.support(version, loader),
            "skinSupport": bool(aliases.skin_release(version, loader)),
            "canUseFabricSkin": loader == "vanilla"
            and bool(aliases.skin_release(version, "fabric")),
            "realName": settings.get("realName", ""),
            "nickname": settings.get("nickname", ""),
            "model": settings.get("model", "classic"),
            "skin": self.skin_path(settings.get("skinDigest", "")).is_file(),
            "account": self.account["name"] if self.account else "",
            "memory": estimated if configured == "auto" else int(configured),
            "autoMemory": configured == "auto",
            "distanceSettings": bool(active and distances.supported(version)),
            "recommendedMemory": estimated,
            "clientId": settings.get("clientId", ""),
            "versions": self.release_catalog(settings.get("versions", "[]")),
            "job": job,
            "mode": settings.get("mode", "local"),
            "gameRunning": job["gameRunning"],
            "gameExit": job["gameExit"],
            "localGameRunning": job["localGameRunning"],
            "runningProfiles": job["runningProfiles"],
            "launcherCount": job["launcherCount"],
        }

    def release_catalog(self, raw=None):
        cached = json.loads(self.store.get("versions", "[]") if raw is None else raw)
        # Older installations may only have cached a subset of today's releases.
        if cached and set(FALLBACK_VERSIONS).issubset(cached):
            return cached
        newer = [v for v in cached if v not in FALLBACK_VERSIONS]
        return list(dict.fromkeys(newer + FALLBACK_VERSIONS))

    def version_catalog(self):
        bundled = json.loads((HERE / "launcher" / "minecraft_versions.json").read_text(encoding="utf-8"))
        try:
            cached = json.loads(self.store.get("versionCatalog", "[]"))
        except ValueError:
            cached = []
        if not isinstance(cached, list):
            cached = []
        merged = {}
        for row in cached + bundled:
            if isinstance(row, dict) and version_ids.valid(row.get("id")) and row.get("type") in {"release", "snapshot", "old_beta", "old_alpha"}:
                merged.setdefault(row["id"], {"id": row["id"], "type": row["type"]})
        return list(merged.values())

    def refresh_releases(self, *, force=False):
        from launcher import game

        with self.catalog_lock, lifecycle.bind(self.scope):
            lifecycle.check()
            updated = float(self.store.get("releaseCatalogUpdated", "0"))
            if not force and 0 <= time.time() - updated < 24 * 3600:
                return self.release_catalog()
            catalog = game.version_catalog()
            releases = [row["id"] for row in catalog if row["type"] == "release"]
            if not releases:
                raise ValueError("Каталог Minecraft вернул пустой список.")
            with self.store.connection() as db:
                db.executemany(
                    "REPLACE INTO settings VALUES (?, ?)",
                    [
                        ("versions", json.dumps(releases)),
                        ("versionCatalog", json.dumps(catalog)),
                        ("releaseCatalogUpdated", str(time.time())),
                    ],
                )
            return self.release_catalog()

    def skin_path(self, digest=None):
        if digest is None:
            digest = self.store.get("skinDigest")
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            return self.store.root / "skins" / (digest + ".png")
        return self.store.root / "skin.png"

    def _track_game(self, process, profile, *, prism=False):
        with self.process_lock:
            if prism:
                self.prism_games[profile["id"]] = (process, profile["name"])
            else:
                self.games[profile["id"]] = (process, profile["name"])
            self.coordinator.attach(profile["id"], process)
            self.game_exit = None
            self.game_started_at[profile["id"]] = time.time()
            if self.closing.is_set():
                return
            if type(getattr(process, "pid", None)) is int:
                self.metrics.start(profile["id"], process)
            if process.poll() is None and not self.parked_for_game:
                self.window.game_started()
                self.parked_for_game = True

    def _prism_waiting(self, pid, process):
        with self.process_lock:
            self.prism_waiting[pid] = process
            self.coordinator.attach(pid, process, stage="prism")

    def _prism_done(self, pid):
        with self.process_lock:
            self.prism_waiting.pop(pid, None)
            if self.closing.is_set():
                return
            if pid not in self.prism_games:
                self.prism_launches.pop(pid, None)
                self.coordinator.release(pid)

    def _game_finished(self, code, name, pid=""):
        if pid:
            started = self.game_started_at.pop(pid, None)
            if code and not self.closing.is_set():
                try:
                    build_errors.record_exit(self.store, pid, code, started)
                except (OSError, ValueError, sqlite3.Error):
                    pass  # Always finish process cleanup even if the journal is unavailable.
            self.coordinator.release(pid)
            self.metrics.finish(pid, code)
            if self.store.get("careAutoWorldBackup:" + pid) == "1":
                self.pending_world_backups.add(pid)
        if code:
            self.game_exit = {
                "id": secrets.token_hex(6),
                "code": code,
                "profile": pid,
                "message": f"Сборка «{name}» завершилась с кодом {code}. Подробности в разделе «Ошибки сборки».",
            }
            self.experience.notify(self.game_exit["message"], "error")

    def background_maintenance(self):
        # Called by the session monitor after game process state has been observed.
        # Backup I/O lives in the normal worker, never in the state/poll endpoint.
        if self.closing.is_set():
            return
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.pending_world_backups and not self.job_snapshot()["busy"] and not self.game_running():
                pid = self.pending_world_backups.pop()
                if any(p["id"] == pid for p in self.store.all()):
                    if not self.profile_running(pid):
                        self.start_job(lambda status: self.care.backup_after_game(pid, status) or "Копии миров актуальны.", profile=pid)
                    else:
                        self.pending_world_backups.add(pid)
        finally:
            self.lock.release()
        if not self.closing.is_set():
            self.updates.tick_auto()
            self.updates.monitor_pending()

    def process_state(self):
        with self.process_lock:
            if self.closing.is_set():
                processes = [self.child] if self.child is not None else []
                processes += [row[0] for row in self.games.values()]
                processes += [row[0] for row in self.prism_games.values()]
                return {"gameRunning": any(process.poll() is None for process in processes), "gameExit": self.game_exit}
            if self.child is not None:
                code = self.child.poll()
                if code is not None:
                    self._game_finished(code, self.playing_name, self.playing_id)
                    self.child = None
            for pid, (process, name) in list(self.games.items()):
                code = process.poll()
                if code is not None:
                    self._game_finished(code, name, pid)
                    del self.games[pid]
            for pid, (process, name) in list(self.prism_games.items()):
                code = process.poll()
                if code is not None:
                    self._game_finished(code, name, pid)
                    del self.prism_games[pid]
                    self.prism_launches.pop(pid, None)
            for pid, process in list(self.prism_waiting.items()):
                if process.poll() is not None:
                    del self.prism_waiting[pid]
                    if pid not in self.prism_games:
                        self.coordinator.release(pid)
            running = self.child is not None or bool(self.games) or bool(self.prism_games)
            if not running and self.parked_for_game:
                self.parked_for_game = False
                self.last_seen = time.monotonic()
                self.window.game_ended()
            return {
                "gameRunning": running,
                "gameExit": self.game_exit,
            }

    def game_running(self):
        return self.process_state()["gameRunning"]

    def profile_running(self, pid):
        self.process_state()
        with self.process_lock:
            return (
                self.child is not None and self.playing_id == pid
            ) or pid in self.games or pid in self.prism_games or pid in self.coordinator.snapshot()["runningProfiles"]

    def player_name(self):
        if self.store.get("mode", "local") == "local":
            return self.store.get("nickname") or "Player"
        return (self.account or {}).get("name") or self.store.get("realName")

    def requested_nickname(self, params):
        # An omitted value reuses the player's saved default. An explicitly
        # empty value is invalid, rather than silently launching as someone else.
        value = params.get("nickname", self.store.get("nickname") or "Player")
        return skins.validate_nickname(value if isinstance(value, str) else "")

    def job_snapshot(self):
        with self.jobs_lock:
            process = self.process_state()
            sessions = self.coordinator.snapshot()
            if self.child is not None and self.playing_id:
                sessions["runningProfiles"] = sorted(set(sessions["runningProfiles"]) | {self.playing_id})
            return {**self.job, **process, **sessions, "localGameRunning": process["gameRunning"], "updateAvailable": self.update_available()}

    def ui_job_snapshot(self):
        # UpdateService may call _can_update while holding its own lock. Read its
        # UI state only after job_snapshot releases jobs_lock, and never from
        # the internal job_snapshot used by that installation guard.
        snapshot = self.job_snapshot()
        updates = self.updates.state()
        keys = (
            "current", "channel", "autoEnabled", "autoChecking", "checkedAt",
            "available", "ready", "pending", "error", "checkStatus", "startupResult",
        )
        progress = {key: updates.get(key) for key in keys}
        release = updates.get("release")
        progress["release"] = {key: release.get(key) for key in ("version", "compatible")} if release else None
        tasks = [row for row in updates.get("transfers", {}).get("tasks", [])
                 if row.get("category") == "launcher-update"]
        active = {"queued", "downloading", "verifying"}
        progress["transfers"] = {"tasks": [row for row in tasks if row.get("state") in active]
                                 + [row for row in tasks if row.get("state") not in active][:6]}
        snapshot["updater"] = progress
        snapshot["startupNotice"] = ({"id": self.coordinator.session + ":bootstrap", "ok": True,
                                      "message": "Компоненты BlockHarbor подготовлены. Лаунчер готов к работе."}
                                     if self.bootstrap_prepared else None)
        return snapshot

    def start_job(self, fn, *, profile=None):
        self.require_mutations_allowed()
        with self.jobs_lock:
            if self.job["busy"]:
                raise ValueError("Дождись завершения текущей операции.")
            jid = secrets.token_hex(5)
            self.job = {"id": jid, "busy": True, "ok": True, "message": "Подготовка…"}

        def status(msg):
            self.scope.check()
            with self.jobs_lock:
                self.job["message"] = str(msg)[:700]

        def worker():
            try:
                with self.activity():
                    with self.coordinator.operation(wait=True, status=status):
                        self.scope.check()
                        if profile:
                            self.require_profile_idle(profile)
                        result = fn(status)
                final = {"message": result or "Готово.", "ok": True, "busy": False}
            except lifecycle.OperationCancelled:
                final = {"message": "Лаунчер закрыт. Операция остановлена.", "ok": False, "busy": False, "cancelled": True}
            except modrinth.NeedsReview as exc:
                final = {
                    "message": str(exc),
                    "ok": False,
                    "busy": False,
                    "review": exc.review,
                }
            except Exception as exc:
                if profile and not self.closing.is_set():
                    try:
                        build_errors.record_operation(self.store, profile, self.job["message"], exc)
                    except (OSError, ValueError, sqlite3.Error):
                        pass  # Preserve the operation error if its history cannot be written.
                final = {
                    "message": str(exc)[:1000] or "Операция не удалась.",
                    "ok": False,
                    "busy": False,
                }
            try:
                if not self.closing.is_set():
                    self.experience.notify(final["message"], "success" if final["ok"] else "error")
            except (OSError, sqlite3.Error):
                pass  # A history failure must not hide the operation's result.
            with self.jobs_lock:
                self.job.update(final)

        def owned_worker():
            try:
                # Keep completion/notification cleanup inside the owned lifetime,
                # not just the main file transaction above.
                with self.activity():
                    worker()
            except lifecycle.OperationCancelled:
                with self.jobs_lock:
                    self.job.update(message="Лаунчер закрыт. Операция остановлена.", ok=False, busy=False, cancelled=True)

        with self.lifecycle_lock:
            self.scope.check()
            thread = threading.Thread(target=owned_worker, daemon=True, name="blockharbor-operation")
            thread.start()
        return {"job": jid}

    def save_appearance(self, params, uploaded):
        store = self.store
        model = params.get("model", store.get("model", "classic"))
        skins._check_model(model)
        if params.get("skinDestination") == "java":
            current = self.skin_path()
            if not uploaded and not current.is_file():
                raise ValueError("Сначала выбери PNG со скином игрока.")
            pixels = skins.appearance_skin(uploaded or current, model)

            def save_for_java(status):
                digest = skins.save_texture(store.root, pixels)
                store.set_many({"skinDigest": digest, "model": model})
                return "PNG и модель сохранены. Теперь можно установить скин в Java-профиле."

            return self.start_job(save_for_java)

        nick = self.requested_nickname(params)
        mode = store.get("mode", "local")
        real = (self.account or {}).get("name") or params.get(
            "realName", store.get("realName")
        )
        if real:
            real = skins.validate_nickname(real)
        target = nick if mode == "local" else real
        current = self.skin_path()
        pixels = (
            skins.appearance_skin(uploaded or current, model)
            if uploaded or current.is_file()
            else None
        )
        if params.get("skinDestination") == "profile":
            # Player identity belongs to the launcher profile. Saving it must
            # work before any build exists and while a different game is open.
            # Compatible builds receive the saved skin during their next launch.
            def save_player(status):
                values = {"nickname": nick, "realName": real or "", "model": model}
                if pixels:
                    values["skinDigest"] = skins.save_texture(store.root, pixels)
                store.set_many(values)
                return "Успешно сохранено"

            return self.start_job(save_player)

        profile = store.profile(params["id"])
        version, loader = profile["version"], profile["loader"]
        convert = bool(
            pixels
            and loader == "vanilla"
            and params.get("fabric") == "1"
            and aliases.skin_release(version, "fabric")
        )
        if convert:
            loader = "fabric"
        apply_skin = bool(pixels and aliases.skin_release(version, loader))
        if uploaded and not apply_skin:
            raise ValueError(
                f"Для скина выбери совместимую сборку с загрузчиком модов. Текущая сборка: {version} · {loader}."
            )
        if apply_skin and not target:
            raise ValueError("Укажи имя своего профиля Minecraft для установки скина.")
        if self.profile_running(profile["id"]) and (apply_skin or convert):
            raise ValueError(
                "Закрой Minecraft и ещё раз сохрани параметры, чтобы игра загрузила новый скин."
            )
        folder = store.folder(profile["id"])

        def save(status):
            values = {"nickname": nick, "realName": real or "", "model": model}
            if apply_skin:
                status("Применяем скин в сборке…")
                aliases.ensure_skin_support(folder, version, loader)
                values["skinDigest"] = skins.save_texture(store.root, pixels)
            if (
                mode != "local"
                and target
                and (version, loader) == ("1.20.1", "fabric")
                and (
                    target != nick or (folder / "config" / "nameprotect.json").is_file()
                )
            ):
                aliases.apply_alias(folder, target, nick)
            if apply_skin:
                skins.install_local_skin(pixels, folder, target, model)
                skin_sync.prepare(folder, version, loader, pixels, model)
            with store.connection() as db:
                if convert:
                    row = db.execute(
                        "UPDATE profiles SET loader='fabric' WHERE id=? AND loader='vanilla'",
                        (profile["id"],),
                    )
                    if row.rowcount != 1:
                        raise ValueError(
                            "Сборка изменилась во время сохранения. Открой профиль заново."
                        )
                db.executemany("REPLACE INTO settings VALUES (?, ?)", values.items())
            store.remember(values)
            return "Успешно сохранено"

        return self.start_job(save, profile=profile["id"])

    def import_card(self, src, pid):
        profile = self.store.profile(pid)
        if (profile["version"], profile["loader"]) != ("1.20.1", "fabric"):
            raise ValueError("Карточки игроков поддерживают сборки Fabric 1.20.1.")
        cards.read_card(src)
        payload = src.read_bytes()
        own_name = self.player_name()
        folder = self.store.folder(pid)

        def apply(status):
            status("Применяем карточку игрока и проверяем компоненты сборки…")
            with tempfile.TemporaryDirectory(dir=self.store.root) as td:
                card = Path(td) / "friend.bhplayer"
                card.write_bytes(payload)
                return cards.apply_card(card, folder, own_name=own_name)

        return self.start_job(apply, profile=pid)

    def action(self, action, p):
        self.scope.check()
        if action == "diagnostics" or any(action in service.ACTIONS for service in self.features):
            return self.feature_action(action, p)
        # Window-specific selection can change while another window prepares a
        # different build. Every filesystem mutation uses the shared operation lock.
        if action in {"select", "logout", "folder", "openSteam"}:
            return self._action(action, p)
        with self.coordinator.operation():
            return self._action(action, p)

    def _action(self, action, p):
        s = self.store
        if action == "diagnostics" or any(action in service.ACTIONS for service in self.features):
            return self.feature_action(action, p)
        self.require_mutations_allowed()
        if action in {"remove", "enableSteam", "skinInstall", "aliasInstall", "prepareSteam", "repairSteamSettings"}:
            if self.job_snapshot()["busy"]:
                raise ValueError("Дождись завершения текущей операции.")
            self.require_profile_idle(p["id"])
        if action == "gameDistances":
            profile = s.profile(p["id"])
            folder = s.folder(p["id"])
            if "expected" in p:
                prism = self.prism_launches.get(p["id"])
                if (
                    self.profile_running(p["id"])
                    or (prism and prism.poll() is None)
                    or self.job_snapshot()["busy"]
                ):
                    raise ValueError(
                        "Закрой Minecraft и дождись завершения операции перед изменением дальности."
                    )
                require_idle_files(folder)
                self.care.before_change(profile["id"], "Изменение дальности в игре")
                distances.apply(folder, profile["version"], p["expected"])
            return distances.preview(folder, profile["version"])
        if action == "installModrinth":
            pid = p["id"]
            s.profile(pid)
            self.require_profile_idle(pid)
            if "items" in p:
                selected = modrinth._selections(p["items"])
                request = {"id": pid, "items": selected}
            else:
                request = {key: p[key] for key in ("id", "kind", "project", "version")}
                selected = [request]
            return self.start_job(
                lambda status: modrinth.install_selection(
                    s, pid, selected, status, self.lock, p.get("acknowledgement", ""),
                    before_publish=lambda: self.care.before_change(pid, "Установка дополнений"),
                    transfer=self.transfers,
                    after_publish=lambda releases: [self.care.remember_project(pid, release, {"title": release.title, "description": release.description}) for release in releases],
                    request=request,
                ), profile=pid
            )
        if action == "create":
            name, version, loader = input_profile(
                p.get("name"), p.get("version"), p.get("loader")
            )
            allow_without_steam = e4steam.notice_permission(p)
            if self.job_snapshot()["busy"]:
                raise ValueError("Дождись завершения текущей операции.")
            loader_catalog.require_supported(s.root / "game-cache", version, loader)
            if e4steam.support(version, loader):

                def create_with_steam(status):
                    def prepare(folder):
                        status("Готовим компоненты для игры с друзьями…")
                        e4steam.ensure_if_supported(
                            folder,
                            version,
                            loader,
                            status,
                            cache_dir=s.root / "steam-mod-cache",
                        )

                        skin_sync.prepare(folder, version, loader)

                    s.create(name, version, loader, prepare=prepare)
                    return "Сборка создана. Игра с друзьями готова."

                return self.start_job(create_with_steam)
            pid = s.create(name, version, loader)
            if allow_without_steam:
                e4steam.acknowledge_notice(s, pid, version, loader)
        elif action == "purgeBuild":
            pid = p["id"]

            def purge(status):
                status("Безвозвратно удаляем файлы сборки…")
                s.purge_profile(pid)
                self.care.purge(pid)
                with s.connection() as db:
                    db.execute("DELETE FROM build_details WHERE profile=?", (pid,))
                    db.execute("DELETE FROM settings WHERE key=?", ("steamNotice:" + pid,))
                return "Файлы сборки удалены с диска."

            return self.start_job(purge)
        elif action in ("deleteBuild", "restoreBuild"):
            pid = p["id"]
            if self.job_snapshot()["busy"]:
                raise ValueError("Дождись завершения текущей операции и повтори.")
            if action == "deleteBuild":
                prism = self.prism_launches.get(pid)
                if self.profile_running(pid) or (
                    prism is not None and prism.poll() is None
                ):
                    raise ValueError(
                        "Закрой эту сборку в Minecraft или Prism перед удалением."
                    )
            s.move_profile(pid, restore=action == "restoreBuild")
            if action == "deleteBuild":
                self.prism_launches.pop(pid, None)
        elif action == "select":
            s.profile(p["id"])
            s.set("active", p["id"])
        elif action == "remove":
            if p["kind"] == "mods":
                profile = s.profile(p["id"])
                version, loader = profile["version"], profile["loader"]
                if p["name"] == room_bridge.FILENAME and room_bridge.installed(s.folder(p["id"])):
                    raise ValueError("Этот компонент передаёт лаунчеру состояние комнаты Steam.")
                if skin_sync.managed_file(
                    s.folder(p["id"]), version, loader, p["name"]
                ):
                    raise ValueError(
                        "Этот файл нужен для передачи скинов участникам мира."
                    )
                if p["name"] in e4steam.protected_names(
                    version, loader
                ) and e4steam.installed(s.folder(p["id"]), version, loader):
                    raise ValueError(
                        "Этот файл входит в поддержку Steam выбранной сборки."
                    )
            self.care.before_change(p["id"], "Удаление дополнения " + p["name"])
            s.remove(p["id"], p["kind"], p["name"])
        elif action == "saveNickname":
            nick = self.requested_nickname(p)
            s.set("nickname", nick)
            return {"ok": True, "nickname": nick, "message": "Успешно сохранено"}
        elif action == "saveProfile":
            nick = self.requested_nickname(p)
            model = p.get("model")
            if model not in ("classic", "slim"):
                raise ValueError("Неизвестная модель.")
            real = p.get("realName", "").strip()
            if real:
                skins.validate_nickname(real)
            s.set_many({"realName": real, "nickname": nick, "model": model})
        elif action == "settings":
            import uuid

            client_id = p.get("clientId", "").strip()
            if client_id:
                try:
                    client_id = str(uuid.UUID(client_id))
                except ValueError:
                    raise ValueError("Client ID должен быть в формате UUID.") from None
            mode = p.get("mode", "local")
            if mode not in ("local", "direct", "prism"):
                raise ValueError("Неизвестный режим запуска.")
            if mode == "direct" and not client_id:
                raise ValueError(
                    "Введи Client ID для входа через собственное приложение Microsoft."
                )
            memory = p.get("memory", s.get("memory", "auto"))
            if memory != "auto":
                memory = int(memory)
                if not 1024 <= memory <= 32768:
                    raise ValueError("Можно выделить от 1024 до 32768 МБ памяти.")
            changed_client = client_id != s.get("clientId")
            s.set_many({"memory": str(memory), "clientId": client_id, "mode": mode})
            if changed_client:
                self.auth_generation += 1
                self.account = None
        elif action == "logout":
            self.auth_generation += 1
            self.account = None
        elif action in ("skinInstall", "aliasInstall"):
            target = skins.validate_nickname(p.get("target", ""))
            profile = s.profile(p["id"])
            folder = s.folder(p["id"])
            starter = profile["version"] == "1.20.1" and profile["loader"] == "fabric"
            nickname = s.get("nickname")
            if action == "aliasInstall":
                if not starter:
                    raise ValueError(
                        "Автоустановка псевдонимов поддерживается для Fabric 1.20.1."
                    )
                skins.validate_nickname(nickname)
            elif not self.skin_path().is_file():
                raise ValueError("Сначала выбери PNG-скин.")

            def apply_local(status):
                if action == "aliasInstall":
                    status("Проверяем и устанавливаем компоненты отображения ника…")
                    aliases.apply_alias(folder, target, nickname)
                    return (
                        "Ник применён в сборке «"
                        + profile["name"]
                        + "». Перезапусти Minecraft."
                    )
                status("Проверяем и устанавливаем CustomSkinLoader…")
                aliases.ensure_skin_support(
                    folder, profile["version"], profile["loader"]
                )
                skins.install_local_skin(
                    self.skin_path(), folder, target, s.get("model", "classic")
                )
                skin_sync.prepare(
                    folder,
                    profile["version"],
                    profile["loader"],
                    self.skin_path(),
                    s.get("model", "classic"),
                )
                return (
                    "Скин применён в сборке «"
                    + profile["name"]
                    + "». Перезапусти Minecraft."
                )

            return self.start_job(apply_local, profile=p["id"])
        elif action == "versions":

            def versions(status):
                status("Загружаем список релизов Minecraft…")
                rows = self.refresh_releases(force=True)
                return f"Доступно релизов: {len(rows)}."

            return self.start_job(versions)
        elif action == "signin":
            client = s.get("clientId")
            if not client:
                raise ValueError("Укажи одобренный Client ID в настройках.")
            generation = self.auth_generation

            def signin(status):
                from launcher import game

                account = game.login(client, status=status)
                with self.lock:
                    if generation != self.auth_generation:
                        raise ValueError(
                            "Настройки входа изменились. Войди в выбранный профиль ещё раз."
                        )
                    self.account = account
                return "Вход выполнен: " + account["name"]

            return self.start_job(signin)
        elif action in ("uploadSkin", "renameOnline"):
            if not self.account:
                raise ValueError("Нужен вход Microsoft во вкладке настроек.")
            session = self.account
            image = self.skin_path()
            nick = s.get("nickname")
            model = s.get("model", "classic")
            if action == "renameOnline":
                skins.validate_nickname(nick)

            def apply_online(status):
                status("Отправляем изменение в Minecraft…")
                if action == "renameOnline":
                    skins.change_name(nick, session["access_token"])
                    session["name"] = nick
                    return "Имя Java-профиля изменено."
                skins.upload_skin(image, model, session["access_token"])
                return "Скин загружен в профиль Minecraft. Перезайди в игру."

            return self.start_job(apply_online)
        elif action == "openSteam":
            return {"ok": True, "steamAction": friends.open_steam()}
        elif action in ("prepareSteam", "repairSteamSettings"):
            pid = p["id"]
            profile = s.profile(pid)
            version, loader = profile["version"], profile["loader"]
            if not e4steam.support(version, loader):
                raise ValueError("Выбери совместимую сетевую сборку.")
            prism = self.prism_launches.get(pid)
            if self.profile_running(pid) or (
                prism is not None and prism.poll() is None
            ):
                raise ValueError("Закрой игру и повтори подготовку Steam.")
            folder = s.folder(pid)

            def prepare_steam(status):
                if action == "repairSteamSettings":
                    status("Проверяем настройки открытия мира…")
                    return steam_network.repair_settings(folder)
                e4steam.ensure(
                    folder,
                    version,
                    loader,
                    status,
                    cache_dir=s.root / "steam-mod-cache",
                )
                return "Файлы для игры через Steam готовы."

            return self.start_job(prepare_steam, profile=pid)
        elif action == "enableSteam":
            profile = s.profile(p["id"])
            if profile["loader"] != "vanilla" or not e4steam.support(
                profile["version"], "fabric"
            ):
                raise ValueError("Переход на Fabric для этой сборки недоступен.")
            if self.profile_running(profile["id"]):
                raise ValueError("Закрой Minecraft перед изменением загрузчика.")
            folder = s.folder(profile["id"])

            def enable(status):
                self.care.before_change(profile["id"], "Переход на Fabric для Steam")
                e4steam.ensure(
                    folder,
                    profile["version"],
                    "fabric",
                    status,
                    cache_dir=s.root / "steam-mod-cache",
                )
                skin_sync.prepare(folder, profile["version"], "fabric")
                s.enable_fabric(profile["id"])
                return f"Сборка «{profile['name']}» готова для Steam. Нажми «Играть»."

            return self.start_job(enable, profile=profile["id"])
        elif action == "play":
            from launcher import game

            if self.updates.pending and (not self.updates.pending.get("automatic") or self.updates.pending.get("stage") in {"preparing", "launching"}):
                raise ValueError("Запланировано обновление лаунчера. Отмени его в «Настройки → Загрузки и обновления» перед новым запуском игры.")

            pro = s.profile(p["id"])
            allow_without_steam = e4steam.notice_permission(p)
            if pro["id"] in self.care.recovery_errors:
                raise ValueError(self.care.recovery_errors[pro["id"]])
            folder = s.folder(p["id"])
            configured_memory = s.get("performanceMemory:" + p["id"], s.get("memory", "auto"))
            memory = automatic_heap() if configured_memory == "auto" else int(configured_memory)
            mode = s.get("mode", "local")
            if self.profile_running(pro["id"]):
                raise ValueError(
                    "Эта сборка уже запущена. Для ещё одного Minecraft выбери другую сборку или заранее сделай отдельную копию этой сборки."
                )
            session = self.account
            if mode == "direct" and not session:
                raise ValueError("Войди в Microsoft в настройках выбранного режима.")
            nick = ""
            if mode == "local":
                nick = self.requested_nickname(p)

            def play(status):
                self.prism_launches.pop(pro["id"], None)
                self.coordinator.claim(pro["id"])
                try:
                    return launch_claimed(status)
                finally:
                    # Preparation errors and failed spawns must not keep a build busy.
                    with self.process_lock:
                        retained = pro["id"] in self.games or pro["id"] in self.prism_games or pro["id"] in self.prism_launches
                    if not retained:
                        self.coordinator.release(pro["id"])

            def launch_claimed(status):
                if allow_without_steam:
                    e4steam.acknowledge_notice(s, pro["id"], pro["version"], pro["loader"])
                if mode == "local":
                    s.set("nickname", nick)
                self.metrics.prepare(pro["id"])
                image = self.skin_path()
                target = nick if mode == "local" else self.player_name()
                if (
                    image.is_file()
                    and target
                    and aliases.skin_release(pro["version"], pro["loader"])
                ):
                    status("Применяем скин персонажа…")
                    aliases.ensure_skin_support(folder, pro["version"], pro["loader"])
                    skins.install_local_skin(
                        image, folder, target, s.get("model", "classic")
                    )
                if e4steam.ensure_if_supported(
                    folder,
                    pro["version"],
                    pro["loader"],
                    status,
                    cache_dir=s.root / "steam-mod-cache",
                ):
                    room_bridge.prepare(folder, pro["version"], pro["loader"])
                skin_sync.prepare(
                    folder,
                    pro["version"],
                    pro["loader"],
                    image if image.is_file() else None,
                    s.get("model", "classic"),
                )
                if mode == "prism":
                    from launcher import prism

                    self.prism_launches[pro["id"]] = prism.launch(
                        s.root,
                        folder,
                        pro["name"],
                        pro["version"],
                        pro["loader"],
                        memory,
                        status,
                        on_game=lambda child: self._track_game(child, pro, prism=True),
                        on_waiting=lambda child: self._prism_waiting(pro["id"], child),
                        on_done=lambda: self._prism_done(pro["id"]),
                    )
                    return "Запуск передан Prism. При первом входе заверши настройку аккаунта."
                cache = s.root / "game-cache"
                game.install(pro["version"], pro["loader"], cache, folder, status)
                if mode == "local":
                    process = game.launch_local(
                        cache, folder, nick, memory_mb=memory, status=status
                    )
                else:
                    process = game.launch(
                        cache, folder, session, memory_mb=memory, status=status
                    )
                self._track_game(process, pro)
                return f"Запущен Minecraft {pro['version']} · {pro['name']}."

            return self.start_job(play, profile=pro["id"])
        elif action == "folder":
            folder = s.folder(p["id"])
            if sys.platform == "win32":
                os.startfile(str(folder))
            else:
                subprocess.Popen(
                    ["open" if sys.platform == "darwin" else "xdg-open", str(folder)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        else:
            raise ValueError("Действие не найдено.")
        return {"ok": True}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    server_version = "BlockHarbor"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(45)

    def handle(self):
        def abort():
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        remove = self.app.scope.on_cancel(abort)
        try:
            with self.app.activity():
                super().handle()
        except (lifecycle.OperationCancelled, BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            remove()

    @property
    def app(self):
        return self.server.app

    def reply(
        self, code, body, content="application/json; charset=utf-8", cache="no-store"
    ):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https://cdn.modrinth.com; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def trusted(self, need_token):
        host = "127.0.0.1:" + str(self.server.server_port)
        if (
            self.headers.get("Host") != host
            or self.headers.get("Origin", "http://" + host) != "http://" + host
        ):
            self.reply(403, {"error": "Доступ только из локального окна."})
            return False
        if need_token:
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(
                supplied.encode(), "Bearer ".encode() + self.app.token.encode()
            ):
                self.reply(
                    403, {"error": "Открой BlockHarbor.exe, чтобы восстановить сессию."}
                )
                return False
            self.app.last_seen = time.monotonic()
        return True

    def do_GET(self):
        u = urlsplit(self.path)
        attachment = u.path.startswith("/api/build-download/")
        content_icon = u.path.startswith("/api/content-icon/")
        media_image = u.path.startswith("/api/media/")
        if not self.trusted(
            u.path not in STATIC and not attachment and not content_icon and not media_image
        ):
            return
        try:
            if media_image:
                try:
                    if not u.path.endswith(".png"):
                        raise ValueError("Нет изображения.")
                    path = self.app.experience.media(u.path.removeprefix("/api/media/").removesuffix(".png"))
                    return self.reply(200, path.read_bytes(), "image/png", cache="private, max-age=31536000, immutable")
                except (ValueError, OSError):
                    return self.reply(404, b"", "image/png")
            if u.path == "/api/feature":
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                return self.reply(200, self.app.feature_action(q.pop("action", ""), q, read=True))
            if attachment:
                ticket = u.path.removeprefix("/api/build-download/")
                cookie = SimpleCookie(self.headers.get("Cookie", "")).get("bh_export")
                try:
                    file, name = self.app.downloads.take(
                        ticket, cookie.value if cookie else ""
                    )
                except ValueError as error:
                    return self.reply(403, {"error": str(error)})
                with file:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header(
                        "Content-Disposition",
                        "attachment; filename=BlockHarbor.zip; filename*=UTF-8''"
                        + quote(name, safe=""),
                    )
                    self.send_header(
                        "Content-Length", str(os.fstat(file.fileno()).st_size)
                    )
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    while block := file.read(1024 * 1024):
                        lifecycle.check()
                        self.wfile.write(block)
                return
            if content_icon:
                try:
                    if not u.path.endswith(".png"):
                        raise ValueError("Изображение не найдено.")
                    digest = u.path.removeprefix("/api/content-icon/").removesuffix(
                        ".png"
                    )
                    image = self.app.store.icons.read(digest)
                except (OSError, ValueError):
                    return self.reply(404, b"", "image/png")
                return self.reply(
                    200,
                    image,
                    "image/png",
                    cache="private, max-age=31536000, immutable",
                )
            if u.path in STATIC:
                path = HERE / "ui" / STATIC[u.path]
                self.reply(200, path.read_bytes(), MIME[path.suffix])
            elif u.path == "/api/state":
                with self.app.lock:
                    state = self.app.state()
                self.reply(200, state)
            elif u.path in ("/api/modrinth/filters", "/api/catalog/filters"):
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                options = modrinth.catalog_filters.schema(q["kind"])
                self.reply(
                    200,
                    modrinth.dependency_suggestions(q["q"]) if "q" in q else options,
                )
            elif u.path == "/api/steam/check":
                q = parse_qs(u.query)
                self.reply(200, steam_network.check(self.app.store, q["id"][0]))
            elif u.path == "/api/steam/compatibility":
                q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
                pid, acknowledged = q.get("id"), False
                if pid is not None:
                    profile = self.app.store.profile(pid)
                    version, loader = profile["version"], profile["loader"]
                    acknowledged = e4steam.notice_acknowledged(
                        self.app.store.settings(), pid, version, loader
                    )
                else:
                    _, version, loader = input_profile("Сборка", q.get("version"), q.get("loader"))
                result = e4steam.compatibility(version, loader, acknowledged=acknowledged)
                if pid is not None:
                    result["id"] = pid
                self.reply(200, result)
            elif u.path in ("/api/modrinth/check", "/api/catalog/check"):
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                result = modrinth.check(
                    self.app.store, q["id"], q["kind"], q["project"], q["version"]
                )
                self.reply(200, result)
            elif u.path in ("/api/modrinth/search", "/api/modrinth/versions", "/api/catalog/search", "/api/catalog/versions"):
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                store = self.app.store
                if u.path.endswith("/search"):
                    result = modrinth.search(
                        store.profile(q["id"]),
                        q["kind"],
                        q.get("q", ""),
                        q.get("offset", 0),
                        json.loads(q.get("filters", "{}")),
                        q.get("sort", "relevance"),
                        source=q.get("source", "all"),
                    )
                else:
                    result = modrinth.versions(store, q["id"], q["kind"], q["project"], source=q.get("source", "all"))
                self.reply(200, result)
            elif u.path == "/api/job":
                self.reply(200, self.app.ui_job_snapshot())
            elif u.path == "/api/releases":
                query = parse_qs(u.query)
                force = query.get("refresh") == ["1"]
                try:
                    if not force and query.get("cached") == ["1"]:
                        versions = self.app.release_catalog()
                    else:
                        versions = self.app.refresh_releases(force=force)
                    result = {"versions": versions, "stale": False}
                except lifecycle.OperationCancelled:
                    raise
                except (OSError, ValueError, preparation.DownloadError):
                    result = {"versions": self.app.release_catalog(), "stale": True, "message": "Не удалось обновить каталог Mojang. Доступны сохранённые версии."}
                result["allVersions"] = self.app.version_catalog()
                self.reply(200, result)
            elif u.path == "/api/loaders":
                q = parse_qs(u.query, keep_blank_values=True)
                _, version, _ = input_profile("Сборка", q.get("version", [None])[0], "vanilla")
                with lifecycle.bind(self.app.scope):
                    lifecycle.check()
                    result = loader_catalog.availability(
                        self.app.store.root / "game-cache", version, refresh=q.get("refresh") == ["1"]
                    )
                self.reply(200, result)
            elif u.path == "/api/card":
                store = self.app.store
                real = self.app.player_name()
                image = self.app.skin_path()
                self.reply(
                    200,
                    cards.build_card(
                        real,
                        store.get("nickname") or real,
                        image if image.is_file() else None,
                    ),
                    "application/octet-stream",
                )
            elif u.path == "/api/skin.png":
                self.reply(
                    200, skins.normalized_skin(self.app.skin_path()), "image/png"
                )
            elif u.path == "/api/preview":
                buf = io.BytesIO()
                skins.skin_preview(
                    self.app.skin_path(),
                    parse_qs(u.query).get(
                        "model", [self.app.store.get("model", "classic")]
                    )[0],
                ).save(buf, format="PNG")
                self.reply(200, buf.getvalue(), "image/png")
            else:
                self.reply(404, {"error": "Страница не найдена."})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.reply(400, {"error": str(exc)})

    def do_POST(self):
        if urlsplit(self.path).path == "/api/window":
            if not self.trusted(False):
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 < length <= 1024:
                    raise ValueError("Некорректное событие окна.")
                data = json.loads(self.rfile.read(length))
                supplied = data.get("token")
                if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), self.app.token.encode()):
                    return self.reply(403, {"error": "Сессия окна недействительна."})
                return self.reply(200, self.app.page_event(data.get("event"), data.get("page")))
            except (ValueError, TypeError, AttributeError):
                return self.reply(400, {"error": "Некорректное событие окна."})
        if not self.trusted(True):
            return
        error_target = None
        try:
            u = urlsplit(self.path)
            length = int(self.headers.get("Content-Length", "-1"))
            if u.path == "/api/catalog/check":
                if not 1 <= length <= 32 * 1024:
                    raise ValueError("Выбранный список дополнений слишком большой.")
                data = json.loads(self.rfile.read(length))
                with lifecycle.bind(self.app.scope):
                    result = modrinth.check_selection(self.app.store, data["id"], data["items"])
                return self.reply(200, result)
            if u.path in ("/api/build-export", "/api/mrpack-export"):
                if not 0 < length <= 1024:
                    raise ValueError("Некорректный запрос выгрузки.")
                data = json.loads(self.rfile.read(length))
                pid, worlds = data["id"], data.get("worlds") is True
                archive = tempfile.TemporaryFile(dir=self.app.store.root)
                try:
                    with self.app.lock, self.app.coordinator.operation():
                        if self.app.job_snapshot()["busy"]:
                            raise ValueError("Дождись завершения текущей операции.")
                        if worlds:
                            folder = self.app.store.folder(pid)
                            if self.app.profile_running(pid):
                                raise ValueError(
                                    "Закрой эту сборку перед выгрузкой миров."
                                )
                            require_idle_files(folder)
                        if u.path == "/api/mrpack-export":
                            self.app.require_profile_idle(pid)
                            name = self.app.packs.export_pack(pid, archive)
                        else:
                            name = build_archive.export_build(self.app.store, pid, archive, worlds)
                        ticket, cookie = self.app.downloads.issue(archive, name)
                except BaseException:
                    archive.close()
                    raise
                url = "/api/build-download/" + ticket
                response = json.dumps(
                    {"download": url, "name": name}, ensure_ascii=False
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("Cache-Control", "no-store")
                self.send_header(
                    "Set-Cookie",
                    f"bh_export={cookie}; Path={url}; Max-Age=300; HttpOnly; SameSite=Strict",
                )
                self.end_headers()
                self.wfile.write(response)
                return
            if u.path == "/api/cover":
                if not 0 < length <= 12 * 1024 * 1024:
                    raise ValueError("Размер обложки: до 12 МБ.")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Передача обложки прервана.")
                pid = parse_qs(u.query).get("id", [""])[0]
                with self.app.lock, self.app.coordinator.operation():
                    self.app.require_mutations_allowed()
                    return self.reply(200, self.app.experience.set_cover(pid, raw))
            if u.path in ("/api/build-import", "/api/mrpack-import"):
                maximum = 2 * 1024**3 if u.path == "/api/mrpack-import" else build_archive.MAX_ZIP
                if not 0 < length <= maximum:
                    raise ValueError("Выбери .mrpack размером до 2 ГБ." if u.path == "/api/mrpack-import" else "Выбери ZIP сборки размером до 8 ГБ.")
                with tempfile.TemporaryFile(dir=self.app.store.root) as archive:
                    if length > shutil.disk_usage(self.app.store.root).free:
                        raise ValueError("Недостаточно места на диске для ZIP сборки.")
                    remaining = length
                    while remaining:
                        block = self.rfile.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ValueError("Передача ZIP прервана.")
                        archive.write(block)
                        remaining -= len(block)
                    with self.app.lock, self.app.coordinator.operation():
                        self.app.require_mutations_allowed()
                        if self.app.job_snapshot()["busy"]:
                            raise ValueError("Дождись завершения текущей операции.")
                        if u.path == "/api/mrpack-import":
                            archive.seek(0)
                            result = self.app.packs.stage_import(archive)
                        else:
                            result = build_archive.import_build(self.app.store, archive)
                return self.reply(200, result)
            if u.path in ("/api/profile", "/api/skin-preview"):
                if not 0 <= length <= 1024 * 1024:
                    raise ValueError("Файл скина превышает 1 МБ.")
                parameters = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Передача скина прервана.")
                if u.path == "/api/skin-preview":
                    preview = io.BytesIO()
                    skins.skin_preview(raw, parameters.get("model", "classic")).save(
                        preview, format="PNG"
                    )
                    return self.reply(200, preview.getvalue(), "image/png")
                with self.app.lock, self.app.coordinator.operation():
                    self.app.require_mutations_allowed()
                    return self.reply(200, self.app.save_appearance(parameters, raw))
            if u.path == "/api/file":
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                skin = q.get("kind") == "skin"
                card = q.get("kind") == "card"
                limit = 65536 if card else (1024 * 1024 if skin else MAX_ADDON)
                if not 0 < length <= limit:
                    raise ValueError("Недопустимый размер файла.")
                with tempfile.TemporaryDirectory(dir=self.app.store.root) as td:
                    tmp = Path(td) / "upload"
                    with tmp.open("wb") as out:
                        left = length
                        while left:
                            part = self.rfile.read(min(1 << 20, left))
                            if not part:
                                raise ValueError("Передача файла прервана.")
                            out.write(part)
                            left -= len(part)
                    operation = q.get("dependencies")
                    if operation in ("plan", "install"):
                        if q["kind"] != "mods":
                            raise ValueError("Зависимости доступны только для модов.")
                        pid, name = q["id"], q["name"]
                        with lifecycle.bind(self.app.scope), self.app.coordinator.operation():
                            self.app.require_mutations_allowed()
                            self.app.require_profile_idle(pid)
                            if self.app.job_snapshot()["busy"]:
                                raise ValueError("Дождись завершения текущей операции.")
                            if operation == "install":
                                error_target = (pid, "Установка " + name + " с зависимостями")
                            report, request = modrinth.manual_plan(self.app.store, pid, name, tmp)
                            if operation == "plan":
                                return self.reply(200, report)
                            try:
                                message = modrinth.install_selection(
                                    self.app.store, pid, request["items"], lambda _: None, self.app.lock,
                                    q.get("acknowledgement", ""), local_mod=(name, tmp), request=request,
                                    transfer=self.app.transfers,
                                    before_publish=lambda: self.app.care.before_change(pid, "Добавление мода с зависимостями"),
                                    after_publish=lambda releases: [self.app.care.remember_project(pid, r) for r in releases],
                                )
                            except modrinth.NeedsReview as exc:
                                return self.reply(200, {"review": exc.review})
                            return self.reply(200, {"ok": True, "message": message})
                    with self.app.lock, self.app.coordinator.operation():
                        self.app.require_mutations_allowed()
                        if card:
                            return self.reply(200, self.app.import_card(tmp, q["id"]))
                        if skin:
                            skins.validate_skin(tmp)
                            digest = skins.save_texture(self.app.store.root, skins.normalized_skin(tmp))
                            self.app.store.set("skinDigest", digest)
                        else:
                            if self.app.job_snapshot()["busy"]:
                                raise ValueError("Дождись завершения текущей операции.")
                            self.app.require_profile_idle(q["id"])
                            if q.get("check") != "1":
                                error_target = (q["id"], "Добавление файла " + q["name"])
                            inspection = modrinth.manual_check(
                                self.app.store, q["id"], q["kind"], q["name"], tmp
                            )
                            if q.get("check") == "1":
                                return self.reply(200, inspection)
                            if inspection["blocked"]:
                                raise ValueError(" ".join(inspection["conflicts"][:3]))
                            if any(not row["installed"] for row in inspection["required"]):
                                raise ValueError("Моду нужны дополнительные файлы. Добавь его через подбор зависимостей.")
                            self.app.care.before_change(q["id"], "Добавление файла " + q["name"])
                            self.app.store.import_file(
                                q["id"], q["kind"], q["name"], tmp
                            )
                            return self.reply(
                                200, {"ok": True, "warnings": inspection["warnings"]}
                            )
                return self.reply(200, {"ok": True})
            if u.path != "/api/action" or not 1 <= length <= 2 * 1024 * 1024:
                raise ValueError("Некорректный запрос.")
            data = json.loads(self.rfile.read(length))
            action = data.pop("action")
            if action == "fitWindow":
                return self.reply(
                    200, {"attached": self.app.window.adjust(data.get("marker"))}
                )
            if action == "diagnostics" or any(action in service.ACTIONS for service in self.app.features):
                result = self.app.feature_action(action, data)
            else:
                with self.app.lock:
                    result = self.app.action(action, data)
            self.reply(200, result)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            if error_target and not self.app.closing.is_set():
                try:
                    build_errors.record_operation(self.app.store, *error_target, exc)
                except (OSError, ValueError, sqlite3.Error):
                    pass
            self.reply(400, {"error": str(exc) or "Операция не удалась."})


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=default_root())
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--profile", help="Open a build from its desktop shortcut")
    args = parser.parse_args()
    app = App(args.data)
    monitor = None
    try:
        if args.profile:
            app.store.profile(args.profile)
            app.store.set("active", args.profile)
        with Server(("127.0.0.1", 0), Handler) as server:
            server.app = app
            app.shutdown = server.shutdown
            url = "http://127.0.0.1:" + str(server.server_port) + "/#" + app.token

            def watch():
                while not app.closing.wait(0.2):
                    # Explicit X has priority over games, queues and update waits.
                    # A hidden native frame is still alive and does not enter here.
                    if not args.no_browser and app.window_closed():
                        app.request_close(reason="user")
                        return
                    try:
                        with app.activity():
                            app.game_running()
                            app.background_maintenance()
                    except lifecycle.OperationCancelled:
                        return
                    except (OSError, ValueError, sqlite3.Error):
                        pass  # A maintenance error must not disable X detection.

            monitor = threading.Thread(target=watch, daemon=True, name="blockharbor-session")
            monitor.start()
            if args.no_browser:
                print(url, flush=True)
            else:
                app.window.show(url)
                app.updates.start_auto()
            if not app.closing.is_set():
                server.serve_forever(poll_interval=0.2)
    finally:
        app.close()
        if monitor:
            monitor.join(timeout=2)


if __name__ == "__main__":
    try:
        run()
    except StartupCancelled:
        pass
    except Exception as e:
        if sys.platform != "win32":
            raise
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, str(e), "BlockHarbor", 16)
