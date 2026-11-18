"""Verified, retryable Minecraft downloads and a Windows installation progress console."""

from __future__ import annotations

import hashlib
import importlib
import json
import lzma
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from functools import lru_cache
from http.client import HTTPException
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import lifecycle

ATTEMPTS = 3
SOCKET_TIMEOUT = 25
MAX_DOWNLOAD = 2 * 1024**3
MAX_METADATA = 32 * 1024**2
_LIB_LOCK = threading.RLock()
_THREADS = threading.BoundedSemaphore(6)
_HEADERS = {"User-Agent": "BlockHarbor/2.3.0", "Accept-Encoding": "identity"}
_RETRY_CODES = {408, 429, 500, 502, 503, 504}


class DownloadError(RuntimeError):
    pass


class _Incomplete(ValueError):
    pass


def _retry(error):
    if isinstance(error, HTTPError):
        return error.code in _RETRY_CODES
    reason = error.reason if isinstance(error, URLError) else error
    return not isinstance(reason, ssl.SSLCertVerificationError)


def _error(url, error):
    host = urlsplit(url).hostname or "сервер файлов"
    if isinstance(error, HTTPError):
        return f"{host} вернул HTTP {error.code}. Повтори подготовку позже."
    if isinstance(error, URLError) and isinstance(
        error.reason, ssl.SSLCertVerificationError
    ):
        return f"Не удалось проверить сертификат {host}. Проверь системное время и настройки подключения."
    return f"Загрузка с {host} не завершена. Проверь подключение и снова нажми «Играть». Скачанные файлы сохраняются."


def _receive(url, output, limit, notify=lambda *_: None):
    lifecycle.check()
    if urlsplit(url).scheme not in ("https", "http"):
        raise ValueError("Адрес загрузки использует неподдерживаемый протокол.")
    # urlopen preserves environment proxies and the Windows Internet Settings
    # proxy, along with system trust roots and explicit SSL_CERT_FILE settings.
    with lifecycle.response(urlopen(Request(url, headers=_HEADERS), timeout=SOCKET_TIMEOUT)) as response:
        try:
            length = int(response.headers.get("Content-Length", "0"))
        except ValueError:
            raise _Incomplete("Сервер вернул неверный размер файла.") from None
        if length < 0 or length > limit:
            raise _Incomplete("Размер загрузки превышает допустимый.")
        count = 0
        deadline = time.monotonic() + 20 * 60
        while block := response.read(256 * 1024):
            lifecycle.check()
            count += len(block)
            if count > limit or time.monotonic() > deadline:
                raise _Incomplete("Загрузка превысила ограничение.")
            output.write(block)
            notify(count, length)
        if length and count != length:
            raise _Incomplete("Передача файла прервана.")
        return dict(response.headers), response.geturl()


def metadata_response(url, **_kwargs):
    """The response shape used by launcher-lib's catalog/XML routines."""
    import io

    import requests

    for attempt in range(ATTEMPTS):
        try:
            data = io.BytesIO()
            headers, effective_url = _receive(url, data, MAX_METADATA)
            reply = requests.Response()
            reply.status_code = 200
            reply.url = effective_url
            reply.headers.update(headers)
            reply._content = data.getvalue()
            reply.encoding = "utf-8"
            return reply
        except (
            HTTPError,
            URLError,
            HTTPException,
            TimeoutError,
            ConnectionError,
            _Incomplete,
        ) as error:
            if not _retry(error) or attempt + 1 == ATTEMPTS:
                raise DownloadError(_error(url, error)) from None
            lifecycle.wait(0.5 * (attempt + 1))
    raise AssertionError("unreachable")


def json_document(url):
    try:
        return metadata_response(url).json()
    except lifecycle.OperationCancelled:
        raise
    except ValueError:
        raise DownloadError(
            "Каталог версий повреждён. Повтори загрузку позже."
        ) from None


def _valid(path, digest=None):
    lifecycle.check()
    if not path.is_file() or path.is_symlink():
        return False
    if digest:
        with path.open("rb") as src:
            result = hashlib.sha1()
            while block := src.read(1024 * 1024):
                lifecycle.check()
                result.update(block)
            return result.hexdigest() == digest
    if not path.stat().st_size:
        return False
    if path.suffix.lower() in (".jar", ".zip"):
        return zipfile.is_zipfile(path)
    if path.suffix.lower() == ".json":
        try:
            if path.stat().st_size > MAX_METADATA:
                return False
            json.loads(path.read_bytes())
        except (ValueError, OSError):
            return False
    return True


