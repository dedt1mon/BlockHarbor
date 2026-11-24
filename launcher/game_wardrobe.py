"""Expose saved skins to real in-game selectors, without rewriting a live config.

Skinss+ reads its PNG directory when its /skinss menu opens. Quick Skin 5.0.2
also watches its upload directory while the menu is open and applies selections
through its renderer. Copying a PNG makes a choice available; it does not claim
that the running player has already changed skin.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tempfile
import threading
import uuid
from pathlib import Path

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from . import aliases, compatibility, lifecycle, modfiles, skin_sync, skins

_LOCK = threading.RLock()
_MARKER = ".blockharbor-wardrobe.json"
_MAX_MARKER = 128 * 1024
_QUICKSKIN = aliases.ModRelease(
    "Quick Skin",
    frozenset({"quickskin"}),
    "https://cdn.modrinth.com/data/zAIE84Ch/versions/Y863HK0w/Quick%20Skin%20-%20NeoForge%20-%201.21.1-5.0.2.jar",
    6170718,
    "c61cde1fdfbfc25e270240d9aa9ad33d2b550393eca137ff1f4fd0ceb5797af53ddee2cd658df55caf7eaf954b65d61f5cbff740639afa42a2e6123276e94856",
)
_ARCHITECTURY = aliases.ModRelease(
    "Architectury API",
    frozenset({"architectury"}),
    "https://cdn.modrinth.com/data/lhGA9TYQ/versions/xGfBrBzj/architectury-13.0.12-neoforge.jar",
    584731,
    "286127f99708d1e2723e1dcc9104ebcab258f04db92040fc2394e5b7faf36d731ef3b84e592d94ec0d0f7f899eca84f806b1e012223da4894d4d8af5030ffe0f",
)


def provider(version, loader):
    if (version, loader) == ("1.21.1", "neoforge"):
        return "quickskin"
    if skin_sync.support(version, loader):
        return "skinss"
    return None


def _inside(instance, relative):
    path = instance / relative
    if path.resolve() != path or path.is_symlink():
        raise skins.SkinError("Папка гардероба в сборке перенаправлена. Проверь её перед синхронизацией.")
    return path


def _matches(path, release):
    return (path.is_file() and not path.is_symlink()
            and path.stat().st_size == release.size
            and modfiles.file_hash(path, modfiles.file_stamp(path), "sha512") == release.sha512)


def _architectury_compatible(path):
    metadata = compatibility._metadata(path, modfiles.file_stamp(path), "neoforge")
    version = metadata.provided.get("architectury", ("", ""))[1]
    if not re.fullmatch(r"13\.0\.[0-9]+", version) or int(version.rsplit(".", 1)[1]) < 6:
        return False
    bounds = [limits for relation, name, limits in metadata.rules
              if relation == "required" and name == "minecraft"]
    return bool(bounds) and all(compatibility._any_match("1.21.1", limits) is True for limits in bounds)


def _quickskin_files(instance, *, install=False, status=lambda _: None):
    """Download official files only at launch; never replace an owner's mod.

    Quick Skin's license permits resolving modpack dependencies from its official
    distribution channels, and forbids bundling the binary in this launcher.
    """
    mods = _inside(instance, "mods")
    if not mods.exists() and not install:
        return None
    mods.mkdir(parents=True, exist_ok=True)
    archives = [(path, modfiles.mod_info(path, modfiles.file_stamp(path))[0])
                for path in mods.iterdir()
                if path.is_file() and path.name.lower().endswith((".jar", ".jar.disabled"))]
    active = [(path, ids) for path, ids in archives if path.suffix.lower() == ".jar"]
    targets, pending = [], []
    for release in (_ARCHITECTURY, _QUICKSKIN):
        found = [path for path, ids in active if ids & release.mod_ids]
        if len(found) > 1:
            raise skins.SkinError(f"В сборке несколько версий {release.title}. Оставь одну совместимую версию.")
        if found:
            path = found[0]
            if (path.is_symlink() or not (_matches(path, release)
                    or release == _ARCHITECTURY and _architectury_compatible(path))):
                raise skins.SkinError(f"Для гардероба нужна совместимая версия {release.title}; существующий файл {path.name} сохранён.")
            targets.append(path)
        else:
            disabled = [path for path, ids in archives
                        if path.name.lower().endswith(".jar.disabled") and ids & release.mod_ids]
            if disabled:
                raise skins.SkinError(f"{release.title} отключён в сборке ({disabled[0].name}). Включи его в списке модов, чтобы пользоваться гардеробом в игре.")
            if not install:
                return None
            path = mods / release.filename
            if path.exists() or path.is_symlink():
                raise skins.SkinError(f"Имя {path.name} уже занято в сборке. Файл сохранён.")
            targets.append(path)
            pending.append((path, release))
    if not pending:
        return targets
    # Validate and stage the complete dependency set before publishing any JAR.
    with tempfile.TemporaryDirectory(dir=mods, prefix=".wardrobe-") as temporary:
        staged = []
        for index, (target, release) in enumerate(pending):
            lifecycle.check()
            status(f"Готовим гардероб в игре: {release.title}…")
            stage = Path(temporary) / str(index)
            stage.write_bytes(aliases._download(release))
            if not _matches(stage, release):
                raise skins.SkinError(f"Файл {release.title} не прошёл проверку.")
            staged.append((stage, target, release))
        with lifecycle.guard():
            written = []
            try:
                for stage, target, release in staged:
                    modfiles.publish_new(stage, target)
                    written.append((target, release))
            except OSError:
                for target, release in reversed(written):
                    if _matches(target, release):
                        target.unlink()
                raise
    return targets


def _receiver(instance, version, loader, backend, *, install=False, status=lambda _: None):
    if backend == "quickskin":
        return _quickskin_files(instance, install=install, status=status)
    release = skin_sync._CATALOG.get(f"{loader}/{version}")
    if release is None:
        return None
    if install:
        return [skin_sync.prepare(instance, version, loader)]
    mods = _inside(instance, "mods")
    if not mods.is_dir():
        return None
    matches = [path for path in mods.iterdir()
               if path.suffix.lower() == ".jar" and skin_sync._matches(path, release)]
    return matches if len(matches) == 1 else None


def _read_marker(instance):
    path = _inside(instance, _MARKER)
    if not path.exists():
        return {}
    if path.stat().st_size > _MAX_MARKER:
        raise skins.SkinError("Запись гардероба в сборке слишком велика.")
    try:
        value = json.loads(path.read_text("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise skins.SkinError("Не удалось прочитать запись гардероба в сборке.") from error
    if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("files"), dict):
        raise skins.SkinError("Неизвестный формат гардероба в сборке.")
    return value


def _texture(source, model, backend):
    pixels = skin_sync._texture(source, model)
    if backend != "quickskin":
        return pixels
    # Quick Skin stores models by a hash of the entire PNG. A deterministic
    # ancillary field keeps Classic and Slim variants of identical pixels
    # separate in its catalog, without changing a single visible pixel.
    info = PngInfo()
    info.add_text("BlockHarborModel", model)
    output = io.BytesIO()
    with Image.open(io.BytesIO(pixels)) as image:
        image.save(output, format="PNG", pnginfo=info)
    return output.getvalue()


def _entries(root, wardrobe, current=None, *, backend=None):
    if not isinstance(wardrobe, list) or len(wardrobe) > 80:
        raise skins.SkinError("Проверь список сохранённых скинов.")
    entries = list(wardrobe)
    if current and current.get("digest") and not any(
            row.get("digest") == current["digest"] and row.get("model") == current.get("model") for row in entries):
        entries.insert(0, {"id": "current", "name": "Текущий скин", **current})
    result = {}
    for row in entries:
        sid, digest, model = row.get("id", ""), row.get("digest", ""), row.get("model", "classic")
        if not (isinstance(sid, str) and (sid == "current" or re.fullmatch(r"[a-f0-9]{32}", sid))
                and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise skins.SkinError("Некорректная запись скина в гардеробе.")
        source = root / "skins" / (digest + ".png")
        if source.is_symlink() or source.resolve().parent != root / "skins":
            raise skins.SkinError("Файл скина перенаправлен за пределы гардероба.")
        # Validate every source before publishing any destination file.
        with source.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
            raise skins.SkinError("Сохранённый файл скина изменён. Заново добавь его в гардероб.")
        pixels = _texture(raw, model, backend)
        checksum = hashlib.sha256(pixels).hexdigest()
        name = re.sub(r"[^\w -]", "_", str(row.get("name", "Скин")), flags=re.UNICODE).strip(" ._")[:40] or "Скин"
        filename = f"BH - {name} - {model} - {sid[:8]}-{checksum[:10]}.png"
        result[filename] = (pixels, checksum, model)
    return result


def _quickskin_models(instance, entries, *, selection=None, account_key=None):
    """Seed model choices before startup; never edit a running mod's settings."""
    path = _inside(instance, "config/skin-preferences.json")
    config_path = _inside(instance, "config/quickskin-client.json")
    values = []
    for file in (path, config_path):
        if file.exists():
            if file.stat().st_size > 1024 * 1024:
                raise skins.SkinError("Настройки гардероба в игре слишком велики.")
            try:
                data = json.loads(file.read_text("utf-8-sig"))
            except (ValueError, UnicodeError, RecursionError) as error:
                raise skins.SkinError("Не удалось прочитать настройки гардероба в игре.") from error
            if not isinstance(data, dict):
                raise skins.SkinError("Неизвестный формат настроек гардероба в игре.")
        else:
            data = {}
        values.append(data)
    data, config = values
    preferences = data.setdefault("preferences", {})
    if not isinstance(preferences, dict):
        raise skins.SkinError("Неизвестный формат моделей скинов в игре.")
    changed = False
    for _, checksum, model in entries.values():
        key = "sha256-" + checksum
        if key not in preferences:
            preferences[key] = {"modelType": model}
            changed = True
    config_changed = False
    if selection is not None and account_key:
        account = config.setdefault("accountSkins", {})
        choices = account.setdefault("skins", {}) if isinstance(account, dict) else None
        if not isinstance(choices, dict):
            raise skins.SkinError("Неизвестный формат выбора скинов Quick Skin.")
        config["activeSkinHash"] = selection
        choices[account_key] = selection
        config_changed = True
    if changed:
        skins._write_inside(instance, path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())
        # Quick Skin's own merge adds missing models to its shared library while
        # preserving choices the player has made there. No external path access.
        config["sharedLibraryPreferencesMerged"] = False
        config_changed = True
    if config_changed:
        skins._write_inside(instance, config_path, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode())


