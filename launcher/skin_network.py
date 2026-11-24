"""Publish local appearances and configure CSL receivers before a game launch.

Minecraft's local profile has no authenticated name authority. A random owner
key prevents an unrelated launcher from overwriting an already claimed name;
Microsoft profiles retain the official Mojang skin source ahead of this API.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import aliases, lifecycle, skins, skin_sync
from .community_catalog import ORIGIN
from .version import VERSION

ROOT = ORIGIN + "/v1/skins/"
_SOURCE = "BlockHarbor shared skins"
_LOCK = threading.RLock()
_MAX_REPLY = 4096
_REFRESH = 10 * 60
_RETRY = 60


class SkinNetworkError(ValueError):
    pass


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SkinAPI:
    origin = ORIGIN

    def request(self, method, name, token, pixels=None, model="classic"):
        name = skins.validate_nickname(name)
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            raise SkinNetworkError("Не удалось прочитать ключ общего скина.")
        lifecycle.check()
        headers = {"User-Agent": "BlockHarbor/" + VERSION,
                   "Accept": "application/json", "Authorization": "Bearer " + token}
        if pixels is not None:
            headers.update({"Content-Type": "image/png", "X-Skin-Model": model})
        request = Request(self.origin + "/v1/skins/profile/" + name,
                          method=method, data=pixels, headers=headers)
        try:
            # A cosmetic outage must not turn game preparation into a retry
            # queue. No redirects (owner credentials) and one bounded attempt.
            with lifecycle.response(build_opener(_NoRedirects()).open(request, timeout=6)) as response:
                raw = response.read(_MAX_REPLY + 1)
            if len(raw) > _MAX_REPLY:
                raise SkinNetworkError("Сервис скинов вернул слишком большой ответ.")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 409:
                reason = "Этот ник уже используется другим профилем BlockHarbor. Для общего скина выбери другой ник."
            elif code == 429:
                reason = "Общий скин не обновлён: достигнут лимит сервиса. Повтори позже."
            elif code in (401, 403):
                reason = "Сервис отклонил ключ общего скина. Локальный скин сохранён."
            else:
                reason = "Сервис общих скинов временно недоступен. Локальный скин сохранён."
            raise SkinNetworkError(reason) from None
        except (OSError, HTTPException) as exc:
            lifecycle.check()
            raise SkinNetworkError("Не удалось передать скин друзьям. Игра запустится с локальным скином; передача повторится при следующем запуске.") from exc
        except (ValueError, TypeError) as exc:
            if isinstance(exc, SkinNetworkError):
                raise
            raise SkinNetworkError("Сервис не подтвердил обновление общего скина.") from exc


def _managed_local(source):
    return (isinstance(source, dict) and source.get("type") == "Legacy"
            and re.fullmatch(r"BlockHarbor [a-z0-9_]{3,16}", str(source.get("name", "")))
            and re.fullmatch(r"LauncherSkins/[a-z0-9_]{3,16}/skin-\{USERNAME\}\.png",
                             str(source.get("skin", ""))))


def configure_receiver(instance_dir, nickname, *, local_profile=True, has_skin=False):
    """Use one owned local texture plus the shared API, preserving other sources."""
    name = skins.validate_nickname(nickname) if nickname else ""
    instance = Path(instance_dir).resolve()
    config = instance / "CustomSkinLoader" / "CustomSkinLoader.json"
    if (config.is_symlink() or config.parent.resolve() != config.parent
            or not config.resolve().is_relative_to(instance)):
        raise skins.SkinError("Файл настроек загрузчика скинов перенаправлен.")
    if config.exists():
        if config.stat().st_size > 2 * 1024**2:
            raise skins.SkinError("Файл настроек загрузчика скинов слишком велик.")
        try:
            data = json.loads(config.read_text("utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise skins.SkinError("Не удалось прочитать настройки загрузчика скинов.") from exc
        if not isinstance(data, dict) or not isinstance(data.get("loadlist", []), list):
            raise skins.SkinError("Проверь формат настроек загрузчика скинов.")
    else:
        data = {"loadlist": [{"name": "Mojang", "type": "MojangAPI"}],
                "forceLoadAllTextures": False}
    original = json.dumps(data, ensure_ascii=False, sort_keys=True)
    own, others = [], []
    for source in data.get("loadlist", []):
        if isinstance(source, dict) and source.get("name") == _SOURCE:
            continue
        if _managed_local(source):
            if has_skin and source["name"] == "BlockHarbor " + name.lower():
                own.append(source)
            # Do not keep another launcher's old local nickname as a higher
            # priority texture for a friend now using that name.
            continue
        others.append(source)
    shared = {"name": _SOURCE, "type": "CustomSkinAPI", "root": ROOT}
    if local_profile:
        data["loadlist"] = own + [shared] + others
    else:
        official = [site for site in others if isinstance(site, dict)
                    and site.get("type") in ("MojangAPI", "GameProfile")]
        fallback = [site for site in others if site not in official]
        # Preserve the owner's saved local preview for themselves; third-party
        # local names never override other authenticated Java profiles.
        data["loadlist"] = own + (official or [{"name": "Mojang", "type": "MojangAPI"}]) + [shared] + fallback
    data.setdefault("enableLocalProfileCache", True)
    if json.dumps(data, ensure_ascii=False, sort_keys=True) != original or not config.exists():
        skins._write_inside(instance, config,
                            (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())
    return config


def _identity(store, origin, name):
    with store.connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS skin_network_identity (
            origin TEXT NOT NULL, profile TEXT NOT NULL, token TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            digest TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
            synced_at REAL NOT NULL DEFAULT 0, attempted_at REAL NOT NULL DEFAULT 0,
            attempt_key TEXT NOT NULL DEFAULT '', PRIMARY KEY(origin,profile))""")
        db.execute("INSERT OR IGNORE INTO skin_network_identity(origin,profile,token) VALUES (?,?,?)",
                   (origin, name.lower(), secrets.token_hex(32)))
        return dict(db.execute("SELECT * FROM skin_network_identity WHERE origin=? AND profile=?",
                               (origin, name.lower())).fetchone())


