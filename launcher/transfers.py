"""Bounded, verified HTTPS transfers with an observable queue and safe resume.

The caller supplies trusted hosts and hashes obtained from its catalogue. This is
not a URL-to-file API exposed to the browser. Completed files become visible only
after all supplied hashes and sizes have been checked.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import threading
import time
from contextlib import ExitStack
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import lifecycle

MAX_BYTES = 8 * 1024**3
MAX_HISTORY = 100
MAX_PENDING = 24
CHUNK = 128 * 1024
ACTIVE = {"queued", "downloading", "verifying"}


class TransferCancelled(ValueError):
    pass


def allowed_url(url, hosts):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 32 for c in url):
        raise ValueError("Недопустимый адрес загрузки.")
    try:
        parsed = urlsplit(url)
        good = (
            parsed.scheme == "https"
            and parsed.hostname in hosts
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and "\\" not in url
        )
    except ValueError:
        good = False
    if not good:
        raise ValueError("Источник загрузки не входит в разрешённый список HTTPS.")
    return url


class SafeRedirect(HTTPRedirectHandler):
    max_repeats = 3
    max_redirections = 5

    def __init__(self, hosts):
        self.hosts = frozenset(hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_https(url, hosts, *, headers=None, timeout=15):
    allowed_url(url, hosts)
    request = Request(
        url,
        headers={
            "User-Agent": "BlockHarbor/2.4.3 (desktop launcher; dedt1mon/BlockHarbor)",
            "Accept-Encoding": "identity",
            **(headers or {}),
        },
    )
    return build_opener(SafeRedirect(hosts)).open(request, timeout=timeout)


def _atomic_json(path, data):
    if path.is_symlink():
        raise ValueError("Файл состояния загрузок перенаправлен.")
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _hashes(value):
    if not isinstance(value, dict) or not value or len(value) > 3:
        raise ValueError("Для загрузки нужна контрольная сумма файла.")
    result = {}
    for algorithm, digest in value.items():
        if algorithm not in {"sha1", "sha256", "sha512"}:
            raise ValueError("Неподдерживаемый алгоритм контрольной суммы.")
        if not isinstance(digest, str) or not re.fullmatch(
            "[0-9a-fA-F]{%d}" % (hashlib.new(algorithm).digest_size * 2), digest
        ):
            raise ValueError("Некорректная контрольная сумма файла.")
        result[algorithm] = digest.lower()
    return result


class TransferManager:
    """Two network slots, one durable background queue, and synchronous callers.

    ``download`` is for callers that perform their own commit after a transfer;
    retrying the containing operation is required after a failure. ``submit`` is
    for durable cache files whose complete operation is the download itself and
    therefore exposes safe independent retries in the download centre.
    """

    def __init__(self, root, *, namespace=""):
        self.root = Path(root).resolve()
        self.cache = self.root / "download-cache"
        if namespace:
            if not re.fullmatch(r"window-[1-9][0-9]{0,2}", namespace):
                raise ValueError("Некорректный идентификатор окна загрузок.")
            if self.cache.is_symlink() or self.cache.resolve().parent != self.root:
                raise ValueError("Кэш загрузок перенаправлен.")
            self.cache = self.cache / namespace
        if self.cache.is_symlink():
            raise ValueError("Кэш загрузок перенаправлен.")
        self.cache.mkdir(parents=True, exist_ok=True)
        self.index = self.cache / "tasks.json"
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)
        self.tasks = {}
        self.cancel_flags = {}
        self.callbacks = {}
        self.responses = {}
        self.runners = set()
        self.scope = lifecycle.CancellationScope()
        self.waiting = queue.Queue()
        self._load()
        self.worker = threading.Thread(target=self._worker, daemon=True, name="bh-downloads")
        self.worker.start()

    def active(self):
        with self.lock:
            return any(row["state"] in ACTIVE for row in self.tasks.values())

    def close(self, timeout=2):
        """Cancel this window's queue and I/O; never resume it after closing."""
        self.scope.cancel()
        with self.lock:
            for task_id, row in self.tasks.items():
                if row["state"] in ACTIVE:
                    self.cancel_flags[task_id].set()
                    if row["state"] == "queued":
                        row.update(state="cancelled", message="Лаунчер закрыт. Загрузку можно повторить.", rate=0)
            self.callbacks.clear()
            while True:
                try:
                    self.waiting.get_nowait()
                except queue.Empty:
                    break
                self.waiting.task_done()
            self.waiting.put(None)
            self._persist()
            threads = set(self.runners) | {self.worker}
        deadline = time.monotonic() + max(0, timeout)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))

    def _inside(self, destination):
        path = Path(destination)
        if not path.is_absolute():
            path = self.root / path
        path = Path(os.path.abspath(path))
        if not path.resolve().is_relative_to(self.root) or path == self.root:
            raise ValueError("Файл загрузки должен находиться в данных BlockHarbor.")
        check = path
        while check != self.root:
            if check.is_symlink():
                raise ValueError("Папка или файл загрузки перенаправлены.")
            check = check.parent
        return path

    def _load(self):
        try:
            if self.index.is_symlink() or self.index.stat().st_size > 2 * 1024 * 1024:
                return
            saved = json.loads(self.index.read_text(encoding="utf-8"))
            for row in saved[-MAX_HISTORY:]:
                if (not isinstance(row, dict)
                    or not isinstance(row.get("id"), str)
                    or not re.fullmatch(r"[a-f0-9]{24}", row["id"])
                    or not isinstance(row.get("created"), (int, float))
                    or not isinstance(row.get("config"), dict)
                    or row.get("state") not in ACTIVE | {"completed", "cancelled", "failed", "interrupted"}):
                    continue
                config = row["config"]
                self._inside(config["destination"])
                allowed_url(config["url"], config["hosts"])
                _hashes(config["hashes"])
                if row.get("state") in ACTIVE:
                    row.update(state="interrupted", message="Лаунчер был закрыт. Загрузку можно повторить.", rate=0)
                # Failed synchronous jobs need their containing operation to be
                # repeated; their temporary destinations cannot be resurrected.
                if not row.get("background"):
                    row["retryable"] = False
                self.tasks[row["id"]] = row
                self.cancel_flags[row["id"]] = threading.Event()
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            self.tasks.clear()

    def _persist(self):
        with self.lock:
            rows = sorted(self.tasks.values(), key=lambda r: r["created"])
            _atomic_json(self.index, rows)

    def _trim(self):
        completed = sorted(
            (row for row in self.tasks.values() if row["state"] not in ACTIVE),
            key=lambda row: row["created"],
        )
        while len(self.tasks) >= MAX_HISTORY and completed:
            self._discard(completed.pop(0)["id"])

    def _discard(self, task_id):
        self.tasks.pop(task_id, None)
        self.cancel_flags.pop(task_id, None)
        self.callbacks.pop(task_id, None)
        (self.cache / (task_id + ".part")).unlink(missing_ok=True)
        (self.cache / (task_id + ".json")).unlink(missing_ok=True)

    def snapshot(self):
        with self.lock:
            fields = (
                "id", "title", "category", "state", "message", "bytes", "total",
                "rate", "created", "updated", "attempt", "retryable", "host", "resumed",
            )
            rows = [
                {key: row.get(key) for key in fields}
                for row in sorted(self.tasks.values(), key=lambda item: item["created"], reverse=True)
            ]
        return {"tasks": rows, "active": sum(row["state"] in ACTIVE for row in rows)}

    def restore_callback(self, destination, callback):
        """Reconnect a durable cache consumer after an application restart."""
        destination = str(self._inside(destination))
        with self.lock:
            self.scope.check()
            for task_id, row in self.tasks.items():
                if row.get("background") and row.get("config", {}).get("destination") == destination:
                    self.callbacks[task_id] = callback

    def _new(self, url, destination, title, hashes, size, max_size, hosts, category, resume, background):
        if not isinstance(hosts, (set, frozenset, list, tuple)) or not hosts or len(hosts) > 20:
            raise ValueError("Для загрузки необходим разрешённый источник.")
        allowed_url(url, hosts)
        destination = self._inside(destination)
        hashes = _hashes(hashes)
        if type(max_size) is not int or not 0 < max_size <= MAX_BYTES:
            raise ValueError("Некорректный предел размера загрузки.")
        if size is not None and (type(size) is not int or not 0 <= size <= max_size):
            raise ValueError("Некорректный размер загрузки.")
        with self.lock:
            self.scope.check()
            if sum(r["state"] in ACTIVE for r in self.tasks.values()) >= MAX_PENDING:
                raise ValueError("Очередь загрузок заполнена. Дождись завершения текущих файлов.")
            if any(
                r["state"] in ACTIVE and r.get("config", {}).get("destination") == str(destination)
                for r in self.tasks.values()
            ):
                raise ValueError("Этот файл уже загружается.")
            self._trim()
            task_id = secrets.token_hex(12)
            # A synchronous operation may stage into a new temporary directory
            # when the user retries it. Reuse only an inactive partial with the
            # exact same URL, hashes and declared size; destination paths never
            # determine content identity.
            if resume:
                for previous in sorted(self.tasks.values(), key=lambda item: item["updated"], reverse=True):
                    prior = previous.get("config", {})
                    if previous["state"] in ACTIVE or prior.get("url") != url or prior.get("hashes") != hashes or prior.get("size") != size:
                        continue
                    source = self.cache / (previous["id"] + ".part")
                    meta = self.cache / (previous["id"] + ".json")
                    if source.is_file() and not source.is_symlink() and meta.is_file() and not meta.is_symlink():
                        source.rename(self.cache / (task_id + ".part"))
                        meta.rename(self.cache / (task_id + ".json"))
                        break
            self.tasks[task_id] = {
                "id": task_id,
                "title": str(title or destination.name)[:180],
                "category": str(category)[:40],
                "state": "queued",
                "message": "В очереди",
                "bytes": 0,
                "total": size,
                "rate": 0,
                "resumed": False,
                "created": time.time(),
                "updated": time.time(),
                "attempt": 0,
                "retryable": bool(background),
                "background": bool(background),
                "host": urlsplit(url).hostname,
                "config": {
                    "url": url, "destination": str(destination), "hashes": hashes,
                    "size": size, "max_size": max_size, "hosts": list(hosts), "resume": bool(resume),
                },
            }
            self.cancel_flags[task_id] = threading.Event()
            self._persist()
            return task_id

    def download(self, url, destination, *, title=None, hashes, size=None, max_size=MAX_BYTES,
                 hosts, category="content", status=lambda text: None, resume=True):
        task_id = self._new(url, destination, title, hashes, size, max_size, hosts, category, resume, False)
        return self._run(task_id, status)

    def submit(self, url, destination, *, title=None, hashes, size=None, max_size=MAX_BYTES,
               hosts, category="content", status=lambda text: None, resume=True, on_complete=None):
        task_id = self._new(url, destination, title, hashes, size, max_size, hosts, category, resume, True)
        with self.lock:
            self.scope.check()
            if on_complete:
                self.callbacks[task_id] = on_complete
            self.waiting.put((task_id, status))
        return task_id

    def _worker(self):
        while True:
            item = self.waiting.get()
            try:
                if item is None:
                    return
                task_id, status = item
                self._run(task_id, status)
            except Exception:
                pass  # The visible task contains the error; the worker stays alive.
            finally:
                self.waiting.task_done()

    def _update(self, task_id, **values):
        with self.lock:
            self.tasks[task_id].update(values, updated=time.time())

    def cancel(self, task_id):
        with self.lock:
            row = self.tasks.get(task_id)
            if not row or row["state"] not in ACTIVE:
                raise ValueError("Эта загрузка уже завершена.")
            self.cancel_flags[task_id].set()
            row["message"] = "Останавливаем загрузку…"
            if row["state"] == "queued":
                row.update(state="cancelled", message="Загрузка отменена.", rate=0)
            self._persist()
            opened = self.responses.get(task_id)
        if opened is not None:
            lifecycle.abort_response(opened)
        return {"ok": True}

    def retry(self, task_id):
        with self.lock:
            self.scope.check()
            row = self.tasks.get(task_id)
            if not row or row["state"] in ACTIVE or not row.get("retryable"):
                raise ValueError("Повтори исходную операцию, чтобы загрузить этот файл заново.")
            if row["state"] == "completed":
                raise ValueError("Файл уже загружен.")
            config = row["config"]
            self._inside(config["destination"])
            allowed_url(config["url"], config["hosts"])
            _hashes(config["hashes"])
            if sum(r["state"] in ACTIVE for r in self.tasks.values()) >= MAX_PENDING:
                raise ValueError("Очередь загрузок заполнена.")
            if any(
                r["id"] != task_id and r["state"] in ACTIVE
                and r.get("config", {}).get("destination") == config["destination"]
                for r in self.tasks.values()
            ):
                raise ValueError("Этот файл уже загружается другой задачей.")
            self.cancel_flags[task_id].clear()
            row.update(state="queued", message="В очереди", rate=0, updated=time.time())
            self._persist()
            self.waiting.put((task_id, lambda text: None))
        return {"ok": True, "task": task_id}

    def clear(self):
        with self.lock:
            for task_id in [key for key, row in self.tasks.items() if row["state"] not in ACTIVE]:
                self._discard(task_id)
            self._persist()
        return {"ok": True}

    def _check_cancel(self, task_id):
        if self.scope.event.is_set() or self.cancel_flags[task_id].is_set():
            raise TransferCancelled("Загрузка отменена. Уже скачанная часть сохранена для повтора, если источник поддерживает продолжение.")

    def _run(self, task_id, status):
        thread = threading.current_thread()
        with self.lock:
            self.runners.add(thread)
        try:
            with lifecycle.bind(self.scope):
                return self._run_scoped(task_id, status)
        except lifecycle.OperationCancelled as exc:
            self._update(task_id, state="cancelled", message="Лаунчер закрыт. Загрузку можно повторить.", rate=0)
            self._persist()
            raise TransferCancelled(str(exc)) from None
        finally:
            with self.lock:
                self.runners.discard(thread)

    def _run_scoped(self, task_id, status):
        acquired = False
        try:
            while not acquired:
                self._check_cancel(task_id)
                acquired = self.slots.acquire(timeout=0.2)
            self._check_cancel(task_id)
            with self.lock:
                config = dict(self.tasks[task_id]["config"])
                title = self.tasks[task_id]["title"]
            for attempt in range(1, 4):
                self._update(task_id, state="downloading", message="Загружаем файл…", attempt=attempt)
                self._persist()
                try:
                    result = self._receive(task_id, config, title, status)
                    self._check_cancel(task_id)
                    self._update(task_id, state="completed", message="Файл загружен и проверен.", rate=0)
                    self._persist()
                    with self.scope.guard():
                        callback = self.callbacks.get(task_id)
                        if callback:
                            callback(result)
                    return result
                except HTTPError as exc:
                    code = exc.code
                    exc.close()
                    if code not in {408, 429, 500, 502, 503, 504} or attempt == 3:
                        raise ValueError(f"Источник вернул HTTP {code}. Повтори загрузку позже.") from None
                except (OSError, URLError, TimeoutError, HTTPException) as exc:
                    self._check_cancel(task_id)
                    if attempt == 3:
                        raise ValueError("Связь с источником прервалась. Проверь подключение и повтори загрузку.") from exc
                self._update(task_id, message="Связь прервалась. Повторяем подключение…", rate=0)
                if self.cancel_flags[task_id].wait(attempt):
                    self._check_cancel(task_id)
        except (TransferCancelled, lifecycle.OperationCancelled) as exc:
            self._update(task_id, state="cancelled", message=str(exc), rate=0)
            self._persist()
            raise TransferCancelled(str(exc)) from None
        except Exception as exc:
            self._update(task_id, state="failed", message=str(exc)[:500] or "Не удалось загрузить файл.", rate=0)
            self._persist()
            raise
        finally:
            if acquired:
                self.slots.release()

    def _receive(self, task_id, config, title, status):
        destination = self._inside(config["destination"])
        partial = self.cache / (task_id + ".part")
        metadata = self.cache / (task_id + ".json")
        if partial.is_symlink() or metadata.is_symlink():
            raise ValueError("Частичная загрузка перенаправлена.")
        start = 0
        saved = {}
        try:
            if config["resume"] and metadata.stat().st_size < 16384:
                saved = json.loads(metadata.read_text(encoding="utf-8"))
                if (
                    saved.get("url") == config["url"]
                    and saved.get("hashes") == config["hashes"]
                    and isinstance(saved.get("validator"), str)
                    and saved["validator"]
                ):
                    start = partial.stat().st_size
        except (OSError, ValueError, TypeError):
            pass
        if start > config["max_size"]:
            start = 0
        headers = {}
        if start:
            headers.update(Range=f"bytes={start}-", **{"If-Range": saved["validator"]})
        response = None
        contexts = ExitStack()
        try:
            try:
                self._check_cancel(task_id)
                response = open_https(config["url"], set(config["hosts"]), headers=headers)
            except HTTPError as exc:
                if exc.code != 416 or not start:
                    raise
                exc.close()
                # A fully received file may have been interrupted immediately
                # before verification. A size match alone is not sufficient.
                if config["size"] is not None and start == config["size"]:
                    self._verify(task_id, partial, config)
                    return self._publish(partial, destination, metadata, task_id)
                start = 0
                self._check_cancel(task_id)
                response = open_https(config["url"], set(config["hosts"]))
            with self.lock:
                self.responses[task_id] = response
            response = contexts.enter_context(lifecycle.response(response))
            self._check_cancel(task_id)
            code = response.getcode()
            if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise ValueError("Источник вернул сжатый транспорт: безопасное продолжение недоступно.")
            if code == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if not start or not match or int(match[1]) != start or int(match[2]) < start or int(match[3]) <= int(match[2]):
                    raise ValueError("Источник вернул некорректный диапазон файла.")
                total = int(match[3])
                expected_remaining = int(match[2]) - start + 1
                # A server cannot silently change a validator while appending.
                returned_validator = response.headers.get(saved.get("validatorKind", "ETag"))
                if returned_validator and returned_validator != saved.get("validator"):
                    raise ValueError("Файл на сервере изменился. Начни загрузку заново.")
            elif code == 200:
                start = 0
                raw_length = response.headers.get("Content-Length")
                total = int(raw_length) if raw_length is not None else config["size"]
                expected_remaining = total
            else:
                raise ValueError(f"Источник вернул неподдерживаемый HTTP {code}.")
            if total is not None and (total < 0 or total > config["max_size"]):
                raise ValueError("Файл превышает разрешённый размер.")
            if config["size"] is not None and total is not None and total != config["size"]:
                raise ValueError("Размер файла отличается от указанного в каталоге.")
            if total is not None and max(0, total - start) + 16 * 1024**2 > shutil.disk_usage(self.root).free:
                raise ValueError("Недостаточно места на диске для загрузки файла.")
            validator = response.headers.get("ETag", "")
            validator_kind = "ETag"
            if validator.startswith("W/"):
                validator = ""
            if not validator:
                validator = response.headers.get("Last-Modified", "")
                validator_kind = "Last-Modified"
            if any(ord(c) < 32 for c in validator) or len(validator) > 1024:
                validator = ""
            _atomic_json(metadata, {
                "url": config["url"], "hashes": config["hashes"],
                "validator": validator, "validatorKind": validator_kind,
            })
            self._update(task_id, bytes=start, total=total, resumed=bool(start), rate=0)
            destination.parent.mkdir(parents=True, exist_ok=True)
            received = start
            began = time.monotonic()
            last_notice = 0.0
            with partial.open("ab" if start else "wb") as out:
                while True:
                    self._check_cancel(task_id)
                    block = response.read(CHUNK)
                    self._check_cancel(task_id)
                    if not block:
                        break
                    received += len(block)
                    if received > config["max_size"] or (total is not None and received > total):
                        raise ValueError("Источник прислал больше данных, чем разрешено.")
                    out.write(block)
                    now = time.monotonic()
                    rate = (received - start) / max(now - began, 0.001)
                    self._update(task_id, bytes=received, rate=round(rate))
                    if now - last_notice >= 0.5:
                        amount = f"{received / 1024**2:.1f} МБ"
                        if total is not None:
                            amount += f" из {total / 1024**2:.1f} МБ"
                        status(f"{title}: {amount}")
                        last_notice = now
                out.flush()
                os.fsync(out.fileno())
            if expected_remaining is not None and received - start != expected_remaining:
                raise OSError("Передача файла оборвалась.")
            self._verify(task_id, partial, config)
            return self._publish(partial, destination, metadata, task_id)
        finally:
            with self.lock:
                self.responses.pop(task_id, None)
            contexts.close()
            if response is not None:
                response.close()

    def _verify(self, task_id, partial, config):
        self._update(task_id, state="verifying", message="Проверяем контрольные суммы…", rate=0)
        checks = {name: hashlib.new(name) for name in config["hashes"]}
        size = partial.stat().st_size
        if size > config["max_size"] or (config["size"] is not None and size != config["size"]):
            partial.unlink(missing_ok=True)
            raise ValueError("Размер загруженного файла не совпадает с каталогом.")
        with partial.open("rb") as source:
            while block := source.read(1024 * 1024):
                self._check_cancel(task_id)
                for digest in checks.values():
                    digest.update(block)
        if any(digest.hexdigest() != config["hashes"][name] for name, digest in checks.items()):
            partial.unlink(missing_ok=True)
            raise ValueError("Контрольная сумма не совпала. Повреждённый файл не установлен; повтори загрузку.")
        self._check_cancel(task_id)
        self._update(task_id, bytes=size, total=size)

    def _publish(self, partial, destination, metadata, task_id=None):
        with self.scope.guard():
            if task_id is not None:
                self._check_cancel(task_id)
            self._inside(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(partial, destination)
            metadata.unlink(missing_ok=True)
        return destination
