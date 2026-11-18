"""Personalization, local media, notification history and UI preferences."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

from PIL import Image, ImageOps

from . import skins
from .diagnostics import redact
from .store import clean_filename, input_profile

COLORS = ("green", "blue", "purple", "orange", "rose")
CACHE_BUDGET = 256 * 1024 * 1024


class Experience:
    READ_ACTIONS = {"experienceState", "experienceGallery", "experienceNotifications"}
    ACTIONS = READ_ACTIONS | {"experienceSaveBuild", "experiencePreferences", "experienceSaveSkin", "experienceUseSkin", "experienceDeleteSkin", "experienceClearNotifications", "experienceShortcut"}
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

    def gallery(self, pid):
        root = self.store.folder(pid).resolve()
        folder = root / "screenshots"
        if not folder.is_dir() or folder.is_symlink() or folder.resolve() != root / "screenshots":
            return {"images": [], "total": 0}
        files = [p for p in folder.iterdir() if p.suffix.lower() == ".png" and p.is_file() and not p.is_symlink()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        images = []
        for path in files[:120]:
            cache_key = ""
            try:
                if path.stat().st_size > 32 * 1024 * 1024:
                    continue
                stamp = str(path) + str(path.stat().st_mtime_ns) + str(path.stat().st_size)
                cache_key = "screenshot:" + hashlib.sha256(stamp.encode()).hexdigest()
                digest = self.store.get(cache_key)
                if digest == "!":
                    continue
                if not digest or not self.media(digest).is_file():
                    digest = self._image(path, size=(960, 540))
                    self.store.set(cache_key, digest)
                images.append({"name": path.name, "modified": path.stat().st_mtime, "size": path.stat().st_size, "image": digest})
            except (OSError, ValueError, Image.DecompressionBombWarning):
                if cache_key:
                    self.store.set(cache_key, "!")
                continue
        self._prune_media(image["image"] for image in images)
        return {"images": images, "total": len(files), "limit": 120}

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
        if action == "experienceNotifications":
            with self.store.connection() as db:
                return {"items": [dict(row) for row in db.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 200")]}
        if action == "experienceClearNotifications":
            with self.store.connection() as db:
                db.execute("DELETE FROM notifications")
            return {"ok": True}
        if action == "experienceSaveBuild":
            return self._save_build(p)
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
            return {"ok": True}
        if action == "experienceShortcut":
            return self.shortcut(p["id"])
        raise ValueError("Неизвестное действие оформления.")
