"""Small, persistent, per-build index of failed operations and Minecraft exits."""

import os
import re
import stat
import time

from .diagnostics import RULES, read_log_chunk, redact

HISTORY_LIMIT = 50
_GAME_SIGNS = [(key, re.compile(pattern, re.I), title, advice)
               for key, pattern, title, _, advice in RULES]
_OPERATION_SIGNS = (
    ("disk", r"No space left|недостаточно места|not enough space|WinError 112", "На диске недостаточно места",
     "Освободи место на диске со сборкой и повтори операцию."),
    ("access", r"PermissionError|Permission denied|WinError (5|32)\b|доступ запрещ|отказано в доступе", "Нет доступа к файлу",
     "Закрой игру и другие программы, использующие этот файл. Проверь права записи в папку сборки."),
    ("integrity", r"контрольн\w* сумм|поврежд|checksum|hash mismatch", "Проверка файла не прошла",
     "Посмотри имя файла в подробностях и повтори загрузку из каталога."),
    ("dependency", r"зависимост|обязательн\w+ библиотек|dependency", "Проблема с зависимостью",
     "Открой каталог дополнений и проверь список обязательных библиотек для этой версии игры и загрузчика."),
    ("loader", r"нет поддерживаемой установки|не принимает сочетание|нет совместимого|incompatible|несовместим",
     "Версии компонентов не подходят друг другу", "Сверь версию игры и загрузчик с требованиями указанного компонента."),
    ("download", r"HTTP\s*\d{3}|timeout|timed out|тайм[\s-]?аут|ист[её]кло время ожидания|"
     r"соединени|не удалось скач|загрузка[^\n]*не завершена|интернет|сетев\w+ ошиб",
     "Не удалось получить файл или сведения о нём", "Посмотри имя файла или сервиса в подробностях. Проверь подключение и повтори загрузку."),
)
_GENERIC_LINE = re.compile(r"Caused by:|Exception in thread|\b(?:FATAL|ERROR)\b|(?:Exception|Error):", re.I)


def _recent_lines(folder, relative, started, *, from_start=False):
    # A mod can write arbitrary files or links. Read only a regular private log
    # inside this instance, with a bounded read and no waiting for special files.
    path = folder / relative
    try:
        if path.resolve() != folder.resolve() / relative:
            return []
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mtime + 1 < started:
            return []
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if not os.path.samestat(info, os.fstat(stream.fileno())):
                return []
            lines = read_log_chunk(stream, 65536, tail=not from_start).splitlines()
        return [line[:1200] for line in (lines[:240] if from_start else lines[-240:])]
    except (OSError, ValueError, RuntimeError):
        return []


def _sequence(db):
    # Old databases used reusable SQLite rowids. Preserve their highest ID
    # before clearing so a delayed clear cannot erase a newly recorded error.
    db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('buildErrorSequence','0')")
    db.execute("""UPDATE settings SET value=CAST(MAX(CAST(value AS INTEGER),
                  (SELECT COALESCE(MAX(id),0) FROM build_errors)) AS TEXT)
                  WHERE key='buildErrorSequence'""")
    return int(db.execute("SELECT value FROM settings WHERE key='buildErrorSequence'").fetchone()[0])


def _save(store, pid, group, title, advice, stage, evidence):
    details = redact(evidence, store.root)[:2400]
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        sequence = _sequence(db) + 1
        db.execute("UPDATE settings SET value=? WHERE key='buildErrorSequence'", (str(sequence),))
        # Bind the entry to a still-existing build in this very transaction.
        db.execute("""INSERT INTO build_errors(id,pid,created,category,title,advice,stage,evidence)
                      SELECT ?,id,?,?,?,?,?,? FROM profiles WHERE id=?""",
                   (sequence, time.time(), group, title, advice, redact(stage, store.root)[:300], details, pid))
        db.execute("""DELETE FROM build_errors WHERE pid=? AND id NOT IN
                      (SELECT id FROM build_errors WHERE pid=? ORDER BY id DESC LIMIT ?)""",
                   (pid, pid, HISTORY_LIMIT))


def record_operation(store, pid, stage, error):
    message = str(error)[:6000] or type(error).__name__
    for key, pattern, title, advice in _OPERATION_SIGNS:
        if re.search(pattern, message, re.I):
            break
    else:
        key, title = "operation", "Операция со сборкой не завершилась"
        advice = "Открой подробности ниже. В первой строке указано действие, во второй — сообщение лаунчера."
    _save(store, pid, key, title, advice, stage, message)


def _latest_crash_file(folder, started):
    directory = folder / "crash-reports"
    try:
        if directory.resolve() != folder.resolve() / "crash-reports":
            return None
        # Minecraft's report names begin with a sortable date and time.
        with os.scandir(directory) as entries:
            name = max((entry.name for entry in entries
                        if re.fullmatch(r"crash-\d{4}-\d{2}-\d{2}_[\w.-]+\.txt", entry.name)
                        and entry.is_file(follow_symlinks=False)
                        and entry.stat(follow_symlinks=False).st_mtime + 1 >= started), default=None)
        return f"crash-reports/{name}" if name else None
    except (OSError, RuntimeError):
        return None


def record_exit(store, pid, code, started):
    folder = store.folder(pid)
    source_lines = []
    if started:
        for source in ("logs/launcher-stderr.log", "logs/latest.log"):
            source_lines.extend((source, line) for line in _recent_lines(folder, source, started))
        report = _latest_crash_file(folder, started)
        if report:
            source_lines.extend((report, line) for line in _recent_lines(folder, report, started, from_start=True))
    stage = f"Завершение Minecraft · код {code}"
    for key, pattern, title, advice in _GAME_SIGNS:
        matches = [(source, line) for source, line in source_lines if pattern.search(line)]
        if matches:
            evidence = "\n".join(f"{s} · {line}" for s, line in matches[-3:])
            _save(store, pid, key, title, advice, stage, evidence)
            return
    matches = [f"{s} · {line}" for s, line in source_lines if _GENERIC_LINE.search(line)]
    _save(store, pid, "crash", "Minecraft завершился с ошибкой",
          "Открой диагностику этой сборки. Она проверит отчёт о сбое и метаданные установленных модов.",
          stage, "\n".join(matches[-3:]) or f"Код завершения процесса: {code}")


def history(store, pid):
    store.profile(pid)
    with store.connection() as db:
        rows = db.execute("""SELECT id,created,category,title,advice,stage,evidence FROM build_errors
                             WHERE pid=? ORDER BY id DESC LIMIT ?""", (pid, HISTORY_LIMIT)).fetchall()
    groups = {}
    for row in rows:
        group = groups.setdefault(row["category"], {"title": row["title"], "advice": row["advice"], "events": []})
        group["events"].append({key: row[key] for key in ("created", "stage", "evidence")})
    return {"groups": list(groups.values()), "limit": HISTORY_LIMIT, "lastId": rows[0]["id"] if rows else 0}


def clear(store, pid, through_id):
    if type(through_id) is not int or not 0 <= through_id <= 2**63 - 1:
        raise ValueError("Обнови историю ошибок перед очисткой.")
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM profiles WHERE id=?", (pid,)).fetchone():
            raise ValueError("Сборка не найдена.")
        _sequence(db)
        deleted = db.execute("DELETE FROM build_errors WHERE pid=? AND id<=?", (pid, through_id)).rowcount
    return {"ok": True, "cleared": deleted, "message": "История ошибок сборки очищена."}
