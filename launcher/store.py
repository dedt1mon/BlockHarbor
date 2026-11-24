"""Local instance metadata and the mod / pack filesystem."""

import json
import os
import re
import shutil
import sqlite3
import uuid
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path

from . import lifecycle, version_ids
from .content_icons import IconCache
from .modfiles import DESCRIPTORS, file_stamp, read_archive_entry

LOADERS = ("fabric", "vanilla", "forge", "neoforge", "quilt")
KINDS = {"mods": ".jar", "shaderpacks": ".zip", "resourcepacks": ".zip"}
MAX_ADDON = 512 * 1024 * 1024
# Snapshot of all published release IDs in Mojang's v2 manifest, newest first.
FALLBACK_VERSIONS = json.loads(
    Path(__file__).with_name("minecraft_releases.json").read_text(encoding="utf-8")
)
BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
DEV = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)


def clean_filename(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 180
        or value.startswith(".")
        or value.endswith((".", " "))
        or BAD_NAME.search(value)
        or DEV.search(value)
    ):
        raise ValueError("Недопустимое имя файла.")
    return value


def input_profile(name, version, loader):
    if (
        not isinstance(name, str)
        or not 1 <= len(name.strip()) <= 48
        or any(ord(c) < 32 for c in name)
    ):
        raise ValueError("Название сборки должно содержать от 1 до 48 символов.")
    if not version_ids.valid(version):
        raise ValueError("Укажи номер версии Minecraft, например 1.20.1.")
    if loader not in LOADERS:
        raise ValueError("Выбери загрузчик из списка.")
    return name.strip(), version, loader


class PackLoaderMismatch(ValueError):
    """A readable JAR has no recognized marker for the selected loader."""


def _manifest_fields(archive):
    raw = read_archive_entry(archive, "META-INF/MANIFEST.MF", 65536)
    if raw is None:
        return {}
    # Only the main manifest section counts. Named entries must not be able
    # to advertise a false loader type; continuation lines are folded.
    main = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").partition(b"\n\n")[0]
    result = {}
    for line in main.replace(b"\n ", b"").split(b"\n"):
        key, separator, value = line.partition(b":")
        if separator:
            result.setdefault(key.lower(), []).append(value.strip())
    return {key: values[0] for key, values in result.items() if len(values) == 1}


def verify_pack(path, kind, loader):
    if kind not in KINDS:
        raise ValueError("Неизвестная категория дополнений.")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if len(names) > 180_000:
                raise ValueError("Слишком много файлов в архиве.")
            if kind == "mods":
                if loader == "vanilla":
                    raise ValueError(
                        "Для модов создай сборку с загрузчиком, например Fabric."
                    )
                markers = {
                    "fabric": ("fabric.mod.json",),
                    "quilt": ("quilt.mod.json", "fabric.mod.json"),
                    "forge": ("META-INF/mods.toml", "mcmod.info"),
                    "neoforge": ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"),
                }
                supported = any(m in names for m in markers[loader])
                if not supported and loader in ("forge", "neoforge"):
                    # Modern FML also loads explicitly declared libraries and
                    # language providers without mods.toml. Do not let a
                    # manifest override an incompatible loader descriptor.
                    if not names.intersection(DESCRIPTORS):
                        fields = _manifest_fields(archive)
                        library = fields.get(b"fmlmodtype", b"").upper()
                        supported = library in (b"LIBRARY", b"GAMELIBRARY", b"LANGPROVIDER")
                        if loader == "forge":
                            supported = supported or bool(fields.get(b"fmlcoreplugin"))
                if not supported:
                    detected = []
                    for label, known in (
                        ("Fabric", ("fabric.mod.json",)),
                        ("Quilt", ("quilt.mod.json",)),
                        ("Forge / ранний NeoForge", ("META-INF/mods.toml", "mcmod.info")),
                        ("NeoForge", ("META-INF/neoforge.mods.toml",)),
                    ):
                        if names.intersection(known):
                            detected.append(label)
                    detail = " Найдены метаданные: " + ", ".join(detected) + "." if detected else ""
                    raise PackLoaderMismatch(
                        "В JAR не найдены метаданные для загрузчика этой сборки (" + loader + ")." + detail
                    )
            elif kind == "resourcepacks" and "pack.mcmeta" not in names:
                raise ValueError(
                    "В корне ZIP ресурс-пака должен находиться pack.mcmeta."
                )
            elif kind == "shaderpacks" and not any(
                n.startswith("shaders/") and not n.endswith("/") for n in names
            ):
                raise ValueError(
                    "В корне ZIP шейдерпака нужна папка shaders с файлами."
                )
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("Файл не удалось прочитать как ZIP/JAR.") from exc