class Progress:
    def __init__(self, status):
        self.status = status
        self.console = None
        self.started = False
        self.lock = threading.RLock()
        self.last = 0.0
        self.phase = "Подготовка"
        self.maximum = 0
        self.release_console = None

    def __call__(self, text):
        lifecycle.check()
        with self.lock:
            self.status(text)
            if self.console and self.console.poll() is None:
                try:
                    self.console.stdin.write(
                        text.replace("\r", "").replace("\n", " ") + "\n"
                    )
                    self.console.stdin.flush()
                except (OSError, ValueError):
                    pass

    def start(self):
        lifecycle.check()
        with self.lock:
            if self.started:
                return
            self.started = True
            if sys.platform == "win32":
                exe = Path(sys.executable).with_name("python.exe")
                if exe.is_file():
                    script = (
                        "import ctypes,sys\n"
                        "ctypes.windll.kernel32.SetConsoleTitleW('BlockHarbor — подготовка Minecraft и Java')\n"
                        "ctypes.windll.kernel32.SetConsoleOutputCP(65001)\n"
                        "output=open('CONOUT$','w',encoding='utf-8',buffering=1)\n"
                        "for line in sys.stdin: print(line,end='',flush=True,file=output)\n"
                    )
                    try:
                        with lifecycle.guard():
                            self.console = subprocess.Popen(
                                [str(exe), "-u", "-c", script],
                                stdin=subprocess.PIPE,
                                encoding="utf-8",
                                creationflags=subprocess.CREATE_NEW_CONSOLE,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                            )
                            self.release_console = lifecycle.own_process(self.console)
                    except OSError:
                        pass
            self("Подготовка файлов Minecraft и Java…")

    def downloaded(self, name, count, maximum):
        with self.lock:
            now = time.monotonic()
            if now - self.last < 0.5:
                return
            self.last = now
            divisor, unit = (
                (1024**2, "МБ") if max(count, maximum) >= 1024**2 else (1024, "КБ")
            )
            value = f"{count / divisor:.1f} {unit}"
            if maximum:
                value += f" / {maximum / divisor:.1f} {unit}"
            self(f"Скачиваем {name} · {value}")

    def callbacks(self):
        phases = {
            "Download Libraries": "Библиотеки",
            "Download Assets": "Ресурсы игры",
            "Install java runtime": "Установка Java",
            "Installation complete": "Файлы установлены",
        }

        def status(message):
            with self.lock:
                if message in phases:
                    self.phase, self.maximum = phases[message], 0
                    self(self.phase + "…")
                elif message.startswith("Running processor "):
                    self(
                        "Настраиваем загрузчик · "
                        + message.removeprefix("Running processor ")
                    )
                elif not message.startswith("Download "):
                    self(message)

        def maximum(value):
            lifecycle.check()
            with self.lock:
                self.maximum = value + 1

        def done(value):
            lifecycle.check()
            with self.lock:
                now = time.monotonic()
                if self.maximum and (value >= self.maximum or now - self.last >= 0.5):
                    self.last = now
                    self(f"{self.phase} · {value} / {self.maximum}")

        return {"setStatus": status, "setMax": maximum, "setProgress": done}

    def close(self):
        if self.console:
            try:
                self.console.stdin.close()
                self.console.wait(timeout=0.5)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                try:
                    self.console.kill()
                    self.console.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            finally:
                if self.release_console:
                    self.release_console()
                    self.release_console = None


