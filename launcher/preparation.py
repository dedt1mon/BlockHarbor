"""Verified, retryable downloads with progress inside the launcher's own UI."""

from __future__ import annotations

import hashlib
import importlib
import io
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
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import lifecycle
from .io_errors import storage_message
from .version import VERSION

ATTEMPTS = 3
SOCKET_TIMEOUT = 25
MAX_RECEIVE_SECONDS = 20 * 60
MAX_DOWNLOAD = 2 * 1024**3
MAX_METADATA = 32 * 1024**2
INSTALLER_SECONDS = 5 * 60
MAVEN_SECONDS = 3 * 60
MAVEN_ATTEMPT_SECONDS = 60
MAX_REDIRECTS = 5
CHECKSUM_SECONDS = 60
CHECKSUM_ATTEMPT_SECONDS = 20
_LIB_LOCK = threading.RLock()
_THREADS = threading.BoundedSemaphore(6)
_HEADERS = {"User-Agent": f"BlockHarbor/{VERSION}", "Accept-Encoding": "identity"}
_RETRY_CODES = {408, 429, 500, 502, 503, 504}
_POOLED_MOJANG = {"resources.download.minecraft.net", "piston-data.mojang.com", "launcher.mojang.com"}
_NEO_ORIGIN = "https://maven.neoforged.net/releases/"
_NEO_INSTALLER_MIRROR = "https://neoforged.forgecdn.net/releases/"
# Reviewed against the official Maven sidecar and installer on 2026-11-26.
# This exact release remains recoverable on a clean PC during an origin outage;
# a checksum served by a fallback host is never accepted as its authority.
_NEO_INSTALLER_SHA1 = {
    "net/neoforged/neoforge/21.1.267/neoforge-21.1.267-installer.jar":
        "4565232f0bf231d0edfe512b949664dc5ff88fd8",
}
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
        # ForgeCDN carries installer coordinates and their dependency inputs.
        # Generic Maven candidates do not reliably carry installer JARs.
        mirror = _NEO_INSTALLER_MIRROR + relative
        routes = routes[:2] + [(mirror, True), (mirror, False)]
        if not _neo_installer_url(url):
            routes += [(mirror + relative, True) for mirror in _NEO_MIRRORS]
    return routes


def _neo_installer_url(url):
    relative = _neo_path(url)
    parts = relative.split("/") if relative else []
    if (len(parts) == 5 and parts[:2] == ["net", "neoforged"]
            and parts[2] in {"neoforge", "forge"} and len(parts[3]) <= 150
            and parts[4] == f"{parts[2]}-{parts[3]}-installer.jar"):
        return _NEO_ORIGIN + relative
    return None


class DownloadError(RuntimeError):
    def __init__(self, message, *, url=None, status_code=None):
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class _Incomplete(ValueError):
    pass


class _RestartDownload(_Incomplete):
    """An existing partial representation cannot safely be appended to."""


def _redirect_target(url, location):
    """Follow publisher CDNs, without allowing HTTPS downgrade or credentials."""
    if (not isinstance(location, str) or not location or len(location) > 8192
            or any(ord(c) < 32 for c in location) or "\\" in location):
        raise _Incomplete("Сервер вернул неверное перенаправление.")
    target = urljoin(url, location)
    try:
        parsed = urlsplit(target)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and parsed.username is None and parsed.password is None and not parsed.fragment
                 and "\\" not in target and not any(ord(c) < 32 for c in target)
                 and not (urlsplit(url).scheme == "https" and parsed.scheme != "https"))
        parsed.port  # Reject malformed ports before either transport sees them.
    except ValueError:
        valid = False
    if not valid:
        raise _Incomplete("Небезопасное перенаправление загрузки отклонено.")
    return target


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urlopen(request, *, timeout):
    """Keep urllib's system proxies/trust roots with our bounded redirect policy."""
    opener = build_opener(_NoRedirect())
    deadline = time.monotonic() + timeout
    seen = {request.full_url}
    for step in range(MAX_REDIRECTS + 1):
        try:
            return opener.open(request, timeout=min(timeout, _remaining(deadline)))
        except HTTPError as error:
            if error.code not in {301, 302, 303, 307, 308}:
                raise
            try:
                target = _redirect_target(request.full_url, error.headers.get("Location"))
            finally:
                error.close()
            if target in seen or step == MAX_REDIRECTS:
                raise _Incomplete("Сервер повторяет перенаправления загрузки.") from None
            seen.add(target)
            request = Request(target, headers=dict(request.header_items()))
    raise AssertionError("unreachable")


