"""Verified, retryable downloads with progress inside the launcher's own UI."""

from __future__ import annotations

import hashlib
import importlib
import json
import lzma
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager, nullcontext
from functools import lru_cache
from http.client import HTTPException
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import lifecycle
from .io_errors import storage_message
from .version import VERSION

ATTEMPTS = 3
SOCKET_TIMEOUT = 25
MAX_RECEIVE_SECONDS = 20 * 60
MAX_DOWNLOAD = 2 * 1024**3
MAX_METADATA = 32 * 1024**2
_LIB_LOCK = threading.RLock()
_THREADS = threading.BoundedSemaphore(6)
_HEADERS = {"User-Agent": f"BlockHarbor/{VERSION}", "Accept-Encoding": "identity"}
_RETRY_CODES = {408, 429, 500, 502, 503, 504}
_POOLED_MOJANG = {"resources.download.minecraft.net", "piston-data.mojang.com", "launcher.mojang.com"}
_NEO_ORIGIN = "https://maven.neoforged.net/releases/"
# These repositories are candidates, not authorities for file hashes. Only an
# exact digest from the original metadata can authorize a fallback artifact.
_NEO_MIRRORS = (
    "https://maven.creeperhost.net/",
    "https://mirrors.nju.edu.cn/bmclapi/",
    "https://repo.maven.apache.org/maven2/",
)


def _neo_path(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc.lower() != "maven.neoforged.net"
            or parsed.query or parsed.fragment):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or any(
        part in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_.+-]+", part)
        for part in parts
    ):
        return None
    if parts[0] == "releases":
        parts = parts[1:]
    elif parts[:2] != ["net", "neoforged"]:
        return None
    return "/".join(parts) if len(parts) >= 4 else None


def _neo_routes(url, sha1=None):
    relative = _neo_path(url)
    if relative is None:
        return [(url, False)] * ATTEMPTS
    primary = _NEO_ORIGIN + relative
    routes = [(primary, False), (primary, True), (primary, False)]
    if isinstance(sha1, str) and re.fullmatch(r"[0-9a-fA-F]{40}", sha1):
        # Switch origin after two transport failures instead of making the
        # player repeatedly wait on the same CDN. Never trust mirror sidecars.
        routes = routes[:2] + [(mirror + relative, True) for mirror in _NEO_MIRRORS]
    return routes


class DownloadError(RuntimeError):
    def __init__(self, message, *, url=None, status_code=None):
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class _Incomplete(ValueError):
    pass


def processor_diagnostic(error):
    """Classify captured Java failures without exposing command lines or URLs."""
    messages = []
    for value in (getattr(error, "stdout", None), getattr(error, "stderr", None)):
        if isinstance(value, bytes):
            value = value[-65536:].decode("utf-8", errors="replace")
        if isinstance(value, str):
            messages.append(value[-65536:].lower())
    detail = "\n".join(messages)
    if any(marker in detail for marker in ("sslhandshakeexception", "pkix path", "certificate_unknown")):
        return "Java не смогла проверить сертификат сервера. Проверь системное время и доверенные сертификаты сети или прокси."
    if any(marker in detail for marker in ("unknownhostexception", "connectexception", "sockettimeoutexception", "unable to download", "failed to download")):
        return "Java не смогла скачать файлы. Проверь подключение, DNS и настройки прокси, затем повтори подготовку."
    if any(marker in detail for marker in ("outofmemoryerror", "could not reserve enough space", "insufficient memory")):
        return "Java не хватило памяти. Закрой лишние приложения и повтори подготовку."
    return "Подготовка загрузчика остановлена. Уже скачанные файлы сохранены; повтори установку."


