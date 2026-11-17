"""Read local e4steam state and repair specific per-instance hosting settings."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path
from urllib.request import getproxies

from . import e4steam
from .modfiles import file_hash, file_stamp

_CONFIG = Path("config/e4steam/e4steam.toml")
_APPID = Path("steam_appid.txt")
_LOG = Path("logs/latest.log")
_HOST_LINE = re.compile(
    rb"""(?m)^([ \t]*(?:hostEnabled|"hostEnabled"|'hostEnabled')[ \t]*=[ \t]*)false(?=[ \t]*(?:\#|\r?$))"""
)

# Literal markers from the pinned upstream release. Never return raw log text:
# its addresses are temporary invitation tokens and Steam IDs identify players.
_MARKERS = {
    "Steam Networking Messages initialized as ": "runtime",
    "Steam LAN share ready for Steam user ": "host",
    "Opened a local Minecraft bridge to Steam user ": "guest",
    "Steam client bridge failed": "join",
    "Steam Networking Messages session with ": "transport",
    "Steam could not create the lobby (": "retry",
    "continuing friends-only sharing by address": "address",
    "Steam lobby creation failed:": "lobby",
    "Steam rejected e4steam lobby metadata": "lobby",
    "SteamAPI_Init failed": "signin",
    "Steam is not running or the current user is not signed in": "signin",
    "expected App ID 480 but found": "appid",
    "Could not verify ownership of the native cache parent": "cache",
    "Native cache entry is owned by another account": "cache",
    "Could not load steam_api and steamworks4j native libraries": "native",
}
_LOG_TEXT = {
    "none": "У сборки пока нет журнала. Войди в настольный Steam и запусти сборку через BlockHarbor.",
    "unknown": "В прочитанной части последнего журнала нет сведений о соединении e4steam.",
    "runtime": "Steam API подключился. Хозяин может открыть одиночный мир для друзей.",
    "host": "e4steam открыл мир через Steam. Используй приглашение или зелёный адрес этого сеанса.",
    "guest": "e4steam открыл локальное соединение к Steam хозяина. Завершение входа подтверждается в самом мире.",
    "signin": "Minecraft не подключился к Steam. Войди в Steam, затем запусти сборку из BlockHarbor. Обе программы открой от одного пользователя Windows с обычными правами.",
    "retry": "Steam повторяет создание лобби. Оставь мир открытым и дождись зелёного адреса в чате.",
    "address": "Лобби приглашений недоступно, но e4steam продолжает делиться миром по адресу. В режиме «Для друзей Steam» друг может вставить новый зелёный адрес в «Прямое подключение».",
    "lobby": "Steam не создал лобби. Открой мир заново в режиме «Для друзей Steam» и дождись зелёного адреса. Проверь, что на обоих компьютерах сеть пропускает игровой трафик Steam.",
    "transport": "Последняя попытка связи с участником завершилась ошибкой транспорта Steam. Проверь игровой трафик Steam на обоих компьютерах и повтори вход с актуальным адресом.",
    "join": "Гостю не удалось открыть соединение. Сверь сборки и режим доступа, затем попроси у хозяина приглашение или адрес текущего сеанса.",
    "appid": "e4steam обнаружил чужой App ID в папке сборки. После закрытия игры здесь можно восстановить ID 480.",
    "cache": "e4steam не смог проверить владельца кэша нативных библиотек. Войди в Steam под той же учётной записью Windows, из которой запускаешь BlockHarbor.",
    "native": "Не загрузились нативные библиотеки Steam. Проверь историю защиты Windows для этой сборки и используй 64-битную Java.",
}


def _local(folder, rel):
    path = folder / rel
    if path.resolve() != path:
        raise ValueError(
            "Настройки Steam или журнал сборки перенаправлены за пределы обычного каталога."
        )
    return path


def _read_small(folder, rel, limit):
    path = _local(folder, rel)
    if not path.is_file():
        return None
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(
            "Файл настроек Steam слишком велик для автоматической проверки."
        )
    return raw


def _settings(folder):
    appid = _read_small(folder, _APPID, 4096)
    cfg = _read_small(folder, _CONFIG, 256 * 1024)
    try:
        host = (
            tomllib.loads(cfg.decode("utf-8-sig")).get("hostEnabled", True)
            if cfg is not None
            else True
        )
    except (ValueError, UnicodeError) as exc:
        raise ValueError(
            "Не удалось прочитать настройки e4steam в config/e4steam/e4steam.toml."
        ) from exc
    problems = []
    if host is False:
        problems.append("host")
    if appid is not None and appid.strip() != b"480":
        problems.append("appid")
    return problems, cfg, appid


def _latest(folder):
    path = _local(folder, _LOG)
    if not path.is_file():
        return "none", None
    with path.open("rb") as stream:
        length = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, length - 512 * 1024))
        tail = stream.read(512 * 1024).decode("utf-8", errors="replace")
    state = "unknown"
    for line in tail.splitlines():
        for marker, kind in _MARKERS.items():
            if marker in line and (kind != "transport" or "failed (reason" in line):
                state = kind
    return state, path.stat().st_mtime