def _read_timeout(response, deadline, maximum):
    """Shorten the active socket timeout as a shared deadline approaches."""
    remaining = min(maximum, _remaining(deadline))
    for chain in (("fp", "raw", "_sock"), ("raw", "_fp", "fp", "raw", "_sock")):
        current = response
        for name in chain:
            current = getattr(current, name, None)
        if current is not None:
            current.settimeout(remaining)
            break


def _transfer_headers(output):
    return {**_HEADERS, **(output.request_headers() if isinstance(output, _PartialDownload) else {})}


def _transfer_start(output, response, code, url, limit):
    if isinstance(output, _PartialDownload):
        return output.begin(code, response.headers, url, limit)
    if code != 200:
        raise HTTPError(url, code, "Unexpected download response", response.headers, None)
    try:
        length = int(response.headers.get("Content-Length", "0"))
    except ValueError:
        raise _Incomplete("Сервер вернул неверный размер файла.") from None
    if length < 0 or length > limit:
        raise _Incomplete("Размер загрузки превышает допустимый.")
    return 0, length, length


class _PartialDownload:
    """A private, checksum-bound installer fragment; never an executable cache hit."""

    def __init__(self, path, url, sha1):
        self.path = path
        self.record = path.with_suffix(".json")
        self.url, self.sha1 = url, sha1
        self.saved = {}
        for item in (self.path, self.record):
            if (item.is_symlink() or getattr(item, "is_junction", lambda: False)()
                    or item.resolve() != item):
                raise DownloadError("Частичная загрузка установщика перенаправлена. Проверь папку данных BlockHarbor.")
        try:
            if self.record.stat().st_size <= 4096:
                record = json.loads(self.record.read_bytes())
                if (isinstance(record, dict) and record.get("format") == 1
                        and record.get("url") == url and record.get("sha1") == sha1
                        and isinstance(record.get("total"), int) and not isinstance(record["total"], bool)
                        and 0 < record["total"] <= MAX_DOWNLOAD
                        and isinstance(record.get("validator"), str) and len(record["validator"]) <= 1024
                        and not any(ord(c) < 32 for c in record["validator"])
                        and record.get("validatorKind") in {"ETag", "Last-Modified"}
                        and isinstance(record.get("source"), str)
                        and self.path.is_file() and 0 < self.path.stat().st_size <= record["total"]):
                    self.saved = record
        except (OSError, ValueError, RecursionError):
            pass
        self.stream = self.path.open("r+b" if self.saved else "w+b")
        self.stream.seek(0, os.SEEK_END)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()

    def write(self, data):
        return self.stream.write(data)

    def tell(self):
        return self.stream.tell()

    def request_headers(self):
        start = self.stream.tell()
        if not start or not self.saved:
            return {}
        result = {"Range": f"bytes={start}-"}
        if self.saved.get("validator"):
            result["If-Range"] = self.saved["validator"]
        return result

    def reset(self):
        self.stream.seek(0)
        self.stream.truncate()
        self.saved = {}
        self.record.unlink(missing_ok=True)

    def begin(self, code, headers, source, limit):
        start = self.stream.tell()
        if headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise _RestartDownload("Источник изменил кодирование файла; загрузка будет начата заново.")
        try:
            length = int(headers.get("Content-Length", "0"))
        except ValueError:
            raise _RestartDownload("Сервер вернул неверный размер файла.") from None
        if length < 0 or length > limit:
            raise _RestartDownload("Размер загрузки превышает допустимый.")
        if code == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", headers.get("Content-Range", ""))
            if (not start or not self.saved or not match or int(match[1]) != start
                    or int(match[2]) < start or int(match[3]) <= int(match[2])):
                raise _RestartDownload("Источник вернул неверный диапазон файла.")
            total, expected = int(match[3]), int(match[2]) - start + 1
            if total != self.saved["total"] or (length and length != expected):
                raise _RestartDownload("Размер продолжаемой загрузки изменился.")
            validator = headers.get(self.saved["validatorKind"])
            if (self.saved.get("validator") and validator
                    and self.saved.get("source") == source and validator != self.saved["validator"]):
                raise _RestartDownload("Файл на сервере изменился; загрузка будет начата заново.")
        elif code == 200:
            self.reset()  # A server may ignore Range or reject If-Range with 200.
            start, total, expected = 0, length, length
        else:
            if code == 416:
                raise _RestartDownload("Сервер отклонил продолжение файла; загрузка будет начата заново.")
            raise HTTPError(source, code, "Unexpected download response", headers, None)
        if total < 0 or total > limit:
            raise _RestartDownload("Размер загрузки превышает допустимый.")
        validator, kind = headers.get("ETag", ""), "ETag"
        if not validator or validator.startswith("W/"):
            validator, kind = headers.get("Last-Modified", ""), "Last-Modified"
        if len(validator) > 1024 or any(ord(c) < 32 for c in validator):
            validator = ""
        self.saved = {"format": 1, "url": self.url, "sha1": self.sha1, "source": source,
                      "total": total, "validator": validator, "validatorKind": kind}
        Download._remember_partial(self.record, self.saved)
        return start, total, expected