def require_idle_files(folder):
    """Prism can delegate a launch to another process. Check the game's own open files too."""
    locks = (folder / "saves").glob("*/session.lock")
    if os.name == "nt":
        import _winapi

        # A live Minecraft process also keeps latest.log open on the title screen.
        files = [folder / "logs" / "latest.log", *locks]
    else:
        import fcntl

        files = locks
    for path in files:
        if not path.is_file() or not path.resolve().is_relative_to(folder.resolve()):
            continue
        try:
            if os.name == "nt":
                handle = _winapi.CreateFile(str(path), 0x80000000, 0, 0, 3, 0, 0)
                _winapi.CloseHandle(handle)
            else:
                with path.open("r+b") as handle:
                    fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(
                "Файлы этой сборки заняты или недоступны. Закрой её в Minecraft и повтори."
            ) from exc


def _check_purge_error(function, path, exc_info):
    # Missing paths are already in the requested state, including simultaneous retries.
    error = exc_info[1]
    if not isinstance(error, FileNotFoundError):
        raise error


class Store:
    def __init__(self, root, *, recover=True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.icons = IconCache(self.root)
        self.instances = self.root / "instances"
        self.instances.mkdir(exist_ok=True)
        self.deleted_instances = self.root / "deleted-instances"
        self.db = self.root / "settings.sqlite3"
        with self.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL, loader TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS deleted_profiles(id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL, loader TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS build_errors(id INTEGER PRIMARY KEY, pid TEXT NOT NULL, created REAL NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL, advice TEXT NOT NULL, stage TEXT NOT NULL, evidence TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS build_errors_by_profile ON build_errors(pid,id);
            CREATE TABLE IF NOT EXISTS build_purges(id TEXT PRIMARY KEY, completed INTEGER NOT NULL DEFAULT 0);
            """)
            db.execute("BEGIN IMMEDIATE")
            if recover:
                self._recover_moves(db)
            # Older portable releases check this marker on startup. Retain it so
            # reopening the same data with one of them keeps an empty list empty.
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('steamStarterAdded', '1')"
            )

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.db, timeout=15)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def _create(self, db, name, version, loader, *, pid=None):
        pid = pid or uuid.uuid4().hex
        folder = self.instances / pid
        try:
            for kind in KINDS:
                (folder / kind).mkdir(parents=True, exist_ok=True)
            db.execute(
                "INSERT INTO profiles VALUES (?, ?, ?, ?)", (pid, name, version, loader)
            )
            db.execute("REPLACE INTO settings VALUES ('active', ?)", (pid,))
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return pid

    def create(self, name, version, loader, *, prepare=None):
        params = input_profile(name, version, loader)
        if prepare is None:
            with lifecycle.guard(), self.connection() as db:
                return self._create(db, *params)

        # Download before publishing this profile or acquiring a database write lock.
        pid = uuid.uuid4().hex
        folder = self.instances / pid
        folder.mkdir()
        try:
            lifecycle.check()
            prepare(folder)
            with lifecycle.guard(), self.connection() as db:
                return self._create(db, *params, pid=pid)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def _profile_directory(self, parent, pid):
        if not isinstance(pid, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", pid):
            raise ValueError("Неверный идентификатор сборки.")
        path = parent / pid
        if (
            path.is_symlink()
            or path.resolve() != self.root.resolve() / parent.name / pid
        ):
            raise ValueError(
                "Папка сборки перенаправлена. Проверь расположение её файлов."
            )
        return path

    def _recover_moves(self, db):
        # A process may have stopped between a directory rename and the SQLite commit.
        # The committed row tells us where the directory belongs; never overwrite a copy.
        if not self.deleted_instances.is_dir():
            return
        for table, expected, other in (
            ("profiles", self.instances, self.deleted_instances),
            ("deleted_profiles", self.deleted_instances, self.instances),
        ):
            for (pid,) in db.execute(
                f"SELECT id FROM {table} WHERE id NOT IN (SELECT id FROM build_purges)"
            ):
                if (expected / pid).exists() or not (other / pid).exists():
                    continue
                source = self._profile_directory(other, pid)
                target = self._profile_directory(expected, pid)
                source.rename(target)

    def move_profile(self, pid, *, restore=False):
        if restore:
            origin, destination = self.deleted_instances, self.instances
            old_table, new_table = "deleted_profiles", "profiles"
        else:
            origin, destination = self.instances, self.deleted_instances
            old_table, new_table = "profiles", "deleted_profiles"
        source = self._profile_directory(origin, pid)
        target = self._profile_directory(destination, pid)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(f"SELECT * FROM {old_table} WHERE id=?", (pid,)).fetchone()
            if row is None:
                if db.execute(
                    f"SELECT 1 FROM {new_table} WHERE id=?", (pid,)
                ).fetchone():
                    return  # The client may be retrying an already committed request.
                raise ValueError("Сборка не найдена.")
            if (
                restore
                and db.execute(
                    "SELECT 1 FROM build_purges WHERE id=?", (pid,)
                ).fetchone()
            ):
                raise ValueError(
                    "Безвозвратное удаление уже началось. Заверши его в списке удалённых сборок."
                )
            if target.exists() or target.is_symlink():
                raise ValueError("В папке назначения уже есть файлы этой сборки.")
            if restore and not source.is_dir():
                raise ValueError("Не найдена папка с файлами удалённой сборки.")
            if not restore:
                require_idle_files(source)
                source.mkdir(exist_ok=True)
            destination.mkdir(exist_ok=True)
            db.execute(
                f"INSERT INTO {new_table} (id,name,version,loader) VALUES (?,?,?,?)",
                tuple(row),
            )
            db.execute(f"DELETE FROM {old_table} WHERE id=?", (pid,))
            # Preserve the current choice, including when another build is restored.
            if not db.execute(
                "SELECT 1 FROM profiles WHERE id=(SELECT value FROM settings WHERE key='active')"
            ).fetchone():
                replacement = db.execute(
                    "SELECT id FROM profiles ORDER BY rowid LIMIT 1"
                ).fetchone()
                db.execute(
                    "REPLACE INTO settings VALUES ('active', ?)",
                    (replacement[0] if replacement else "",),
                )
            try:
                source.rename(target)
            except OSError as exc:
                raise ValueError(
                    "Не удалось переместить папку сборки. Закрой приложения, использующие её файлы, и проверь права доступа."
                ) from exc
            try:
                db.commit()
            except BaseException:
                target.rename(source)
                raise

    def deleted(self):
        with self.connection() as db:
            rows = db.execute(
                """SELECT d.*, p.id IS NOT NULL AS purging FROM deleted_profiles d
                LEFT JOIN build_purges p ON p.id=d.id ORDER BY d.rowid DESC"""
            )
            return [{**row, "purging": bool(row["purging"])} for row in rows]

    def purge_profile(self, pid):
        folder = self._profile_directory(self.deleted_instances, pid)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            archived = db.execute(
                "SELECT 1 FROM deleted_profiles WHERE id=?", (pid,)
            ).fetchone()
            if not archived:
                if db.execute(
                    "SELECT 1 FROM build_purges WHERE id=? AND completed=1", (pid,)
                ).fetchone():
                    return  # A successful purge can be acknowledged again after a lost response.
                raise ValueError(
                    "Безвозвратно удалить можно только сборку из списка удалённых."
                )
            require_idle_files(folder)
            # Commit intent before unlinking anything. If the process is interrupted,
            # the next launch offers a retry and never restores half of a world.
            db.execute("INSERT OR IGNORE INTO build_purges(id) VALUES (?)", (pid,))

        try:
            # The standard implementation unlinks internal symlinks; on supported
            # Python versions it also removes Windows junctions without traversing them.
            shutil.rmtree(
                self._profile_directory(self.deleted_instances, pid),
                onerror=_check_purge_error,
            )
        except OSError as exc:
            raise ValueError(
                "Часть файлов не удалось удалить. Закрой приложения, использующие сборку, проверь права и повтори удаление."
            ) from exc
        with self.connection() as db:
            db.execute("DELETE FROM build_errors WHERE pid=?", (pid,))
            db.execute("DELETE FROM deleted_profiles WHERE id=?", (pid,))
            db.execute("UPDATE build_purges SET completed=1 WHERE id=?", (pid,))

    def enable_fabric(self, pid):
        with self.connection() as db:
            row = db.execute(
                "UPDATE profiles SET loader='fabric' WHERE id=? AND loader='vanilla'",
                (pid,),
            )
            if row.rowcount != 1:
                raise ValueError("Тип сборки изменился. Открой вкладку заново.")

    def profile(self, pid):
        with self.connection() as db:
            row = db.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
            if not row:
                raise ValueError("Сборка не найдена.")
            return dict(row)

    def folder(self, pid):
        self.profile(pid)
        folder = self.instances / pid
        if folder.resolve().parent != self.instances.resolve():
            raise ValueError("Каталог сборки перемещён или является внешней ссылкой.")
        return folder

    def all(self):
        with self.connection() as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM profiles ORDER BY rowid")
            ]

    def get(self, key, default=""):
        with self.connection() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def settings(self):
        with self.connection() as db:
            return dict(db.execute("SELECT key, value FROM settings"))

    def set(self, key, value):
        self.set_many({key: value})

    def set_many(self, values):
        with self.connection() as db:
            db.executemany("REPLACE INTO settings VALUES (?, ?)", values.items())

    def content_folder(self, pid, kind):
        if kind not in KINDS:
            raise ValueError("Неизвестная категория дополнений.")
        root = self.folder(pid)
        dest = root / kind
        if dest.resolve().parent != root.resolve():
            raise ValueError("Папка дополнений находится вне сборки.")
        dest.mkdir(exist_ok=True)
        return dest

    def content(self, pid):
        result = {}
        for kind, ext in KINDS.items():
            folder = self.content_folder(pid, kind)
            rows = []
            with os.scandir(folder) as entries:
                for entry in entries:
                    if not entry.name.lower().endswith(ext):
                        continue
                    try:
                        if entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            rows.append(
                                {
                                    "name": entry.name,
                                    "size": stat.st_size,
                                    "icon": self.icons.icon(
                                        folder / entry.name,
                                        file_stamp(folder / entry.name),
                                    ),
                                }
                            )
                    except FileNotFoundError:
                        continue  # Minecraft or a manual file operation removed it.
            rows.sort(key=lambda item: item["name"].casefold())
            result[kind] = rows
        return result

    def import_file(self, pid, kind, name, incoming):
        lifecycle.check()
        filename = clean_filename(name)
        dest = self.content_folder(pid, kind) / filename
        if dest.suffix.lower() != KINDS[kind]:
            raise ValueError("Для этой категории нужен файл " + KINDS[kind])
        verify_pack(incoming, kind, self.profile(pid)["loader"])
        # All content mutations in the UI are serialized. Never overwrite an existing file.
        try:
            with dest.open("xb") as out:
                try:
                    with open(incoming, "rb") as src:
                        while chunk := src.read(1024**2):
                            lifecycle.check()
                            out.write(chunk)
                        lifecycle.check()
                except BaseException:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise
        except FileExistsError as exc:
            raise ValueError("Файл с таким именем уже есть в сборке.") from exc

    def remove(self, pid, kind, name):
        target = self.content_folder(pid, kind) / clean_filename(name)
        if (
            target.is_symlink()
            or target.suffix.lower() != KINDS[kind]
            or not target.is_file()
        ):
            raise ValueError("Файл дополнения не найден.")
        with lifecycle.guard():
            target.unlink()