def synchronize(store, nickname, image=None, model="classic", *, api=None, now=None):
    """Refresh at most every ten minutes, immediately after an appearance edit."""
    api = api or SkinAPI()
    now = time.time() if now is None else now
    name = skins.validate_nickname(nickname)
    pixels = skin_sync._texture(image, model) if image is not None else None
    digest = hashlib.sha256(pixels).hexdigest() if pixels is not None else ""
    desired = name.lower() + ":" + digest + ":" + model
    with _LOCK:
        # Independent launcher windows may be playing under different names.
        # Their owner keys must not rename each other's still-active record.
        row = _identity(store, api.origin, name)
        current = row["username"].lower() + ":" + row["digest"] + ":" + row["model"]
        if (pixels is not None and current == desired and 0 <= now-row["synced_at"] < _REFRESH):
            return {"shared": True, "cached": True}
        if pixels is None and not row["username"]:
            return {"shared": False, "cached": True}
        if row["attempt_key"] == desired and 0 <= now-row["attempted_at"] < _RETRY:
            return {"shared": False, "deferred": True}
        with store.connection() as db:
            db.execute("UPDATE skin_network_identity SET attempted_at=?,attempt_key=? WHERE origin=? AND profile=?",
                       (now, desired, api.origin, row["profile"]))
        if pixels is None:
            result = api.request("DELETE", row["username"], row["token"])
            if result.get("removed") is not True:
                raise SkinNetworkError("Сервис не подтвердил снятие старого скина.")
            name, model = "", ""
        else:
            result = api.request("PUT", name, row["token"], pixels, model)
            if (result.get("username", "").lower() != name.lower()
                    or result.get("sha256") != digest or result.get("model") != model):
                raise SkinNetworkError("Сервис не подтвердил обновление общего скина.")
        with store.connection() as db:
            db.execute("UPDATE skin_network_identity SET username=?,digest=?,model=?,synced_at=? WHERE origin=? AND profile=?",
                       (name, digest, model, now, api.origin, row["profile"]))
        return {"shared": pixels is not None, "cached": False}


def prepare_player(store, instance_dir, version, loader, nickname, image=None,
                   model="classic", *, mode="local", status=None, notify=None):
    """Best-effort cosmetics: install for observers too; never block game startup."""
    if aliases.skin_release(version, loader) is None:
        return {"supported": False}
    try:
        if status:
            status("Готовим скины игроков…")
        aliases.ensure_skin_support(instance_dir, version, loader)
        if image is not None and nickname:
            skins.install_local_skin(image, instance_dir, nickname, model)
        configure_receiver(instance_dir, nickname, local_profile=mode == "local",
                           has_skin=image is not None)
        # Online accounts use their authenticated Minecraft profile. Do not
        # claim an unauthenticated shared name on their behalf.
        result = synchronize(store, nickname, image, model) if mode == "local" else {"shared": False}
        return {"supported": True, **result}
    except (OSError, ValueError) as exc:
        lifecycle.check()
        message = str(exc) if isinstance(exc, (SkinNetworkError, skins.SkinError)) else "Не удалось подготовить общие скины. Запуск игры продолжен."
        if notify:
            event = hashlib.sha256((nickname + "\0" + message).encode()).hexdigest()[:24]
            notify(message, "error", event_key="skin-network:" + event + ":" + str(int(time.time()) // 600))
        return {"supported": True, "shared": False, "warning": message}