class Download:
    def __init__(self, progress):
        self.progress = progress

    def __call__(
        self,
        url,
        path,
        callback=None,
        sha1=None,
        lzma_compressed=False,
        session=None,
        minecraft_directory=None,
        overwrite=False,
    ):
        lifecycle.check()
        path = Path(path)
        if minecraft_directory is not None:
            root = Path(minecraft_directory).resolve()
            if not path.resolve().is_relative_to(root):
                raise ValueError("Файл игры находится вне каталога установки.")
        if not overwrite and _valid(path, sha1):
            return False
        self.progress.start()
        while not _THREADS.acquire(timeout=0.1):
            lifecycle.check()
        try:
            lifecycle.check()
            return self.fetch(url, path, sha1, lzma_compressed)
        finally:
            _THREADS.release()

    def fetch(self, url, path, sha1, compressed):
        lifecycle.check()
        path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(ATTEMPTS):
            packed = plain = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=path.parent, prefix=".blockharbor-download-", delete=False
                ) as out:
                    packed = Path(out.name)
                    _receive(
                        url,
                        out,
                        MAX_DOWNLOAD,
                        lambda n, total: self.progress.downloaded(path.name, n, total),
                    )
                plain = packed
                if compressed:
                    with (
                        lzma.open(packed, "rb") as source,
                        tempfile.NamedTemporaryFile(
                            dir=path.parent, prefix=".blockharbor-java-", delete=False
                        ) as output,
                    ):
                        plain = Path(output.name)
                        while block := source.read(256 * 1024):
                            lifecycle.check()
                            if output.tell() + len(block) > MAX_DOWNLOAD:
                                raise _Incomplete("Файл Java слишком велик.")
                            output.write(block)
                if not _valid(plain, sha1):
                    raise _Incomplete("Контрольная сумма файла не совпала.")
                if (
                    not sha1
                    and path.suffix.lower() in (".jar", ".zip")
                    and not zipfile.is_zipfile(plain)
                ):
                    raise _Incomplete("Архив загрузчика повреждён.")
                if not sha1 and path.suffix.lower() == ".json":
                    json.loads(plain.read_bytes())
                with lifecycle.guard():
                    os.replace(plain, path)
                return True
            except (
                HTTPError,
                URLError,
                HTTPException,
                TimeoutError,
                ConnectionError,
                _Incomplete,
                lzma.LZMAError,
                EOFError,
                json.JSONDecodeError,
            ) as error:
                if not _retry(error) or attempt + 1 == ATTEMPTS:
                    raise DownloadError(_error(url, error)) from None
                self.progress(
                    f"Повтор загрузки {path.name} · {attempt + 2} / {ATTEMPTS}"
                )
                lifecycle.wait(0.5 * (attempt + 1))
            finally:
                if plain is not None:
                    plain.unlink(missing_ok=True)
                if packed is not None and packed != plain:
                    packed.unlink(missing_ok=True)
        raise AssertionError("unreachable")


@contextmanager
def _replace(module, name, value):
    old = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, old)


class _Processors:
    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, command, **kwargs):
        if isinstance(command, (tuple, list)) and Path(
            str(command[0])
        ).name.lower() in ("java", "java.exe"):
            command = [command[0], "-Djava.net.useSystemProxies=true", *command[1:]]
        kwargs.setdefault("timeout", 900)
        check = kwargs.pop("check", True)
        try:
            return lifecycle.run(command, check=check, **kwargs)
        except subprocess.TimeoutExpired:
            raise DownloadError(
                "Установщик загрузчика не завершился за 15 минут. Повтори подготовку."
            ) from None


@contextmanager
def environment(progress):
    """Limit customization to pinned installer modules, restoring every alias afterward."""
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import ExitStack

    import requests
    from minecraft_launcher_lib import _helper

    names = [
        "_helper",
        "install",
        "runtime",
        "fabric",
        "forge",
        "quilt",
        "mod_loader._fabric",
        "mod_loader._forge",
        "mod_loader._neoforge",
        "mod_loader._quilt",
    ]
    while not _LIB_LOCK.acquire(timeout=0.1):
        lifecycle.check()
    try:
        lifecycle.check()
        with ExitStack() as stack:
            yield from _environment_locked(progress, stack, ThreadPoolExecutor, requests, _helper, names)
    finally:
        _LIB_LOCK.release()


def _environment_locked(progress, stack, ThreadPoolExecutor, requests, _helper, names):
    modules = [
        importlib.import_module("minecraft_launcher_lib." + name) for name in names
    ]
    original = _helper.download_file
    downloader = Download(progress)
    network = SimpleNamespace(
        get=metadata_response, session=requests.Session, Session=requests.Session
    )

    def executor(max_workers=None, **kwargs):
        return lifecycle.Executor(max_workers=min(max_workers or 6, 6), **kwargs)

    def extraction(original):
        def guarded(*args, **kwargs):
            # install_libraries deliberately swallows a first download failure;
            # cancellation must not fall through to extraction of a cached JAR.
            lifecycle.check()
            result = original(*args, **kwargs)
            lifecycle.check()
            return result
        return guarded

    for module in modules:
        if getattr(module, "download_file", None) is original:
            stack.enter_context(_replace(module, "download_file", downloader))
        if getattr(module, "requests", None) is requests:
            stack.enter_context(_replace(module, "requests", network))
        if getattr(module, "ThreadPoolExecutor", None) is ThreadPoolExecutor:
            stack.enter_context(_replace(module, "ThreadPoolExecutor", executor))
        if hasattr(module, "extract_natives_file"):
            stack.enter_context(_replace(module, "extract_natives_file", extraction(module.extract_natives_file)))
        if module.__name__.endswith(("._forge", "._neoforge", ".forge")):
            stack.enter_context(_replace(module, "subprocess", _Processors()))
    yield
    lifecycle.check()


