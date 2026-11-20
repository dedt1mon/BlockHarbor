"""Bounded local crash analysis and privacy-aware, user-reviewable reports.

Rules explain evidence, not certainty about arbitrary third-party mod code. No
report is transmitted anywhere by this module.
"""

from __future__ import annotations

import codecs
import ctypes
import io
import json
import os
import re
import stat
import subprocess
import threading
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from . import compatibility
from .store import KINDS
from .version import VERSION

MAX_LOG = 2 * 1024 * 1024


@contextmanager
def game_stderr(folder):
    """Give Java its own log handle so closing Python cannot break a pipe.

    A fresh launch truncates the prior log. Reports still read a bounded tail;
    no launcher thread has to keep writing or pumping after a manual X.
    """
    folder = Path(folder).resolve()
    target = folder / "logs" / "launcher-stderr.log"
    stream = None
    try:
        if target.parent.resolve() != folder / "logs" or target.is_symlink():
            raise OSError("Game log directory is redirected")
        target.parent.mkdir(exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        stream = os.fdopen(descriptor, "wb")
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or not os.path.samestat(info, target.lstat())
                or target.parent.resolve() != folder / "logs"):
            raise OSError("Game log file is redirected")
        stream.truncate(0)
    except OSError:
        if stream:
            stream.close()
        stream = None
    try:
        yield stream if stream is not None else subprocess.DEVNULL
    finally:
        if stream:
            stream.close()


def capture_stderr(stream, folder):
    """Drain Java stderr continuously while retaining only the last two MiB.

    The command line/access token is never logged here. Report export still
    redacts game output, since a mod may write arbitrary messages itself.
    """
    if not isinstance(stream, io.IOBase):
        return None
    folder = Path(folder).resolve()
    target = folder / "logs" / "launcher-stderr.log"

    def write(raw):
        temporary = None
        try:
            if target.parent.resolve() != folder / "logs" or target.is_symlink():
                return
            target.parent.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".stderr-", delete=False) as file:
                temporary = Path(file.name)
                file.write(raw)
            if target.parent.resolve() == folder / "logs" and not target.is_symlink():
                os.replace(temporary, target)
        except OSError:
            pass  # Draining the pipe must continue even if the disk is full.
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def pump():
        tail, last = bytearray(), 0.0
        try:
            write(b"")
            while block := stream.read(8192):
                tail.extend(block)
                if len(tail) > MAX_LOG:
                    del tail[:-MAX_LOG]
                if time.monotonic() - last > 0.75:
                    write(tail)
                    last = time.monotonic()
        except (OSError, ValueError):
            pass
        finally:
            write(tail)
            stream.close()

    thread = threading.Thread(target=pump, daemon=True, name="blockharbor-game-stderr")
    thread.start()
    return thread


def redact(text, root=None):
    text = str(text)
    if root:
        for value in (str(Path(root)), str(Path(root)).replace("\\", "/")):
            text = text.replace(value, "<BlockHarbor>")
    text = re.sub(r"(?i)(?:[a-z]:[\\/]users[\\/]|/(?:home|Users)/)[^\\/\s]+", "<user>", text)
    text = re.sub(r'''(?i)(authorization[\\"']*\s*[:=]\s*[\\"']*(?:(?:bearer|basic|token)\s+)?)[^\\"'\s,;}\]]+''', r"\1<hidden>", text)
    text = re.sub(r"(?i)((?:access[_-]?token|refresh[_-]?token|client[_-]?secret|session(?:id|token)?|password|api[_-]?key)\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+", r"\1<hidden>", text)
    text = re.sub(r"(?i)(--(?:accessToken|clientId|uuid|username)\s+)[^\s]+", r"\1<hidden>", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "<token>", text)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "<email>", text)
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b", "<ip>", text)
    text = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "<uuid>", text)
    text = re.sub(r"\b7656119\d{10}\b", "<steam-id>", text)
    text = re.sub(r"(?i)\b(?:https?://|s-)[^\s\"'<>]+", "<address>", text)
    return text


