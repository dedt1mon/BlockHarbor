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
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from .transfers import _atomic_json, open_https

REPOSITORY = "dedt1mon/BlockHarbor"
API = "https://api.github.com/repos/" + REPOSITORY
WEB = "https://github.com/" + REPOSITORY
ASSET_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
DATA_SCHEMA = 1
MAX_INSTALLER = 512 * 1024**2
VERSION_RE = re.compile(r"v?(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})(?:-(alpha|beta|rc)\.(0|[1-9]\d{0,4}))?\Z")


def version_key(value):
    match = VERSION_RE.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("Неподдерживаемый номер версии BlockHarbor.")
    major, minor, patch, channel, serial = match.groups()
    return (int(major), int(minor), int(patch), {"alpha": 0, "beta": 1, "rc": 2, None: 3}[channel], int(serial or 0))


def _read_json(url, *, hosts={"api.github.com"}, limit=2 * 1024 * 1024):
    with open_https(url, hosts, headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Ответ сервиса обновлений слишком большой.")
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Сервис обновлений вернул нечитаемый ответ.") from exc


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
        with open_https(sums["browser_download_url"], ASSET_HOSTS) as response:
            raw = response.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError("Список контрольных сумм слишком большой.")
        matches = re.findall(r"^([a-fA-F0-9]{64})[ \t]+\*?" + re.escape(filename) + r"\r?$", raw.decode("utf-8"), re.M)
        if len(matches) != 1:
            raise ValueError("Для установщика не опубликована контрольная сумма SHA-256.")
        digest = "sha256:" + matches[0]
    # The only legacy format supported here is the published baseline. New
    # versions must state their data compatibility explicitly in the release.
    if key <= version_key("1.10.0") and key[0] == 1:
        compatibility = {"format": 1, "version": version, "dataSchema": 1, "minDataSchema": 1, "maxDataSchema": 1, "rollbackSafe": True}
    else:
        manifest = _asset(release, "BlockHarbor-update.json", 64 * 1024)
        compatibility = _read_json(manifest["browser_download_url"], hosts=ASSET_HOSTS, limit=64 * 1024)
    if (
        not isinstance(compatibility, dict)
        or compatibility.get("format") != 1
        or compatibility.get("version") != version
        or any(type(compatibility.get(field)) is not int for field in ("dataSchema", "minDataSchema", "maxDataSchema"))
        or not 1 <= compatibility["minDataSchema"] <= compatibility["maxDataSchema"] <= 10000
        or not 1 <= compatibility["dataSchema"] <= 10000
        or type(compatibility.get("rollbackSafe")) is not bool
    ):
        raise ValueError("Манифест обновления не описывает совместимость данных.")
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


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


class UpdateService:
    ACTIONS = {
        "updateState", "updateCheck", "updateChannel", "updateDownload", "updateInstall",
        "updateCancelInstall", "updateRollback", "transferCancel", "transferRetry", "transferClear",
    }
    JOBS = {"updateCheck"}

    def __init__(self, store, transfers, current_version="2.0.0", *, can_install=lambda: False, request_shutdown=None):
        version_key(current_version)
        self.store, self.transfers = store, transfers
        self.current_version = current_version
        self.can_install, self.request_shutdown = can_install, request_shutdown
        self.root = store.root / "updates"
        if self.root.is_symlink():
            raise ValueError("Папка обновлений перенаправлена.")
        self.root.mkdir(exist_ok=True)
        self.cache = self.root / "installers"
        if self.cache.is_symlink():
            raise ValueError("Папка установщиков перенаправлена.")
        self.cache.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.check_lock = threading.Lock()
        self.install_lock = threading.Lock()
        self.cancel_install = threading.Event()
        self.release = None
        self.checked = 0
        self.error = ""
        self.receipts = []
        self.pending_downloads = []
        self.pending = None
        self.rollback_binary = None
        self.installed_compatibility = {"version": current_version, "dataSchema": DATA_SCHEMA, "rollbackSafe": True}
        self._load()

    def _load(self):
        path = self.root / "state.json"
        try:
            if path.is_symlink() or path.stat().st_size > 512 * 1024:
                return
            data = json.loads(path.read_text(encoding="utf-8"))
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
            # A crashed launch must not unexpectedly close a later user session.
            if data.get("pending") and data["pending"].get("version") != self.current_version:
                self.error = "Предыдущая отложенная установка не завершилась. Нажми «Установить», когда будешь готов."
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
                "release": self.release, "checked": self.checked, "receipts": self.receipts[-6:],
                "downloads": self.pending_downloads[-6:], "rollbackBinary": self.rollback_binary, "pending": self.pending,
                "installedCompatibility": self.installed_compatibility,
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
            release = dict(self.release) if self.release else None
            ready = self._ready(release["version"]) if release else None
            rollback = self._rollback()
            return {
                "current": self.current_version,
                "channel": self.store.get("updateChannel", "stable"),
                "checkedAt": self.checked,
                "release": release,
                "available": bool(release and version_key(release["version"]) > version_key(self.current_version)),
                "ready": bool(ready),
                "pending": {key: self.pending[key] for key in ("version", "rollback", "stage")} if self.pending else None,
                "error": self.error,
                "windows": os.name == "nt",
                "rollback": {"version": rollback["version"], "kind": rollback["kind"]} if rollback else None,
                "trust": "Обновления загружаются из dedt1mon/BlockHarbor по HTTPS и проверяются по SHA-256. Подписи издателя Windows пока нет.",
                "transfers": self.transfers.snapshot(),
            }

    def check(self, status=lambda text: None):
        if not self.check_lock.acquire(blocking=False):
            raise ValueError("Проверка обновлений уже выполняется.")
        try:
            status("Проверяем опубликованные версии BlockHarbor…")
            rows = _read_json(API + "/releases?per_page=30")
            if not isinstance(rows, list):
                raise ValueError("GitHub вернул неверный список релизов.")
            stable = self.store.get("updateChannel", "stable") == "stable"
            candidates = []
            for release in rows[:30]:
                if not isinstance(release, dict) or release.get("draft"):
                    continue
                try:
                    key = version_key(release.get("tag_name"))
                except ValueError:
                    continue
                if stable and (release.get("prerelease") or key[3] < 3):
                    continue
                candidates.append((key, release))
            if not candidates:
                raise ValueError("В выбранном канале пока нет опубликованных установщиков.")
            # Do not silently install an older release if the latest is malformed.
            selected = max(candidates, key=lambda item: item[0])[1]
            info = release_info(selected)
            with self.lock:
                self.release, self.checked, self.error = info, time.time(), ""
                self._save()
            if not info["compatible"]:
                return "Новая версия требует другой формат данных. Автоматическая установка недоступна."
            if version_key(info["version"]) > version_key(self.current_version):
                return "Доступен BlockHarbor " + info["version"] + "."
            return "У тебя актуальная версия BlockHarbor."
        except Exception as exc:
            self.error = str(exc)[:500]
            raise
        finally:
            self.check_lock.release()

    def _remember(self, info, path):
        # TransferManager already validated the bytes. A direct cache reuse is
        # separately hashed before this method is called.
        with self.lock:
            self.receipts = [row for row in self.receipts if row["version"] != info["version"]]
            self.receipts.append({**info, "downloadedAt": time.time()})
            self.receipts = sorted(self.receipts, key=lambda row: row["downloadedAt"])[-6:]
            self.pending_downloads = [row for row in self.pending_downloads if row["version"] != info["version"]]
            if info["version"] == self.current_version:
                self.installed_compatibility = dict(info["compatibility"])
            self._save()

    def _enqueue(self, info, *, previous=False):
        self._validate_receipt(info, require_file=False)
        destination = self.cache / info["filename"]
        if destination.is_file() and not destination.is_symlink() and destination.stat().st_size == info["size"] and _sha256(destination) == info["sha256"]:
            self._remember(info, destination)
            return None
        with self.lock:
            self.pending_downloads = [row for row in self.pending_downloads if row["version"] != info["version"]]
            self.pending_downloads.append(info)
            self._save()
        return self.transfers.submit(
            info["download"], destination,
            title=("Копия для отката · " if previous else "BlockHarbor · ") + info["version"],
            hashes={"sha256": info["sha256"]}, size=info["size"], max_size=MAX_INSTALLER,
            hosts=ASSET_HOSTS, category="launcher-update",
            on_complete=lambda path: self._remember(info, path),
        )

    def download(self):
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
                if not self._ready(self.current_version):
                    prior = release_info(_read_json(API + "/releases/tags/v" + self.current_version))
                    self._enqueue(prior, previous=True)
            except Exception:
                # The running executable is preserved independently at handoff.
                pass

        threading.Thread(target=retain_current, daemon=True, name="bh-update-rollback").start()
        return {"ok": True, "task": task_id, "ready": task_id is None}

    def schedule_install(self, *, rollback=False, when="now", acknowledge=False):
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
            ready = self._ready((self.release or {}).get("version", ""))
            if not ready or version_key(ready["version"]) <= version_key(self.current_version):
                raise ValueError("Сначала скачай и проверь новый установщик.")
            comp = ready["compatibility"]
            if not comp["minDataSchema"] <= DATA_SCHEMA <= comp["maxDataSchema"]:
                raise ValueError("Установщик несовместим с текущим форматом данных.")
            selection = {"kind": "installer", "version": ready["version"], "receipt": ready}
        if when == "now" and not self.can_install():
            raise ValueError("Закрой Minecraft и дождись завершения операций перед установкой.")
        with self.lock:
            if self.pending or self.install_lock.locked():
                raise ValueError("Установка уже ожидает завершения игры.")
            self.cancel_install.clear()
            self.pending = {"version": selection["version"], "rollback": rollback, "stage": "queued", "selection": selection}
            self.error = ""
            self._save()
        self.monitor_pending()
        return {"ok": True, "scheduled": True}

    def monitor_pending(self):
        """Call from the app's existing idle/process monitor, including during play."""
        with self.lock:
            if not self.pending or self.pending["stage"] != "queued" or not self.can_install():
                return
            self.pending["stage"] = "preparing"
            self._save()
        threading.Thread(target=self._perform_install, daemon=True, name="bh-update-handoff").start()

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
                shutil.copyfileobj(source, output, 1024 * 1024)
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
            source.backup(destination_db)
        self._save()

    def _perform_install(self):
        if not self.install_lock.acquire(blocking=False):
            return
        process = None
        try:
            with self.lock:
                if not self.pending:
                    return
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
            if self.cancel_install.is_set():
                return
            if not self.can_install():
                with self.lock:
                    if self.pending:
                        self.pending["stage"] = "queued"
                return
            request_id = secrets.token_hex(12)
            request_path = self.root / ("handoff-" + request_id + ".json")
            ready_path = self.root / ("ready-" + request_id + ".json")
            bootstrap = os.environ.get("BLOCKHARBOR_BOOTSTRAP_PID", "")
            wait_pids = [os.getpid()]
            if bootstrap.isdecimal() and int(bootstrap) > 0 and int(bootstrap) != os.getpid():
                wait_pids.append(int(bootstrap))
            _atomic_json(request_path, {
                "format": 1, "kind": selection["kind"], "file": str(binary),
                "sha256": receipt["sha256"], "size": binary.stat().st_size,
                "target": str(target), "version": selection["version"],
                "waitPids": wait_pids, "ready": str(ready_path), "id": request_id,
            })
            log = (self.root / "handoff.log").open("ab")
            try:
                process = subprocess.Popen(
                    [sys.executable, "-B", "-X", "utf8", str(Path(__file__).with_name("update_helper.py")), str(request_path)],
                    cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0),
                )
            finally:
                log.close()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if self.cancel_install.wait(0.1):
                    process.terminate()
                    return
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
                process.terminate()
                with self.lock:
                    if self.pending:
                        self.pending["stage"] = "queued"
                        self._save()
                return
            with self.lock:
                if not self.pending:
                    process.terminate()
                    return
                self.pending["stage"] = "launching"
                self._save()
            self.request_shutdown()
        except Exception as exc:
            if process and process.poll() is None:
                process.terminate()
            with self.lock:
                self.error = str(exc)[:600]
                self.pending = None
                self._save()
        finally:
            self.install_lock.release()

    def handle(self, action, params, status=lambda text: None):
        if action == "updateState":
            return self.state()
        if action == "updateCheck":
            return self.check(status)
        if action == "updateChannel":
            channel = params.get("channel")
            if channel not in {"stable", "test"}:
                raise ValueError("Выбери стабильный или тестовый канал обновлений.")
            with self.lock:
                if self.pending:
                    raise ValueError("Сначала отмени запланированную установку.")
                self.store.set("updateChannel", channel)
                self.release, self.checked = None, 0
                self._save()
            return {"ok": True}
        if action == "updateDownload":
            return self.download()
        if action in {"updateInstall", "updateRollback"}:
            return self.schedule_install(rollback=action == "updateRollback", when=params.get("when", "now"), acknowledge=params.get("acknowledge", False))
        if action == "updateCancelInstall":
            with self.lock:
                if self.pending and self.pending["stage"] == "launching":
                    raise ValueError("Лаунчер уже передал управление установщику.")
                self.cancel_install.set()
                self.pending = None
                self._save()
            return {"ok": True}
        if action == "transferCancel":
            return self.transfers.cancel(params.get("id"))
        if action == "transferRetry":
            return self.transfers.retry(params.get("id"))
        if action == "transferClear":
            return self.transfers.clear()
        raise ValueError("Неизвестное действие обновлений.")