def _remaining(deadline):
    lifecycle.check()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Incomplete("Загрузка превысила ограничение по времени.")
    return remaining


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


def _receive(url, output, limit, notify=lambda *_: None, *, quick_catalog=False, deadline=None):
    lifecycle.check()
    if urlsplit(url).scheme not in ("https", "http"):
        raise ValueError("Адрес загрузки использует неподдерживаемый протокол.")
    # urlopen preserves environment proxies and the Windows Internet Settings
    # proxy, along with system trust roots and explicit SSL_CERT_FILE settings.
    timeout = 5 if quick_catalog else SOCKET_TIMEOUT
    local_deadline = time.monotonic() + (8 if quick_catalog else MAX_RECEIVE_SECONDS)
    deadline = min(deadline, local_deadline) if deadline is not None else local_deadline
    timeout = min(timeout, _remaining(deadline))
    try:
        response = urlopen(Request(url, headers=_transfer_headers(output)), timeout=timeout)
    except HTTPError as error:
        if error.code == 416 and isinstance(output, _PartialDownload):
            error.close()
            raise _RestartDownload("Сервер отклонил продолжение файла; загрузка будет начата заново.") from None
        raise
    with lifecycle.response(response) as response:
        start, length, expected = _transfer_start(output, response, getattr(response, "getcode", lambda: 200)() or 200, response.geturl(), limit)
        count = start
        read_chunk = getattr(response, "read1", response.read)
        while True:
            _read_timeout(response, deadline, timeout)
            block = read_chunk(256 * 1024)
            _remaining(deadline)
            if not block:
                break
            count += len(block)
            if count > limit or (length and count > length):
                raise _Incomplete("Загрузка превысила ограничение.")
            output.write(block)
            notify(count, length)
        if expected and count - start != expected:
            raise _Incomplete("Передача файла прервана.")
        if isinstance(output, _PartialDownload) and length and count != length:
            # A valid 206 can cover less than the requested remainder. Keep it
            # as an unfinished fragment, rather than hashing/deleting it.
            raise _Incomplete("Получена часть диапазона файла. Продолжаем загрузку.")
        return dict(response.headers), response.geturl()