def read_log_chunk(stream, limit, *, tail=True):
    """Decode bounded excerpts using the file's BOM even when reading its tail."""
    size = stream.seek(0, os.SEEK_END)
    offset = max(0, size - limit) if tail else 0
    stream.seek(0)
    header = stream.read(4)
    for bom, encoding, alignment in (
        (codecs.BOM_UTF32_LE, "utf-32-le", 4), (codecs.BOM_UTF32_BE, "utf-32-be", 4),
        (codecs.BOM_UTF16_LE, "utf-16-le", 2), (codecs.BOM_UTF16_BE, "utf-16-be", 2),
        (codecs.BOM_UTF8, "utf-8", 1),
    ):
        if header.startswith(bom):
            start = max(len(bom), offset + (-offset % alignment))
            stream.seek(start)
            raw = stream.read(max(0, offset + limit - start))
            if offset and encoding == "utf-8":
                raw = re.sub(rb"^[\x80-\xbf]{1,3}", b"", raw)
            return codecs.getincrementaldecoder(encoding)("replace").decode(raw, final=stream.tell() >= size)
    stream.seek(offset)
    raw = stream.read(limit)
    # A tail can start halfway through an otherwise valid UTF-8 character.
    candidate = re.sub(rb"^[\x80-\xbf]{1,3}", b"", raw) if offset else raw
    try:
        return codecs.getincrementaldecoder("utf-8")().decode(candidate, final=stream.tell() >= size)
    except UnicodeDecodeError:
        pass

    # Older Windows JVMs and console tools use ANSI or OEM encodings.
    from charset_normalizer import from_bytes
    encodings = ["cp1251", "cp866", "cp1252"]
    if os.name == "nt":
        encodings[:0] = [f"cp{ctypes.windll.kernel32.GetACP()}", f"cp{ctypes.windll.kernel32.GetOEMCP()}"]
    # Java and individual mods may write different encodings to the same log.
    # Detect the legacy encoding from an affected line, excluding UTF-8 lines.
    encoding = None
    lines = []
    for line in raw.split(b"\n"):
        try:
            lines.append(line.decode("utf-8"))
        except UnicodeDecodeError as error:
            if encoding is None:
                sample = line[max(0, error.start - 64):error.start + 8192]
                match = from_bytes(sample, cp_isolation=encodings).best()
                encoding = match.encoding if match else encodings[0]
            lines.append(line.decode(encoding, "replace"))
    return "\n".join(lines)


def _tail(folder, path):
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
        return ""
    try:
        with path.open("rb") as stream:
            return read_log_chunk(stream, MAX_LOG)
    except (FileNotFoundError, PermissionError, OSError):
        return ""


RULES = (
    ("memory", r"OutOfMemoryError|Java heap space|GC overhead limit exceeded", "Игре не хватило памяти Java", "high", "Открой «Настройки» и проверь, сколько оперативной памяти выделено игре. Закрой лишние приложения или отключи тяжёлые дополнения."),
    ("native-memory", r"Could not reserve enough space|Native memory allocation.*failed|unable to create native thread|There is insufficient memory", "Недостаточно свободной памяти системы", "high", "Уменьши лимит памяти Java или закрой другие приложения. Слишком большой лимит также мешает запуску."),
    ("java", r"UnsupportedClassVersionError|class file version .*only recognizes|requires.*Java (?:17|21|25)|Unsupported Java", "Версия Java не подходит компоненту сборки", "high", "Проверь версию Minecraft и мода. BlockHarbor подбирает Java для Minecraft; мод для другой версии игры может требовать другую Java."),
    ("duplicate", r"DuplicateModsFoundException|Duplicate mod[s :]|Found duplicate mods|duplicate mod ID", "Обнаружены повторяющиеся моды", "high", "В управлении содержимым отключи один из дубликатов. Перед изменением будет создана точка восстановления."),
    ("dependencies", r"requires .*which is missing|Missing (?:mandatory )?dependenc|ModResolutionException|requires version .* of|Incompatible mod set", "Не сходятся зависимости модов", "medium", "Проверь список зависимостей ниже и версии Minecraft/загрузчика. Открой каталог дополнений и выбери требуемые библиотеки для своей сборки."),
    ("graphics", r"GLFW error 6554[23]|WGL:.*OpenGL|Failed to create (?:window|OpenGL)|Pixel format not accelerated|OpenGL.*not supported", "Не удалось создать графический контекст", "high", "Проверь драйвер видеокарты и поддержку этой версии Minecraft. На ноутбуке можно выбрать другую видеокарту в настройках Windows."),
    ("mixin", r"MixinApplyError|MixinTransformerError|InvalidMixinException|Critical injection failure", "Сбой применения изменений одного из модов", "medium", "Сравни последние изменения сборки. Попробуй восстановить предыдущую точку или отключить последний добавленный мод вместе с его зависимостями."),
    ("file", r"ZipException|zip END header not found|invalid LOC header|Invalid or corrupt jarfile", "Повреждён или не полностью скачан JAR/ZIP", "high", "Проверь имя файла в строке журнала. Переустанови этот файл из каталога; контрольная сумма проверяется при загрузке."),
    ("network", r"UnknownHostException|ConnectException|SocketTimeoutException|Network is unreachable|SteamAPI_Init.*fail", "Есть ошибка подключения", "medium", "Открой проверку Steam во вкладке «Друзья». Проверь доступность Steam и выбранного мира; VPN и сетевые фильтры могут менять маршрут."),
)


