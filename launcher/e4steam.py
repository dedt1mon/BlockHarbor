"""Install the pinned e4steam client, Public Servers addon and matched Fabric API."""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import ssl
import tempfile
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import lifecycle

from .modfiles import file_hash as _file_hash
from .modfiles import file_stamp as _file_stamp
from .modfiles import mod_info as _mod_info
from .modfiles import publish_new as _publish
from .version import VERSION

_HERE = Path(__file__).resolve().parent
_BUNDLED = _HERE.parent / "bundled" / "e4steam"
_CATALOG = json.loads((_HERE / "e4steam_catalog.json").read_text(encoding="utf-8"))
CORE_VERSION = _CATALOG["core"]
ADDON_VERSION = _CATALOG["addon"]
_LOCK = threading.RLock()
_TITLES = {"core": "e4steam", "addon": "Public Servers", "fabric-api": "Fabric API"}
_MOD_IDS = {
    "core": {"e4steam", "e4steam_minecraft"},
    "addon": {"e4steam_public_servers"},
    "fabric-api": {"fabric-api", "fabric", "quilted_fabric_api"},
}


@dataclass(frozen=True)
class Asset:
    role: str
    filename: str
    size: int
    algorithm: str
    digest: str
    urls: tuple[str, ...]


def _numbers(version):
    return tuple(int(n) for n in version.split("."))


def _asset(role, key):
    data = _CATALOG["files"][key]
    digest_type = "sha256" if "sha256" in data else "sha512"
    urls = [data["url"]]
    if "github_asset_id" in data:
        repo = urlsplit(urls[0]).path.split("/releases/download/")[0]
        urls.append(
            f"https://api.github.com/repos{repo}/releases/assets/{data['github_asset_id']}"
        )
    return Asset(
        role,
        data["filename"],
        data["size"],
        digest_type,
        data[digest_type],
        tuple(urls),
    )


@lru_cache(maxsize=256)
def _plan(version, loader):
    if version not in _CATALOG["fabric_api"]:
        return ()
    mc = _numbers(version)
    assets = []
    for branch in _CATALOG["branches"]:
        if loader in branch["loaders"] and _numbers(
            branch["minimum"]
        ) <= mc <= _numbers(branch["maximum"]):
            if branch["asset"] not in _CATALOG["files"]:
                return ()
            assets.append(_asset(branch["role"], branch["asset"]))
    if len(assets) != 2 or {a.role for a in assets} != {"core", "addon"}:
        return ()
    if loader in ("fabric", "quilt"):
        api = _CATALOG["fabric_api"][version]
        if api not in _CATALOG["files"]:
            return ()
        assets.append(_asset("fabric-api", api))
    return tuple(assets)


def support(version: str, loader: str) -> bool:
    return bool(_plan(version, loader))


def compatibility(version: str, loader: str, *, acknowledged=False):
    """Describe bundled Steam support without downloading or inspecting an instance."""
    supported = support(version, loader)
    reason = "supported" if supported else "vanilla" if loader == "vanilla" else "unsupported"
    message = (
        "Для этой версии доступна встроенная игра с друзьями через Steam."
        if supported else
        "В Vanilla нет встроенной игры через Steam. Можно продолжить без Steam, сохранив выбранную версию и загрузчик."
        if loader == "vanilla" else
        "Для этой версии и загрузчика нет проверенного совместимого комплекта Steam/e4steam. Можно продолжить без Steam."
    )
    return {"version": version, "loader": loader, "supported": supported,
            "canContinue": True, "reason": reason, "message": message,
            "acknowledged": acknowledged is True}


def notice_acknowledged(settings, pid, version, loader):
    return settings.get("steamNotice:" + pid) == version + "|" + loader


def notice_permission(params):
    allowed = params.get("allowWithoutSteam", False)
    if type(allowed) is not bool:
        raise ValueError("Подтверждение продолжения без Steam должно быть true или false.")
    return allowed