def cleanup_interrupted_downloads(cache):
    """Remove our abandoned scratch files while the caller owns the install lock."""
    root = Path(cache).resolve()
    removed = 0
    for path in root.rglob(".blockharbor-*"):
        lifecycle.check()
        if (re.fullmatch(r"\.blockharbor-(?:download|java)-[a-z0-9_]{8}", path.name)
                and not path.is_symlink() and path.is_file()
                and path.resolve().is_relative_to(root)):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass  # A locked abandoned file must not block recovery itself.
    return removed


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
    if _neo_path(url) is not None:
        name = urlsplit(url).path.rsplit("/", 1)[-1]
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, socket.gaierror):
            detail = "не удалось определить адрес сервера"
        elif isinstance(reason, TimeoutError):
            detail = "истекло время ожидания ответа"
        elif isinstance(reason, _Incomplete):
            detail = str(reason)
        elif isinstance(reason, HTTPException):
            detail = "сервер прервал передачу"
        else:
            detail = "ошибка соединения или передачи данных"
        return (
            f"Не удалось скачать {name} с {host}: {detail}. "
            "Проверь подключение и снова нажми «Играть». "
            "Ранее проверенные файлы сохраняются."
        )
    return f"Загрузка с {host} не завершена. Проверь подключение и снова нажми «Играть». Скачанные файлы сохраняются."


def _receive(url, output, limit, notify=lambda *_: None, *, quick_catalog=False):
    lifecycle.check()
    if urlsplit(url).scheme not in ("https", "http"):
        raise ValueError("Адрес загрузки использует неподдерживаемый протокол.")
    # urlopen preserves environment proxies and the Windows Internet Settings
    # proxy, along with system trust roots and explicit SSL_CERT_FILE settings.
    timeout = 5 if quick_catalog else (45 if _neo_path(url) else SOCKET_TIMEOUT)
    deadline = time.monotonic() + (8 if quick_catalog else MAX_RECEIVE_SECONDS)
    with lifecycle.response(urlopen(Request(url, headers=_HEADERS), timeout=timeout)) as response:
        try:
            length = int(response.headers.get("Content-Length", "0"))
        except ValueError:
            raise _Incomplete("Сервер вернул неверный размер файла.") from None
        if length < 0 or length > limit:
            raise _Incomplete("Размер загрузки превышает допустимый.")
        count = 0
        read_chunk = getattr(response, "read1", response.read)
        while block := read_chunk(256 * 1024):
            lifecycle.check()
            count += len(block)
            if count > limit or time.monotonic() > deadline:
                raise _Incomplete("Загрузка превысила ограничение.")
            output.write(block)
            notify(count, length)
        if length and count != length:
            raise _Incomplete("Передача файла прервана.")
        return dict(response.headers), response.geturl()


def _receive_requests(url, output, limit, notify=lambda *_: None, *, session=None):
    """Independent HTTPS transport for Maven; keep cancellation and TLS verification."""
    import requests
    from urllib3 import exceptions as urllib3_errors

    lifecycle.check()
    deadline = time.monotonic() + MAX_RECEIVE_SECONDS
    # Requests uses the environment/system proxy configuration. Respect an
    # explicitly configured CA file; do not disable certificate verification.
    try:
        with (requests.Session() if session is None else nullcontext(session)) as session:
            if os.environ.get("SSL_CERT_FILE") and not (
                os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
            ):
                session.verify = os.environ["SSL_CERT_FILE"]
            with lifecycle.response(session.get(
                url, headers=_HEADERS, timeout=(12, 45), stream=True,
                allow_redirects=False,
            )) as response:
                if response.status_code != 200:
                    raise HTTPError(url, response.status_code, "Maven HTTP error",
                                    response.headers, None)
                try:
                    length = int(response.headers.get("Content-Length", "0"))
                except ValueError:
                    raise _Incomplete("Сервер вернул неверный размер файла.") from None
                if length < 0 or length > limit:
                    raise _Incomplete("Размер загрузки превышает допустимый.")
                count = 0
                # iter_content fills its requested chunk: a peer trickling one
                # byte can postpone the deadline check for hours. read1 checks
                # between socket reads instead of waiting for 64 KiB to fill.
                while block := response.raw.read1(64 * 1024, decode_content=True):
                    lifecycle.check()
                    count += len(block)
                    if count > limit or time.monotonic() > deadline:
                        raise _Incomplete("Загрузка превысила ограничение.")
                    output.write(block)
                    notify(count, length)
                if length and count != length:
                    raise _Incomplete("Передача файла прервана.")
                return dict(response.headers), response.url
    except (requests.exceptions.SSLError, urllib3_errors.SSLError):
        lifecycle.check()
        raise URLError(ssl.SSLCertVerificationError("TLS verification failed")) from None
    except (requests.exceptions.Timeout, urllib3_errors.TimeoutError):
        lifecycle.check()
        raise TimeoutError("Maven transfer timed out") from None
    except (requests.exceptions.RequestException, urllib3_errors.HTTPError):
        lifecycle.check()
        raise ConnectionError("Maven transfer failed") from None