def _receive_requests(url, output, limit, notify=lambda *_: None, *, session=None, deadline=None):
    """Independent HTTPS transport for Maven; keep cancellation and TLS verification."""
    import requests
    from urllib3 import exceptions as urllib3_errors

    lifecycle.check()
    local_deadline = time.monotonic() + MAX_RECEIVE_SECONDS
    deadline = min(deadline, local_deadline) if deadline is not None else local_deadline
    from urllib3.util import Timeout
    # Requests uses the environment/system proxy configuration. Respect an
    # explicitly configured CA file; do not disable certificate verification.
    try:
        with (requests.Session() if session is None else nullcontext(session)) as session:
            if os.environ.get("SSL_CERT_FILE") and not (
                os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
            ):
                session.verify = os.environ["SSL_CERT_FILE"]
            seen = {url}
            for step in range(MAX_REDIRECTS + 1):
                remaining = _remaining(deadline)
                timeout = Timeout(total=remaining, connect=min(12, remaining), read=min(SOCKET_TIMEOUT, remaining))
                with lifecycle.response(session.get(
                    url, headers=_transfer_headers(output), timeout=timeout, stream=True,
                    allow_redirects=False,
                )) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = _redirect_target(url, response.headers.get("Location"))
                        if target in seen or step == MAX_REDIRECTS:
                            raise _Incomplete("Сервер повторяет перенаправления загрузки.")
                        seen.add(target)
                        url = target
                        continue
                    start, length, expected = _transfer_start(output, response, response.status_code, response.url, limit)
                    count = start
                    # Check after each socket read, including near the deadline.
                    while True:
                        _read_timeout(response, deadline, SOCKET_TIMEOUT)
                        block = response.raw.read1(64 * 1024, decode_content=True)
                        _remaining(deadline)
                        if not block:
                            break
                        count += len(block)
                        if count > limit or (length and count > length):
                            raise _Incomplete("Загрузка превысила ограничение.")
                        output.write(block)
                        notify(count, length)
                    if expected and count - start != expected:
                        raise _Incomplete("Передача файла прервана.")
                    if isinstance(output, _PartialDownload) and length and count != length:
                        raise _Incomplete("Получена часть диапазона файла. Продолжаем загрузку.")
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


