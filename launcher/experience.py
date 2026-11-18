"""Personalization, local media, notification history and UI preferences."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import stat
import subprocess
import time
from pathlib import Path

from PIL import Image, ImageOps

from . import skins
from .diagnostics import redact
from .store import clean_filename, input_profile

COLORS = ("green", "blue", "purple", "orange", "rose")
CACHE_BUDGET = 256 * 1024 * 1024
SCREENSHOT_LIMIT = 120
SCREENSHOT_MAX_BYTES = 32 * 1024 * 1024
SCREENSHOT_TRASH = ".blockharbor-trash"


class Experience:
    READ_ACTIONS = {"experienceState", "experienceGallery", "experienceNotifications"}
    ACTIONS = READ_ACTIONS | {"experienceSaveBuild", "experienceFavorite", "experiencePreferences", "experienceSaveSkin", "experienceUseSkin", "experienceDeleteSkin", "experienceClearNotifications", "experienceShortcut", "experienceDeleteScreenshots", "experienceRestoreScreenshots"}
    JOBS = set()

    def __init__(self, store):
        self.store = store
        self.media_root = store.root / "media-cache"
        if self.media_root.resolve() != store.root.resolve() / "media-cache":
            raise ValueError("Папка изображений перенаправлена за пределы данных лаунчера.")
        self.media_root.mkdir(exist_ok=True)
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS build_details(profile TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '', group_name TEXT NOT NULL DEFAULT '', favorite INTEGER NOT NULL DEFAULT 0, color TEXT NOT NULL DEFAULT 'green', cover TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS wardrobe(id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT NOT NULL, model TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, created REAL NOT NULL, message TEXT NOT NULL, level TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS gallery_cache(key TEXT PRIMARY KEY, image TEXT NOT NULL, checksum TEXT NOT NULL, created REAL NOT NULL);
            """)

    def metadata(self):
        with self.store.connection() as db:
            return {row["profile"]: dict(row) for row in db.execute("SELECT * FROM build_details")}

    def preferences(self):
        settings = self.store.settings()
        scale = settings.get("uiScale", "100")
        return {"scale": int(scale) if scale in ("85", "90", "100", "110", "120") else 100, "reducedMotion": settings.get("reducedMotion") == "1", "checkUpdatesOnStart": settings.get("checkUpdatesOnStart", "1") == "1"}

    def state(self, pid=None):
        with self.store.connection() as db:
            wardrobe = [dict(row) for row in db.execute("SELECT * FROM wardrobe ORDER BY created DESC LIMIT 80")]
        return {"details": self.metadata(), "preferences": self.preferences(), "wardrobe": wardrobe}

    def notify(self, message, level="info"):
        message = redact(str(message), self.store.root)[:1000]
        if not message:
            return
        with self.store.connection() as db:
            last = db.execute("SELECT message,created FROM notifications ORDER BY id DESC LIMIT 1").fetchone()
            if last and last["message"] == message and time.time() - last["created"] < 4:
                return
            db.execute("INSERT INTO notifications(created,message,level) VALUES(?,?,?)", (time.time(), message, level if level in ("info", "success", "error") else "info"))
            db.execute("DELETE FROM notifications WHERE id NOT IN (SELECT id FROM notifications ORDER BY id DESC LIMIT 200)")

    def media(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Изображение не найдено.")
        path = self.media_root / (digest + ".png")
        if path.is_symlink() or not path.resolve().is_relative_to(self.media_root.resolve()):
            raise ValueError("Изображение недоступно.")
        return path

    def _image(self, source, *, size=(960, 540), crop=False):
        try:
            with Image.open(source) as image:
                if image.width * image.height > 32_000_000:
                    raise ValueError("Изображение слишком большое: максимум 32 мегапикселя.")
                image = ImageOps.exif_transpose(image).convert("RGB")
                if crop:
                    image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
                else:
                    image.thumbnail(size, Image.Resampling.LANCZOS)
                out = io.BytesIO()
                image.save(out, "PNG", optimize=True)
        except (OSError, Image.DecompressionBombError) as exc:
            raise ValueError("Выбери читаемое изображение PNG, JPEG или WebP.") from exc
        raw = out.getvalue()
        digest = hashlib.sha256(raw).hexdigest()
        target = self.media(digest)
        if not target.exists():
            temp = self.media_root / (secrets.token_hex(12) + ".tmp")
            try:
                temp.write_bytes(raw)
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
        return digest

    def set_cover(self, pid, raw):
        self.store.profile(pid)
        if len(raw) > 12 * 1024 * 1024:
            raise ValueError("Обложка должна быть меньше 12 МБ.")
        digest = self._image(io.BytesIO(raw), crop=True)
        with self.store.connection() as db:
            db.execute("INSERT OR IGNORE INTO build_details(profile) VALUES(?)", (pid,))
            db.execute("UPDATE build_details SET cover=? WHERE profile=?", (digest, pid))
        self._prune_media()
        return {"ok": True, "cover": digest}

    def _prune_media(self, protected=()):
        # Original screenshots and saved covers/wardrobe remain user content.
        # Only expendable generated previews are evicted; missing previews rebuild.
        with self.store.connection() as db:
            pinned = {r[0] for r in db.execute("SELECT cover FROM build_details WHERE cover<>''")}
            pinned.update(r[0] for r in db.execute("SELECT value FROM settings WHERE key LIKE 'wardrobePreview:%'"))
            db.execute("DELETE FROM settings WHERE key LIKE 'screenshot:%' AND rowid NOT IN (SELECT rowid FROM settings WHERE key LIKE 'screenshot:%' ORDER BY rowid DESC LIMIT 512)")
            db.execute("DELETE FROM gallery_cache WHERE key NOT IN (SELECT key FROM gallery_cache ORDER BY created DESC LIMIT 512)")
        pinned.update(protected)
        files = []
        for path in self.media_root.glob("*.png"):
            if path.is_file() and not path.is_symlink() and path.resolve().parent == self.media_root.resolve():
                info = path.stat()
                files.append((info.st_mtime, info.st_size, path))
        total = sum(size for _, size, _ in files)
        removed = []
        for _, size, path in sorted(files):
            if total <= CACHE_BUDGET:
                break
            if path.stem in pinned:
                continue
            path.unlink(missing_ok=True)
            total -= size
            removed.append(path.stem)
        if removed:
            with self.store.connection() as db:
                db.executemany("DELETE FROM settings WHERE key LIKE 'screenshot:%' AND value=?", [(digest,) for digest in removed])
                db.executemany("DELETE FROM gallery_cache WHERE image=?", [(digest,) for digest in removed])

    @staticmethod
    def _redirected(path):
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)

    def _gallery_folder(self, pid, *, required=False):
        root = self.store.folder(pid)
        if self._redirected(root) or root.resolve() != self.store.instances.resolve() / pid:
            raise ValueError("Папка сборки перенаправлена. Скриншоты недоступны.")
        folder = root / "screenshots"
        if not folder.exists():
            if required or folder.is_symlink():
                raise ValueError("Папка скриншотов не найдена или перенаправлена.")
            return folder
        if not folder.is_dir() or self._redirected(folder) or folder.resolve() != root.resolve() / "screenshots":
            raise ValueError("Папка скриншотов перенаправлена за пределы сборки.")
        return folder

    @staticmethod
    def _safe_directory(parent, name, *, create=False):
        path = parent / name
        if create:
            path.mkdir(exist_ok=True)
        if not path.is_dir() or Experience._redirected(path) or path.resolve() != parent.resolve() / name:
            raise ValueError("Папка удалённых скриншотов недоступна или перенаправлена.")
        return path

    @staticmethod
    def _screenshot_name(name):
        if not isinstance(name, str) or not name or len(name) > 255 or clean_filename(name) != name or Path(name).suffix.lower() != ".png":
            raise ValueError("Недопустимое имя скриншота.")
        return name

    @staticmethod
    def _stamp(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _shot_key(self, pid, path, info):
        value = [pid, path.name, *self._stamp(info)]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _shot_token(key, checksum):
        return hashlib.sha256((key + ":" + checksum).encode("ascii")).hexdigest()

    def _read_screenshot(self, path):
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or self._redirected(path) or before.st_size > SCREENSHOT_MAX_BYTES:
            raise ValueError("Скриншот недоступен или слишком большой.")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if self._stamp(opened) != self._stamp(before):
                raise ValueError("Скриншот изменился. Обнови галерею.")
            raw = source.read(SCREENSHOT_MAX_BYTES + 1)
            if len(raw) > SCREENSHOT_MAX_BYTES or self._stamp(os.fstat(source.fileno())) != self._stamp(opened):
                raise ValueError("Скриншот изменился. Обнови галерею.")
        if self._stamp(path.lstat()) != self._stamp(opened):
            raise ValueError("Скриншот изменился. Обнови галерею.")
        return opened, raw, hashlib.sha256(raw).hexdigest()

    def _trash_manifest(self, pid, batch):
        if not isinstance(batch, str) or not re.fullmatch(r"[a-f0-9]{32}", batch):
            raise ValueError("Удалённые скриншоты не найдены.")
        folder = self._gallery_folder(pid, required=True)
        trash = self._safe_directory(folder, SCREENSHOT_TRASH)
        directory = self._safe_directory(trash, batch)
        manifest = directory / "manifest.json"
        if not manifest.is_file() or self._redirected(manifest) or manifest.stat().st_size > 256 * 1024:
            raise ValueError("Запись об удалении недоступна.")
        try:
            record = json.loads(manifest.read_text(encoding="utf-8"))
            if record["format"] != 1 or record["profile"] != pid or record["batch"] != batch or not isinstance(record["created"], (int, float)):
                raise ValueError()
            entries = record["images"]
            if not isinstance(entries, list) or not 1 <= len(entries) <= SCREENSHOT_LIMIT:
                raise ValueError()
            names = set()
            for entry in entries:
                name = self._screenshot_name(entry["name"])
                if name.casefold() in names or not re.fullmatch(r"[a-f0-9]{64}", entry["checksum"]) or type(entry["size"]) is not int or not 0 <= entry["size"] <= SCREENSHOT_MAX_BYTES:
                    raise ValueError()
                names.add(name.casefold())
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("Запись об удалении повреждена.") from exc
        return directory, record

    def _trash_items(self, pid):
        folder = self._gallery_folder(pid)
        trash = folder / SCREENSHOT_TRASH
        if not trash.exists():
            return []
        self._safe_directory(folder, SCREENSHOT_TRASH)
        batches = []
        for entry in trash.iterdir():
            if re.fullmatch(r"[a-f0-9]{32}", entry.name) and entry.is_dir() and not self._redirected(entry):
                batches.append(entry)
        batches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        items = []
        for path in batches[:100]:
            try:
                directory, record = self._trash_manifest(pid, path.name)
                available = [x for x in record["images"] if (directory / x["name"]).is_file() and not self._redirected(directory / x["name"])]
                if available:
                    items.append({"id": path.name, "created": record["created"], "count": len(available), "names": [x["name"] for x in available], "size": sum(x["size"] for x in available)})
            except (OSError, ValueError):
                continue
        return items

    def gallery(self, pid):
        folder = self._gallery_folder(pid)
        files = []
        if folder.is_dir():
            for path in folder.iterdir():
                try:
                    if path.suffix.lower() == ".png" and path.is_file() and not self._redirected(path):
                        files.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        files.sort(key=lambda row: row[0], reverse=True)
        with self.store.connection() as db:
            cached = {row["key"]: dict(row) for row in db.execute("SELECT * FROM gallery_cache")}
        images, updates = [], []
        for _, path in files[:SCREENSHOT_LIMIT]:
            key, checksum = "", ""
            try:
                info = path.lstat()
                if info.st_size > SCREENSHOT_MAX_BYTES:
                    continue
                key = self._shot_key(pid, path, info)
                old = cached.get(key)
                if old and not old["image"]:
                    continue
                if old and self.media(old["image"]).is_file():
                    digest, checksum = old["image"], old["checksum"]
                else:
                    info, raw, checksum = self._read_screenshot(path)
                    key = self._shot_key(pid, path, info)
                    digest = self._image(io.BytesIO(raw), size=(960, 540))
                    updates.append((key, digest, checksum, time.time()))
                images.append({"name": path.name, "modified": info.st_mtime, "size": info.st_size, "image": digest, "token": self._shot_token(key, checksum)})
            except (OSError, ValueError, Image.DecompressionBombWarning):
                if key:
                    updates.append((key, "", checksum, time.time()))
                continue
        if updates:
            with self.store.connection() as db:
                db.executemany("INSERT OR REPLACE INTO gallery_cache VALUES(?,?,?,?)", updates)
        self._prune_media(image["image"] for image in images)
        return {"images": images, "total": len(files), "limit": SCREENSHOT_LIMIT, "trash": self._trash_items(pid)}

    def delete_screenshots(self, pid, requested):
        folder = self._gallery_folder(pid, required=True)
        if not isinstance(requested, list) or not 1 <= len(requested) <= SCREENSHOT_LIMIT:
            raise ValueError("Выбери от 1 до 120 скриншотов.")
        seen, checked = set(), []
        # Validate the whole selection before moving anything. A stale selection
        # never deletes a replacement written under the same filename.
        for item in requested:
            if not isinstance(item, dict):
                raise ValueError("Обнови галерею и выбери скриншоты заново.")
            name = self._screenshot_name(item.get("name"))
            if name.casefold() in seen:
                raise ValueError("Один скриншот выбран несколько раз.")
            seen.add(name.casefold())
            token = item.get("token")
            if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
                raise ValueError("Обнови галерею и выбери скриншоты заново.")
            path = folder / name
            info, _, checksum = self._read_screenshot(path)
            key = self._shot_key(pid, path, info)
            if not secrets.compare_digest(token, self._shot_token(key, checksum)):
                raise ValueError("Скриншот «" + name + "» изменился. Обнови галерею и проверь выбор.")
            checked.append((path, info, checksum, key))
        self._gallery_folder(pid, required=True)
        trash = self._safe_directory(folder, SCREENSHOT_TRASH, create=True)
        batch = secrets.token_hex(16)
        directory = trash / batch
        directory.mkdir()
        record = {"format": 1, "profile": pid, "batch": batch, "created": time.time(), "images": [{"name": path.name, "size": info.st_size, "checksum": checksum} for path, info, checksum, _ in checked]}
        # Write the complete receipt first. If the process ends mid-batch, every
        # file already moved is discoverable and can still be restored.
        (directory / "manifest.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        moved, skipped = [], []
        for path, info, checksum, key in checked:
            try:
                self._gallery_folder(pid, required=True)
                self._safe_directory(folder, SCREENSHOT_TRASH)
                self._safe_directory(trash, batch)
                if self._stamp(path.lstat()) != self._stamp(info) or self._redirected(path):
                    raise ValueError("Скриншот изменился.")
                os.rename(path, directory / path.name)
                moved.append(key)
            except (OSError, ValueError):
                skipped.append(path.name)
        with self.store.connection() as db:
            db.executemany("DELETE FROM gallery_cache WHERE key=?", [(key,) for key in moved])
        if not moved:
            try:
                safe_directory, _ = self._trash_manifest(pid, batch)
                (safe_directory / "manifest.json").unlink(missing_ok=True)
                safe_directory.rmdir()
            except (OSError, ValueError):
                pass
            raise ValueError("Скриншоты изменились или заняты. Обнови галерею и повтори.")
        message = "Удалено скриншотов: " + str(len(moved)) + ". Их можно вернуть из удалённых."
        if skipped:
            message += " Не удалось переместить: " + str(len(skipped)) + "."
        return {"ok": True, "deleted": len(moved), "skipped": skipped, "batch": batch, "message": message}

    def restore_screenshots(self, pid, batch):
        directory, record = self._trash_manifest(pid, batch)
        folder = self._gallery_folder(pid, required=True)
        checked = []
        for entry in record["images"]:
            source = directory / entry["name"]
            if not source.exists() and not source.is_symlink():
                continue
            info, raw, checksum = self._read_screenshot(source)
            if info.st_size != entry["size"] or checksum != entry["checksum"]:
                raise ValueError("Удалённый скриншот «" + entry["name"] + "» изменился. Восстановление отменено.")
            target = folder / entry["name"]
            if target.exists() or target.is_symlink():
                raise ValueError("В галерее уже есть «" + entry["name"] + "». Переименуй его перед восстановлением.")
            checked.append((source, target, info, checksum))
        if not checked:
            raise ValueError("Эти скриншоты уже восстановлены.")
        # Exclusive creation prevents overwriting a new F2 screenshot even if it
        # appears after the preflight check. Originals remain in trash until the
        # complete copy has been flushed successfully.
        restored = []
        try:
            for source, target, info, checksum in checked:
                self._gallery_folder(pid, required=True)
                self._trash_manifest(pid, batch)
                current, raw, current_checksum = self._read_screenshot(source)
                if self._stamp(current) != self._stamp(info) or current_checksum != checksum:
                    raise ValueError("Удалённый скриншот изменился. Повтори восстановление.")
                with target.open("xb") as output:
                    created = os.fstat(output.fileno())
                    restored.append((target, created.st_dev, created.st_ino))
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
        except (OSError, ValueError):
            for target, device, inode in reversed(restored):
                try:
                    self._gallery_folder(pid, required=True)
                    info = target.lstat()
                    if (info.st_dev, info.st_ino) == (device, inode) and not self._redirected(target):
                        target.unlink()
                except (OSError, ValueError):
                    pass
            raise
        for source, _, info, _ in checked:
            self._trash_manifest(pid, batch)
            if self._stamp(source.lstat()) == self._stamp(info):
                source.unlink()
        self._trash_manifest(pid, batch)
        if all(not (directory / entry["name"]).exists() for entry in record["images"]):
            (directory / "manifest.json").unlink()
            try:
                directory.rmdir()
            except OSError:
                pass
        return {"ok": True, "restored": len(restored), "message": "Восстановлено скриншотов: " + str(len(restored)) + "."}

    def _favorite(self, p):
        pid, favorite = p.get("id"), p.get("favorite")
        if not isinstance(pid, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", pid):
            raise ValueError("Неверный идентификатор сборки.")
        if type(favorite) is not bool:
            raise ValueError("Укажи, должна ли сборка быть в избранном.")
        with self.store.connection() as db:
            # Keep existence validation and the metadata write in one transaction:
            # another window must not remove this build between them.
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM profiles WHERE id=?", (pid,)).fetchone():
                raise ValueError("Сборка не найдена.")
            db.execute("INSERT OR IGNORE INTO build_details(profile) VALUES(?)", (pid,))
            db.execute("UPDATE build_details SET favorite=? WHERE profile=?", (int(favorite), pid))
        return {"ok": True, "id": pid, "favorite": favorite,
                "message": "Сборка добавлена в избранное." if favorite else "Сборка убрана из избранного."}

    def _save_build(self, p):
        pid = p["id"]
        profile = self.store.profile(pid)
        name, _, _ = input_profile(p.get("name", profile["name"]), profile["version"], profile["loader"])
        description, group = p.get("description", ""), p.get("group", "")
        if not isinstance(description, str) or len(description) > 1200 or not isinstance(group, str) or len(group) > 40:
            raise ValueError("Описание: до 1200 символов, группа: до 40.")
        if any(ord(c) < 32 and c not in "\n\t" for c in description + group):
            raise ValueError("В тексте есть недопустимые символы.")
        color = p.get("color", "green")
        if color not in COLORS:
            raise ValueError("Выбери цвет из списка.")
        with self.store.connection() as db:
            db.execute("UPDATE profiles SET name=? WHERE id=?", (name, pid))
            db.execute("INSERT OR IGNORE INTO build_details(profile) VALUES(?)", (pid,))
            db.execute("UPDATE build_details SET description=?,group_name=?,favorite=?,color=? WHERE profile=?", (description.strip(), group.strip(), int(p.get("favorite") is True), color, pid))
            if p.get("clearCover") is True:
                db.execute("UPDATE build_details SET cover='' WHERE profile=?", (pid,))
        return {"ok": True}

    def _save_skin(self, p):
        name = p.get("name", "Мой скин")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError("Название скина: от 1 до 60 символов.")
        digest = self.store.get("skinDigest")
        path = self.store.root / "skins" / (digest + ".png") if re.fullmatch(r"[a-f0-9]{64}", digest) else self.store.root / "skin.png"
        skins.validate_skin(path)
        digest = skins.save_texture(self.store.root, skins.normalized_skin(path))
        model = self.store.get("model", "classic")
        image = self._image(io.BytesIO(skins._encode_png(skins.skin_preview(path, model))), size=(192, 256))
        sid = secrets.token_hex(16)
        with self.store.connection() as db:
            if db.execute("SELECT count(*) FROM wardrobe").fetchone()[0] >= 80:
                raise ValueError("В гардеробе уже 80 скинов. Удали ненужные и повтори.")
            db.execute("INSERT INTO wardrobe VALUES(?,?,?,?,?)", (sid, name.strip(), digest, model, time.time()))
        self.store.set("wardrobePreview:" + sid, image)
        return {"ok": True}

    def shortcut(self, pid):
        profile = self.store.profile(pid)
        executable = Path(os.getenv("BLOCKHARBOR_LAUNCHER_EXE", ""))
        if os.name != "nt" or not executable.is_file() or executable.suffix.lower() != ".exe":
            raise ValueError("Ярлык создаётся в установленном Windows-приложении BlockHarbor.")
        # Values are passed as environment variables, never interpolated into code.
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", profile["name"]).strip(" .")[:64] or "Сборка"
        env = {**os.environ, "BH_SHORTCUT_TARGET": str(executable.resolve()), "BH_SHORTCUT_ARGS": subprocess.list2cmdline(["--profile", pid]), "BH_SHORTCUT_NAME": "BlockHarbor — " + filename + "-" + pid[:6] + ".lnk"}
        script = "$ErrorActionPreference='Stop'; $desktop=[Environment]::GetFolderPath('DesktopDirectory'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop $env:BH_SHORTCUT_NAME)); $s.TargetPath=$env:BH_SHORTCUT_TARGET; $s.Arguments=$env:BH_SHORTCUT_ARGS; $s.WorkingDirectory=[IO.Path]::GetDirectoryName($env:BH_SHORTCUT_TARGET); $s.IconLocation=$env:BH_SHORTCUT_TARGET+',0'; $s.Description='Открыть сборку в BlockHarbor'; $s.Save()"
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        subprocess.run([str(system), "-NoProfile", "-NonInteractive", "-Command", script], env=env, check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from .windows_identity import mark_desktop_shortcut
        mark_desktop_shortcut(env["BH_SHORTCUT_NAME"], executable)
        return {"ok": True, "message": "Ярлык сборки добавлен на рабочий стол."}

    def handle(self, action, p, status=lambda _: None):
        if action == "experienceState":
            data = self.state(p.get("id"))
            for skin in data["wardrobe"]:
                skin["preview"] = self.store.get("wardrobePreview:" + skin["id"])
            return data
        if action == "experienceGallery":
            return self.gallery(p["id"])
        if action == "experienceDeleteScreenshots":
            return self.delete_screenshots(p["id"], p.get("images"))
        if action == "experienceRestoreScreenshots":
            return self.restore_screenshots(p["id"], p.get("batch"))
        if action == "experienceNotifications":
            with self.store.connection() as db:
                return {"items": [dict(row) for row in db.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 200")]}
        if action == "experienceClearNotifications":
            with self.store.connection() as db:
                db.execute("DELETE FROM notifications")
            return {"ok": True}
        if action == "experienceSaveBuild":
            return self._save_build(p)
        if action == "experienceFavorite":
            return self._favorite(p)
        if action == "experiencePreferences":
            if str(p.get("scale")) not in ("85", "90", "100", "110", "120") or type(p.get("reducedMotion")) is not bool:
                raise ValueError("Неизвестные настройки интерфейса.")
            values = {"uiScale": str(p["scale"]), "reducedMotion": "1" if p["reducedMotion"] else "0"}
            if "checkUpdatesOnStart" in p:
                if type(p["checkUpdatesOnStart"]) is not bool:
                    raise ValueError("Неизвестный режим проверки обновлений.")
                values["checkUpdatesOnStart"] = "1" if p["checkUpdatesOnStart"] else "0"
            self.store.set_many(values)
            return {"ok": True}
        if action == "experienceSaveSkin":
            return self._save_skin(p)
        if action in ("experienceUseSkin", "experienceDeleteSkin"):
            with self.store.connection() as db:
                skin = db.execute("SELECT * FROM wardrobe WHERE id=?", (p.get("skin"),)).fetchone()
                if not skin:
                    raise ValueError("Скин не найден в гардеробе.")
                if action == "experienceDeleteSkin":
                    db.execute("DELETE FROM wardrobe WHERE id=?", (skin["id"],))
                    db.execute("DELETE FROM settings WHERE key=?", ("wardrobePreview:" + skin["id"],))
                else:
                    path = self.store.root / "skins" / (skin["digest"] + ".png")
                    skins.validate_skin(path)
                    db.executemany("REPLACE INTO settings VALUES (?,?)", [("skinDigest", skin["digest"]), ("model", skin["model"])])
            if action == "experienceUseSkin" and hasattr(self.store, "remember"):
                self.store.remember({"skinDigest": skin["digest"], "model": skin["model"]})
            return {"ok": True}
        if action == "experienceShortcut":
            return self.shortcut(p["id"])
        raise ValueError("Неизвестное действие оформления.")