def metadata_response(url, *, quick_catalog=False, **_kwargs):
    """The response shape used by launcher-lib's catalog/XML routines."""
    import io

    import requests

    relative = _neo_path(url)
    if relative is not None:
        url = _NEO_ORIGIN + relative
    attempts = 1 if quick_catalog else ATTEMPTS
    for attempt in range(attempts):
        try:
            data = io.BytesIO()
            if relative is not None and attempt == 1 and not quick_catalog:
                headers, effective_url = _receive_requests(url, data, MAX_METADATA)
            else:
                headers, effective_url = _receive(url, data, MAX_METADATA, quick_catalog=quick_catalog)
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
            if not _retry(error) or attempt + 1 == attempts:
                raise DownloadError(
                    _error(url, error), url=url,
                    status_code=error.code if isinstance(error, HTTPError) else None,
                ) from None
            lifecycle.wait(0.5 * (attempt + 1))
    raise AssertionError("unreachable")


def json_document(url, *, quick_catalog=False):
    try:
        return metadata_response(url, quick_catalog=quick_catalog).json()
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
        self.started = False
        self.lock = threading.RLock()
        self.last = 0.0
        self.phase = "Подготовка"
        self.maximum = 0

    def __call__(self, text):
        lifecycle.check()
        with self.lock:
            self.status(text)

    def start(self):
        lifecycle.check()
        with self.lock:
            if self.started:
                return
            self.started = True
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
            "Running installer": "Настраиваем загрузчик",
            "Running fabric installer": "Настраиваем Fabric",
            "Running quilt installer": "Настраиваем Quilt",
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
        # Keep the lifecycle contract used by game preparation. Progress no
        # longer owns a separate interpreter/window that needs to be stopped.
        pass


