"""Release checks and an explicit, restart-safe Windows update handoff.

Trust is the configured GitHub repository over HTTPS plus its asset hashes. This
checks corruption and the permitted source; it is not an Authenticode signature
or protection from a compromised repository administrator.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError

from .lifecycle import CancellationScope, OperationCancelled, bind, check as check_cancelled, current_scope, response as cancellable_response
from .transfers import _atomic_json, open_https
from .update_cache import CHECK_TTL, MAX_SERVER_RETRY, SharedUpdateCache, header_values, number, retry_at

REPOSITORY = "dedt1mon/BlockHarbor"
API = "https://api.github.com/repos/" + REPOSITORY
WEB = "https://github.com/" + REPOSITORY
ASSET_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
DATA_SCHEMA = 1
MAX_INSTALLER = 512 * 1024**2
AUTO_CHECK_INTERVAL = 10 * 60
VERSION_RE = re.compile(r"v?(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})(?:-(alpha|beta|rc)\.(0|[1-9]\d{0,4}))?\Z")


def version_key(value):
    match = VERSION_RE.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("Неподдерживаемый номер версии BlockHarbor.")
    major, minor, patch, channel, serial = match.groups()
    return (int(major), int(minor), int(patch), {"alpha": 0, "beta": 1, "rc": 2, None: 3}[channel], int(serial or 0))


def _read_json(url, *, hosts={"api.github.com"}, limit=2 * 1024 * 1024, response_headers=None):
    check_cancelled()
    headers = {"Accept": "application/json"}
    if hosts == {"api.github.com"}:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    with cancellable_response(open_https(url, hosts, headers=headers)) as response:
        if response_headers:
            response_headers(getattr(response, "headers", {}))
        data = response.read(limit + 1)
    check_cancelled()
    if len(data) > limit:
        raise ValueError("Ответ сервиса обновлений слишком большой.")
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Сервис обновлений вернул нечитаемый ответ.") from exc


def _compatibility(value, version):
    if (
        not isinstance(value, dict)
        or type(value.get("format")) is not int or value["format"] != 1
        or value.get("version") != version
        or any(type(value.get(field)) is not int for field in ("dataSchema", "minDataSchema", "maxDataSchema"))
        or not 1 <= value["minDataSchema"] <= value["maxDataSchema"] <= 10000
        or not 1 <= value["dataSchema"] <= 10000
        or type(value.get("rollbackSafe")) is not bool
    ):
        raise ValueError("Манифест обновления не описывает совместимость данных.")
    if "installer" in value:
        installer = value["installer"]
        if (not isinstance(installer, dict)
            or installer.get("name") != "BlockHarbor-Setup-" + version + ".exe"
            or type(installer.get("size")) is not int or not 0 < installer["size"] <= MAX_INSTALLER
            or not isinstance(installer.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", installer["sha256"])):
            raise ValueError("Манифест содержит неверное имя, размер или SHA-256 установщика.")
    return value


def _checksum(url, filename):
    check_cancelled()
    with cancellable_response(open_https(url, ASSET_HOSTS)) as response:
        raw = response.read(128 * 1024 + 1)
    check_cancelled()
    if len(raw) > 128 * 1024:
        raise ValueError("Список контрольных сумм слишком большой.")
    try:
        content = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Список контрольных сумм имеет неверный формат.") from exc
    matches = re.findall(r"^([a-fA-F0-9]{64})[ \t]+\*?" + re.escape(filename) + r"\r?$", content, re.M)
    if len(matches) != 1:
        raise ValueError("Для установщика не опубликована единственная контрольная сумма SHA-256.")
    return matches[0].lower()


def _asset(release, name, maximum):
    tag = release["tag_name"]
    rows = release.get("assets", [])
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("Неверный список файлов релиза.")
    found = [row for row in rows if isinstance(row, dict) and row.get("name") == name]
    if len(found) != 1:
        raise ValueError(f"В релизе нет единственного файла {name}.")
    row = found[0]
    expected_url = WEB + "/releases/download/" + tag + "/" + name
    if (
        row.get("browser_download_url") != expected_url
        or row.get("state", "uploaded") != "uploaded"
        or type(row.get("size")) is not int
        or not 0 < row["size"] <= maximum
    ):
        raise ValueError("Файл релиза имеет недопустимый адрес или размер.")
    return row


def release_info(release):
    if not isinstance(release, dict) or release.get("draft"):
        raise ValueError("Релиз ещё не опубликован.")
    tag = release.get("tag_name")
    key = version_key(tag)
    version = tag.removeprefix("v")
    filename = "BlockHarbor-Setup-" + version + ".exe"
    executable = _asset(release, filename, MAX_INSTALLER)
    digest = executable.get("digest", "")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
        sums = _asset(release, "SHA256SUMS.txt", 128 * 1024)
        digest = "sha256:" + _checksum(sums["browser_download_url"], filename)
    # The only legacy format supported here is the published baseline. New
    # versions must state their data compatibility explicitly in the release.
    if key <= version_key("1.10.0") and key[0] == 1:
        compatibility = {"format": 1, "version": version, "dataSchema": 1, "minDataSchema": 1, "maxDataSchema": 1, "rollbackSafe": True}
    else:
        manifest = _asset(release, "BlockHarbor-update.json", 64 * 1024)
        compatibility = _read_json(manifest["browser_download_url"], hosts=ASSET_HOSTS, limit=64 * 1024)
    _compatibility(compatibility, version)
    installer = compatibility.get("installer")
    if installer and (installer["size"] != executable["size"] or installer["sha256"] != digest.split(":", 1)[1].lower()):
        raise ValueError("Манифест и файл релиза не совпадают по размеру или SHA-256.")
    compatible = compatibility["minDataSchema"] <= DATA_SCHEMA <= compatibility["maxDataSchema"]
    return {
        "version": version, "tag": tag, "name": str(release.get("name") or tag)[:180],
        "notes": str(release.get("body") or "Описание изменений не опубликовано.")[:24000],
        "published": str(release.get("published_at") or "")[:40],
        "prerelease": bool(release.get("prerelease") or key[3] < 3),
        "url": WEB + "/releases/tag/" + tag,
        "download": executable["browser_download_url"], "filename": filename,
        "size": executable["size"], "sha256": digest.split(":", 1)[1].lower(),
        "compatibility": compatibility, "compatible": compatible,
    }


def _installer_size(url):
    """Older manifests omit the size. Request at most one installer byte.

    Some intermediaries ignore Range. A 200 response is acceptable only with a
    bounded exact Content-Length; its body is closed without downloading it.
    """
    check_cancelled()
    with cancellable_response(open_https(url, ASSET_HOSTS, headers={"Range": "bytes=0-0"})) as response:
        headers = header_values(getattr(response, "headers", {}))
        status = getattr(response, "status", 200)
        if headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError("GitHub изменил кодировку установщика; размер нельзя проверить.")
        if status == 206:
            match = re.fullmatch(r"bytes 0-0/([1-9][0-9]{0,9})", headers.get("content-range", ""))
            if not match or headers.get("content-length", "1") != "1" or len(response.read(2)) != 1:
                raise ValueError("GitHub вернул неверный диапазон размера установщика.")
            size = int(match[1])
        elif status == 200 and re.fullmatch(r"[1-9][0-9]{0,9}", headers.get("content-length", "")):
            size = int(headers["content-length"])
        else:
            raise ValueError("GitHub не сообщил проверяемый размер установщика.")
    check_cancelled()
    if not 0 < size <= MAX_INSTALLER:
        raise ValueError("Недопустимый размер установщика.")
    return size


class _NotesText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_endtag(self, tag):
        if tag in {"p", "li", "h1", "h2", "h3", "br", "pre"}:
            self.parts.append("\n")


def _feed_release():
    check_cancelled()
    with cancellable_response(open_https(WEB + "/releases.atom", {"github.com"}, headers={"Accept": "application/atom+xml"})) as response:
        raw = response.read(512 * 1024 + 1)
    check_cancelled()
    if len(raw) > 512 * 1024 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("Публичный список релизов имеет недопустимый формат.")
    try:
        feed = ET.fromstring(raw)
    except (ET.ParseError, ValueError) as exc:
        raise ValueError("GitHub вернул нечитаемый публичный список релизов.") from exc
    ns = "{http://www.w3.org/2005/Atom}"
    entries = feed.findall(ns + "entry")
    if feed.tag != ns + "feed" or len(entries) > 100:
        raise ValueError("Публичный список релизов имеет недопустимый формат.")
    candidates = []
    prefix = WEB + "/releases/tag/"
    for entry in entries:
        links = [item.get("href", "") for item in entry.findall(ns + "link") if item.get("rel", "alternate") == "alternate"]
        if len(links) != 1 or not links[0].startswith(prefix):
            continue
        tag = links[0][len(prefix):]
        try:
            key = version_key(tag)
        except ValueError:
            continue
        candidates.append((key, tag, entry))
    if not candidates:
        raise ValueError("В тестовом канале пока нет проверяемых публичных релизов.")
    # As with the API, a broken newest release must not select an older version.
    _, tag, entry = max(candidates, key=lambda row: row[0])
    text = _NotesText()
    text.feed(entry.findtext(ns + "content", ""))
    return tag, {"name": entry.findtext(ns + "title", tag)[:180],
                 "published": entry.findtext(ns + "updated", "")[:40],
                 "notes": "".join(text.parts).strip()[:24000]}


def public_release(channel="stable", *, tag=None):
    """Use only public assets of the configured repository, without REST quota."""
    details = {}
    if tag is None and channel == "stable":
        comp = _read_json(WEB + "/releases/latest/download/BlockHarbor-update.json", hosts=ASSET_HOSTS, limit=64 * 1024)
        version = comp.get("version") if isinstance(comp, dict) else None
        if version_key(version)[3] != 3 or version.startswith("v"):
            raise ValueError("Стабильный канал не может устанавливать тестовую версию.")
        # Published BlockHarbor releases use v<version>; the following assets
        # must exist under that exact tag, not an arbitrary URL from the JSON.
        tag = "v" + version
    else:
        if tag is None:
            tag, details = _feed_release()
        version_key(tag)
        version = tag.removeprefix("v")
        comp = _read_json(WEB + "/releases/download/" + tag + "/BlockHarbor-update.json", hosts=ASSET_HOSTS, limit=64 * 1024)
    _compatibility(comp, version)
    filename = "BlockHarbor-Setup-" + version + ".exe"
    download = WEB + "/releases/download/" + tag + "/" + filename
    if "installer" in comp:
        size, digest = comp["installer"]["size"], comp["installer"]["sha256"]
    else:
        digest = _checksum(WEB + "/releases/download/" + tag + "/SHA256SUMS.txt", filename)
        size = _installer_size(download)
    return {"version": version, "tag": tag, "name": details.get("name") or "BlockHarbor " + version,
            "notes": details.get("notes") or "Описание изменений опубликовано на странице релиза GitHub.",
            "published": details.get("published", ""), "prerelease": version_key(version)[3] < 3,
            "url": WEB + "/releases/tag/" + tag, "download": download, "filename": filename,
            "size": size, "sha256": digest, "compatibility": comp,
            "compatible": comp["minDataSchema"] <= DATA_SCHEMA <= comp["maxDataSchema"]}


def _friendly_error(error):
    if isinstance(error, HTTPError):
        if error.code in {403, 429}:
            return "GitHub временно ограничил запросы к обновлениям. BlockHarbor повторит проверку после паузы."
        if error.code == 404:
            return "Официальные файлы обновления пока недоступны. Проверь обновления немного позже."
        return "GitHub временно не может выдать файлы обновления. Повторим проверку после паузы."
    if isinstance(error, (URLError, TimeoutError, ConnectionError, HTTPException)):
        return "Не удалось соединиться с GitHub. Проверь подключение; повторная проверка будет выполнена позже."
    text = str(error)[:500]
    if "rate limit" in text.lower() or "http error 403" in text.lower() or "http error 429" in text.lower():
        return "GitHub временно ограничил запросы к обновлениям. Проверка продолжится через официальный публичный источник."
    return text or "Не удалось проверить обновления. Повторим проверку после паузы."


class _UsePublicRelease(Exception):
    """The shared API deadline is active; use the repository's public assets."""


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(1024 * 1024):
            check_cancelled()
            digest.update(block)
    return digest.hexdigest()


