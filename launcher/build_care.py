"""Local restore points, world backups and reviewed changes to installed content.

Nothing in this module executes a JAR. Recovery archives stay on this computer;
portable sharing is deliberately handled by the separate archive modules.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import struct
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict, deque
from pathlib import Path, PurePosixPath

from . import compatibility, e4steam, modrinth, room_bridge, skin_sync
from .content_icons import _small_directory
from .modfiles import file_hash, file_stamp, read_descriptor
from .store import KINDS, clean_filename, input_profile, require_idle_files, verify_pack

BUILD_SECTIONS = (
    "mods", "resourcepacks", "shaderpacks", "config", "defaultconfigs",
    "options.txt", "optionsof.txt", "optionsshaders.txt", ".blockharbor-install.json",
)
MAX_BYTES = 16 * 1024**3
MAX_FILES = 60000
MAX_MANIFEST = 16 * 1024**2
PROFILE_SETTINGS = ("performanceMemory", "performancePreset", "careAutoWorldBackup")
_ID = re.compile(r"[a-f0-9]{32}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _plain(value, limit=500):
    if isinstance(value, dict):
        value = value.get("text", "")
    if isinstance(value, list):
        value = " ".join(_plain(v, limit) for v in value[:32])
    return str(value or "")[:limit]


def _reparse(path):
    st = path.lstat()
    return bool(getattr(st, "st_file_attributes", 0) & 0x400) or stat.S_ISLNK(st.st_mode)


def _atomic_json(path, value):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, separators=(",", ":"))
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def _remove(path):
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _safe_relative(name):
    if not isinstance(name, str) or not name or len(name) > 700 or "\\" in name:
        raise ValueError("Недопустимый путь в точке восстановления.")
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or len(path.parts) > 32:
        raise ValueError("Недопустимый путь в точке восстановления.")
    for part in path.parts:
        if part == ".blockharbor-install.json" and len(path.parts) == 1:
            continue
        if part in (".", ".."):
            raise ValueError("Путь выходит за пределы сборки.")
        # Dot files inside configs/worlds are allowed locally, but never '..'.
        clean_filename(part.lstrip(".") if part.startswith(".") else part)
    return path


def _archive_limit(path):
    """Bound the central directory before ZipFile allocates all entry objects."""
    with path.open("rb") as source:
        end = zipfile._EndRecData(source)
        if not end or end[1] or end[2] or end[3] != end[4] or not 1 <= end[4] <= MAX_FILES + 1 or end[5] > 32 * 1024**2:
            raise ValueError("Недопустимый каталог резервной копии.")
        # Recovery archives are written without a self-extracting prefix.
        source.seek(end[6])
        raw = source.read(end[5])
        offset, count = 0, 0
        while offset < len(raw):
            if count >= end[4] or offset + 46 > len(raw) or raw[offset:offset + 4] != b"PK\x01\x02":
                raise ValueError("Повреждён каталог резервной копии.")
            name, extra, comment = struct.unpack_from("<3H", raw, offset + 28)
            offset += 46 + name + extra + comment
            count += 1
        if offset != len(raw) or count != end[4]:
            raise ValueError("Повреждён каталог резервной копии.")


def _walk(root, sections):
    """Do not traverse a link, Windows junction or an unexpectedly changing file."""
    count, size = 0, 0
    for section in sections:
        relative = _safe_relative(section)
        base = root
        for part in relative.parts:
            base = base / part
            if os.path.lexists(base) and _reparse(base):
                raise ValueError("В сборке есть ссылка или junction: " + section)
        if not os.path.lexists(base):
            continue
        pending = [base]
        while pending:
            path = pending.pop()
            if _reparse(path):
                raise ValueError("В сборке есть ссылка или junction: " + str(path.relative_to(root)))
            st = path.stat()
            if stat.S_ISDIR(st.st_mode):
                pending.extend(sorted(path.iterdir(), reverse=True))
            elif stat.S_ISREG(st.st_mode):
                name = path.relative_to(root).as_posix()
                _safe_relative(name)
                if name.endswith("/session.lock"):
                    continue
                count += 1
                size += st.st_size
                if count > MAX_FILES or size > MAX_BYTES:
                    raise ValueError("Для одной копии доступно до 16 ГБ и 60 000 файлов.")
                yield path, name, (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            else:
                raise ValueError("В сборке обнаружен неподдерживаемый тип файла.")


def _copy_sections(source, target, sections):
    files = list(_walk(source, sections))
    needed = sum(st[0] for _, _, st in files)
    if needed + 16 * 1024**2 > shutil.disk_usage(target.parent).free:
        raise ValueError("Недостаточно места на диске для безопасной копии.")
    for path, name, stamp in files:
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        if file_stamp(path) != stamp:
            raise ValueError("Файл изменился во время копирования. Закрой игру и повтори.")
    # Preserve empty roots (e.g. an empty mods directory).
    for section in sections:
        src = source / section
        if src.is_dir():
            (target / section).mkdir(parents=True, exist_ok=True)


class BuildCare:
    READ_ACTIONS = {"careState", "careDetails", "careWorlds", "careHistory"}
    JOBS = {"careSnapshot", "careWorldBackup", "careBackupWorlds", "careRestore", "careClone", "careCheckUpdates", "careApplyUpdates"}
    ACTIONS = READ_ACTIONS | JOBS | {"careToggle", "careRemove", "carePin", "careRetention", "careAutoBackup"}

    def __init__(self, store, transfers=None, on_clone=None):
        self.store = store
        self.transfers = transfers
        self.on_clone = on_clone
        self.root = store.root / "recovery"
        self.root.mkdir(exist_ok=True)
        if _reparse(self.root):
            raise ValueError("Папка восстановления не должна быть ссылкой.")
        self.lock = threading.RLock()
        self._meta = OrderedDict()
        self._plans = {}
        self.recovery_errors = {}
        with store.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS care_snapshots(id TEXT PRIMARY KEY, pid TEXT NOT NULL,
              scope TEXT NOT NULL, world TEXT NOT NULL, label TEXT NOT NULL, created REAL NOT NULL,
              size INTEGER NOT NULL, count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS care_history(id INTEGER PRIMARY KEY AUTOINCREMENT,
              pid TEXT NOT NULL, created REAL NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS care_pins(pid TEXT NOT NULL, kind TEXT NOT NULL,
              name TEXT NOT NULL, PRIMARY KEY(pid,kind,name));
            CREATE TABLE IF NOT EXISTS care_projects(pid TEXT NOT NULL, kind TEXT NOT NULL,
              name TEXT NOT NULL, stamp TEXT NOT NULL, metadata TEXT NOT NULL, PRIMARY KEY(pid,kind,name));
            """)
        self._recover()

    def _profile_root(self, pid):
        root = self.store._profile_directory(self.store.instances, pid)
        self.store.profile(pid)
        if root.exists() and _reparse(root):
            raise ValueError("Папка сборки не должна быть ссылкой.")
        return root

    def _archive_dir(self, pid):
        self.store.profile(pid)
        if not _ID.fullmatch(pid):
            raise ValueError("Неверный идентификатор сборки.")
        root = self.root / pid
        root.mkdir(exist_ok=True)
        if _reparse(root):
            raise ValueError("Папка копий перенаправлена.")
        return root

    def _writable_root(self, pid):
        if pid in self.recovery_errors:
            raise ValueError("Предыдущие изменения не удалось восстановить: " + self.recovery_errors[pid] + ". Проверь доступ к папке сборки и перезапусти BlockHarbor.")
        return self._profile_root(pid)

    def _apply_settings(self, pid, settings):
        with self.store.connection() as db:
            for key, value in settings.items():
                if value is None:
                    db.execute("DELETE FROM settings WHERE key=?", (key + ":" + pid,))
                else:
                    db.execute("REPLACE INTO settings VALUES(?,?)", (key + ":" + pid, value))

    def _content_records(self, pid):
        return [{key: row.get(key, "") for key in ("kind", "name", "size", "title", "version", "description", "project", "projectVersion", "icon")}
                for rows in self.inventory(pid).values() for row in rows]

    def _inherit_content(self, pid, records, renamed=None):
        renamed = renamed or {}
        for row in records:
            kind, name = row.get("kind"), row.get("name")
            if kind not in KINDS or not isinstance(name, str):
                continue
            name = renamed.get((kind, name), name)
            clean_filename(name)
            path = self.store.content_folder(pid, kind) / name
            if not path.is_file() or _reparse(path) or path.stat().st_size != row.get("size"):
                continue
            stamp = file_stamp(path)
            base = name[:-9] if name.lower().endswith(".disabled") else name
            metadata = {k: _plain(row.get(k), 500 if k == "description" else 180) for k in ("title", "version", "description", "project", "projectVersion")}
            with self.store.connection() as db:
                db.execute("REPLACE INTO care_projects VALUES(?,?,?,?,?)", (pid, kind, base, json.dumps(stamp), json.dumps(metadata)))
            icon = row.get("icon", "")
            match = re.fullmatch(r"/api/content-icon/([a-f0-9]{64})\.png", icon) if isinstance(icon, str) else None
            if match:
                try:
                    data = self.store.icons.read(match[1])
                    self.store.icons.remember_project(path, stamp, data, metadata.get("project", ""), metadata.get("projectVersion", ""))
                except (OSError, ValueError):
                    pass

    def purge(self, pid):
        """Remove owned recovery data only after Store's confirmed permanent purge."""
        if not isinstance(pid, str) or not _ID.fullmatch(pid):
            raise ValueError("Неверный идентификатор сборки.")
        with self.lock:
            with self.store.connection() as db:
                complete = db.execute("SELECT 1 FROM build_purges WHERE id=? AND completed=1", (pid,)).fetchone()
                active = db.execute("SELECT 1 FROM profiles WHERE id=? UNION SELECT 1 FROM deleted_profiles WHERE id=?", (pid, pid)).fetchone()
                if not complete or active:
                    raise ValueError("Сначала заверши безвозвратное удаление сборки.")
            directory = self.root / pid
            if os.path.lexists(directory):
                if _reparse(directory):
                    raise ValueError("Папка копий перенаправлена. Проверь её перед удалением.")
                shutil.rmtree(directory)
            plan = self._plans.pop(pid, None)
            if plan:
                shutil.rmtree(plan["directory"], ignore_errors=True)
            for work in self.root.glob("transaction-*"):
                journal = work / "journal.json"
                try:
                    if _reparse(work) or not journal.is_file() or journal.stat().st_size > 65536:
                        continue
                    if json.loads(journal.read_text(encoding="utf-8")).get("pid") == pid:
                        shutil.rmtree(work)
                except (OSError, ValueError, AttributeError):
                    continue
            with self.store.connection() as db:
                for table in ("care_snapshots", "care_history", "care_pins", "care_projects"):
                    db.execute("DELETE FROM " + table + " WHERE pid=?", (pid,))
                keys = [*(key + ":" + pid for key in PROFILE_SETTINGS), "careAutoWorldBackup:" + pid]
                db.executemany("DELETE FROM settings WHERE key=?", [(key,) for key in keys])
                db.execute("DELETE FROM settings WHERE key LIKE ?", ("careWorldSaved:" + pid + ":%",))
            self.recovery_errors.pop(pid, None)
            self._meta.clear()

    def history(self, pid, action, detail=""):
        with self.store.connection() as db:
            db.execute("INSERT INTO care_history(pid,created,action,detail) VALUES(?,?,?,?)",
                       (pid, time.time(), _plain(action, 180), _plain(detail, 1000)))
            db.execute("DELETE FROM care_history WHERE pid=? AND id NOT IN (SELECT id FROM care_history WHERE pid=? ORDER BY id DESC LIMIT 500)", (pid, pid))

    def _sections(self, scope, world):
        if scope == "build":
            return list(BUILD_SECTIONS)
        if scope == "world":
            clean_filename(world)
            return ["saves/" + world]
        raise ValueError("Неизвестный тип резервной копии.")

    def retention(self):
        try:
            count = max(1, min(100, int(self.store.get("careRetentionCount", "10"))))
            budget = max(128, min(102400, int(self.store.get("careBudgetMiB", "5120"))))
        except ValueError:
            count, budget = 10, 5120
        return {"count": count, "budgetMiB": budget}

    def _prune(self, preserve=""):
        policy = self.retention()
        with self.store.connection() as db:
            rows = list(db.execute("SELECT * FROM care_snapshots ORDER BY created DESC"))
        keep_count, keep_bytes = {}, 0
        keep = set()
        # Always retain the point just created, including an unusually large world.
        first = next((r for r in rows if r["id"] == preserve), None)
        if first:
            keep.add(preserve)
            keep_bytes = first["size"]
            keep_count[(first["pid"], first["scope"], first["world"])] = 1
        for row in rows:
            if row["id"] in keep:
                continue
            group = row["pid"], row["scope"], row["world"]
            if keep_count.get(group, 0) < policy["count"] and keep_bytes + row["size"] <= policy["budgetMiB"] * 1024**2:
                keep.add(row["id"])
                keep_count[group] = keep_count.get(group, 0) + 1
                keep_bytes += row["size"]
                continue
            path = self.root / row["pid"] / (row["id"] + ".zip")
            if not path.is_symlink():
                path.unlink(missing_ok=True)
            with self.store.connection() as db:
                db.execute("DELETE FROM care_snapshots WHERE id=?", (row["id"],))

    def before_change(self, pid, label):
        """Called by existing install/import/remove routes while their mutation lock is held."""
        return self.snapshot(pid, label=label)

    def snapshot(self, pid, *, scope="build", world="", label="Ручная точка восстановления", status=lambda _x: None):
        with self.lock:
            root = self._writable_root(pid)
            require_idle_files(root)
            sections = self._sections(scope, world)
            if scope == "world" and not (root / sections[0]).is_dir():
                raise ValueError("Мир не найден.")
            profile = self.store.profile(pid)
            with self.store.connection() as db:
                pins = [list(row) for row in db.execute("SELECT kind,name FROM care_pins WHERE pid=?", (pid,))]
                settings = {key: self.store.get(key + ":" + pid, None) for key in PROFILE_SETTINGS}
            files = list(_walk(root, sections))
            total = sum(st[0] for _, _, st in files)
            if total + 32 * 1024**2 > shutil.disk_usage(self.root).free:
                raise ValueError("Недостаточно места для резервной копии. Освободи диск.")
            sid = secrets.token_hex(16)
            directory = self._archive_dir(pid)
            target, temporary = directory / (sid + ".zip"), directory / (sid + ".part")
            manifest = {"format": "BlockHarborRecovery", "version": 1, "id": sid, "profile": profile,
                        "scope": scope, "world": world, "sections": sections, "pins": pins, "settings": settings,
                        "content": self._content_records(pid) if scope == "build" else [], "files": [], "created": time.time()}
            try:
                with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                    for index, (path, name, stamp) in enumerate(files):
                        if index % 20 == 0:
                            status(f"Сохраняем резервную копию: {index + 1} / {len(files)}…")
                        digest = hashlib.sha256()
                        written = 0
                        with path.open("rb") as source, archive.open("payload/" + name, "w", force_zip64=stamp[0] >= 2**31) as output:
                            while chunk := source.read(1024**2):
                                written += len(chunk)
                                if written > stamp[0]:
                                    raise ValueError("Файл изменился во время резервного копирования.")
                                digest.update(chunk)
                                output.write(chunk)
                        if written != stamp[0] or file_stamp(path) != stamp:
                            raise ValueError("Файлы изменились во время копирования. Повтори после закрытия игры.")
                        manifest["files"].append({"path": name, "size": written, "sha256": digest.hexdigest()})
                    raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
                    if len(raw) > MAX_MANIFEST:
                        raise ValueError("Слишком большой список файлов в резервной копии.")
                    archive.writestr("recovery.json", raw)
                os.replace(temporary, target)
                with self.store.connection() as db:
                    db.execute("INSERT INTO care_snapshots VALUES(?,?,?,?,?,?,?,?)",
                               (sid, pid, scope, world, _plain(label, 180), manifest["created"], target.stat().st_size, len(files)))
                self.history(pid, "Копия мира" if scope == "world" else "Точка восстановления", label + (": " + world if world else ""))
                self._prune(sid)
                return sid
            except BaseException:
                temporary.unlink(missing_ok=True)
                with self.store.connection() as db:
                    present = db.execute("SELECT 1 FROM care_snapshots WHERE id=?", (sid,)).fetchone()
                if not present:
                    target.unlink(missing_ok=True)
                raise

    def _snapshot(self, pid, sid):
        if not isinstance(sid, str) or not _ID.fullmatch(sid):
            raise ValueError("Неверная точка восстановления.")
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM care_snapshots WHERE id=? AND pid=?", (sid, pid)).fetchone()
        if row is None:
            raise ValueError("Точка восстановления больше не доступна.")
        path = self._archive_dir(pid) / (sid + ".zip")
        if not path.is_file() or _reparse(path):
            raise ValueError("Файл резервной копии отсутствует или перенаправлен.")
        return path, dict(row)

    def _extract(self, path, directory, pid):
        _archive_limit(path)
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES + 1:
                raise ValueError("В резервной копии слишком много файлов.")
            info = archive.getinfo("recovery.json")
            if info.file_size > MAX_MANIFEST:
                raise ValueError("Повреждено описание резервной копии.")
            manifest = json.loads(archive.read(info))
            if manifest.get("format") != "BlockHarborRecovery" or manifest.get("version") != 1 or manifest.get("profile", {}).get("id") != pid:
                raise ValueError("Резервная копия относится к другой сборке или формату.")
            sections = self._sections(manifest.get("scope"), manifest.get("world", ""))
            if manifest.get("sections") != sections:
                raise ValueError("Повреждён список каталогов резервной копии.")
            pins = manifest.get("pins", [])
            if not isinstance(pins, list) or len(pins) > 4096:
                raise ValueError("Повреждён список закреплённых версий.")
            for pin in pins:
                if not isinstance(pin, list) or len(pin) != 2 or pin[0] not in KINDS:
                    raise ValueError("Повреждён список закреплённых версий.")
                clean_filename(pin[1])
            settings = manifest.get("settings", {})
            if not isinstance(settings, dict) or any(k not in PROFILE_SETTINGS or v is not None and (not isinstance(v, str) or len(v) > 100) for k, v in settings.items()):
                raise ValueError("Повреждены настройки производительности в копии.")
            content = manifest.get("content", [])
            if not isinstance(content, list) or len(content) > MAX_FILES:
                raise ValueError("Повреждены сведения о дополнениях в копии.")
            for row in content:
                if not isinstance(row, dict) or row.get("kind") not in KINDS:
                    raise ValueError("Повреждены сведения о дополнениях в копии.")
                clean_filename(row.get("name"))
            records = manifest.get("files")
            if not isinstance(records, list) or len(records) > MAX_FILES:
                raise ValueError("Повреждён список файлов резервной копии.")
            expected, folded, total = {"recovery.json"}, set(), 0
            for record in records:
                name = str(_safe_relative(record["path"]))
                if not any(name == s or name.startswith(s + "/") for s in sections):
                    raise ValueError("Файл находится вне области восстановления.")
                if name.casefold() in folded:
                    raise ValueError("В резервной копии повторяются имена файлов.")
                folded.add(name.casefold())
                expected.add("payload/" + name)
                size, digest = record.get("size"), record.get("sha256")
                if not isinstance(size, int) or size < 0 or not isinstance(digest, str) or not _HASH.fullmatch(digest):
                    raise ValueError("Повреждена контрольная сумма резервной копии.")
                total += size
                if total > MAX_BYTES:
                    raise ValueError("Резервная копия превышает допустимый размер.")
            if len(expected) != len(infos) or {i.filename for i in infos} != expected:
                raise ValueError("Содержимое резервной копии не совпадает с описанием.")
            if total + 16 * 1024**2 > shutil.disk_usage(directory).free:
                raise ValueError("Недостаточно места для восстановления.")
            for record in records:
                name, size = record["path"], record["size"]
                info = archive.getinfo("payload/" + name)
                if info.file_size != size or info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("Неверный тип или размер файла в резервной копии.")
                dest = directory / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                digest, remaining = hashlib.sha256(), size
                with archive.open(info) as src, dest.open("xb") as out:
                    while remaining:
                        chunk = src.read(min(1024**2, remaining))
                        if not chunk:
                            raise ValueError("Файл в резервной копии обрезан.")
                        out.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if src.read(1) or digest.hexdigest() != record["sha256"]:
                        raise ValueError("Контрольная сумма резервной копии не совпала.")
            return manifest

    def _validate_target(self, root, target):
        relative = _safe_relative(target)
        current = root
        for part in relative.parts:
            current = current / part
            if os.path.lexists(current) and _reparse(current):
                raise ValueError("Восстановление через ссылку запрещено.")
        return current

    def _rollback_transaction(self, work, journal):
        root = self._profile_root(journal["pid"])
        for target in reversed(journal["targets"]):
            name, had = target["name"], target["had"]
            dest = self._validate_target(root, name)
            backup, staged = work / "old" / name, work / "new" / name
            if os.path.lexists(backup):
                _remove(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, dest)
            elif not had and not os.path.lexists(staged):
                _remove(dest)
        old = journal["oldProfile"]
        with self.store.connection() as db:
            db.execute("UPDATE profiles SET version=?,loader=? WHERE id=?", (old["version"], old["loader"], journal["pid"]))
            if "oldPins" in journal:
                db.execute("DELETE FROM care_pins WHERE pid=?", (journal["pid"],))
                db.executemany("INSERT OR IGNORE INTO care_pins VALUES(?,?,?)", [(journal["pid"], k, n) for k, n in journal["oldPins"]])
            if "oldSettings" in journal:
                for key, value in journal["oldSettings"].items():
                    if value is None:
                        db.execute("DELETE FROM settings WHERE key=?", (key + ":" + journal["pid"],))
                    else:
                        db.execute("REPLACE INTO settings VALUES(?,?)", (key + ":" + journal["pid"], value))

    def _recover(self):
        for work in self.root.glob("transaction-*"):
            if not work.is_dir() or _reparse(work):
                continue
            journal_file = work / "journal.json"
            if not journal_file.is_file() or journal_file.stat().st_size > 65536:
                continue
            journal = {}
            try:
                journal = json.loads(journal_file.read_text(encoding="utf-8"))
                if journal.get("phase") != "committed":
                    self._rollback_transaction(work, journal)
                    self.history(journal["pid"], "Восстановление после прерывания", "Незавершённые изменения отменены.")
                shutil.rmtree(work)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                pid = journal.get("pid") if isinstance(journal, dict) else None
                if isinstance(pid, str) and _ID.fullmatch(pid):
                    self.recovery_errors[pid] = str(exc)[:250]
                else:
                    # A malformed journal cannot identify a safe build to resume.
                    for profile in self.store.all():
                        self.recovery_errors[profile["id"]] = "повреждён журнал восстановления"
        for work in self.root.glob("plan-*"):
            if work.is_dir() and not _reparse(work) and _ID.fullmatch(work.name.removeprefix("plan-")):
                shutil.rmtree(work, ignore_errors=True)

    def _transaction(self, pid, prepared, sections, new_profile=None, pins=None, settings=None):
        root = self._writable_root(pid)
        require_idle_files(root)
        work = self.root / ("transaction-" + secrets.token_hex(16))
        work.mkdir()
        journal = {"phase": "prepared", "pid": pid, "oldProfile": self.store.profile(pid),
                   "targets": [{"name": name, "had": os.path.lexists(self._validate_target(root, name))} for name in sections]}
        if pins is not None:
            with self.store.connection() as db:
                journal["oldPins"] = [list(r) for r in db.execute("SELECT kind,name FROM care_pins WHERE pid=?", (pid,))]
        if settings is not None:
            journal["oldSettings"] = {k: self.store.get(k + ":" + pid, None) for k in PROFILE_SETTINGS}
        try:
            os.replace(prepared, work / "new")
            (work / "old").mkdir()
            _atomic_json(work / "journal.json", journal)
            for target in journal["targets"]:
                name = target["name"]
                dest, old, new = root / name, work / "old" / name, work / "new" / name
                old.parent.mkdir(parents=True, exist_ok=True)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if target["had"]:
                    os.replace(dest, old)
                if os.path.lexists(new):
                    os.replace(new, dest)
            if new_profile:
                _, version, loader = input_profile(new_profile["name"], new_profile["version"], new_profile["loader"])
                with self.store.connection() as db:
                    db.execute("UPDATE profiles SET version=?,loader=? WHERE id=?", (version, loader, pid))
            if pins is not None:
                with self.store.connection() as db:
                    db.execute("DELETE FROM care_pins WHERE pid=?", (pid,))
                    db.executemany("INSERT OR IGNORE INTO care_pins VALUES(?,?,?)", [(pid, k, n) for k, n in pins])
            if settings is not None:
                self._apply_settings(pid, settings)
            journal["phase"] = "committed"
            _atomic_json(work / "journal.json", journal)
        except BaseException:
            if (work / "journal.json").exists():
                try:
                    self._rollback_transaction(work, journal)
                except Exception as error:
                    self.recovery_errors[pid] = str(error)[:250]
                    raise ValueError("Восстановление прервано. Журнал и исходные файлы сохранены; проверь доступ к диску и перезапусти BlockHarbor.") from error
            shutil.rmtree(work, ignore_errors=True)
            raise
        shutil.rmtree(work)

    def restore(self, pid, sid, status=lambda _x: None):
        with self.lock:
            require_idle_files(self._writable_root(pid))
            path, row = self._snapshot(pid, sid)
            with tempfile.TemporaryDirectory(dir=self.root, prefix="restore-") as td:
                stage = Path(td) / "new"
                stage.mkdir()
                status("Проверяем контрольные суммы резервной копии…")
                manifest = self._extract(path, stage, pid)
                # The old state remains available as its own point, even after rollback.
                if row["scope"] == "build" or (self._profile_root(pid) / "saves" / row["world"]).exists():
                    self.snapshot(pid, scope=row["scope"], world=row["world"], label="Перед восстановлением", status=status)
                self._transaction(pid, stage, manifest["sections"], manifest["profile"] if row["scope"] == "build" else None,
                                  manifest.get("pins", []) if row["scope"] == "build" else None,
                                  manifest.get("settings", {}) if row["scope"] == "build" else None)
                if row["scope"] == "build":
                    self._inherit_content(pid, manifest.get("content", []))
            self.history(pid, "Восстановлено", row["label"] + (": " + row["world"] if row["world"] else ""))
            return "Мир восстановлен." if row["scope"] == "world" else "Сборка восстановлена. Миры сохранены отдельно и не менялись."

    def clone(self, pid, name, worlds=False, status=lambda _x: None):
        with self.lock:
            source = self._writable_root(pid)
            require_idle_files(source)
            profile = self.store.profile(pid)
            content = self._content_records(pid)
            name, version, loader = input_profile(name, profile["version"], profile["loader"])
            sections = [*BUILD_SECTIONS, *( ["saves"] if worlds else [])]
            status("Копируем сборку" + (" и миры…" if worlds else "…"))
            clone = self.store.create(name, version, loader, prepare=lambda folder: _copy_sections(source, folder, sections))
            with self.store.connection() as db:
                db.execute("INSERT OR IGNORE INTO care_pins SELECT ?,kind,name FROM care_pins WHERE pid=?", (clone, pid))
            self._apply_settings(clone, {k: self.store.get(k + ":" + pid, None) for k in PROFILE_SETTINGS})
            self._inherit_content(clone, content)
            if self.on_clone:
                self.on_clone(pid, clone)
            self.history(clone, "Копия сборки", profile["name"] + (" · с мирами" if worlds else " · без миров"))
            return "Создана сборка «" + name + "»."

    def _metadata(self, path, kind, loader, stamp):
        key = str(path), stamp, kind, loader
        with self.lock:
            if key in self._meta:
                self._meta.move_to_end(key)
                return dict(self._meta[key])
        data = {"title": path.name.removesuffix(".disabled"), "version": "", "description": "", "ids": [], "requires": []}
        try:
            with path.open("rb") as src:
                readable = _small_directory(src)
            if readable:
                with zipfile.ZipFile(path) as archive:
                    if kind == "mods":
                        info = compatibility._metadata(path, stamp, loader)
                        data["ids"] = sorted(info.provided)
                        primary = next(iter(info.direct), next(iter(info.provided), ""))
                        if primary:
                            data["title"], data["version"] = info.provided[primary]
                        data["requires"] = [{"id": key, "versions": list(bounds), "relation": relation} for relation, key, bounds in info.rules if relation in ("required", "recommended", "optional")]
                        for label in ("fabric.mod.json", "quilt.mod.json", "META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
                            meta = read_descriptor(archive, label)
                            if label == "quilt.mod.json":
                                meta = meta.get("quilt_loader", {}).get("metadata", {})
                            if label.endswith(".toml"):
                                meta = next((m for m in meta.get("mods", []) if isinstance(m, dict)), {})
                            if meta.get("description"):
                                data["description"] = _plain(meta["description"])
                                break
                    elif kind == "resourcepacks":
                        meta = read_descriptor(archive, "pack.mcmeta").get("pack", {})
                        data["description"] = _plain(meta.get("description"))
                        if meta.get("pack_format") is not None:
                            data["version"] = "Формат " + str(meta["pack_format"])
        except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError, zipfile.BadZipFile):
            pass
        with self.lock:
            self._meta[key] = dict(data)
            while len(self._meta) > 2048:
                self._meta.popitem(last=False)
        return data

    def remember_project(self, pid, release, metadata=None):
        """Optional post-install hook, also used after an explicit update lookup."""
        path = self.store.content_folder(pid, release.kind) / release.filename
        if not path.is_file() or _reparse(path):
            return
        # Network metadata is already available at an install/check; this hook
        # deliberately never performs a request while rendering the launcher.
        metadata = metadata or {}
        data = {"title": _plain(metadata.get("title") or release.name, 180),
                "version": _plain(release.number, 128), "description": _plain(metadata.get("description")),
                "project": release.project, "projectVersion": release.version}
        with self.store.connection() as db:
            db.execute("REPLACE INTO care_projects VALUES(?,?,?,?,?)", (pid, release.kind, release.filename, json.dumps(file_stamp(path)), json.dumps(data)))

    def inventory(self, pid):
        profile, root = self.store.profile(pid), self._profile_root(pid)
        with self.store.connection() as db:
            pins = {(r[0], r[1]) for r in db.execute("SELECT kind,name FROM care_pins WHERE pid=?", (pid,))}
            catalog = {(r[0], r[1]): (json.loads(r[2]), json.loads(r[3])) for r in db.execute("SELECT kind,name,stamp,metadata FROM care_projects WHERE pid=?", (pid,))}
        results = {kind: [] for kind in KINDS}
        enabled_mods = self.store.content(pid)["mods"]
        managed = skin_sync.managed_files(root, profile["version"], profile["loader"], enabled_mods)
        if e4steam.installed(root, profile["version"], profile["loader"], sizes={r["size"] for r in enabled_mods}):
            managed.update(e4steam.protected_names(profile["version"], profile["loader"]))
        managed.update(room_bridge.managed_names(root, profile["version"], profile["loader"]))
        for kind, ext in KINDS.items():
            folder = self.store.content_folder(pid, kind)
            if _reparse(folder):
                raise ValueError("Папка дополнений перенаправлена.")
            for path in folder.iterdir():
                if not path.name.lower().endswith((ext, ext + ".disabled")) or not path.is_file() or _reparse(path):
                    continue
                stamp = file_stamp(path)
                enabled = not path.name.lower().endswith(".disabled")
                name = path.name if enabled else path.name[:-9]
                meta = self._metadata(path, kind, profile["loader"], stamp)
                known = catalog.get((kind, name))
                # A rename changes ctime; size and mtime still identify our local
                # receipt while enabled/disabled. Full hashes bind update plans.
                if known and tuple(known[0][:2]) == stamp[:2]:
                    meta.update({k: v for k, v in known[1].items() if v})
                results[kind].append({**meta, "name": path.name, "filename": name, "kind": kind, "enabled": enabled, "size": stamp[0],
                                      "stamp": list(stamp), "icon": self.store.icons.icon(path, stamp), "pinned": (kind, name) in pins, "managed": kind == "mods" and name in managed})
            results[kind].sort(key=lambda r: (r["title"].casefold(), r["name"].casefold()))
        return results

    def _fingerprint(self, pid):
        inventory = self.inventory(pid)
        value = {"profile": self.store.profile(pid), "files": [(k, r["name"], r["stamp"], r["pinned"]) for k, rows in inventory.items() for r in rows]}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def details(self, pid, kind, name):
        inventory = self.inventory(pid)
        row = next((r for r in inventory.get(kind, []) if r["name"] == name), None)
        if row is None:
            raise ValueError("Дополнение не найдено.")
        providers = {}
        for other in inventory["mods"]:
            if other["enabled"]:
                for key in other["ids"]:
                    providers.setdefault(key, []).append(other["title"])
        required = [{**dep, "installed": providers.get(dep["id"], []), "platform": dep["id"] in compatibility._PLATFORM or dep["id"] == "minecraft"} for dep in row["requires"]]
        dependents = [{"name": other["name"], "title": other["title"]} for other in inventory["mods"] if other["enabled"] and other["name"] != name and any(d["relation"] == "required" and d["id"] in row["ids"] for d in other["requires"])]
        return {**row, "requires": required, "dependents": dependents}

    def _selection(self, pid, kind, names):
        if kind not in KINDS or not isinstance(names, list) or not 1 <= len(names) <= 1024 or any(not isinstance(v, str) for v in names):
            raise ValueError("Выбери дополнения из списка.")
        chosen = set(names)
        rows = [r for r in self.inventory(pid)[kind] if r["name"] in chosen]
        if len(rows) != len(chosen):
            raise ValueError("Состав сборки изменился. Обнови список дополнений.")
        if any(r["managed"] for r in rows):
            raise ValueError("Встроенные сетевые компоненты обслуживает BlockHarbor.")
        return rows

    def mutate(self, pid, kind, names, mode, force=False):
        with self.lock:
            root = self._writable_root(pid)
            require_idle_files(root)
            rows = self._selection(pid, kind, names)
            content = self._content_records(pid)
            if mode not in ("enable", "disable", "remove"):
                raise ValueError("Неизвестное действие с дополнениями.")
            if mode != "remove":
                rows = [r for r in rows if r["enabled"] != (mode == "enable")]
            if not rows:
                return {"ok": True, "message": "Выбранные файлы уже в этом состоянии."}
            chosen = {r["name"] for r in rows}
            dependents = sorted({d["title"] for r in rows for d in self.details(pid, kind, r["name"])["dependents"] if d["name"] not in chosen}) if mode != "enable" and kind == "mods" else []
            if dependents and not force:
                return {"review": True, "dependents": dependents, "message": "Эти файлы требуются другим модам. Проверь список перед изменением."}
            with tempfile.TemporaryDirectory(dir=self.root, prefix="content-") as td:
                stage = Path(td) / "new"
                stage.mkdir()
                _copy_sections(root, stage, [kind])
                for row in rows:
                    path = stage / kind / row["name"]
                    if mode == "remove":
                        path.unlink()
                    else:
                        target = path.with_name(row["filename"] + (".disabled" if mode == "disable" else ""))
                        if any(p.name.casefold() == target.name.casefold() for p in target.parent.iterdir() if p != path):
                            raise ValueError("В папке уже есть файл «" + target.name + "».")
                        os.rename(path, target)
                if mode == "enable" and kind == "mods":
                    additions = {r["filename"]: stage / kind / r["filename"] for r in rows}
                    existing = {p.name: p for p in (stage / kind).iterdir() if p.name.endswith(".jar") and p.name not in additions}
                    report = compatibility.local_report(self.store.profile(pid), existing, additions)
                    if report["blocked"]:
                        raise ValueError(" ".join(report["conflicts"][:3]))
                    warnings = report["warnings"] + ["Требуется " + d["name"] + " " + d["version"] for d in report["required"] if not d["installed"]]
                    if warnings and not force:
                        return {"review": True, "warnings": warnings, "dependents": [], "message": "Проверь совместимость перед включением."}
                label = {"enable": "Перед включением дополнений", "disable": "Перед отключением дополнений", "remove": "Перед удалением дополнений"}[mode]
                self.before_change(pid, label)
                self._transaction(pid, stage, [kind])
                renamed = {(kind, r["name"]): r["filename"] + (".disabled" if mode == "disable" else "") for r in rows} if mode != "remove" else {}
                self._inherit_content(pid, content, renamed)
            title = {"enable": "Включены дополнения", "disable": "Отключены дополнения", "remove": "Удалены дополнения"}[mode]
            self.history(pid, title, ", ".join(r["title"] for r in rows))
            return {"ok": True, "message": title + ": " + str(len(rows)) + "."}

    def pin(self, pid, kind, names, pinned):
        rows = self._selection(pid, kind, names)
        with self.store.connection() as db:
            for row in rows:
                if pinned:
                    db.execute("INSERT OR IGNORE INTO care_pins VALUES(?,?,?)", (pid, kind, row["filename"]))
                else:
                    db.execute("DELETE FROM care_pins WHERE pid=? AND kind=? AND name=?", (pid, kind, row["filename"]))
        return {"ok": True, "message": "Версии закреплены." if pinned else "Закрепление снято."}

    def worlds(self, pid):
        root = self._profile_root(pid)
        saves = root / "saves"
        if not saves.is_dir() or _reparse(saves):
            return {"worlds": []}
        rows = []
        for folder in saves.iterdir():
            if not folder.is_dir() or _reparse(folder) or not (folder / "level.dat").is_file():
                continue
            files = list(_walk(root, ["saves/" + folder.name]))
            level = folder / "level.dat"
            rows.append({"name": folder.name, "size": sum(st[0] for _, _, st in files), "lastPlayed": level.stat().st_mtime,
                         "files": len(files)})
        rows.sort(key=lambda r: r["lastPlayed"], reverse=True)
        return {"worlds": rows}

    def backup_after_game(self, pid, status=lambda _x: None):
        """Called only after the tracked game has exited, under the app mutation lock."""
        if self.store.get("careAutoWorldBackup:" + pid) != "1":
            return ""
        count = 0
        with self.lock:
            require_idle_files(self._writable_root(pid))
            for world in self.worlds(pid)["worlds"]:
                key = "careWorldSaved:" + pid + ":" + hashlib.sha256(world["name"].encode()).hexdigest()
                stamp = json.dumps([world["lastPlayed"], world["size"]])
                with self.store.connection() as db:
                    existing_copy = db.execute("SELECT 1 FROM care_snapshots WHERE pid=? AND scope='world' AND world=? LIMIT 1", (pid, world["name"])).fetchone()
                if existing_copy and self.store.get(key) == stamp:
                    continue
                self.snapshot(pid, scope="world", world=world["name"], label="После выхода из игры", status=status)
                self.store.set(key, stamp)
                count += 1
        return "После игры сохранено миров: " + str(count) + "." if count else "Миры не изменились после последней копии."

    def state(self, pid):
        self.store.profile(pid)
        with self.store.connection() as db:
            snapshots = [dict(r) for r in db.execute("SELECT * FROM care_snapshots WHERE pid=? ORDER BY created DESC", (pid,))]
            history = [dict(r) for r in db.execute("SELECT created,action,detail FROM care_history WHERE pid=? ORDER BY id DESC LIMIT 80", (pid,))]
            used = db.execute("SELECT COALESCE(SUM(size),0) FROM care_snapshots").fetchone()[0]
        plan = self._plans.get(pid)
        return {"contents": self.inventory(pid), "snapshots": snapshots, "history": history, "retention": self.retention(), "recoveryBytes": used,
                "updatePlan": plan["public"] if plan else None, "recoveryError": self.recovery_errors.get(pid, ""),
                "autoWorldBackup": self.store.get("careAutoWorldBackup:" + pid) == "1"}

    def _recognized(self, pid, rows):
        """Network calls only happen on an explicit update check, never on UI polling."""
        by_hash, total = {}, 0
        root = self._profile_root(pid)
        for row in rows:
            total += row["size"]
            if len(by_hash) >= 1024 or total > 4 * 1024**3:
                raise ValueError("Проверяй обновления частями: до 1024 файлов и 4 ГБ за раз.")
            path = root / row["kind"] / row["name"]
            digest = file_hash(path, file_stamp(path), "sha512")
            by_hash[digest] = row
        found = {}
        hashes = list(by_hash)
        for offset in range(0, len(hashes), 32):
            found.update(modrinth._versions_by_hash(tuple(hashes[offset:offset + 32]), int(time.monotonic() // 300)))
        recognized = []
        for digest, row in by_hash.items():
            version = found.get(digest)
            if isinstance(version, dict) and any(f.get("hashes", {}).get("sha512") == digest for f in version.get("files", [])):
                recognized.append((row, version, digest))
        return recognized

    def check_updates(self, pid, kind=None, names=None, status=lambda _x: None):
        profile = self.store.profile(pid)
        fingerprint = self._fingerprint(pid)
        inventory = self.inventory(pid)
        all_rows = [r for rows in inventory.values() for r in rows if r["enabled"]]
        status("Определяем установленные версии по контрольным суммам…")
        recognized = self._recognized(pid, all_rows)
        installed = {v["project_id"]: (r, v, digest) for r, v, digest in recognized}
        proposed = {}
        unrecognized = len(all_rows) - len(recognized)
        skipped = sum(r["pinned"] or r["managed"] for r in all_rows)
        selected = set(names) if isinstance(names, list) else None
        for row, old, digest in recognized:
            if row["pinned"] or row["managed"] or (kind and row["kind"] != kind) or (selected is not None and row["name"] not in selected):
                continue
            status("Ищем совместимый выпуск: " + row["title"] + "…")
            releases = modrinth._releases(profile, row["kind"], old["project_id"])
            newest = next((r for r in releases if r.channel == "release"), None)
            if newest and newest.sha512 != digest and newest.published > old.get("date_published", ""):
                proposed[newest.project] = newest
        # Resolve requirements as one graph so two chosen updates cannot silently
        # select different releases of the same dependency.
        queue = deque(proposed.values())
        constraints = {p: r.version for p, r in proposed.items()}
        visited = set()
        while queue:
            release = queue.popleft()
            if release.version in visited:
                continue
            visited.add(release.version)
            if len(visited) > 128:
                raise ValueError("Слишком большая цепочка зависимостей. Обновляй сборку частями.")
            for dep in release.deps:
                if dep.get("dependency_type") != "required":
                    continue
                project, exact = dep.get("project_id"), dep.get("version_id")
                exact_data = modrinth._get("/version/" + modrinth._id(exact)) if exact else None
                project = project or (exact_data or {}).get("project_id")
                if not project:
                    if dep.get("file_name") not in {r["name"] for r in all_rows}:
                        raise ValueError("Обновление требует ручной установки: " + str(dep.get("file_name") or "внешняя зависимость"))
                    continue
                modrinth._id(project)
                if exact and project in constraints and constraints[project] != exact:
                    raise ValueError("Выбранные обновления требуют разные версии одной зависимости. Закрепи одну из версий и повтори проверку.")
                if exact:
                    constraints[project] = exact
                current = installed.get(project)
                chosen = proposed.get(project)
                if chosen and (not exact or chosen.version == exact):
                    queue.append(chosen)
                    continue
                if current and (not exact or current[1]["id"] == exact):
                    continue
                if current and (current[0]["pinned"] or current[0]["managed"]):
                    raise ValueError("Для обновления нужна другая версия закреплённого или встроенного компонента: " + current[0]["title"])
                category = modrinth._project_kind(project)
                if exact_data:
                    chosen = modrinth._package(exact_data, profile, category, project)
                else:
                    chosen = next((r for r in modrinth._releases(profile, category, project) if r.channel == "release"), None)
                if chosen is None:
                    raise ValueError("Нет совместимой обязательной зависимости: " + project)
                proposed[project] = chosen
                queue.append(chosen)
        token = secrets.token_hex(16)
        directory = self.root / ("plan-" + token)
        directory.mkdir()
        try:
            remove = {}
            for project, release in proposed.items():
                current = installed.get(project)
                if current and current[2] != release.sha512:
                    remove[(current[0]["kind"], current[0]["name"])] = current[0]
            staged = {}
            for release in proposed.values():
                current = installed.get(release.project)
                if current and current[2] == release.sha512:
                    continue
                target = directory / release.version
                status("Проверяем файл обновления: " + release.filename + "…")
                if self.transfers:
                    self.transfers.download(release.url, target, title=release.filename,
                                            hashes={"sha512": release.sha512}, size=release.size,
                                            hosts=modrinth._CDN_HOSTS, category="content", status=status)
                else:
                    modrinth._download(release, target)
                verify_pack(target, release.kind, profile["loader"])
                staged[release.version] = target
            remaining = {self._profile_root(pid) / r["kind"] / r["name"]: tuple(r["stamp"]) for r in all_rows if (r["kind"], r["name"]) not in remove}
            catalog = modrinth._catalog_report(list(proposed.values()), remaining) if proposed else compatibility.report()
            additions = {r.filename: staged[r.version] for r in proposed.values() if r.kind == "mods" and r.version in staged}
            existing = {p.name: p for p in remaining if p.parent.name == "mods"}
            local = compatibility.local_report(profile, existing, additions) if additions else compatibility.report()
            warnings = sorted(set(catalog["warnings"] + modrinth._staged_warnings(local, catalog)))
            conflicts = sorted(set(catalog["conflicts"] + local["conflicts"]))
            planned_names = set()
            for release in proposed.values():
                destination = release.kind, release.filename.casefold()
                if destination in planned_names:
                    conflicts.append("Несколько проектов используют одно имя файла: " + release.filename)
                planned_names.add(destination)
                path = self._profile_root(pid) / release.kind / release.filename
                if path.exists() and (release.kind, release.filename) not in remove and file_hash(path, file_stamp(path), "sha512") != release.sha512:
                    conflicts.append("В сборке уже есть другой файл с именем " + release.filename)
            if self._fingerprint(pid) != fingerprint:
                raise ValueError("Сборка изменилась во время проверки. Повтори поиск обновлений.")
            entries = []
            projects = {}
            for release in proposed.values():
                if release.version not in staged:
                    continue
                old = installed.get(release.project)
                project = modrinth._get("/project/" + release.project)
                projects[release.project] = project
                entries.append({"title": project.get("title") or release.name, "from": old[1].get("version_number", "") if old else "", "to": release.number,
                                "kind": release.kind, "filename": release.filename, "size": release.size, "dependency": not old,
                                "changelog": _plain(modrinth._get("/version/" + release.version).get("changelog", ""), 12000)})
            public = {"token": token, "created": time.time(), "entries": entries, "warnings": warnings, "conflicts": conflicts, "blocked": bool(conflicts),
                      "unrecognized": unrecognized, "skipped": skipped, "size": sum(r["size"] for r in entries), "fingerprint": fingerprint}
            previous = self._plans.get(pid)
            self._plans[pid] = {"public": public, "directory": directory, "releases": list(proposed.values()), "staged": staged, "remove": list(remove), "fingerprint": fingerprint, "projects": projects}
            if previous:
                shutil.rmtree(previous["directory"], ignore_errors=True)
            return "План обновления готов: " + str(len(entries)) + " файлов. Проверь изменения перед применением." if entries else "Совместимых стабильных обновлений не найдено."
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def apply_updates(self, pid, token, status=lambda _x: None):
        with self.lock:
            plan = self._plans.get(pid)
            if not plan or not isinstance(token, str) or not secrets.compare_digest(token, plan["public"]["token"]):
                raise ValueError("План обновления устарел. Выполни проверку заново.")
            if plan["public"]["blocked"]:
                raise ValueError("В плане есть несовместимые файлы. Сначала устрани конфликт.")
            if self._fingerprint(pid) != plan["fingerprint"]:
                raise ValueError("Файлы или закреплённые версии изменились. Проверь обновления ещё раз.")
            if not plan["public"]["entries"]:
                return "Новых файлов нет."
            root = self._profile_root(pid)
            require_idle_files(root)
            content = self._content_records(pid)
            sections = sorted({r.kind for r in plan["releases"]})
            with tempfile.TemporaryDirectory(dir=self.root, prefix="updates-") as td:
                prepared = Path(td) / "new"
                prepared.mkdir()
                _copy_sections(root, prepared, sections)
                for kind, name in plan["remove"]:
                    (prepared / kind / name).unlink()
                for release in plan["releases"]:
                    source = plan["staged"].get(release.version)
                    if source:
                        if file_hash(source, file_stamp(source), "sha512") != release.sha512:
                            raise ValueError("Кэш обновления повреждён. Повтори проверку.")
                        target = prepared / release.kind / release.filename
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
                self.snapshot(pid, label="Перед обновлением дополнений", status=status)
                status("Применяем проверенный план обновления…")
                self._transaction(pid, prepared, sections)
                self._inherit_content(pid, content)
            self.history(pid, "Обновлены дополнения", "; ".join(e["title"] + " → " + e["to"] for e in plan["public"]["entries"]))
            for release in plan["releases"]:
                self.remember_project(pid, release, plan["projects"].get(release.project))
            modrinth._save_project_icons(self.store, pid, plan["releases"], status)
            count = len(plan["public"]["entries"])
            shutil.rmtree(plan["directory"], ignore_errors=True)
            self._plans.pop(pid, None)
            return "Обновлено файлов: " + str(count) + ". Точка восстановления сохранена."

    def handle(self, action, p, status=lambda _x: None):
        if action not in self.ACTIONS:
            raise ValueError("Неизвестная операция управления сборкой.")
        pid = p.get("id", "")
        if action == "careState":
            return self.state(pid)
        if action == "careHistory":
            data = self.state(pid)
            return {"snapshots": data["snapshots"], "history": data["history"], "retention": data["retention"], "recoveryBytes": data["recoveryBytes"]}
        if action == "careDetails":
            return self.details(pid, p.get("kind"), p.get("name"))
        if action == "careWorlds":
            return self.worlds(pid)
        if action == "careSnapshot":
            self.snapshot(pid, label=_plain(p.get("label") or "Ручная точка восстановления", 180), status=status)
            return "Точка восстановления создана."
        if action == "careWorldBackup":
            self.snapshot(pid, scope="world", world=p.get("world", ""), label="Ручная копия мира", status=status)
            return "Копия мира сохранена."
        if action == "careBackupWorlds":
            require_idle_files(self._writable_root(pid))
            worlds = self.worlds(pid)["worlds"]
            for world in worlds:
                self.snapshot(pid, scope="world", world=world["name"], label="Ручная копия всех миров", status=status)
            return "Сохранено миров: " + str(len(worlds)) + "."
        if action == "careAutoBackup":
            self.store.profile(pid)
            self.store.set("careAutoWorldBackup:" + pid, "1" if p.get("enabled") is True else "0")
            return {"ok": True, "message": "Копии после выхода из игры включены." if p.get("enabled") is True else "Копии после выхода из игры отключены."}
        if action == "careRestore":
            return self.restore(pid, p.get("snapshot"), status)
        if action == "careClone":
            return self.clone(pid, p.get("name"), p.get("worlds") is True, status)
        if action == "careToggle":
            return self.mutate(pid, p.get("kind"), p.get("names"), "enable" if p.get("enabled") is True else "disable", p.get("force") is True)
        if action == "careRemove":
            return self.mutate(pid, p.get("kind"), p.get("names"), "remove", p.get("force") is True)
        if action == "carePin":
            return self.pin(pid, p.get("kind"), p.get("names"), p.get("pinned") is True)
        if action == "careRetention":
            count, budget = int(p.get("count", 10)), int(p.get("budgetMiB", 5120))
            if not 1 <= count <= 100 or not 128 <= budget <= 102400:
                raise ValueError("Укажи от 1 до 100 копий и от 128 до 102400 МБ.")
            self.store.set_many({"careRetentionCount": str(count), "careBudgetMiB": str(budget)})
            self._prune()
            return {"ok": True, "message": "Ограничения хранения сохранены."}
        if action == "careCheckUpdates":
            return self.check_updates(pid, p.get("kind"), p.get("names"), status)
        if action == "careApplyUpdates":
            return self.apply_updates(pid, p.get("token"), status)