def acknowledge_notice(store, pid, version, loader):
    if not support(version, loader):
        store.set("steamNotice:" + pid, version + "|" + loader)


def protected_names(version: str, loader: str) -> frozenset[str]:
    return frozenset(
        a.filename for a in _plan(version, loader) if a.role != "fabric-api"
    )


def _mods_folder(instance):
    parent = Path(instance).resolve()
    mods = parent / "mods"
    if mods.resolve() != mods:
        raise ValueError(
            "Папка модов находится за пределами сборки или перенаправлена."
        )
    return mods


def _verified(file, asset):
    if not file.is_file() or file.is_symlink():
        return False
    stamp = _file_stamp(file)
    return (
        stamp[0] == asset.size
        and _file_hash(file, stamp, asset.algorithm) == asset.digest
    )


def _active_files(mods, assets, version, loader):
    """Map each expected component to an existing compatible jar, detecting duplicates."""
    active = {}
    if not mods.exists():
        return active
    for file in mods.iterdir():
        if file.suffix.lower() != ".jar" or not file.is_file():
            continue
        ids, mod_version = _mod_info(file, _file_stamp(file))
        for asset in assets:
            named = file.name.casefold() == asset.filename.casefold()
            if not named and not ids.intersection(_MOD_IDS[asset.role]):
                continue
            title = _TITLES[asset.role]
            if asset.role in active:
                raise ValueError(
                    f"В сборке две версии {title}: {active[asset.role].name} и {file.name}. Оставь один совместимый JAR."
                )
            same_game = bool(re.search(rf"(?:\+|-){re.escape(version)}$", mod_version))
            expected_id = "fabric-api" if _numbers(version) >= (26, 1) else "fabric"
            compatible_api = (
                asset.role == "fabric-api"
                and not named
                and not file.is_symlink()
                and expected_id in ids
                and (loader == "quilt" or "quilted_fabric_api" not in ids)
                and same_game
            )
            if not compatible_api and not _verified(file, asset):
                raise ValueError(
                    f"В сборке уже есть другой файл {title}: {file.name}. Чтобы установить встроенную версию, "
                    "перемести конфликтующий JAR из папки mods и повтори."
                )
            active[asset.role] = file
    return active


def installed(instance_dir, version: str, loader: str, *, sizes=None) -> bool:
    assets = _plan(version, loader)
    if not assets or (
        sizes is not None
        and any(a.size not in sizes for a in assets if a.role != "fabric-api")
    ):
        return False
    try:
        return len(
            _active_files(_mods_folder(instance_dir), assets, version, loader)
        ) == len(assets)
    except (ValueError, OSError):
        return False