class UpdateService:
    ACTIONS = {
        "updateState", "updateCheck", "updateChannel", "updateDownload", "updateInstall",
        "updateCancelInstall", "updateRollback", "updateAutomatic", "transferCancel", "transferRetry", "transferClear",
    }
    JOBS = {"updateCheck"}

    def __init__(self, store, transfers, current_version="2.4.1", *, can_install=lambda: False, request_shutdown=None,
                 namespace="", install_guard=None, handoff_metadata=None, cancel_handoff=None):
        version_key(current_version)
        self.store, self.transfers = store, transfers
        self.current_version = current_version
        self.can_install, self.request_shutdown = can_install, request_shutdown
        self.install_guard = install_guard
        self.handoff_metadata = handoff_metadata
        self.cancel_handoff = cancel_handoff
        self.root = store.root / "updates"
        self.source_cache = SharedUpdateCache(self.root, REPOSITORY)
        if namespace:
            if not re.fullmatch(r"window-[1-9][0-9]{0,2}", namespace):
                raise ValueError("Некорректный идентификатор окна обновления.")
            if self.root.is_symlink() or self.root.resolve().parent != store.root.resolve():
                raise ValueError("Папка обновлений перенаправлена.")
            self.root = self.root / namespace
        if self.root.is_symlink():
            raise ValueError("Папка обновлений перенаправлена.")
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache = self.root / "installers"
        if self.cache.is_symlink():
            raise ValueError("Папка установщиков перенаправлена.")
        self.cache.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.check_lock = threading.Lock()
        self.install_lock = threading.Lock()
        self.cancel_install = threading.Event()
        self.closed = threading.Event()
        self._close_lock = threading.Lock()
        self._close_reason = None
        self.scope = CancellationScope()
        self._workers = set()
        self._auto_worker = None
        self._auto_scope = None
        self._auto_started = False
        self._auto_generation = 0
        self._auto_candidate = None
        self._auto_snoozed = ""
        self._auto_failure = None
        self._automatic_tasks = set()
        self._next_auto_check = 0
        self._next_install_attempt = 0
        self._observed_channel = self.store.get("updateChannel", "stable")
        self._observed_automatic = self.automatic_enabled()
        self._helper_process = None
        self._handoff_commit = None
        self._handoff_request = None
        self._handoff_committed = False
        self._cancelling_handoff = False
        self.release = None
        self.release_channel = self._observed_channel
        self.checked = 0
        self.error = ""
        self.check_status = {"source": "", "message": "", "retryAt": 0, "apiRetryAt": 0, "cached": False, "stale": False}
        self._check_failed = False
        self.startup_result = None
        self.receipts = []
        self.pending_downloads = []
        self.pending = None
        self.rollback_binary = None
        self.installed_compatibility = {"version": current_version, "dataSchema": DATA_SCHEMA, "rollbackSafe": True}
        self._load()

    def automatic_enabled(self):
        # Preserve an earlier explicit refusal of startup checks during migration.
        default = "0" if self.store.get("checkUpdatesOnStart", "1") == "0" else "1"
        return self.store.get("autoUpdate", default) == "1"

    def _require_open(self):
        if self.closed.is_set():
            raise OperationCancelled("BlockHarbor закрывается. Обновление отменено.")
        check_cancelled()

    def _spawn(self, function, *, name, automatic=False):
        def run():
            try:
                function()
            finally:
                with self.lock:
                    self._workers.discard(threading.current_thread())
        with self.lock:
            self._require_open()
            worker = threading.Thread(target=run, daemon=True, name=name)
            self._workers.add(worker)
            if automatic:
                self._auto_worker = worker
            worker.start()
            return worker

    @property
    def handoff_ready(self):
        with self.lock:
            return bool(self._handoff_committed and self.pending and self.pending["stage"] == "launching"
                        and not self.cancel_install.is_set())

    def start_auto(self):
        """Start only after the live application's shutdown callback is attached."""
        with self.lock:
            if self.closed.is_set() or self._auto_started:
                return
            self._auto_started = True
            self._next_auto_check = 0
        self.tick_auto()

    def _cancel_automatic(self):
        with self.lock:
            self._auto_generation += 1
            self._auto_candidate = None
            scope = self._auto_scope
            task_ids = list(self._automatic_tasks)
            self._automatic_tasks.clear()
            automatic_pending = bool(self.pending and self.pending.get("automatic"))
        if scope:
            scope.cancel()
        for task_id in task_ids:
            try:
                self.transfers.cancel(task_id)
            except (ValueError, KeyError):
                pass
        if automatic_pending:
            self._cancel_pending(automatic_only=True)

    def set_automatic(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("Выбери режим автоматического обновления.")
        self._require_open()
        self.store.set("autoUpdate", "1" if enabled else "0")
        self._cancel_automatic()
        with self.lock:
            self._observed_automatic = enabled
            self._next_auto_check = 0
            self._auto_snoozed = ""
            if enabled:
                self._auto_failure = None
                self.error = ""
            self._save()
        self.tick_auto()
        return {"ok": True, "enabled": enabled}

    def tick_auto(self):
        """Nonblocking scheduler, called only by the running application's monitor."""
        if self.closed.is_set() or not self._auto_started:
            return
        enabled = self.automatic_enabled()
        channel = self.store.get("updateChannel", "stable")
        if enabled != self._observed_automatic or channel != self._observed_channel:
            self._cancel_automatic()
            with self.lock:
                self._observed_automatic, self._observed_channel = enabled, channel
                self._next_auto_check = 0
        if not enabled:
            return
        with self.lock:
            if self.closed.is_set() or (self._auto_worker and self._auto_worker.is_alive()) or self.pending:
                return
            now = time.monotonic()
            remote = now >= self._next_auto_check
            candidate = self._auto_candidate
            ready = bool(os.name == "nt" and candidate and candidate["compatible"]
                         and version_key(candidate["version"]) > version_key(self.current_version)
                         and self._ready(candidate["version"]))
            blocked = bool(candidate and (candidate["version"] == self._auto_snoozed
                                         or candidate["version"] == (self._auto_failure or {}).get("version")))
            if not remote and (not ready or blocked or now < self._next_install_attempt):
                return
            if remote:
                self._next_auto_check = now + AUTO_CHECK_INTERVAL
            generation = self._auto_generation
            scope = CancellationScope()
            self._auto_scope = scope
        try:
            self._spawn(lambda: self._auto_cycle(generation, scope, remote=remote), name="bh-auto-update", automatic=True)
        except OperationCancelled:
            pass

    def _require_automatic(self, generation):
        self._require_open()
        if not self.automatic_enabled() or generation != self._auto_generation:
            raise OperationCancelled("Автоматическое обновление отменено.")

    def _auto_cycle(self, generation, scope, *, remote):
        try:
            with bind(scope):
                self._require_automatic(generation)
                if remote:
                    self.check()
                    self._require_automatic(generation)
                    with self.lock:
                        self._auto_candidate = dict(self.release) if self.release else None
                with self.lock:
                    info = dict(self._auto_candidate) if self._auto_candidate else None
                    blocked = info and (info["version"] == self._auto_snoozed or info["version"] == (self._auto_failure or {}).get("version"))
                if (not info or blocked or not info["compatible"] or version_key(info["version"]) <= version_key(self.current_version)
                    or self.release_channel != self.store.get("updateChannel", "stable") or os.name != "nt"):
                    return
                self._require_automatic(generation)
                if not self._ready(info["version"]):
                    if remote:
                        self._enqueue(info, automatic=True)
                    return
                self._require_automatic(generation)
                self.schedule_install(when="idle", automatic=True)
        except OperationCancelled:
            pass
        except Exception as exc:
            with self.lock:
                if not self.closed.is_set() and generation == self._auto_generation:
                    self.error = str(exc)[:600]
                    self._next_install_attempt = time.monotonic() + AUTO_CHECK_INTERVAL
                    self._save()

    @staticmethod
    def _stop_helper(process, timeout=2):
        if not process or process.poll() is not None:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=max(0, timeout))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.1)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _cancel_pending(self, *, automatic_only=False, timeout=2):
        with self.lock:
            if automatic_only and not (self.pending and self.pending.get("automatic")):
                return
            self.cancel_install.set()
            self._handoff_committed = False
            self._cancelling_handoff = True
            commit, process = self._handoff_commit, self._helper_process
            self.pending = None
        try:
            self._stop_helper(process, timeout)
            if commit:
                try:
                    commit.unlink(missing_ok=True)
                except OSError:
                    pass  # The owned helper is stopped even if cleanup is blocked.
            with self.lock:
                try:
                    self._save()
                except OSError:
                    pass
            if self.cancel_handoff:
                with bind(None):
                    self.cancel_handoff()
        finally:
            with self.lock:
                self._cancelling_handoff = False

    def close(self, reason="user", timeout=2):
        """User close revokes the handoff; an update close keeps only its commit."""
        deadline = time.monotonic() + max(0, timeout)
        with self._close_lock:
            if self._close_reason is not None:
                return
            self._close_reason = reason
            self.closed.set()
        self.scope.cancel()
        if self._auto_scope:
            self._auto_scope.cancel()
        preserve = reason == "update" and self.handoff_ready
        if not preserve:
            self._cancel_pending(timeout=max(0, deadline - time.monotonic()))
        with self.lock:
            workers = list(self._workers)
            helper = None if preserve else self._helper_process
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=max(0, deadline - time.monotonic()))
        if helper and helper.poll() is None:
            try:
                helper.wait(timeout=max(0.05, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                helper.kill()

    def _load(self):
        path = self.root / "state.json"
        try:
            if path.is_symlink() or path.stat().st_size > 512 * 1024:
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            previous_error = str(data.get("error", ""))[:600]
            self.error = _friendly_error(previous_error) if previous_error else ""
            failure = data.get("automaticFailure")
            if isinstance(failure, dict) and isinstance(failure.get("version"), str):
                version_key(failure["version"])
                self._auto_failure = {"version": failure["version"], "message": str(failure.get("message", ""))[:600]}
            installed = data.get("installedCompatibility", {})
            pending_receipt = ((data.get("pending") or {}).get("selection") or {}).get("receipt", {})
            if pending_receipt.get("version") == self.current_version:
                installed = pending_receipt.get("compatibility", installed)
            if isinstance(installed, dict) and installed.get("version") == self.current_version and type(installed.get("dataSchema")) is int and type(installed.get("rollbackSafe")) is bool:
                self.installed_compatibility = installed
            self.checked = float(data.get("checked", 0))
            release = data.get("release")
            if release:
                self._validate_receipt(release, require_file=False)
                self.release = release
                self.release_channel = data.get("releaseChannel", "stable")
            for receipt in data.get("receipts", [])[-6:]:
                try:
                    self._validate_receipt(receipt, require_file=True)
                    self.receipts.append(receipt)
                except (OSError, ValueError, TypeError, KeyError):
                    continue
            for candidate in data.get("downloads", [])[-6:]:
                try:
                    self._validate_receipt(candidate, require_file=False)
                    self.pending_downloads.append(candidate)
                except (OSError, ValueError, TypeError, KeyError):
                    continue
            backup = data.get("rollbackBinary")
            if isinstance(backup, dict):
                version_key(backup["version"])
                filename = "BlockHarbor-" + backup["version"] + ".exe"
                target = Path(backup["target"])
                binary = self.root / "rollback" / filename
                if (binary.is_file() and not binary.is_symlink() and target.is_absolute()
                    and target.name.lower() == "blockharbor.exe" and re.fullmatch(r"[a-f0-9]{64}", backup["sha256"])):
                    self.rollback_binary = backup
            # Never loop through failed silent installs every time the app reopens.
            pending = data.get("pending")
            if isinstance(pending, dict):
                handoff = pending.get("handoff", "")
                result_id = handoff if isinstance(handoff, str) and re.fullmatch(r"[a-f0-9]{24}", handoff) else "update-" + str(pending.get("version", ""))[:40] + "-" + str(pending.get("stage", ""))[:30]
                if pending.get("version") == self.current_version:
                    self.error = ""
                    self.startup_result = {"id": result_id, "ok": True, "message": "BlockHarbor обновлён до версии " + self.current_version + "."}
                    if (self._auto_failure or {}).get("version") == self.current_version:
                        self._auto_failure = None
                else:
                    message = "Предыдущая отложенная установка не завершилась. Нажми «Установить», когда будешь готов."
                    request_id = pending.get("handoff", "")
                    if isinstance(request_id, str) and re.fullmatch(r"[a-f0-9]{24}", request_id):
                        result_path = self.root / ("result-" + request_id + ".json")
                        if result_path.is_file() and not result_path.is_symlink() and result_path.stat().st_size <= 16384:
                            result = json.loads(result_path.read_text(encoding="utf-8"))
                            if isinstance(result, dict) and result.get("ok") is False:
                                message = str(result.get("error") or "Установщик завершился с кодом " + str(result.get("exitCode", "ошибки")))[:600]
                    self.error = _friendly_error(message)
                    self.startup_result = {"id": result_id, "ok": False, "message": self.error}
                    if pending.get("automatic") and pending.get("stage") in {"preparing", "launching"}:
                        self._auto_failure = {"version": pending["version"], "message": message}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.release = None
        for candidate in list(self.pending_downloads):
            path = self.cache / candidate["filename"]
            self.transfers.restore_callback(path, lambda result, info=candidate: self._remember(info, result))
            try:
                if path.is_file() and not path.is_symlink() and path.stat().st_size == candidate["size"] and _sha256(path) == candidate["sha256"]:
                    self._remember(candidate, path)
            except OSError:
                pass

    def _save(self):
        with self.lock:
            _atomic_json(self.root / "state.json", {
                "release": self.release, "releaseChannel": self.release_channel, "checked": self.checked, "receipts": self.receipts[-6:],
                "downloads": self.pending_downloads[-6:], "rollbackBinary": self.rollback_binary, "pending": self.pending,
                "installedCompatibility": self.installed_compatibility, "error": self.error, "automaticFailure": self._auto_failure,
            })

    def _validate_receipt(self, receipt, *, require_file):
        version = receipt["version"]
        version_key(version)
        name = "BlockHarbor-Setup-" + version + ".exe"
        if receipt["filename"] != name or receipt["download"] != WEB + "/releases/download/" + receipt["tag"] + "/" + name:
            raise ValueError("Недопустимая запись обновления.")
        if receipt["tag"].removeprefix("v") != version or not re.fullmatch(r"[a-f0-9]{64}", receipt["sha256"]):
            raise ValueError("Недопустимая запись обновления.")
        if type(receipt["size"]) is not int or not 0 < receipt["size"] <= MAX_INSTALLER:
            raise ValueError("Недопустимый размер установщика.")
        comp = receipt["compatibility"]
        if not isinstance(comp, dict) or comp.get("version") != version or comp.get("format") != 1:
            raise ValueError("Неизвестная совместимость данных установщика.")
        if any(type(comp.get(key)) is not int for key in ("dataSchema", "minDataSchema", "maxDataSchema")):
            raise ValueError("Неизвестная совместимость данных установщика.")
        if not 1 <= comp["minDataSchema"] <= comp["maxDataSchema"] <= 10000 or not 1 <= comp["dataSchema"] <= 10000 or type(comp.get("rollbackSafe")) is not bool:
            raise ValueError("Неизвестная совместимость данных установщика.")
        _compatibility(comp, version)
        if "installer" in comp and (comp["installer"]["size"] != receipt["size"] or comp["installer"]["sha256"] != receipt["sha256"]):
            raise ValueError("Манифест и запись установщика не совпадают.")
        receipt["compatible"] = comp["minDataSchema"] <= DATA_SCHEMA <= comp["maxDataSchema"]
        if require_file:
            path = self.cache / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size != receipt["size"]:
                raise ValueError("Установщик ещё не загружен.")
        return receipt

    def _ready(self, version):
        return next((receipt for receipt in self.receipts if receipt["version"] == version and (self.cache / receipt["filename"]).is_file()), None)

    def _rollback(self):
        if self.installed_compatibility.get("rollbackSafe") is not True or self.installed_compatibility.get("dataSchema") != DATA_SCHEMA:
            return None
        installed = self._ready(self.current_version)
        if installed and installed["compatibility"].get("rollbackSafe") is not True:
            return None
        choices = [r for r in self.receipts if version_key(r["version"]) < version_key(self.current_version)
                   and r["compatibility"].get("rollbackSafe") is True
                   and r["compatibility"]["dataSchema"] == DATA_SCHEMA
                   and r["compatibility"]["minDataSchema"] <= DATA_SCHEMA <= r["compatibility"]["maxDataSchema"]]
        if choices:
            receipt = max(choices, key=lambda r: version_key(r["version"]))
            return {"version": receipt["version"], "kind": "installer", "receipt": receipt}
        if self.rollback_binary and self.rollback_binary.get("dataSchema") == DATA_SCHEMA and self.rollback_binary.get("rollbackSafe", True) is True and version_key(self.rollback_binary["version"]) < version_key(self.current_version):
            return {"version": self.rollback_binary["version"], "kind": "binary", "receipt": self.rollback_binary}
        return None

    def state(self):
        with self.lock:
            release = dict(self.release) if self.release and self.release_channel == self.store.get("updateChannel", "stable") else None
            ready = self._ready(release["version"]) if release else None
            rollback = self._rollback()
            return {
                "current": self.current_version,
                "channel": self.store.get("updateChannel", "stable"),
                "autoEnabled": self.automatic_enabled(),
                "autoChecking": bool(self._auto_worker and self._auto_worker.is_alive()),
                "autoSnoozed": self._auto_snoozed,
                "autoFailure": dict(self._auto_failure) if self._auto_failure else None,
                "autoIntervalMinutes": AUTO_CHECK_INTERVAL // 60,
                "checkedAt": self.checked,
                "checkStatus": dict(self.check_status),
                "startupResult": dict(self.startup_result) if self.startup_result else None,
                "release": release,
                "available": bool(release and version_key(release["version"]) > version_key(self.current_version)),
                "ready": bool(ready),
                "pending": {key: self.pending.get(key, False) for key in ("version", "rollback", "stage", "automatic")} if self.pending else None,
                "error": self.error or ((self._auto_failure or {}).get("message", "")
                                         if release and release["version"] == (self._auto_failure or {}).get("version") else ""),
                "windows": os.name == "nt",
                "rollback": {"version": rollback["version"], "kind": rollback["kind"]} if rollback else None,
                "trust": "Обновления загружаются из dedt1mon/BlockHarbor по HTTPS и проверяются по SHA-256. Подписи издателя Windows пока нет.",
                "transfers": self.transfers.snapshot(),
            }

    def check(self, status=lambda text: None):
        with bind(current_scope() or self.scope):
            return self._check(status)

    def _api_json(self, cache, url):
        now = time.time()
        api = cache["api"]
        if now < number(api.get("retryAt")) <= now + MAX_SERVER_RETRY:
            raise _UsePublicRelease()

        def observed(headers):
            values = header_values(headers)
            if values.get("x-ratelimit-remaining") == "0":
                api["retryAt"] = retry_at(values)
                api["limited"] = True
            else:
                api["retryAt"] = 0
                api["limited"] = False
            api["failures"] = 0
            self.source_cache.save(cache)

        try:
            return _read_json(url, response_headers=observed)
        except (HTTPError, URLError, TimeoutError, ConnectionError, HTTPException) as exc:
            self._require_open()
            if isinstance(exc, HTTPError) and exc.code not in {403, 429} and exc.code < 500:
                raise
            failures = min(16, max(0, int(number(api.get("failures")))) + 1)
            api["failures"] = failures
            api["retryAt"] = retry_at(getattr(exc, "headers", {}), failures=failures)
            api["limited"] = isinstance(exc, HTTPError) and exc.code in {403, 429}
            # Persist the deadline before trying another source, so another
            # window/restart cannot immediately hit the exhausted API again.
            self.source_cache.save(cache)
            if isinstance(exc, HTTPError):
                exc.close()
            raise _UsePublicRelease() from exc

    def _cached_info(self, entry, *, channel=None, version=None):
        info = entry.get("info") if isinstance(entry, dict) else None
        try:
            if not isinstance(info, dict):
                return None
            info = self._validate_receipt(dict(info), require_file=False)
            if version is not None and info["version"] != version:
                return None
            if channel == "stable" and (info.get("prerelease") or version_key(info["version"])[3] < 3):
                return None
            return info
        except (ValueError, KeyError, TypeError, AttributeError):
            return None

    def _lookup_release(self, cache, channel, status):
        api = cache["api"]
        now = time.time()
        rows = api.get("rows")
        try:
            if not isinstance(rows, list) or not 0 <= now - number(api.get("checkedAt")) < CHECK_TTL:
                rows = self._api_json(cache, API + "/releases?per_page=30")
                if not isinstance(rows, list):
                    raise ValueError("GitHub вернул неверный список релизов.")
                api["rows"], api["checkedAt"] = rows[:30], time.time()
                self.source_cache.save(cache)
        except _UsePublicRelease:
            status("Проверяем обновления через официальные файлы релиза GitHub…")
            return public_release(channel), "public"
        candidates = []
        for release in rows[:30]:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            try:
                key = version_key(release.get("tag_name"))
            except ValueError:
                continue
            if channel == "stable" and (release.get("prerelease") or key[3] < 3):
                continue
            candidates.append((key, release))
        if not candidates:
            raise ValueError("В выбранном канале пока нет опубликованных установщиков.")
        # Invalid data in a successful API response is not a reason to fall back
        # or choose an older release: integrity errors stay visible.
        selected = max(candidates, key=lambda row: row[0])[1]
        return release_info(selected), "api"

    def _apply_check(self, entry, cache, channel, *, cached):
        info = self._cached_info(entry, channel=channel)
        error = str(entry.get("error", ""))[:500]
        source = entry.get("source", "")
        source = source if source in {"api", "public"} else ""
        message = error
        if not message:
            if source == "public":
                message = "Проверка выполнена через официальные файлы релиза GitHub."
            if cached:
                message = "Используем результат последней проверки. " + message
        with self.lock:
            self._require_open()
            if channel != self.store.get("updateChannel", "stable"):
                raise OperationCancelled("Канал обновлений изменился. Проверяем выбранный канал заново.")
            if info:
                self.release, self.checked, self.release_channel = info, number(entry.get("checkedAt")), channel
            self.error = _friendly_error(error) if error else ""
            self._check_failed = bool(error)
            if error:
                self._auto_candidate = None
            self.check_status = {"source": source, "message": message.strip(), "retryAt": number(entry.get("nextAt")),
                                 "apiRetryAt": number(cache["api"].get("retryAt")), "cached": cached,
                                 "stale": bool(error and info)}
            self._save()
        if error:
            self._cancel_pending(automatic_only=True)
            raise ValueError(self.error)
        if not info:
            raise ValueError("В кэше обновлений нет проверенных данных релиза.")
        return info

    def _result_message(self, info):
        if not info["compatible"]:
            return "Новая версия требует другой формат данных. Автоматическая установка недоступна."
        if version_key(info["version"]) > version_key(self.current_version):
            return "Доступен BlockHarbor " + info["version"] + "."
        return "У тебя актуальная версия BlockHarbor."

    def _check(self, status):
        self._require_open()
        if not self.check_lock.acquire(blocking=False):
            raise ValueError("Проверка обновлений уже выполняется.")
        try:
            status("Проверяем опубликованные версии BlockHarbor…")
            channel = self.store.get("updateChannel", "stable")
            with self.source_cache.lease() as cache:
                now = time.time()
                entry = cache["checks"].get(channel, {})
                info = self._cached_info(entry, channel=channel)
                if (now < number(entry.get("nextAt")) <= now + MAX_SERVER_RETRY
                    and (info or entry.get("error"))):
                    return self._result_message(self._apply_check(entry, cache, channel, cached=True))
                try:
                    info, source = self._lookup_release(cache, channel, status)
                    self._require_open()
                    self._validate_receipt(info, require_file=False)
                    now = time.time()
                    entry = {"info": info, "source": source, "checkedAt": now, "nextAt": now + CHECK_TTL, "error": "", "failures": 0}
                except OperationCancelled:
                    raise
                except Exception as exc:
                    failures = min(16, max(0, int(number(entry.get("failures")))) + 1)
                    # Keep the last verified result visible, but do not turn a
                    # failed check into permission for an automatic install.
                    prior = info or (dict(self.release) if self.release and self.release_channel == channel else None)
                    entry = {"info": prior, "source": entry.get("source", ""),
                             "checkedAt": number(entry.get("checkedAt"), self.checked),
                             "nextAt": retry_at(getattr(exc, "headers", {}), failures=failures),
                             "error": _friendly_error(exc), "failures": failures}
                cache["checks"][channel] = entry
                self.source_cache.save(cache)
                return self._result_message(self._apply_check(entry, cache, channel, cached=False))
        except OperationCancelled:
            raise
        except Exception as exc:
            with self.lock:
                if not self.closed.is_set():
                    self.error = _friendly_error(exc)
                    self._check_failed = True
                    self._auto_candidate = None
                    self._save()
            self._cancel_pending(automatic_only=True)
            if isinstance(exc, ValueError) and str(exc) == self.error:
                raise
            raise ValueError(self.error) from exc
        finally:
            self.check_lock.release()

    def _remember(self, info, path):
        # TransferManager already validated the bytes. A direct cache reuse is
        # separately hashed before this method is called.
        with self.lock:
            if self.closed.is_set():
                return
            self.receipts = [row for row in self.receipts if row["version"] != info["version"]]
            self.receipts.append({**info, "downloadedAt": time.time()})
            self.receipts = sorted(self.receipts, key=lambda row: row["downloadedAt"])[-6:]
            self.pending_downloads = [row for row in self.pending_downloads if row["version"] != info["version"]]
            if info["version"] == self.current_version:
                self.installed_compatibility = dict(info["compatibility"])
            self._save()

    def _enqueue(self, info, *, previous=False, automatic=False):
        self._require_open()
        self._validate_receipt(info, require_file=False)
        destination = self.cache / info["filename"]
        if destination.is_file() and not destination.is_symlink() and destination.stat().st_size == info["size"] and _sha256(destination) == info["sha256"]:
            self._require_open()
            self._remember(info, destination)
            return None
        with self.lock:
            self._require_open()
            self.pending_downloads = [row for row in self.pending_downloads if row["version"] != info["version"]]
            self.pending_downloads.append(info)
            self._save()
        self._require_open()
        task_id = self.transfers.submit(
            info["download"], destination,
            title=("Копия для отката · " if previous else "BlockHarbor · ") + info["version"],
            hashes={"sha256": info["sha256"]}, size=info["size"], max_size=MAX_INSTALLER,
            hosts=ASSET_HOSTS, category="launcher-update",
            on_complete=lambda path: self._remember(info, path),
        )
        with self.lock:
            if automatic:
                self._automatic_tasks.add(task_id)
            if self.closed.is_set() or (automatic and not self.automatic_enabled()):
                self.transfers.cancel(task_id)
        return task_id

    def _version_release(self, version):
        """Rollback metadata uses the same API deadline and cross-window cache."""
        version_key(version)
        tag = "v" + version
        with self.source_cache.lease() as cache:
            now = time.time()
            entry = cache["versions"].get(tag, {})
            info = self._cached_info(entry, version=version)
            if now < number(entry.get("nextAt")) <= now + MAX_SERVER_RETRY:
                if info:
                    return info
                if entry.get("error"):
                    raise ValueError(str(entry["error"])[:500])
            try:
                try:
                    release = self._api_json(cache, API + "/releases/tags/" + tag)
                except _UsePublicRelease:
                    info = public_release(tag=tag)
                else:
                    info = release_info(release)
                self._validate_receipt(info, require_file=False)
                if info["version"] != version:
                    raise ValueError("Релиз для отката содержит другую версию.")
                entry = {"info": info, "nextAt": time.time() + CHECK_TTL, "error": ""}
            except OperationCancelled:
                raise
            except Exception as exc:
                entry = {"info": None, "nextAt": retry_at(getattr(exc, "headers", {})), "error": _friendly_error(exc)}
            cache["versions"][tag] = entry
            self.source_cache.save(cache)
            if entry["error"]:
                raise ValueError(entry["error"])
            return info

    def download(self):
        self._require_open()
        with self.lock:
            if not self.release:
                raise ValueError("Сначала проверь обновления.")
            info = dict(self.release)
        if version_key(info["version"]) <= version_key(self.current_version):
            raise ValueError("Более новая версия пока не опубликована.")
        if not info["compatible"]:
            raise ValueError("Эта версия не поддерживает текущий формат данных.")
        task_id = self._enqueue(info)

        def retain_current():
            try:
                with bind(self.scope):
                    self._require_open()
                    if not self._ready(self.current_version):
                        prior = self._version_release(self.current_version)
                        self._enqueue(prior, previous=True)
            except Exception:
                # The running executable is preserved independently at handoff.
                pass

        self._spawn(retain_current, name="bh-update-rollback")
        return {"ok": True, "task": task_id, "ready": task_id is None}

    def schedule_install(self, *, rollback=False, when="now", acknowledge=False, automatic=False):
        self._require_open()
        if automatic and (rollback or not self.automatic_enabled()):
            raise OperationCancelled("Автоматическая установка отменена.")
        if os.name != "nt":
            raise ValueError("Автоустановка этого установщика доступна в Windows.")
        if not self.request_shutdown:
            raise ValueError("Заверши текущий запуск лаунчера и открой установленное приложение.")
        if when not in {"now", "idle"}:
            raise ValueError("Неизвестное время установки.")
        if rollback:
            if acknowledge is not True:
                raise ValueError("Подтверди возврат к предыдущей версии приложения.")
            selection = self._rollback()
            if not selection:
                raise ValueError("Совместимая предыдущая версия пока не сохранена.")
        else:
            if self.release_channel != self.store.get("updateChannel", "stable"):
                raise ValueError("Проверь обновления в выбранном канале ещё раз.")
            ready = self._ready((self.release or {}).get("version", ""))
            if not ready or version_key(ready["version"]) <= version_key(self.current_version):
                raise ValueError("Сначала скачай и проверь новый установщик.")
            comp = ready["compatibility"]
            if not comp["minDataSchema"] <= DATA_SCHEMA <= comp["maxDataSchema"]:
                raise ValueError("Установщик несовместим с текущим форматом данных.")
            selection = {"kind": "installer", "version": ready["version"], "receipt": ready}
        if when == "now" and not self.can_install():
            raise ValueError("Закрой все Minecraft и остальные окна BlockHarbor, затем дождись завершения операций перед установкой.")
        with self.lock:
            self._require_open()
            if automatic and (not self.automatic_enabled() or self._check_failed):
                raise OperationCancelled("Автоматическая установка отменена.")
            if self.pending or self.install_lock.locked() or self._cancelling_handoff:
                raise ValueError("Установка уже ожидает завершения игры.")
            if not automatic:
                self._next_install_attempt = 0
            self.cancel_install.clear()
            self.pending = {"version": selection["version"], "rollback": rollback, "stage": "queued", "selection": selection, "automatic": automatic}
            self.error = ""
            self._save()
        self.monitor_pending()
        return {"ok": True, "scheduled": True}

    def monitor_pending(self):
        """Call from the app's existing idle/process monitor, including during play."""
        with self.lock:
            if (self.closed.is_set() or not self.pending or self.pending["stage"] != "queued"
                or time.monotonic() < self._next_install_attempt or not self.can_install()
                or (self.pending.get("automatic") and (not self.automatic_enabled() or self._check_failed))):
                return
            self.pending["stage"] = "preparing"
            self._save()
        try:
            self._spawn(self._perform_install, name="bh-update-handoff")
        except OperationCancelled:
            pass

    def _launcher(self):
        supplied = os.environ.get("BLOCKHARBOR_LAUNCHER_EXE", "")
        if supplied:
            target = Path(supplied)
            if target.is_absolute() and target.name.lower() == "blockharbor.exe" and target.is_file() and not target.is_symlink():
                return target
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\BlockHarbor", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                target = Path(winreg.QueryValueEx(key, "InstallLocation")[0]) / "BlockHarbor.exe"
            if target.is_absolute() and target.is_file() and not target.is_symlink():
                return target
        except (ImportError, OSError, TypeError):
            pass
        raise ValueError("Не найден установленный BlockHarbor.exe. Открой лаунчер через его ярлык и повтори.")

    def _preserve_current(self, target):
        folder = self.root / "rollback"
        if folder.is_symlink():
            raise ValueError("Папка резервной версии перенаправлена.")
        folder.mkdir(exist_ok=True)
        destination = folder / ("BlockHarbor-" + self.current_version + ".exe")
        temp = folder / (secrets.token_hex(12) + ".tmp")
        try:
            with target.open("rb") as source, temp.open("xb") as output:
                while block := source.read(1024 * 1024):
                    self._require_open()
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            digest = _sha256(temp)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
        next_compatibility = ((self.pending or {}).get("selection", {}).get("receipt") or {}).get("compatibility", {})
        rollback_safe = next_compatibility.get("rollbackSafe", True) is True and next_compatibility.get("dataSchema", DATA_SCHEMA) == DATA_SCHEMA
        self.rollback_binary = {"version": self.current_version, "target": str(target), "sha256": digest, "size": destination.stat().st_size, "dataSchema": DATA_SCHEMA, "rollbackSafe": rollback_safe}
        # SQLite's backup API produces a consistent snapshot even in WAL mode.
        backup = folder / ("settings-before-" + self.current_version + "-" + secrets.token_hex(6) + ".sqlite3")
        with sqlite3.connect(self.store.db) as source, sqlite3.connect(backup) as destination_db:
            source.backup(destination_db, pages=128, progress=lambda *_: self._require_open())
        self._save()

    def _perform_install(self):
        try:
            with bind(self.scope):
                self._require_open()
                if self.install_guard is not None:
                    with self.install_guard():
                        return self._perform_install_guarded()
                return self._perform_install_guarded()
        except OperationCancelled:
            return
        except Exception as exc:
            with self.lock:
                if not self.closed.is_set():
                    self.error = str(exc)[:600]
                    if self.pending and self.pending.get("stage") != "launching":
                        self.pending["stage"] = "queued"
                    self._next_install_attempt = time.monotonic() + 15
                    self._save()

    def _require_install(self, pending):
        self._require_open()
        if (self.cancel_install.is_set() or self.pending is not pending
            or (pending.get("automatic") and not self.automatic_enabled())):
            raise OperationCancelled("Установка отменена.")

    def _perform_install_guarded(self):
        if not self.install_lock.acquire(blocking=False):
            return
        process = None
        pending = None
        try:
            with self.lock:
                if not self.pending:
                    return
                pending = self.pending
                self._require_install(pending)
                selection = self.pending["selection"]
                rollback = self.pending["rollback"]
            if not self.can_install():
                with self.lock:
                    if self.pending:
                        self.pending["stage"] = "queued"
                return
            receipt = selection["receipt"]
            if selection["kind"] == "binary":
                binary = self.root / "rollback" / ("BlockHarbor-" + selection["version"] + ".exe")
            else:
                self._validate_receipt(receipt, require_file=True)
                binary = self.cache / receipt["filename"]
            if binary.is_symlink() or _sha256(binary) != receipt["sha256"]:
                raise ValueError("Контрольная сумма сохранённой версии изменилась. Скачай установщик заново.")
            target = self._launcher()
            if not rollback:
                self._preserve_current(target)
            self._require_install(pending)
            if not self.can_install():
                with self.lock:
                    if self.pending:
                        self.pending["stage"] = "queued"
                return
            request_id = secrets.token_hex(12)
            request_path = self.root / ("handoff-" + request_id + ".json")
            ready_path = self.root / ("ready-" + request_id + ".json")
            commit_path = self.root / ("commit-" + request_id + ".json")
            bootstrap = os.environ.get("BLOCKHARBOR_BOOTSTRAP_PID", "")
            wait_pids = [os.getpid()]
            if bootstrap.isdecimal() and int(bootstrap) > 0 and int(bootstrap) != os.getpid():
                wait_pids.append(int(bootstrap))
            _atomic_json(request_path, {
                "format": 1, "kind": selection["kind"], "file": str(binary),
                "sha256": receipt["sha256"], "size": binary.stat().st_size,
                "target": str(target), "version": selection["version"],
                "waitPids": wait_pids, "ready": str(ready_path), "id": request_id,
                "commit": str(commit_path), "automatic": bool(pending.get("automatic")),
                **(self.handoff_metadata() if self.handoff_metadata else {}),
            })
            log = (self.root / "handoff.log").open("ab")
            try:
                with self.lock:
                    self._require_install(pending)
                    process = subprocess.Popen(
                        [sys.executable, "-B", "-X", "utf8", str(Path(__file__).with_name("update_helper.py")), str(request_path)],
                        cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0),
                    )
                    self._helper_process, self._handoff_commit, self._handoff_request = process, commit_path, request_path
                    self._handoff_committed = False
                    self._require_install(pending)
            finally:
                log.close()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                self.cancel_install.wait(0.1)
                self._require_install(pending)
                if ready_path.is_file():
                    result = json.loads(ready_path.read_text(encoding="utf-8"))
                    if result.get("id") != request_id or result.get("ready") is not True:
                        raise ValueError("Помощник установки не подтвердил готовность.")
                    break
                if process.poll() is not None:
                    raise ValueError("Помощник установки завершился раньше времени. Подробности в журнале обновления.")
            else:
                raise ValueError("Помощник установки не ответил. Лаунчер остаётся открытым.")
            if not self.can_install():
                self._stop_helper(process)
                with self.lock:
                    if self.pending:
                        self.pending["stage"] = "queued"
                        self._save()
                return
            with self.lock:
                self._require_install(pending)
                _atomic_json(commit_path, {"id": request_id, "install": True})
                self._handoff_committed = True
                self.pending["stage"] = "launching"
                self.pending["handoff"] = request_id
                self._save()
            self.request_shutdown()
        except OperationCancelled:
            self._stop_helper(process)
            with self.lock:
                if self.pending is pending:
                    self.pending = None
                self._handoff_committed = False
                if self._handoff_commit:
                    self._handoff_commit.unlink(missing_ok=True)
                self._save()
        except Exception as exc:
            self._stop_helper(process)
            with self.lock:
                if not self.closed.is_set():
                    self.error = str(exc)[:600]
                    if pending and pending.get("automatic"):
                        self._auto_failure = {"version": pending["version"], "message": self.error}
                self._handoff_committed = False
                if self._handoff_commit:
                    self._handoff_commit.unlink(missing_ok=True)
                self.pending = None
                self._save()
        finally:
            self.install_lock.release()

    def handle(self, action, params, status=lambda text: None):
        if action == "updateState":
            return self.state()
        if action == "updateCheck":
            return self.check(status)
        if action == "updateAutomatic":
            return self.set_automatic(params.get("enabled"))
        if action == "updateChannel":
            channel = params.get("channel")
            if channel not in {"stable", "test"}:
                raise ValueError("Выбери стабильный или тестовый канал обновлений.")
            with self.lock:
                if self.pending:
                    raise ValueError("Сначала отмени запланированную установку.")
                self.store.set("updateChannel", channel)
                self.release, self.checked = None, 0
                self.release_channel = channel
                self.error = ""
                self.check_status = {"source": "", "message": "", "retryAt": 0, "apiRetryAt": 0, "cached": False, "stale": False}
                self._save()
            self._cancel_automatic()
            with self.lock:
                self._observed_channel = channel
                self._next_auto_check = 0
            self.tick_auto()
            return {"ok": True}
        if action == "updateDownload":
            return self.download()
        if action in {"updateInstall", "updateRollback"}:
            return self.schedule_install(rollback=action == "updateRollback", when=params.get("when", "now"), acknowledge=params.get("acknowledge", False))
        if action == "updateCancelInstall":
            with self.lock:
                if self.pending and self.pending["stage"] == "launching" and not self.pending.get("automatic"):
                    raise ValueError("Лаунчер уже передал управление установщику.")
                if self.pending and self.pending.get("automatic"):
                    self._auto_snoozed = self.pending["version"]
            self._cancel_pending()
            return {"ok": True}
        if action == "transferCancel":
            return self.transfers.cancel(params.get("id"))
        if action == "transferRetry":
            return self.transfers.retry(params.get("id"))
        if action == "transferClear":
            return self.transfers.clear()
        raise ValueError("Неизвестное действие обновлений.")