def analyze(store, pid, exit_code=None):
    profile, folder = store.profile(pid), store.folder(pid)
    sources = [("logs/latest.log", folder / "logs/latest.log"), ("logs/launcher-stderr.log", folder / "logs/launcher-stderr.log")]
    crash_dir = folder / "crash-reports"
    if crash_dir.is_dir() and not crash_dir.is_symlink():
        candidates = [p for p in crash_dir.glob("*.txt") if p.is_file() and not p.is_symlink()]
        if candidates:
            latest = max(candidates, key=lambda p: p.stat().st_mtime)
            sources.append(("crash-reports/" + latest.name, latest))
    findings, log_rows, seen = [], [], set()
    encoding_warnings = []
    for name, path in sources:
        text = _tail(folder, path)
        if not text:
            continue
        lines = text.splitlines()
        if re.search(r"\?{3,}(?:[ \t]+\?{2,})+", text):
            encoding_warnings.append(f"{name}: в исходном журнале уже записаны группы знаков вопроса. Исходные буквы в этих фрагментах восстановить невозможно.")
        log_rows.append({"name": name, "modified": path.stat().st_mtime, "excerpt": redact("\n".join(lines[-100:]), store.root)[-24000:]})
        for key, pattern, title, confidence, advice in RULES:
            if key in seen:
                continue
            matching = [line for line in lines if re.search(pattern, line, re.I)]
            if matching:
                seen.add(key)
                findings.append({"id": key, "title": title, "confidence": confidence, "evidence": [redact(line, store.root)[:500] for line in matching[-3:]], "source": name, "advice": advice})
    mods = {f.name: f for f in store.content_folder(pid, "mods").glob("*.jar") if f.is_file() and not f.is_symlink()}
    local = compatibility.local_report(profile, {}, mods)
    missing = [r for r in local["required"] if not r["installed"]]
    for entry in missing[:30]:
        findings.append({"id": "missing-" + entry["id"], "title": "Не найдена зависимость: " + entry["name"], "confidence": "high", "evidence": [entry["source"] + " → " + entry["id"] + " " + entry["version"]], "source": "Метаданные установленного мода", "advice": "Открой каталог дополнений и установи библиотеку для версии этой сборки.", "dependency": entry["id"]})
    for n, message in enumerate(local["conflicts"][:30]):
        findings.append({"id": "conflict-" + str(n), "title": "Конфликт метаданных", "confidence": "high", "evidence": [message], "source": "Метаданные установленных модов", "advice": "Исправь состав сборки в управлении содержимым или восстанови предыдущую точку."})
    return {"profile": {k: profile[k] for k in ("id", "name", "version", "loader")}, "exitCode": exit_code, "findings": findings[:50], "logs": log_rows, "warnings": encoding_warnings + local["warnings"][:15], "checkedAt": time.time(), "empty": not log_rows, "notice": "Анализ основан на строках журнала и метаданных. Совпадение признака не доказывает причину каждого сбоя."}