def _obtain(asset, cache, status):
    lifecycle.check()
    bundled = _BUNDLED / asset.filename
    if _verified(bundled, asset):
        return bundled
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (asset.digest + ".jar")
    if _verified(target, asset):
        return target
    title = _TITLES[asset.role]
    headers = {"User-Agent": f"BlockHarbor/{VERSION}", "Accept": "application/octet-stream"}
    attempts = list(asset.urls)
    reason = "Проверь соединение с сервером загрузки."
    for number, url in enumerate(attempts):
        lifecycle.check()
        status(
            f"Загружаем {title} для Steam…"
            if number == 0
            else f"Повторяем загрузку {title}…"
        )
        tmp = None
        retry = False
        try:
            with tempfile.NamedTemporaryFile(
                dir=cache, suffix=".part", delete=False
            ) as out:
                tmp = Path(out.name)
                with lifecycle.response(urlopen(Request(url, headers=headers), timeout=40)) as source:
                    remaining = asset.size
                    while remaining:
                        block = source.read(min(256 * 1024, remaining))
                        if not block:
                            break
                        out.write(block)
                        remaining -= len(block)
                    if remaining:
                        raise EOFError
                    if source.read(1):
                        reason = (
                            "Размер загрузки отличается от опубликованного размера."
                        )
                        continue
            if not _verified(tmp, asset):
                reason = "Контрольная сумма файла не совпадает с опубликованной."
                continue
            with lifecycle.guard():
                os.replace(tmp, target)
            return target
        except HTTPError as exc:
            exc.close()
            if exc.code == 407:
                reason = "Прокси требует авторизации. Проверь учётные данные в настройках сети."
                break
            reason = f"Сервер загрузки вернул HTTP {exc.code}."
            retry = exc.code in (408, 429, 500, 502, 503, 504)
        except (EOFError, http.client.IncompleteRead, ConnectionError, TimeoutError):
            reason = (
                "Передача прервалась. Проверь маршрут к серверу загрузки и повтори."
            )
            retry = True
        except URLError as exc:
            if "407" in str(exc.reason):
                reason = "Прокси требует авторизации. Проверь учётные данные в настройках сети."
                break
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                reason = "Windows не смогла подтвердить сертификат сервера. Проверь время и настройки HTTPS-прокси."
                break
            reason = "Сервер загрузки недоступен. Проверь интернет и настройки прокси для приложений."
            retry = True
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        # A mirror may already succeed on its first try. Retry the failed
        # endpoint once for a temporary transport error.
        if retry and number < len(asset.urls):
            attempts.append(url)
    raise ValueError(f"Не удалось получить проверенный {title}. {reason}")


def ensure_if_supported(
    instance_dir, version: str, loader: str, status=lambda _: None, *, cache_dir=None
) -> list[Path]:
    """Steam is optional for Minecraft; actual preparation errors still propagate."""
    if not support(version, loader):
        status("Для выбранной версии встроенный Steam недоступен. Продолжаем без Steam.")
        return []
    return ensure(instance_dir, version, loader, status, cache_dir=cache_dir)


def ensure(
    instance_dir, version: str, loader: str, status=lambda _: None, *, cache_dir=None
) -> list[Path]:
    """Prepare a playable set as one operation. Existing user files are never overwritten."""
    assets = _plan(version, loader)
    if not assets:
        raise ValueError(
            f"Для Minecraft {version} · {loader} нет подходящего комплекта e4steam."
        )
    mods = _mods_folder(instance_dir)
    cache = (
        Path(cache_dir)
        if cache_dir is not None
        else Path(instance_dir).resolve().parent / ".blockharbor-steam-cache"
    )
    with _LOCK:
        mods.mkdir(parents=True, exist_ok=True)
        active = _active_files(mods, assets, version, loader)
        missing = [asset for asset in assets if asset.role not in active]
        if not missing:
            return [active[asset.role] for asset in assets]
        with tempfile.TemporaryDirectory(
            dir=mods, prefix=".blockharbor-steam-"
        ) as temp:
            stage = Path(temp)
            staged = {}
            for asset in missing:
                payload = _obtain(asset, cache, status)
                part = stage / asset.role
                shutil.copyfile(payload, part)
                if not _verified(part, asset):
                    raise ValueError(
                        f"Файл {_TITLES[asset.role]} изменился во время копирования. Повтори подготовку."
                    )
                staged[asset.role] = part
            # A user may have imported a jar while a large dependency was downloading.
            active = _active_files(mods, assets, version, loader)
            if any(a.role not in active and a.role not in staged for a in assets):
                raise ValueError(
                    "Во время подготовки изменилось содержимое папки mods. Повтори запуск."
                )
            with lifecycle.guard():
                written = []
                try:
                    for asset in missing:
                        if asset.role in active:
                            continue
                        output = mods / asset.filename
                        _publish(staged[asset.role], output)
                        written.append((output, asset))
                        active[asset.role] = output
                except OSError:
                    for file, asset in reversed(written):
                        if _verified(file, asset):
                            file.unlink()
                    raise
        return [active[asset.role] for asset in assets]
