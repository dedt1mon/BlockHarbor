"""BlockHarbor desktop launcher. Its UI is served exclusively on loopback."""

from __future__ import annotations

import argparse
import hmac
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from launcher import (
    aliases,
    build_archive,
    cards,
    distances,
    e4steam,
    friends,
    modrinth,
    skin_sync,
    skins,
    steam_network,
)
from launcher.memory import automatic_heap
from launcher.singleton import SingleInstance
from launcher.store import (
    FALLBACK_VERSIONS,
    KINDS,
    MAX_ADDON,
    Store,
    input_profile,
    require_idle_files,
)
from launcher.window import DesktopWindow

HERE = Path(__file__).resolve().parent
STATIC = {
    "/": "index.html",
    "/app.js": "app.js",
    "/catalog.js": "catalog.js",
    "/compatibility.js": "compatibility.js",
    "/catalog-filters.js": "catalog-filters.js",
    "/style.css": "style.css",
    "/island.js": "island.js",
    "/window.js": "window.js",
    "/icon.png": "icon.png",
    "/icon.svg": "icon.svg",
}
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


def default_root():
    return (
        Path(os.getenv("LOCALAPPDATA") or (Path.home() / ".local" / "share"))
        / "BlockHarbor"
    )


class App:
    def __init__(self, root):
        self.store = Store(root)
        self.window = DesktopWindow(root)
        # Versions before 1.1 used Microsoft Launcher for the default mode.
        if self.store.get("mode") in ("official", "alias"):
            self.store.set("mode", "local")
        self.downloads = build_archive.Downloads()
        self.token = secrets.token_urlsafe(32)
        self.account = None
        self.auth_generation = 0
        self.lock = threading.RLock()
        self.catalog_lock = threading.Lock()
        self.jobs_lock = threading.Lock()
        self.job = {"id": "", "busy": False, "message": "", "ok": True}
        self.last_seen = time.monotonic()
        self.child = None
        self.process_lock = threading.Lock()
        self.game_exit = None
        self.playing_id = ""
        self.playing_name = ""
        self.prism_launches = {}
        self.prism_games = {}
        self.parked_for_game = False

    def state(self):
        s = self.store
        settings = s.settings()
        pid = settings.get("active")
        job = self.job_snapshot()
        profiles = s.all()
        active = next(
            (profile for profile in profiles if profile["id"] == pid),
            profiles[0] if profiles else None,
        )
        pid = active["id"] if active else ""
        version, loader = (active["version"], active["loader"]) if active else ("", "")
        supported = bool(active and e4steam.support(version, loader))
        content = s.content(pid) if active else {kind: [] for kind in KINDS}
        folder = s.folder(pid) if active else None
        ready = supported and e4steam.installed(
            folder, version, loader, sizes={row["size"] for row in content["mods"]}
        )
        managed = skin_sync.managed_files(folder, version, loader, content["mods"])
        if ready:
            managed.update(e4steam.protected_names(version, loader))
        for entry in content["mods"]:
            entry["managed"] = entry["name"] in managed
        configured = settings.get("memory", "auto")
        estimated = automatic_heap()
        return {
            "profiles": profiles,
            "deletedProfiles": s.deleted(),
            "active": pid,
            "contents": content,
            "steam": {
                "supported": supported,
                "installed": ready,
                "canEnable": loader == "vanilla" and e4steam.support(version, "fabric"),
                "reason": ""
                if supported
                else (
                    "Для Steam нужен загрузчик модов."
                    if loader == "vanilla"
                    else "Для этой версии и загрузчика нет совместимого комплекта e4steam и Public Servers."
                ),
            },
            "skinNetwork": skin_sync.support(version, loader),
            "skinSupport": bool(aliases.skin_release(version, loader)),
            "canUseFabricSkin": loader == "vanilla"
            and bool(aliases.skin_release(version, "fabric")),
            "realName": settings.get("realName", ""),
            "nickname": settings.get("nickname", ""),
            "model": settings.get("model", "classic"),
            "skin": self.skin_path(settings.get("skinDigest", "")).is_file(),
            "account": self.account["name"] if self.account else "",
            "memory": estimated if configured == "auto" else int(configured),
            "autoMemory": configured == "auto",
            "distanceSettings": bool(active and distances.supported(version)),
            "recommendedMemory": estimated,
            "clientId": settings.get("clientId", ""),
            "versions": self.release_catalog(settings.get("versions", "[]")),
            "job": job,
            "mode": settings.get("mode", "local"),
            "gameRunning": job["gameRunning"],
            "gameExit": job["gameExit"],
        }

    def release_catalog(self, raw=None):
        cached = json.loads(self.store.get("versions", "[]") if raw is None else raw)
        # Older installations may only have cached a subset of today's releases.
        if cached and set(FALLBACK_VERSIONS).issubset(cached):
            return cached
        newer = [v for v in cached if v not in FALLBACK_VERSIONS]
        return list(dict.fromkeys(newer + FALLBACK_VERSIONS))

    def refresh_releases(self, *, force=False):
        from launcher import game

        with self.catalog_lock:
            updated = float(self.store.get("releaseCatalogUpdated", "0"))
            if not force and 0 <= time.time() - updated < 24 * 3600:
                return self.release_catalog()
            releases = game.list_versions()
            if not releases:
                raise ValueError("Каталог Minecraft вернул пустой список.")
            with self.store.connection() as db:
                db.executemany(
                    "REPLACE INTO settings VALUES (?, ?)",
                    [
                        ("versions", json.dumps(releases)),
                        ("releaseCatalogUpdated", str(time.time())),
                    ],
                )
            return self.release_catalog()

    def skin_path(self, digest=None):
        if digest is None:
            digest = self.store.get("skinDigest")
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            return self.store.root / "skins" / (digest + ".png")
        return self.store.root / "skin.png"

    def _track_game(self, process, profile, *, prism=False):
        with self.process_lock:
            if prism:
                self.prism_games[profile["id"]] = (process, profile["name"])
            else:
                self.child = process
                self.playing_id = profile["id"]
                self.playing_name = profile["name"]
            self.game_exit = None
            if process.poll() is None and not self.parked_for_game:
                self.window.game_started()
                self.parked_for_game = True

    def _game_finished(self, code, name):
        if code:
            self.game_exit = {
                "id": secrets.token_hex(6),
                "code": code,
                "message": f"Сборка «{name}» завершилась с кодом {code}. Открой папку этой сборки и посмотри logs/latest.log или crash-reports.",
            }

    def process_state(self):
        with self.process_lock:
            if self.child is not None:
                code = self.child.poll()
                if code is not None:
                    self._game_finished(code, self.playing_name)
                    self.child = None
            for pid, (process, name) in list(self.prism_games.items()):
                code = process.poll()
                if code is not None:
                    self._game_finished(code, name)
                    del self.prism_games[pid]
            running = self.child is not None or bool(self.prism_games)
            if not running and self.parked_for_game:
                self.parked_for_game = False
                self.last_seen = time.monotonic()
                self.window.game_ended()
            return {
                "gameRunning": running,
                "gameExit": None if running else self.game_exit,
            }

    def game_running(self):
        return self.process_state()["gameRunning"]

    def profile_running(self, pid):
        self.process_state()
        with self.process_lock:
            return (
                self.child is not None and self.playing_id == pid
            ) or pid in self.prism_games

    def player_name(self):
        if self.store.get("mode", "local") == "local":
            return self.store.get("nickname") or "Player"
        return (self.account or {}).get("name") or self.store.get("realName")

    def job_snapshot(self):
        with self.jobs_lock:
            return {**self.job, **self.process_state()}

    def start_job(self, fn):
        with self.jobs_lock:
            if self.job["busy"]:
                raise ValueError("Дождись завершения текущей операции.")
            jid = secrets.token_hex(5)
            self.job = {"id": jid, "busy": True, "ok": True, "message": "Подготовка…"}

        def status(msg):
            with self.jobs_lock:
                self.job["message"] = str(msg)[:700]

        def worker():
            try:
                result = fn(status)
                final = {"message": result or "Готово.", "ok": True, "busy": False}
            except modrinth.NeedsReview as exc:
                final = {
                    "message": str(exc),
                    "ok": False,
                    "busy": False,
                    "review": exc.review,
                }
            except Exception as exc:
                final = {
                    "message": str(exc)[:1000] or "Операция не удалась.",
                    "ok": False,
                    "busy": False,
                }
            with self.jobs_lock:
                self.job.update(final)

        threading.Thread(target=worker, daemon=True).start()
        return {"job": jid}

    def save_appearance(self, params, uploaded):
        store = self.store
        model = params.get("model", "classic")
        skins._check_model(model)
        if params.get("skinDestination") == "java":
            current = self.skin_path()
            if not uploaded and not current.is_file():
                raise ValueError("Сначала выбери PNG со скином игрока.")
            pixels = skins.appearance_skin(uploaded or current, model)

            def save_for_java(status):
                digest = skins.save_texture(store.root, pixels)
                store.set_many({"skinDigest": digest, "model": model})
                return "PNG и модель сохранены. Теперь можно установить скин в Java-профиле."

            return self.start_job(save_for_java)

        profile = store.profile(params["id"])
        nick = skins.validate_nickname(params.get("nickname") or "Player")
        mode = store.get("mode", "local")
        real = (self.account or {}).get("name") or params.get(
            "realName", store.get("realName")
        )
        if real:
            real = skins.validate_nickname(real)
        target = nick if mode == "local" else real
        current = self.skin_path()
        pixels = (
            skins.appearance_skin(uploaded or current, model)
            if uploaded or current.is_file()
            else None
        )
        version, loader = profile["version"], profile["loader"]
        convert = bool(
            pixels
            and loader == "vanilla"
            and params.get("fabric") == "1"
            and aliases.skin_release(version, "fabric")
        )
        if convert:
            loader = "fabric"
        apply_skin = bool(pixels and aliases.skin_release(version, loader))
        if uploaded and not apply_skin:
            raise ValueError(
                f"Для скина выбери совместимую сборку с загрузчиком модов. Текущая сборка: {version} · {loader}."
            )
        if apply_skin and not target:
            raise ValueError("Укажи имя своего профиля Minecraft для установки скина.")
        if self.game_running() and (apply_skin or convert):
            raise ValueError(
                "Закрой Minecraft и ещё раз сохрани параметры, чтобы игра загрузила новый скин."
            )
        folder = store.folder(profile["id"])

        def save(status):
            values = {"nickname": nick, "realName": real or "", "model": model}
            if apply_skin:
                status("Применяем скин в сборке…")
                aliases.ensure_skin_support(folder, version, loader)
                values["skinDigest"] = skins.save_texture(store.root, pixels)
            if (
                mode != "local"
                and target
                and (version, loader) == ("1.20.1", "fabric")
                and (
                    target != nick or (folder / "config" / "nameprotect.json").is_file()
                )
            ):
                aliases.apply_alias(folder, target, nick)
            if apply_skin:
                skins.install_local_skin(pixels, folder, target, model)
                skin_sync.prepare(folder, version, loader, pixels, model)
            with store.connection() as db:
                if convert:
                    row = db.execute(
                        "UPDATE profiles SET loader='fabric' WHERE id=? AND loader='vanilla'",
                        (profile["id"],),
                    )
                    if row.rowcount != 1:
                        raise ValueError(
                            "Сборка изменилась во время сохранения. Открой профиль заново."
                        )
                db.executemany("REPLACE INTO settings VALUES (?, ?)", values.items())
            return "Успешно сохранено"

        return self.start_job(save)

    def import_card(self, src, pid):
        profile = self.store.profile(pid)
        if (profile["version"], profile["loader"]) != ("1.20.1", "fabric"):
            raise ValueError("Карточки игроков поддерживают сборки Fabric 1.20.1.")
        cards.read_card(src)
        payload = src.read_bytes()
        own_name = self.player_name()
        folder = self.store.folder(pid)

        def apply(status):
            status("Применяем карточку игрока и проверяем компоненты сборки…")
            with tempfile.TemporaryDirectory(dir=self.store.root) as td:
                card = Path(td) / "friend.bhplayer"
                card.write_bytes(payload)
                return cards.apply_card(card, folder, own_name=own_name)

        return self.start_job(apply)

    def action(self, action, p):
        s = self.store
        if action == "gameDistances":
            profile = s.profile(p["id"])
            folder = s.folder(p["id"])
            if "expected" in p:
                prism = self.prism_launches.get(p["id"])
                if (
                    self.profile_running(p["id"])
                    or (prism and prism.poll() is None)
                    or self.job_snapshot()["busy"]
                ):
                    raise ValueError(
                        "Закрой Minecraft и дождись завершения операции перед изменением дальности."
                    )
                require_idle_files(folder)
                distances.apply(folder, profile["version"], p["expected"])
            return distances.preview(folder, profile["version"])
        if action == "installModrinth":
            pid, kind, project, version = (
                p[k] for k in ("id", "kind", "project", "version")
            )
            s.profile(pid)
            prism = self.prism_launches.get(pid)
            if self.profile_running(pid) or (
                prism is not None and prism.poll() is None
            ):
                raise ValueError(
                    "Закрой эту сборку в Minecraft или Prism перед установкой дополнений."
                )
            return self.start_job(
                lambda status: modrinth.install(
                    s,
                    pid,
                    kind,
                    project,
                    version,
                    status,
                    self.lock,
                    p.get("acknowledgement", ""),
                )
            )
        if action == "create":
            name, version, loader = input_profile(
                p.get("name"), p.get("version"), p.get("loader")
            )
            if self.job_snapshot()["busy"]:
                raise ValueError("Дождись завершения текущей операции.")
            if e4steam.support(version, loader):

                def create_with_steam(status):
                    def prepare(folder):
                        status("Готовим компоненты для игры с друзьями…")
                        e4steam.ensure(
                            folder,
                            version,
                            loader,
                            status,
                            cache_dir=s.root / "steam-mod-cache",
                        )

                        skin_sync.prepare(folder, version, loader)

                    s.create(name, version, loader, prepare=prepare)
                    return "Сборка создана. Игра с друзьями готова."

                return self.start_job(create_with_steam)
            s.create(name, version, loader)
        elif action == "purgeBuild":
            pid = p["id"]

            def purge(status):
                status("Безвозвратно удаляем файлы сборки…")
                s.purge_profile(pid)
                return "Файлы сборки удалены с диска."

            return self.start_job(purge)
        elif action in ("deleteBuild", "restoreBuild"):
            pid = p["id"]
            if self.job_snapshot()["busy"]:
                raise ValueError("Дождись завершения текущей операции и повтори.")
            if action == "deleteBuild":
                prism = self.prism_launches.get(pid)
                if self.profile_running(pid) or (
                    prism is not None and prism.poll() is None
                ):
                    raise ValueError(
                        "Закрой эту сборку в Minecraft или Prism перед удалением."
                    )
            s.move_profile(pid, restore=action == "restoreBuild")
            if action == "deleteBuild":
                self.prism_launches.pop(pid, None)
        elif action == "select":
            s.profile(p["id"])
            s.set("active", p["id"])
        elif action == "remove":
            if p["kind"] == "mods":
                profile = s.profile(p["id"])
                version, loader = profile["version"], profile["loader"]
                if skin_sync.managed_file(
                    s.folder(p["id"]), version, loader, p["name"]
                ):
                    raise ValueError(
                        "Этот файл нужен для передачи скинов участникам мира."
                    )
                if p["name"] in e4steam.protected_names(
                    version, loader
                ) and e4steam.installed(s.folder(p["id"]), version, loader):
                    raise ValueError(
                        "Этот файл входит в поддержку Steam выбранной сборки."
                    )
            s.remove(p["id"], p["kind"], p["name"])
        elif action == "saveProfile":
            nick = p.get("nickname", "").strip()
            if nick:
                skins.validate_nickname(nick)
            model = p.get("model")
            if model not in ("classic", "slim"):
                raise ValueError("Неизвестная модель.")
            real = p.get("realName", "").strip()
            if real:
                skins.validate_nickname(real)
            s.set("realName", real)
            s.set("nickname", nick)
            s.set("model", model)
        elif action == "settings":
            import uuid

            client_id = p.get("clientId", "").strip()
            if client_id:
                try:
                    client_id = str(uuid.UUID(client_id))
                except ValueError:
                    raise ValueError("Client ID должен быть в формате UUID.") from None
            mode = p.get("mode", "local")
            if mode not in ("local", "direct", "prism"):
                raise ValueError("Неизвестный режим запуска.")
            if mode == "direct" and not client_id:
                raise ValueError(
                    "Введи Client ID для входа через собственное приложение Microsoft."
                )
            memory = p.get("memory", s.get("memory", "auto"))
            if memory != "auto":
                memory = int(memory)
                if not 1024 <= memory <= 32768:
                    raise ValueError("Можно выделить от 1024 до 32768 МБ памяти.")
            changed_client = client_id != s.get("clientId")
            s.set_many({"memory": str(memory), "clientId": client_id, "mode": mode})
            if changed_client:
                self.auth_generation += 1
                self.account = None
        elif action == "logout":
            self.auth_generation += 1
            self.account = None
        elif action in ("skinInstall", "aliasInstall"):
            target = skins.validate_nickname(p.get("target", ""))
            profile = s.profile(p["id"])
            folder = s.folder(p["id"])
            starter = profile["version"] == "1.20.1" and profile["loader"] == "fabric"
            nickname = s.get("nickname")
            if action == "aliasInstall":
                if not starter:
                    raise ValueError(
                        "Автоустановка псевдонимов поддерживается для Fabric 1.20.1."
                    )
                skins.validate_nickname(nickname)
            elif not self.skin_path().is_file():
                raise ValueError("Сначала выбери PNG-скин.")

            def apply_local(status):
                if action == "aliasInstall":
                    status("Проверяем и устанавливаем компоненты отображения ника…")
                    aliases.apply_alias(folder, target, nickname)
                    return (
                        "Ник применён в сборке «"
                        + profile["name"]
                        + "». Перезапусти Minecraft."
                    )
                status("Проверяем и устанавливаем CustomSkinLoader…")
                aliases.ensure_skin_support(
                    folder, profile["version"], profile["loader"]
                )
                skins.install_local_skin(
                    self.skin_path(), folder, target, s.get("model", "classic")
                )
                skin_sync.prepare(
                    folder,
                    profile["version"],
                    profile["loader"],
                    self.skin_path(),
                    s.get("model", "classic"),
                )
                return (
                    "Скин применён в сборке «"
                    + profile["name"]
                    + "». Перезапусти Minecraft."
                )

            return self.start_job(apply_local)
        elif action == "versions":

            def versions(status):
                status("Загружаем список релизов Minecraft…")
                rows = self.refresh_releases(force=True)
                return f"Доступно релизов: {len(rows)}."

            return self.start_job(versions)
        elif action == "signin":
            client = s.get("clientId")
            if not client:
                raise ValueError("Укажи одобренный Client ID в настройках.")
            generation = self.auth_generation

            def signin(status):
                from launcher import game

                account = game.login(client, status=status)
                with self.lock:
                    if generation != self.auth_generation:
                        raise ValueError(
                            "Настройки входа изменились. Войди в выбранный профиль ещё раз."
                        )
                    self.account = account
                return "Вход выполнен: " + account["name"]

            return self.start_job(signin)
        elif action in ("uploadSkin", "renameOnline"):
            if not self.account:
                raise ValueError("Нужен вход Microsoft во вкладке настроек.")
            session = self.account
            image = self.skin_path()
            nick = s.get("nickname")
            model = s.get("model", "classic")
            if action == "renameOnline":
                skins.validate_nickname(nick)

            def apply_online(status):
                status("Отправляем изменение в Minecraft…")
                if action == "renameOnline":
                    skins.change_name(nick, session["access_token"])
                    session["name"] = nick
                    return "Имя Java-профиля изменено."
                skins.upload_skin(image, model, session["access_token"])
                return "Скин загружен в профиль Minecraft. Перезайди в игру."

            return self.start_job(apply_online)
        elif action == "openSteam":
            return {"ok": True, "steamAction": friends.open_steam()}
        elif action in ("prepareSteam", "repairSteamSettings"):
            pid = p["id"]
            profile = s.profile(pid)
            version, loader = profile["version"], profile["loader"]
            if not e4steam.support(version, loader):
                raise ValueError("Выбери совместимую сетевую сборку.")
            prism = self.prism_launches.get(pid)
            if self.profile_running(pid) or (
                prism is not None and prism.poll() is None
            ):
                raise ValueError("Закрой игру и повтори подготовку Steam.")
            folder = s.folder(pid)

            def prepare_steam(status):
                if action == "repairSteamSettings":
                    status("Проверяем настройки открытия мира…")
                    return steam_network.repair_settings(folder)
                e4steam.ensure(
                    folder,
                    version,
                    loader,
                    status,
                    cache_dir=s.root / "steam-mod-cache",
                )
                return "Файлы для игры через Steam готовы."

            return self.start_job(prepare_steam)
        elif action == "enableSteam":
            profile = s.profile(p["id"])
            if profile["loader"] != "vanilla" or not e4steam.support(
                profile["version"], "fabric"
            ):
                raise ValueError("Переход на Fabric для этой сборки недоступен.")
            if self.game_running():
                raise ValueError("Закрой Minecraft перед изменением загрузчика.")
            folder = s.folder(profile["id"])

            def enable(status):
                e4steam.ensure(
                    folder,
                    profile["version"],
                    "fabric",
                    status,
                    cache_dir=s.root / "steam-mod-cache",
                )
                skin_sync.prepare(folder, profile["version"], "fabric")
                s.enable_fabric(profile["id"])
                return f"Сборка «{profile['name']}» готова для Steam. Нажми «Играть»."

            return self.start_job(enable)
        elif action == "play":
            from launcher import game

            pro = s.profile(p["id"])
            folder = s.folder(p["id"])
            configured_memory = s.get("memory", "auto")
            memory = (
                automatic_heap()
                if configured_memory == "auto"
                else int(configured_memory)
            )
            mode = s.get("mode", "local")
            if self.game_running():
                raise ValueError(
                    "Minecraft уже запущен. Закрой текущее окно игры, чтобы запустить другую сборку."
                )
            session = self.account
            if mode == "direct" and not session:
                raise ValueError("Войди в Microsoft в настройках выбранного режима.")
            nick = ""
            if mode == "local":
                nick = skins.validate_nickname(
                    p.get("nickname") or s.get("nickname") or "Player"
                )

            def play(status):
                image = self.skin_path()
                target = nick if mode == "local" else self.player_name()
                if (
                    image.is_file()
                    and target
                    and aliases.skin_release(pro["version"], pro["loader"])
                ):
                    status("Применяем скин персонажа…")
                    aliases.ensure_skin_support(folder, pro["version"], pro["loader"])
                    skins.install_local_skin(
                        image, folder, target, s.get("model", "classic")
                    )
                if e4steam.support(pro["version"], pro["loader"]):
                    e4steam.ensure(
                        folder,
                        pro["version"],
                        pro["loader"],
                        status,
                        cache_dir=s.root / "steam-mod-cache",
                    )
                skin_sync.prepare(
                    folder,
                    pro["version"],
                    pro["loader"],
                    image if image.is_file() else None,
                    s.get("model", "classic"),
                )
                if mode == "prism":
                    from launcher import prism

                    self.prism_launches[pro["id"]] = prism.launch(
                        s.root,
                        folder,
                        pro["name"],
                        pro["version"],
                        pro["loader"],
                        memory,
                        status,
                        on_game=lambda child: self._track_game(child, pro, prism=True),
                    )
                    return "Запуск передан Prism. При первом входе заверши настройку аккаунта."
                cache = s.root / "game-cache"
                if mode == "local":
                    s.set("nickname", nick)
                game.install(pro["version"], pro["loader"], cache, folder, status)
                if mode == "local":
                    process = game.launch_local(
                        cache, folder, nick, memory_mb=memory, status=status
                    )
                else:
                    process = game.launch(
                        cache, folder, session, memory_mb=memory, status=status
                    )
                self._track_game(process, pro)
                return f"Запущен Minecraft {pro['version']} · {pro['name']}."

            return self.start_job(play)
        elif action == "folder":
            folder = s.folder(p["id"])
            if sys.platform == "win32":
                os.startfile(str(folder))
            else:
                subprocess.Popen(
                    ["open" if sys.platform == "darwin" else "xdg-open", str(folder)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        else:
            raise ValueError("Действие не найдено.")
        return {"ok": True}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    server_version = "BlockHarbor"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(45)

    @property
    def app(self):
        return self.server.app

    def reply(
        self, code, body, content="application/json; charset=utf-8", cache="no-store"
    ):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https://cdn.modrinth.com; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def trusted(self, need_token):
        host = "127.0.0.1:" + str(self.server.server_port)
        if (
            self.headers.get("Host") != host
            or self.headers.get("Origin", "http://" + host) != "http://" + host
        ):
            self.reply(403, {"error": "Доступ только из локального окна."})
            return False
        if need_token:
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(
                supplied.encode(), "Bearer ".encode() + self.app.token.encode()
            ):
                self.reply(
                    403, {"error": "Открой BlockHarbor.exe, чтобы восстановить сессию."}
                )
                return False
            self.app.last_seen = time.monotonic()
        return True

    def do_GET(self):
        u = urlsplit(self.path)
        attachment = u.path.startswith("/api/build-download/")
        content_icon = u.path.startswith("/api/content-icon/")
        if not self.trusted(
            u.path not in STATIC and not attachment and not content_icon
        ):
            return
        try:
            if attachment:
                ticket = u.path.removeprefix("/api/build-download/")
                cookie = SimpleCookie(self.headers.get("Cookie", "")).get("bh_export")
                try:
                    file, name = self.app.downloads.take(
                        ticket, cookie.value if cookie else ""
                    )
                except ValueError as error:
                    return self.reply(403, {"error": str(error)})
                with file:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header(
                        "Content-Disposition",
                        "attachment; filename=BlockHarbor.zip; filename*=UTF-8''"
                        + quote(name, safe=""),
                    )
                    self.send_header(
                        "Content-Length", str(os.fstat(file.fileno()).st_size)
                    )
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    shutil.copyfileobj(file, self.wfile, 1024 * 1024)
                return
            if content_icon:
                try:
                    if not u.path.endswith(".png"):
                        raise ValueError("Изображение не найдено.")
                    digest = u.path.removeprefix("/api/content-icon/").removesuffix(
                        ".png"
                    )
                    image = self.app.store.icons.read(digest)
                except (OSError, ValueError):
                    return self.reply(404, b"", "image/png")
                return self.reply(
                    200,
                    image,
                    "image/png",
                    cache="private, max-age=31536000, immutable",
                )
            if u.path in STATIC:
                path = HERE / "ui" / STATIC[u.path]
                self.reply(200, path.read_bytes(), MIME[path.suffix])
            elif u.path == "/api/state":
                with self.app.lock:
                    state = self.app.state()
                self.reply(200, state)
            elif u.path == "/api/modrinth/filters":
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                options = modrinth.catalog_filters.schema(q["kind"])
                self.reply(
                    200,
                    modrinth.dependency_suggestions(q["q"]) if "q" in q else options,
                )
            elif u.path == "/api/steam/check":
                q = parse_qs(u.query)
                self.reply(200, steam_network.check(self.app.store, q["id"][0]))
            elif u.path == "/api/modrinth/check":
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                result = modrinth.check(
                    self.app.store, q["id"], q["kind"], q["project"], q["version"]
                )
                self.reply(200, result)
            elif u.path in ("/api/modrinth/search", "/api/modrinth/versions"):
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                store = self.app.store
                if u.path.endswith("/search"):
                    result = modrinth.search(
                        store.profile(q["id"]),
                        q["kind"],
                        q.get("q", ""),
                        q.get("offset", 0),
                        json.loads(q.get("filters", "{}")),
                        q.get("sort", "relevance"),
                    )
                else:
                    result = modrinth.versions(store, q["id"], q["kind"], q["project"])
                self.reply(200, result)
            elif u.path == "/api/job":
                self.reply(200, self.app.job_snapshot())
            elif u.path == "/api/releases":
                force = parse_qs(u.query).get("refresh") == ["1"]
                self.reply(200, {"versions": self.app.refresh_releases(force=force)})
            elif u.path == "/api/card":
                store = self.app.store
                real = self.app.player_name()
                image = self.app.skin_path()
                self.reply(
                    200,
                    cards.build_card(
                        real,
                        store.get("nickname") or real,
                        image if image.is_file() else None,
                    ),
                    "application/octet-stream",
                )
            elif u.path == "/api/skin.png":
                self.reply(
                    200, skins.normalized_skin(self.app.skin_path()), "image/png"
                )
            elif u.path == "/api/preview":
                buf = io.BytesIO()
                skins.skin_preview(
                    self.app.skin_path(),
                    parse_qs(u.query).get(
                        "model", [self.app.store.get("model", "classic")]
                    )[0],
                ).save(buf, format="PNG")
                self.reply(200, buf.getvalue(), "image/png")
            else:
                self.reply(404, {"error": "Страница не найдена."})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.reply(400, {"error": str(exc)})

    def do_POST(self):
        if not self.trusted(True):
            return
        try:
            u = urlsplit(self.path)
            length = int(self.headers.get("Content-Length", "-1"))
            if u.path == "/api/build-export":
                if not 0 < length <= 1024:
                    raise ValueError("Некорректный запрос выгрузки.")
                data = json.loads(self.rfile.read(length))
                pid, worlds = data["id"], data.get("worlds") is True
                archive = tempfile.TemporaryFile(dir=self.app.store.root)
                try:
                    with self.app.lock:
                        if self.app.job_snapshot()["busy"]:
                            raise ValueError("Дождись завершения текущей операции.")
                        if worlds:
                            folder = self.app.store.folder(pid)
                            if self.app.profile_running(pid):
                                raise ValueError(
                                    "Закрой эту сборку перед выгрузкой миров."
                                )
                            require_idle_files(folder)
                        name = build_archive.export_build(
                            self.app.store, pid, archive, worlds
                        )
                        ticket, cookie = self.app.downloads.issue(archive, name)
                except BaseException:
                    archive.close()
                    raise
                url = "/api/build-download/" + ticket
                response = json.dumps(
                    {"download": url, "name": name}, ensure_ascii=False
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("Cache-Control", "no-store")
                self.send_header(
                    "Set-Cookie",
                    f"bh_export={cookie}; Path={url}; Max-Age=300; HttpOnly; SameSite=Strict",
                )
                self.end_headers()
                self.wfile.write(response)
                return
            if u.path == "/api/build-import":
                if not 0 < length <= build_archive.MAX_ZIP:
                    raise ValueError("Выбери ZIP сборки размером до 8 ГБ.")
                with tempfile.TemporaryFile(dir=self.app.store.root) as archive:
                    if length > shutil.disk_usage(self.app.store.root).free:
                        raise ValueError("Недостаточно места на диске для ZIP сборки.")
                    remaining = length
                    while remaining:
                        block = self.rfile.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ValueError("Передача ZIP прервана.")
                        archive.write(block)
                        remaining -= len(block)
                    with self.app.lock:
                        if self.app.job_snapshot()["busy"]:
                            raise ValueError("Дождись завершения текущей операции.")
                        result = build_archive.import_build(self.app.store, archive)
                return self.reply(200, result)
            if u.path in ("/api/profile", "/api/skin-preview"):
                if not 0 <= length <= 1024 * 1024:
                    raise ValueError("Файл скина превышает 1 МБ.")
                parameters = {k: v[0] for k, v in parse_qs(u.query).items()}
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Передача скина прервана.")
                if u.path == "/api/skin-preview":
                    preview = io.BytesIO()
                    skins.skin_preview(raw, parameters.get("model", "classic")).save(
                        preview, format="PNG"
                    )
                    return self.reply(200, preview.getvalue(), "image/png")
                with self.app.lock:
                    return self.reply(200, self.app.save_appearance(parameters, raw))
            if u.path == "/api/file":
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                skin = q.get("kind") == "skin"
                card = q.get("kind") == "card"
                limit = 65536 if card else (1024 * 1024 if skin else MAX_ADDON)
                if not 0 < length <= limit:
                    raise ValueError("Недопустимый размер файла.")
                with tempfile.TemporaryDirectory(dir=self.app.store.root) as td:
                    tmp = Path(td) / "upload"
                    with tmp.open("wb") as out:
                        left = length
                        while left:
                            part = self.rfile.read(min(1 << 20, left))
                            if not part:
                                raise ValueError("Передача файла прервана.")
                            out.write(part)
                            left -= len(part)
                    with self.app.lock:
                        if card:
                            return self.reply(200, self.app.import_card(tmp, q["id"]))
                        if skin:
                            skins.validate_skin(tmp)
                            os.replace(tmp, self.app.store.root / "skin.png")
                            self.app.store.set("skinDigest", "")
                        else:
                            inspection = modrinth.manual_check(
                                self.app.store, q["id"], q["kind"], q["name"], tmp
                            )
                            if q.get("check") == "1":
                                return self.reply(200, inspection)
                            if inspection["blocked"]:
                                raise ValueError(" ".join(inspection["conflicts"][:3]))
                            self.app.store.import_file(
                                q["id"], q["kind"], q["name"], tmp
                            )
                            return self.reply(
                                200, {"ok": True, "warnings": inspection["warnings"]}
                            )
                return self.reply(200, {"ok": True})
            if u.path != "/api/action" or not 1 <= length <= 16384:
                raise ValueError("Некорректный запрос.")
            data = json.loads(self.rfile.read(length))
            action = data.pop("action")
            if action == "fitWindow":
                return self.reply(
                    200, {"attached": self.app.window.adjust(data.get("marker"))}
                )
            with self.app.lock:
                result = self.app.action(action, data)
            self.reply(200, result)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.reply(400, {"error": str(exc) or "Операция не удалась."})


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=default_root())
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    # Reopening after an upgrade must select the current window implementation.
    with SingleInstance(args.data / "activation-v10") as instance:
        if not instance.primary:
            return
        app = App(args.data)
        with Server(("127.0.0.1", 0), Handler) as server:
            server.app = app
            url = "http://127.0.0.1:" + str(server.server_port) + "/#" + app.token

            def reopen():
                app.last_seen = time.monotonic()
                app.window.show(url)

            def idle():
                running = app.game_running()
                if args.no_browser or running or app.job_snapshot()["busy"]:
                    return False
                return time.monotonic() - app.last_seen > 180

            instance.monitor(reopen, idle, server.shutdown)
            if args.no_browser:
                print(url, flush=True)
            else:
                reopen()
            server.serve_forever()


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        if sys.platform != "win32":
            raise
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, str(e), "BlockHarbor", 16)