def _selected_files(instance, backend, old):
    """Keep native selections, or all old copies if their config is unreadable."""
    relative = "config/quickskin-client.json" if backend == "quickskin" else "config/skinss/settings.json"
    path = _inside(instance, relative)
    if not path.exists():
        return set()
    try:
        if path.stat().st_size > 1024 * 1024:
            return set(old)
        data = json.loads(path.read_text("utf-8-sig"))
        if not isinstance(data, dict):
            return set(old)
        if backend == "skinss":
            selected = data.get("selectedSkin")
            return {selected} if isinstance(selected, str) else set(old)
        hashes = {data.get("activeSkinHash", "")}
        accounts = data.get("accountSkins", {}).get("skins", {})
        if not isinstance(accounts, dict):
            return set(old)
        hashes.update(accounts.values())
        if any(not isinstance(value, str) for value in hashes):
            return set(old)
        return {name for name, checksum in old.items() if "sha256-" + checksum in hashes}
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return set(old)


def sync(root, instance, version, loader, wardrobe, *, current=None, install=False,
         running=False, status=lambda _: None, player_name=None, player_id=None):
    backend = provider(version, loader)
    if backend is None:
        return {"supported": False, "ready": False}
    if install and running:
        raise skins.SkinError("Подключить гардероб можно при следующем запуске сборки.")
    instance, root = Path(instance).resolve(), Path(root).resolve()
    with _LOCK:
        files = _entries(root, wardrobe, current, backend=backend)
        receiver = _receiver(instance, version, loader, backend, install=install, status=status)
        if not receiver:
            return {"supported": True, "ready": False, "provider": backend}
        relative = "quickskin/uploads/skins" if backend == "quickskin" else "config/skinss/skin"
        directory = _inside(instance, relative)
        prior = _read_marker(instance)
        old = prior.get("files", {}) if prior.get("directory") == relative else {}
        for name, (pixels, _, _) in files.items():
            target = _inside(instance, relative + "/" + name)
            if target.exists() and not skins._same_texture(instance, target, pixels):
                raise skins.SkinError(f"Файл гардероба {name} изменён вручную. Существующий файл сохранён.")
        for name, (pixels, _, _) in files.items():
            target = directory / name
            if not skins._same_texture(instance, target, pixels):
                skins._write_inside(instance, target, pixels)
        applied = prior.get("launcherSelections", {})
        if not isinstance(applied, dict):
            applied = {}
        if install and backend == "quickskin":
            selection, account_key = None, None
            if current and player_name and player_id:
                name = skins.validate_nickname(player_name)
                account_key = str(uuid.UUID(str(player_id))) + ":" + name
                requested = current["digest"] + ":" + current.get("model", "classic")
                if applied.get(account_key) != requested:
                    source = root / "skins" / (current["digest"] + ".png")
                    selection = "sha256-" + hashlib.sha256(_texture(source, current.get("model", "classic"), backend)).hexdigest()
                    applied = {**applied, account_key: requested}
            _quickskin_models(instance, files, selection=selection, account_key=account_key)
        elif install and backend == "skinss" and current:
            # Skinss stores one native selection per instance. Reapplying the
            # launcher image at every start would undo a choice made in /skinss.
            requested = current["digest"] + ":" + current.get("model", "classic")
            if applied.get("skinss") != requested:
                skin_sync.prepare(instance, version, loader,
                                  root / "skins" / (current["digest"] + ".png"),
                                  current.get("model", "classic"))
                applied = {**applied, "skinss": requested}
        retained = {}
        selected = set(old) if running else _selected_files(instance, backend, old)
        # A skin selected inside Minecraft may still be in use after the user
        # deletes it from the launcher's wardrobe. Keep that session's files.
        # Idle cleanup removes only unchanged files recorded as our own.
        for name, checksum in old.items():
            if name in files or not isinstance(name, str) or not re.fullmatch(
                    r"BH - [\w -]{1,40} - (?:classic|slim) - (?:[a-f0-9]{8}|current)-[a-f0-9]{10}\.png", name):
                continue
            if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
                continue
            target = directory / name
            if target.is_symlink() or not target.is_file() or target.stat().st_size > 1024 * 1024:
                continue
            if hashlib.sha256(target.read_bytes()).hexdigest() != checksum:
                continue
            if name in selected:
                retained[name] = checksum
            else:
                target.unlink()
        record = {"schema": 1, "provider": backend, "version": version, "loader": loader,
                  "directory": relative, "receivers": [path.name for path in receiver],
                  "files": {**retained, **{name: value[1] for name, value in files.items()}},
                  "launcherSelections": applied,
                  "count": len(files)}
        encoded = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode()
        marker = _inside(instance, _MARKER)
        if not marker.exists() or marker.read_bytes() != encoded:
            skins._write_inside(instance, marker, encoded)
        return {"supported": True, "ready": True, "provider": backend, "count": len(files)}