class Download:
    def __init__(self, progress):
        self.progress = progress
        self._sessions = {}
        self._sessions_lock = threading.Lock()

    def close(self):
        with self._sessions_lock:
            sessions, self._sessions = tuple(self._sessions.values()), {}
        for session in sessions:
            session.close()

    def _receive_asset(self, url, output, limit, notify):
        """Keep one verified HTTPS pool per worker for Mojang assets and Java.

        A fresh TLS connection for each of several thousand asset objects made
        preparation needlessly slow on high-latency networks. Sessions are not
        shared between workers and are closed with the preparation environment.
        """
        import requests

        thread = threading.get_ident()
        with self._sessions_lock:
            session = self._sessions.get(thread)
            if session is None:
                context = ssl.create_default_context()
                class SystemRootsAdapter(requests.adapters.HTTPAdapter):
                    def init_poolmanager(self, *args, **kwargs):
                        kwargs["ssl_context"] = context
                        return super().init_poolmanager(*args, **kwargs)
                    def proxy_manager_for(self, proxy, **kwargs):
                        kwargs["ssl_context"] = context
                        return super().proxy_manager_for(proxy, **kwargs)
                session = requests.Session()
                session.mount("https://", SystemRootsAdapter(pool_connections=2, pool_maxsize=2))
                self._sessions[thread] = session
        try:
            return _receive_requests(url, output, limit, notify, session=session)
        except HTTPError as error:
            if error.code not in {301, 302, 303, 307, 308}:
                raise
            # Preserve the existing redirect-capable transport for a publisher
            # endpoint which no longer directly serves its checksum-pinned file.
            error.close()
            return _receive(url, output, limit, notify)

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
        except OSError as error:
            message = storage_message(error)
            if message:
                raise DownloadError(message) from error
            raise
        finally:
            _THREADS.release()

    def fetch(self, url, path, sha1, compressed):
        lifecycle.check()
        path.parent.mkdir(parents=True, exist_ok=True)
        relative = _neo_path(url)
        if relative is not None:
            url = _NEO_ORIGIN + relative
            # launcher-lib may omit the checksum for the initial installer.
            # Try its official sidecar so mirror fallback is safe even there.
            if not sha1 and relative.startswith(("net/neoforged/neoforge/",
                                                   "net/neoforged/forge/")) and relative.endswith("-installer.jar"):
                try:
                    candidate = metadata_response(url + ".sha1", quick_catalog=True).text.strip()
                    if re.fullmatch(r"[0-9a-fA-F]{40}", candidate):
                        sha1 = candidate.lower()
                except DownloadError:
                    pass
        routes = _neo_routes(url, sha1)
        primary_error = None
        for attempt, (source_url, alternate) in enumerate(routes):
            packed = plain = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=path.parent, prefix=".blockharbor-download-", delete=False
                ) as out:
                    packed = Path(out.name)
                    receive = _receive_requests if alternate else _receive
                    if (not alternate and sha1 and urlsplit(source_url).scheme == "https"
                            and urlsplit(source_url).hostname in _POOLED_MOJANG):
                        receive = self._receive_asset
                    receive(
                        source_url,
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
                if source_url == url:
                    primary_error = error
                # A checksum-pinned alternate origin may legitimately have
                # the artifact even when the original CDN returns 403/404.
                verified_fallback = (
                    relative is not None
                    and any(next_url != source_url for next_url, _ in routes[attempt + 1:])
                    and not isinstance(
                        error.reason if isinstance(error, URLError) else error,
                        ssl.SSLCertVerificationError,
                    )
                )
                if (not _retry(error) and not verified_fallback) or attempt + 1 == len(routes):
                    cause = primary_error if primary_error is not None else error
                    message = _error(url, cause)
                    if relative is not None and len(routes) > ATTEMPTS:
                        message += " Резервная загрузка также не завершена."
                    raise DownloadError(
                        message, url=url,
                        status_code=cause.code if isinstance(cause, HTTPError) else None,
                    ) from None
                next_url, _ = routes[attempt + 1]
                if next_url != source_url:
                    self.progress(
                        f"Резервный источник {urlsplit(next_url).hostname} · {path.name}"
                    )
                else:
                    self.progress(
                        f"Повтор загрузки {path.name} · {attempt + 2} / {len(routes)}"
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
    def __init__(self, *, check_default=False, downloader=None, cache=None):
        self.check_default = check_default
        self.downloader = downloader
        self.cache = Path(cache).resolve() if cache is not None else None

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def _download_mappings(self, command):
        """Perform the publishers' raw download task with our verified transport.

        Java URLConnection does not inherit Python/environment proxy settings.
        Both InstallerTools implementations use this task solely to copy the
        official client mappings when no transformation flags are present. All
        other tasks/arguments still run in the original Java processor.
        """
        from .version_ids import valid

        if self.downloader is None or self.cache is None or len(command) != 12:
            return False
        if command[1] != "-cp" or command[3] not in {
            "net.minecraftforge.installertools.ConsoleTool",
            "net.neoforged.installertools.ConsoleTool",
        }:
            return False
        pairs = list(zip(command[4::2], command[5::2]))
        options = dict(pairs)
        if (len(options) != 4 or set(options) != {"--task", "--version", "--side", "--output"}
                or options["--task"] != "DOWNLOAD_MOJMAPS" or options["--side"] != "client"):
            return False
        version = options["--version"]
        if not valid(version):
            raise DownloadError("Установщик указал некорректную версию для mappings.")
        root = self.cache
        output = Path(options["--output"])
        allowed = root / "libraries" / "net" / "minecraft" / "client"
        try:
            relative = output.resolve().relative_to(allowed.resolve())
        except ValueError:
            relative = Path()
        # Forge's MCP coordinate includes the mappings timestamp, even though
        # --version still names the plain Minecraft release.
        if (not output.is_absolute() or not output.resolve().is_relative_to(root)
                or len(relative.parts) != 2
                or not re.fullmatch(re.escape(version) + r"(?:-\d{8}\.\d{6})?", relative.parts[0])
                or relative.name != "client-" + relative.parts[0] + "-mappings.txt"):
            raise DownloadError("Установщик указал путь mappings вне каталога игры.")
        for ancestor in (output, *output.parents):
            if ancestor.is_symlink() or getattr(ancestor, "is_junction", lambda: False)():
                raise DownloadError("Папка mappings перенаправлена.")
            if ancestor.resolve() == root:
                break
        manifest = root / "versions" / version / (version + ".json")
        if (manifest.is_symlink() or not manifest.resolve().is_relative_to(root)
                or not manifest.is_file() or manifest.stat().st_size > MAX_METADATA):
            raise DownloadError("Не найден проверяемый манифест для mappings Minecraft.")
        metadata = json.loads(manifest.read_bytes())
        mapping = metadata.get("downloads", {}).get("client_mappings", {})
        address, digest = mapping.get("url"), mapping.get("sha1")
        parsed = urlsplit(address) if isinstance(address, str) else None
        if (metadata.get("id") != version or parsed is None or parsed.scheme != "https"
                or parsed.netloc not in {"piston-data.mojang.com", "launcher.mojang.com"}
                or not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", digest)
                or type(mapping.get("size")) is not int or not 0 < mapping["size"] <= MAX_DOWNLOAD):
            raise DownloadError("Манифест Minecraft не содержит проверяемых официальных mappings.")
        self.downloader.progress(f"Подготовка mappings Minecraft {version}…")
        self.downloader(address, output, sha1=digest.lower(), minecraft_directory=root)
        lifecycle.check()
        return True

    def run(self, command, **kwargs):
        if isinstance(command, (tuple, list)) and command and Path(
            str(command[0])
        ).name.lower() in ("java", "java.exe"):
            if self._download_mappings(command):
                empty = "" if kwargs.get("text") or kwargs.get("universal_newlines") else b""
                return subprocess.CompletedProcess(command, 0, empty, empty)
            command = [command[0], "-Djava.net.useSystemProxies=true", *command[1:]]
        kwargs.setdefault("timeout", 900)
        check = kwargs.pop("check", self.check_default)
        try:
            return lifecycle.run(command, check=check, **kwargs)
        except subprocess.TimeoutExpired:
            raise DownloadError(
                "Установщик загрузчика не завершился за 15 минут. Повтори подготовку."
            ) from None


@contextmanager
def environment(progress, *, cache=None):
    """Limit customization to pinned installer modules, restoring every alias afterward."""
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import ExitStack

    import requests
    from minecraft_launcher_lib import _helper

    names = [
        "_helper",
        "install",
        "runtime",
        "java_utils",
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
            yield from _environment_locked(progress, stack, ThreadPoolExecutor, requests, _helper, names, cache)
    finally:
        _LIB_LOCK.release()


def _environment_locked(progress, stack, ThreadPoolExecutor, requests, _helper, names, cache):
    modules = [
        importlib.import_module("minecraft_launcher_lib." + name) for name in names
    ]
    original = _helper.download_file
    downloader = Download(progress)
    stack.callback(downloader.close)
    native_extraction = threading.Lock()
    network = SimpleNamespace(
        get=metadata_response, session=requests.Session, Session=requests.Session
    )

    def executor(max_workers=None, **kwargs):
        return lifecycle.Executor(max_workers=min(max_workers or 6, 6), **kwargs)

    def extraction(original):
        def guarded(*args, **kwargs):
            # install_libraries deliberately swallows a first download failure;
            # cancellation must not fall through to extraction of a cached JAR.
            while not native_extraction.acquire(timeout=0.1):
                lifecycle.check()
            try:
                lifecycle.check()
                # Native JARs share META-INF and sometimes library filenames.
                # zipfile.extract's exists/makedirs pair races between workers;
                # serialize extraction while leaving downloads parallel.
                result = original(*args, **kwargs)
                lifecycle.check()
                return result
            finally:
                native_extraction.release()
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
        if getattr(module, "subprocess", None) is subprocess:
            # Fabric/Quilt and Java probes also spawn installers. Scope this
            # adapter to the pinned preparation modules; do not patch the global
            # subprocess module or game/Prism/Steam launch functions.
            strict = module.__name__.endswith(("._forge", "._neoforge", ".forge"))
            stack.enter_context(_replace(module, "subprocess", _Processors(
                check_default=strict, downloader=downloader if strict else None, cache=cache)))
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
    from .modfiles import file_stamp

    path = shutil.which("java")
    if not path:
        return None
    try:
        stamp = file_stamp(Path(path))
    except OSError:
        return None
    major = _java_major(path, stamp)
    return path if major is not None and 6 <= major <= 8 else None


def runtime_problem(cache, component, java):
    """Verify changed Java files; reuse a full file-stamp index on an unchanged launch."""
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
        checked = list(st)
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
def job(status, *, cache=None):
    progress = Progress(status)
    try:
        with environment(progress, cache=cache):
            yield progress
    finally:
        progress.close()