@contextmanager
def account_environment(accounts):
    """Give the pinned Microsoft flow finite, scoped requests without token logs."""
    import requests

    def request(method, *args, **kwargs):
        lifecycle.check()
        kwargs.setdefault("timeout", SOCKET_TIMEOUT)
        kwargs["stream"] = True
        reply = requests.request(method, *args, **kwargs)
        try:
            with lifecycle.response(reply) as opened:
                data = bytearray()
                for block in opened.iter_content(64 * 1024):
                    if len(data) + len(block) > MAX_METADATA:
                        raise ValueError("Ответ службы входа слишком велик.")
                    data.extend(block)
                lifecycle.check()
                reply._content = bytes(data)
                reply._content_consumed = True
                return reply
        except requests.RequestException:
            lifecycle.check()
            raise

    network = SimpleNamespace(get=lambda *a, **k: request("GET", *a, **k),
                              post=lambda *a, **k: request("POST", *a, **k))
    while not _LIB_LOCK.acquire(timeout=0.1):
        lifecycle.check()
    try:
        lifecycle.check()
        with _replace(accounts, "requests", network):
            yield
    finally:
        _LIB_LOCK.release()


@lru_cache(maxsize=8)
def _java_major(path, stamp):
    try:
        process = lifecycle.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=8,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'version "(?:1\.)?(\d+)', process.stderr + process.stdout)
    return int(match.group(1)) if process.returncode == 0 and match else None


def legacy_system_java():
    """Only probe the user's PATH, never an executable from imported metadata."""
    path = shutil.which("java")
    if not path:
        return None
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    major = _java_major(path, (stat.st_size, stat.st_mtime_ns))
    return path if major is not None and 6 <= major <= 8 else None


def runtime_problem(cache, component, java):
    """Verify changed Java files; reuse a size/mtime index on an unchanged launch."""
    from .modfiles import file_hash, file_stamp

    root = cache / "runtime" / component
    platform = root / Path(java).relative_to(root).parts[0]
    inventory = platform / (component + ".sha1")
    if not inventory.is_file() or not 0 < inventory.stat().st_size < 2 * 1024**2:
        return inventory.name
    raw = inventory.read_bytes()
    index_file = platform / ".blockharbor-verified.json"
    try:
        saved = (
            json.loads(index_file.read_text(encoding="utf-8"))
            if index_file.stat().st_size < 2 * 1024**2
            else {}
        )
    except (OSError, ValueError):
        saved = {}
    manifest = hashlib.sha256(raw).hexdigest()
    previous = (
        saved.get("files", {})
        if isinstance(saved, dict) and saved.get("manifest") == manifest
        else {}
    )
    if not isinstance(previous, dict):
        previous = {}
    verified = {}
    for line in raw.decode("utf-8").splitlines():
        lifecycle.check()
        name, separator, digest_time = line.partition(" /#// ")
        path = platform / component / name
        if (
            not separator
            or not path.resolve().is_relative_to((platform / component).resolve())
            or not path.is_file()
        ):
            return "Java: " + name
        digest = digest_time.split(" ", 1)[0]
        st = file_stamp(path)
        checked = [st[0], st[1]]
        if previous.get(name) != checked and file_hash(path, st, "sha1") != digest:
            return "Java: " + name
        verified[name] = checked
    if previous != verified:
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=platform, prefix=".blockharbor-verify-", delete=False
            ) as stream:
                tmp = Path(stream.name)
                stream.write(
                    json.dumps({"manifest": manifest, "files": verified}).encode()
                )
            with lifecycle.guard():
                os.replace(tmp, index_file)
        finally:
            if tmp:
                tmp.unlink(missing_ok=True)
    return None


@contextmanager
def job(status):
    progress = Progress(status)
    try:
        with environment(progress):
            yield progress
    finally:
        progress.close()