def report_text(store, result):
    data = {"launcher": "BlockHarbor " + VERSION, "minecraft": result["profile"]["version"], "loader": result["profile"]["loader"], "exitCode": result["exitCode"], "findings": result["findings"], "logs": result["logs"], "warnings": result["warnings"]}
    return redact(json.dumps(data, ensure_ascii=False, indent=2), store.root)


def resident_bytes(pid):
    """Actual process working set, or None when the OS cannot provide it."""
    if os.name == "nt":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), *[(name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]

        kernel, psapi = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("psapi", use_last_error=True)
        kernel.OpenProcess.restype, kernel.OpenProcess.argtypes = wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
        if not handle:
            return None
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            return counters.WorkingSetSize if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb) else None
        finally:
            kernel.CloseHandle(handle)
    try:
        text = Path(f"/proc/{int(pid)}/status").read_text()
        value = re.search(r"(?m)^VmRSS:\s+(\d+) kB", text)
        return int(value[1]) * 1024 if value else None
    except (OSError, ValueError):
        return None


class SessionMetrics:
    def __init__(self, store):
        self.store, self.lock = store, threading.RLock()
        self.sessions, self.preparing = {}, {}
        self.stop = threading.Event()
        self.threads = set()
        with store.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS game_sessions(id INTEGER PRIMARY KEY, profile TEXT, started REAL, duration REAL, preparation REAL, peak_rss INTEGER, exit_code INTEGER)")

    def prepare(self, pid):
        with self.lock:
            if self.stop.is_set():
                return
            self.preparing[pid] = time.monotonic()

    def start(self, pid, process):
        now = time.monotonic()
        with self.lock:
            if self.stop.is_set():
                return
            self.sessions[pid] = {"pid": int(process.pid), "started": time.time(), "clock": now, "preparation": round(now - self.preparing.pop(pid, now), 2), "peak": 0, "rss": None}
            thread = threading.Thread(target=self._sample, args=(pid,), daemon=True, name="blockharbor-metrics")
            self.threads.add(thread)
            thread.start()

    def _sample(self, pid):
        while not self.stop.is_set():
            with self.lock:
                row = self.sessions.get(pid)
                if not row:
                    return
                process_id, started = row["pid"], row["started"]
            try:
                rss = resident_bytes(process_id)
            except (OSError, AttributeError, ValueError):
                rss = None
            with self.lock:
                current = self.sessions.get(pid)
                if not current or current["started"] != started:
                    return
                current["rss"] = rss
                current["peak"] = max(current["peak"], rss or 0)
            self.stop.wait(3)

    def close(self, timeout=0.5):
        self.stop.set()
        with self.lock:
            threads = tuple(self.threads)
            self.sessions.clear()
            self.preparing.clear()
        deadline = time.monotonic() + max(0, timeout)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))
        with self.lock:
            self.threads = {thread for thread in self.threads if thread.is_alive()}

    def finish(self, pid, code):
        with self.lock:
            session = self.sessions.pop(pid, None)
            self.preparing.pop(pid, None)
        if session and not self.stop.is_set():
            with self.store.connection() as db:
                db.execute("INSERT INTO game_sessions(profile,started,duration,preparation,peak_rss,exit_code) VALUES(?,?,?,?,?,?)", (pid, session["started"], round(time.monotonic() - session["clock"], 2), session["preparation"], session["peak"] or None, code))
                db.execute("DELETE FROM game_sessions WHERE id NOT IN (SELECT id FROM game_sessions ORDER BY id DESC LIMIT 200)")

    def state(self, pid):
        with self.lock:
            current = self.sessions.get(pid)
            current = {"residentBytes": current["rss"], "peakBytes": current["peak"] or None, "seconds": round(time.monotonic() - current["clock"]), "preparationSeconds": current["preparation"]} if current else None
        with self.store.connection() as db:
            recent = [dict(row) for row in db.execute("SELECT started,duration,preparation,peak_rss,exit_code FROM game_sessions WHERE profile=? ORDER BY id DESC LIMIT 20", (pid,))]
        return {"current": current, "recent": recent, "label": "Подготовка до запуска процесса Java; память — рабочий набор процесса. FPS здесь не измеряется."}