def describe(instance, version, loader):
    backend = provider(version, loader)
    if backend is None:
        return {"supported": False, "ready": False,
                "hint": "Для этой версии сборки смена скинов в игре пока недоступна."}
    result = {"supported": True, "ready": False, "provider": backend,
              "hint": "В игре нажми Esc → «Сменить скин» (Change Skin)." if backend == "quickskin"
              else "В чате Minecraft введи /skinss и выбери скин из гардероба."}
    try:
        root = Path(instance).resolve()
        marker = _read_marker(root)
        if marker.get("provider") != backend or marker.get("version") != version or marker.get("loader") != loader:
            return result
        receivers = marker.get("receivers", [])
        if not isinstance(receivers, list) or not 1 <= len(receivers) <= 2:
            return result
        if any(not isinstance(name, str) or Path(name).name != name or "\\" in name for name in receivers):
            return result
        paths = [_inside(root, "mods/" + name) for name in receivers]
        if backend == "quickskin":
            ready = len(paths) == 2 and _matches(paths[1], _QUICKSKIN) and (
                _matches(paths[0], _ARCHITECTURY) or _architectury_compatible(paths[0]))
        else:
            ready = len(paths) == 1 and skin_sync._matches(paths[0], skin_sync._CATALOG[f"{loader}/{version}"])
        result.update(ready=ready, count=marker.get("count", 0) if ready else 0)
    except (OSError, ValueError):
        pass
    return result