def _installer_checksum(url, *, deadline):
    """Resolve only the exact official sidecar, with finite transport retries."""
    canonical = _neo_installer_url(url)
    if canonical is None:
        raise ValueError("Некорректный адрес установщика NeoForge.")
    url = canonical + ".sha1"
    deadline = min(deadline, time.monotonic() + CHECKSUM_SECONDS)
    receivers = (_receive, _receive_requests, _receive)
    for attempt, receive in enumerate(receivers):
        try:
            _remaining(deadline)
            output = io.BytesIO()
            _, effective = receive(url, output, 1024,
                                   deadline=min(deadline, time.monotonic() + CHECKSUM_ATTEMPT_SECONDS))
            _remaining(deadline)
            # urllib follows redirects. A mirror sidecar must not become an
            # authority merely because the original endpoint redirected to it.
            if effective != url:
                raise _Incomplete("Сервер перенаправил контрольную сумму установщика.")
            try:
                digest = output.getvalue().decode("ascii").strip()
            except UnicodeError:
                digest = ""
            if not re.fullmatch(r"[0-9a-fA-F]{40}", digest):
                raise _Incomplete("Сервер вернул неверную контрольную сумму установщика.")
            return digest.lower()
        except (HTTPError, URLError, HTTPException, TimeoutError, ConnectionError, _Incomplete) as error:
            if not _retry(error) or attempt + 1 == len(receivers) or time.monotonic() >= deadline:
                raise DownloadError(_error(url, error), url=url,
                                    status_code=error.code if isinstance(error, HTTPError) else None) from None
            lifecycle.wait(min(0.5 * (attempt + 1), max(0, deadline - time.monotonic())))
    raise AssertionError("unreachable")


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
    def __init__(self, progress, *, cache=None):
        self.progress = progress
        self.cache = Path(cache).resolve() if cache is not None else None
        self._sessions = {}
        self._sessions_lock = threading.Lock()
        self._preferred_neo_source = None

    def _installer_cache(self, url):
        if self.cache is None:
            return None
        directory = self.cache / "installer-cache" / hashlib.sha256(url.encode()).hexdigest()
        paths = directory / "installer.jar", directory / "source.json"
        for path in (directory.parent, directory, *paths, directory / "installer.part", directory / "installer.json"):
            if (path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                    or path.resolve() != path):
                raise DownloadError("Кэш установщиков перенаправлен. Проверь папку данных BlockHarbor.")
        directory.mkdir(parents=True, exist_ok=True)
        return paths

    @staticmethod
    def _cached_installer_digest(record, url):
        try:
            with record.open("rb") as source:
                raw = source.read(4097)
            if len(raw) > 4096:
                return None
            data = json.loads(raw)
            if (isinstance(data, dict) and data.get("format") == 1 and data.get("url") == url
                    and isinstance(data.get("sha1"), str) and re.fullmatch(r"[a-f0-9]{40}", data["sha1"])):
                return data["sha1"]
        except (OSError, ValueError, RecursionError):
            pass
        return None

    @staticmethod
    def _remember_installer_digest(record, url, digest):
        Download._remember_partial(record, {"format": 1, "url": url, "sha1": digest})

    @staticmethod
    def _remember_partial(record, value):
        data = json.dumps(value).encode()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=record.parent, prefix=".blockharbor-download-", delete=False) as out:
                temporary = Path(out.name)
                out.write(data)
            with lifecycle.guard():
                os.replace(temporary, record)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _copy_installer(source, target, digest):
        """Verify the actual copied bytes before exposing a new vendor temp file."""
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with source.open("rb") as src, tempfile.NamedTemporaryFile(
                    dir=target.parent, prefix=".blockharbor-download-", delete=False) as out:
                temporary = Path(out.name)
                checksum, count = hashlib.sha1(), 0
                while block := src.read(1024 * 1024):
                    lifecycle.check()
                    count += len(block)
                    if count > MAX_DOWNLOAD:
                        raise DownloadError("Сохранённый установщик превышает допустимый размер.")
                    checksum.update(block)
                    out.write(block)
            if checksum.hexdigest() != digest:
                raise DownloadError("Сохранённый установщик изменился во время проверки. Повтори подготовку.")
            with lifecycle.guard():
                os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _fetch_installer(self, url, path, sha1, *, overwrite=False):
        deadline = time.monotonic() + INSTALLER_SECONDS
        paths = self._installer_cache(url)
        cached_digest = self._cached_installer_digest(paths[1], url) if paths else None
        if sha1 is not None and not (isinstance(sha1, str) and re.fullmatch(r"[a-fA-F0-9]{40}", sha1)):
            raise ValueError("Некорректная контрольная сумма установщика.")
        sha1 = sha1.lower() if sha1 else _NEO_INSTALLER_SHA1.get(_neo_path(url)) or cached_digest
        if not sha1:
            self.progress("Подготовка файлов NeoForge · проверяем контрольную сумму…")
            try:
                sha1 = _installer_checksum(url, deadline=deadline)
            except DownloadError:
                # Preserve the previous primary-only behavior for releases
                # without a trusted digest. Never mirror or cache such bytes.
                pass
        if not overwrite and _valid(path, sha1):
            return False
        if paths and sha1:
            cached, record = paths
            if cached_digest != sha1:
                self._remember_installer_digest(record, url, sha1)
            self.progress("Подготовка файлов NeoForge · проверяем сохранённый установщик…")
            if not _valid(cached, sha1):
                self.fetch(url, cached, sha1, False, deadline=deadline, partial=cached.with_suffix(".part"))
            # The upstream provider creates and removes its own temp directory
            # on each attempt. Keep our verified source even if Java then fails.
            self._copy_installer(cached, path, sha1)
            return True
        return self.fetch(url, path, sha1, False, deadline=deadline)

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
        installer = _neo_installer_url(url) if not lzma_compressed else None
        if not installer and not overwrite and _valid(path, sha1):
            return False
        self.progress.start()
        while not _THREADS.acquire(timeout=0.1):
            lifecycle.check()
        try:
            lifecycle.check()
            if installer:
                return self._fetch_installer(installer, path, sha1, overwrite=overwrite)
            return self.fetch(url, path, sha1, lzma_compressed)
        except OSError as error:
            message = storage_message(error)
            if message:
                raise DownloadError(message) from error
            raise
        finally:
            _THREADS.release()

    def fetch(self, url, path, sha1, compressed, *, deadline=None, partial=None):
        lifecycle.check()
        path.parent.mkdir(parents=True, exist_ok=True)
        relative = _neo_path(url)
        if relative is not None:
            url = _NEO_ORIGIN + relative
        if not compressed:
            from . import bundled_loaders

            try:
                bundled = bundled_loaders.copy_file(url, path, sha1)
            except bundled_loaders.BundleError as error:
                raise DownloadError(str(error), url=url) from error
            if bundled:
                self.progress("Подготовка файлов NeoForge · используем комплект BlockHarbor: " + path.name)
                if partial is not None:
                    partial.unlink(missing_ok=True)
                    partial.with_suffix(".json").unlink(missing_ok=True)
                return True
        routes = _neo_routes(url, sha1)
        if relative is not None:
            deadline = deadline if deadline is not None else time.monotonic() + MAVEN_SECONDS
            # Within this preparation only, reuse an independently verified CDN
            # route. A cold PC must not wait for the same dead origin 100 times.
            with self._sessions_lock:
                preferred = self._preferred_neo_source
            if sha1 and preferred:
                routes.sort(key=lambda route: not route[0].startswith(preferred))
        failures = []
        for attempt, (source_url, alternate) in enumerate(routes):
            packed = plain = None
            resumable = None
            try:
                if deadline is not None:
                    _remaining(deadline)
                if partial is not None:
                    resumable = _PartialDownload(partial, url, sha1)
                    packed = partial
                    # The final bytes can already be on disk after a cancelled
                    # EOF/verification step; the checksum is still mandatory.
                    if _valid(partial, sha1):
                        resumable.stream.close()
                        with lifecycle.guard():
                            os.replace(partial, path)
                        resumable.record.unlink(missing_ok=True)
                        return True
                    context = resumable
                else:
                    context = tempfile.NamedTemporaryFile(
                        dir=path.parent, prefix=".blockharbor-download-", delete=False)
                with context as out:
                    if packed is None:
                        packed = Path(out.name)
                    receive = _receive_requests if alternate else _receive
                    if (not alternate and sha1 and urlsplit(source_url).scheme == "https"
                            and urlsplit(source_url).hostname in _POOLED_MOJANG):
                        receive = self._receive_asset
                    attempt_deadline = deadline
                    if relative is not None and deadline is not None:
                        # Reserve useful time for every route. A slow or stalled
                        # origin cannot consume the mirror's entire budget.
                        allowance = min(MAVEN_ATTEMPT_SECONDS, _remaining(deadline) / (len(routes) - attempt))
                        attempt_deadline = min(deadline, time.monotonic() + allowance)
                    receive(
                        source_url,
                        out,
                        MAX_DOWNLOAD,
                        lambda n, total: self.progress.downloaded(path.name, n, total),
                        **({"deadline": attempt_deadline} if attempt_deadline is not None else {}),
                    )
                if deadline is not None:
                    _remaining(deadline)
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
                    if resumable is not None:
                        packed.unlink(missing_ok=True)
                        resumable.record.unlink(missing_ok=True)
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
                if resumable is not None:
                    resumable.record.unlink(missing_ok=True)
                if relative is not None and sha1:
                    with self._sessions_lock:
                        if source_url.startswith(_NEO_INSTALLER_MIRROR):
                            self._preferred_neo_source = _NEO_INSTALLER_MIRROR
                        elif source_url.startswith(_NEO_ORIGIN):
                            self._preferred_neo_source = _NEO_ORIGIN
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
                if isinstance(error, HTTPError):
                    error.close()
                if isinstance(error, _RestartDownload) and resumable is not None:
                    partial.unlink(missing_ok=True)
                    resumable.record.unlink(missing_ok=True)
                failures.append((source_url, alternate, error))
                # A verified artifact can use a different HTTPS transport even
                # at the same URL; a mirror's redirect/status must not skip it.
                verified_fallback = (
                    relative is not None
                    and sha1
                    and bool(routes[attempt + 1:])
                    and not isinstance(
                        error.reason if isinstance(error, URLError) else error,
                        ssl.SSLCertVerificationError,
                    )
                )
                exhausted = deadline is not None and time.monotonic() >= deadline
                if (not _retry(error) and not verified_fallback) or attempt + 1 == len(routes) or exhausted:
                    message = _error(source_url, error)
                    if relative is not None:
                        name = urlsplit(url).path.rsplit("/", 1)[-1]
                        message = f"Не удалось подготовить {name}. " + message
                        details = []
                        for failed_url, transport, failed_error in failures:
                            host = urlsplit(failed_url).hostname or "сервер файлов"
                            reason = failed_error.reason if isinstance(failed_error, URLError) else failed_error
                            if isinstance(failed_error, HTTPError):
                                detail = f"HTTP {failed_error.code}"
                            elif isinstance(reason, ssl.SSLCertVerificationError):
                                detail = "ошибка сертификата"
                            elif isinstance(reason, TimeoutError):
                                detail = "тайм-аут"
                            elif isinstance(reason, socket.gaierror):
                                detail = "ошибка DNS"
                            elif isinstance(reason, _Incomplete):
                                detail = str(reason).rstrip(".")
                            else:
                                detail = "соединение прервано"
                            details.append(f"{host} ({'requests' if transport else 'urllib'}): {detail}")
                        message += " Проверенные маршруты: " + "; ".join(details) + "."
                        if partial is not None and partial.is_file() and partial.stat().st_size:
                            message += " Полученная часть установщика сохранена для следующей попытки."
                    if exhausted:
                        message += " Общее время подготовки файла истекло."
                    elif relative is not None and any(failed_url != url for failed_url, _, _ in failures):
                        message += " Резервная загрузка также не завершена."
                    raise DownloadError(
                        message, url=source_url,
                        status_code=error.code if isinstance(error, HTTPError) else None,
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
                delay = 0.5 * (attempt + 1)
                if deadline is not None:
                    # Backoff must also leave useful time for every untried
                    # route, including a nearly exhausted shared deadline.
                    remaining = max(0, deadline - time.monotonic())
                    delay = min(delay, remaining / (len(routes) - attempt) / 10)
                lifecycle.wait(delay)
            finally:
                if resumable is not None:
                    resumable.stream.close()
                if plain is not None and plain != partial:
                    plain.unlink(missing_ok=True)
                if packed is not None and packed != plain and packed != partial:
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
        prepared = nullcontext(command)
        if isinstance(command, (tuple, list)) and command and Path(
            str(command[0])
        ).name.lower() in ("java", "java.exe"):
            if self._download_mappings(command):
                empty = "" if kwargs.get("text") or kwargs.get("universal_newlines") else b""
                return subprocess.CompletedProcess(command, 0, empty, empty)
            if self.downloader is not None and self.cache is not None:
                from .neoforge_install import adapt

                prepared = adapt(command, downloader=self.downloader, cache=self.cache,
                                 cwd=kwargs.get("cwd"))
        kwargs.setdefault("timeout", 900)
        check = kwargs.pop("check", self.check_default)
        try:
            with prepared as command:
                if isinstance(command, (tuple, list)) and command and Path(
                    str(command[0])
                ).name.lower() in ("java", "java.exe"):
                    command = [command[0], "-Djava.net.useSystemProxies=true", *command[1:]]
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
    downloader = Download(progress, cache=cache)
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