def _mods(folder, profile):
    mods = e4steam._mods_folder(folder)
    paths = (
        sorted(
            (p for p in mods.iterdir() if p.suffix.lower() == ".jar"),
            key=lambda p: p.name,
        )
        if mods.is_dir()
        else []
    )
    if any(p.is_symlink() or not p.is_file() for p in paths):
        raise ValueError(
            "В mods есть ссылка или нечитаемый JAR. Код сравнения недоступен."
        )
    # Game/loader version and active archive contents matter; installation path,
    # nickname and Jar filenames don't. Duplicate copies count separately.
    hashes = sorted(file_hash(p, file_stamp(p), "sha256") for p in paths)
    value = (
        hashlib.sha256(
            "\n".join(
                ["BlockHarbor mods 1", profile["version"], profile["loader"], *hashes]
            ).encode()
        )
        .hexdigest()[:16]
        .upper()
    )
    return len(hashes), "-".join(value[i : i + 4] for i in range(0, 16, 4))


def check(store, pid):
    from . import game

    profile = store.profile(pid)
    folder = store.folder(pid).resolve()
    version, loader = profile["version"], profile["loader"]
    supported = e4steam.support(version, loader)
    ready = e4steam.installed(folder, version, loader) if supported else False
    try:
        issues, _, _ = _settings(folder) if supported else ([], None, None)
        config_note = ""
    except (ValueError, OSError) as exc:
        issues, config_note = (
            [],
            str(exc)
            if isinstance(exc, ValueError)
            else "Настройки Steam недоступны для чтения.",
        )
    try:
        state, mtime = _latest(folder)
        log = _LOG_TEXT[state]
    except (ValueError, OSError):
        state, mtime, log = "unknown", None, "Журнал этой сборки недоступен для чтения."
    try:
        count, code = _mods(folder, profile)
        mod_note = ""
    except (ValueError, OSError):
        count, code, mod_note = (
            None,
            "",
            "Код сравнения недоступен. Проверь, что все JAR в папке mods доступны.",
        )
    install = game._record(folder)
    pin = (
        install.get("loader_version")
        if (install.get("loader"), install.get("version")) == (loader, version)
        else ""
    )
    return {
        "profile": profile["name"],
        "version": version,
        "loader": loader,
        "loaderVersion": pin or "",
        "supported": supported,
        "ready": ready,
        "coreVersion": e4steam.CORE_VERSION,
        "addonVersion": e4steam.ADDON_VERSION,
        "issues": issues,
        "configNote": config_note,
        "state": state,
        "log": log,
        "logTime": mtime,
        "modCount": count,
        "modCode": code,
        "modNote": mod_note,
        "downloadProxy": any(
            getproxies().get(kind) for kind in ("http", "https", "all")
        ),
    }


def repair_settings(folder):
    folder = Path(folder).resolve()
    issues, cfg, appid = _settings(folder)
    changes = []
    if "host" in issues:
        updated, n = _HOST_LINE.subn(rb"\g<1>true", cfg, count=1)
        if (
            n != 1
            or tomllib.loads(updated.decode("utf-8-sig")).get("hostEnabled") is not True
        ):
            raise ValueError(
                "Параметр hostEnabled в нестандартном формате. Используй настройки e4steam в игре."
            )
        changes.append((_local(folder, _CONFIG), cfg, updated))
    if "appid" in issues:
        changes.append((_local(folder, _APPID), appid, b"480\n"))
    if not changes:
        return "Настройки открытия мира уже готовы."
    backups = _local(folder, Path("blockharbor-backups/steam"))
    backups.mkdir(parents=True, exist_ok=True)
    # Preserve custom values in a unique backup; verify originals again before replacement.
    saved = Path(tempfile.mkdtemp(prefix="settings-", dir=backups))
    written = []
    try:
        for path, original, updated in changes:
            backup = saved / path.name
            backup.write_bytes(original)
            if path.read_bytes() != original:
                raise ValueError(
                    "Настройки изменились во время исправления. Повтори проверку."
                )
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
                tmp.write(updated)
                staged = Path(tmp.name)
            try:
                os.replace(staged, path)
            finally:
                staged.unlink(missing_ok=True)
            written.append((path, backup, updated))
    except (OSError, ValueError):
        for path, backup, updated in reversed(written):
            if path.read_bytes() == updated:
                shutil.copyfile(backup, path)
        raise
    return "Настройки открытия мира исправлены. Копия сохранена в blockharbor-backups/steam."
