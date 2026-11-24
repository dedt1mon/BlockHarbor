"""Portable BlockHarbor ZIPs. All paths describe files inside one new instance."""

import hmac
import json
import os
import re
import secrets
import shutil
import stat
import struct
import threading
import unicodedata
import zipfile
from pathlib import Path

from . import lifecycle, modfiles
from .store import KINDS, MAX_ADDON, clean_filename, input_profile

MANIFEST = "blockharbor.json"
STAMP = ".blockharbor-install.json"
MAX_ZIP = 8 * 1024**3
MAX_TOTAL = MAX_ZIP - 32 * 1024**2
MAX_FILES = 40_000
MAX_CONFIG = 8 * 1024**2
_CONFIG_EXT = {
    ".ini",
    ".toml",
    ".cfg",
    ".conf",
    ".json",
    ".json5",
    ".properties",
    ".yml",
    ".yaml",
    ".txt",
    ".snbt",
    ".xml",
}
_PRIVATE = re.compile(
    r"account|auth(?:me|entication|orization|lib)?|credential|passw(?:or)?d|passwd|token|secret|webhook|api[_.-]?key|private|session|nickname|nameprotect|quickskin|skin[_.-]?preferences|skin(?:ss|loader|restorer)|e4steam|^skins?$|^player(?:data|profiles?|skins?)$|^steam$|^logs?$|^cache$|^user(?:s|cache|name)?$",
    re.IGNORECASE,
)
_SECRET = re.compile(
    rb"""(?ix)["']?(?:access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret|password|passwd|private[_-]?key|discord[_-]?webhook|authorization|user[_-]?name|real[_-]?name)["']?\s*[:=]|https?://(?:[^ /]+:[^ /@]+@|(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/)"""
)
_PIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]{0,149}\Z")


def _name_parts(name):
    if len(name) > 512 or len(name.split("/")) > 24:
        raise ValueError("Слишком длинный путь в архиве сборки.")
    return tuple(clean_filename(p) for p in name.split("/"))


def _allowed(parts, worlds, directory=False):
    section, *rest = parts
    if any(_PRIVATE.search(p) for p in rest) and section in (
        "config",
        "defaultconfigs",
    ):
        return False
    if directory and not rest:
        return section in {*KINDS, "config", "defaultconfigs"} or (
            worlds and section == "saves"
        )
    if not rest:
        return False
    if section == "mods":
        return (
            not directory
            and len(rest) == 1
            and rest[-1].lower().endswith((".jar", ".jar.disabled"))
        )
    if section in ("shaderpacks", "resourcepacks"):
        return (
            directory
            or len(rest) > 1
            or rest[-1].lower().endswith((".zip", ".zip.disabled", ".zip.txt"))
        )
    if section in ("config", "defaultconfigs"):
        return directory or Path(rest[-1]).suffix.lower() in _CONFIG_EXT
    if section == "saves":
        return worlds and (directory or len(rest) >= 2 and rest[-1] != "session.lock")
    return False


def _private_config(parts, data):
    return parts[0] in ("config", "defaultconfigs") and bool(_SECRET.search(data))


def _pin(value):
    if value is not None and (
        not isinstance(value, str) or not _PIN.fullmatch(value) or ".." in value
    ):
        raise ValueError("В архиве указана недопустимая версия загрузчика.")
    return value


def _reparse(st):
    return bool(getattr(st, "st_file_attributes", 0) & 0x400) or stat.S_ISLNK(
        st.st_mode
    )


def _pack_options(folder):
    path = folder / "options.txt"
    if path.is_file() and not _reparse(path.lstat()) and path.stat().st_size < 2**20:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return [
            line
            for line in lines
            if line.partition(":")[0] in ("resourcePacks", "incompatibleResourcePacks")
        ][:2]
    return []


def _catalog_path(parts, directory, folders, configs):
    if parts[0] in ("config", "defaultconfigs"):
        return configs
    if parts[0] not in KINDS:
        return False
    if tuple(parts[:2]) in folders:
        return len(parts) > 2 or directory
    if directory:
        return len(parts) == 1
    ext = KINDS[parts[0]]
    return len(parts) == 2 and parts[1].lower().endswith((ext, ext + ".disabled"))


def _folder_marker(parts):
    if (len(parts) == 3 and parts[0] == "resourcepacks" and parts[2] == "pack.mcmeta"
            or len(parts) >= 4 and parts[0] == "shaderpacks" and parts[2] == "shaders"):
        return tuple(parts[:2])
    return None


def _catalog_folders(root):
    folders = set()
    for kind in ("resourcepacks", "shaderpacks"):
        parent = root / kind
        if not parent.is_dir() or _reparse(parent.lstat()):
            continue
        for candidate in parent.iterdir():
            if candidate.name.startswith(".") or _reparse(candidate.lstat()) or not candidate.is_dir():
                continue
            parts = (kind, candidate.name)
            marker = "pack.mcmeta" if kind == "resourcepacks" else "shaders"
            path = candidate / marker
            if not path.exists() or _reparse(path.lstat()):
                continue
            if kind == "resourcepacks" and path.is_file():
                folders.add(parts)
            elif kind == "shaderpacks" and path.is_dir() and next(_walk(root, [(*parts, marker)], False), None):
                folders.add(parts)
    return folders


def _catalog_options(options, pack_names):
    """Carry only selections referring to shipped packs or the vanilla resources."""
    # Older Minecraft versions store the bare pack name, newer ones add "file/".
    names = set(pack_names)
    permitted = {"vanilla", *names, *("file/" + name for name in names)}
    selected = []
    for line in options:
        key, _, raw = line.partition(":")
        try:
            values = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if isinstance(values, list):
            choices = [item for item in values if isinstance(item, str) and item in permitted]
            selected.append(key + ":" + json.dumps(choices, ensure_ascii=False, separators=(",", ":")))
    return selected


def _walk(folder, sections, worlds, *, catalog_folders=None, configs=False):
    roots = [section if isinstance(section, tuple) else (section,) for section in sections]
    stack = [(folder.joinpath(*parts), parts) for parts in roots]
    while stack:
        lifecycle.check()
        path, parts = stack.pop()
        try:
            st = path.lstat()
        except FileNotFoundError:
            if len(parts) == 1:
                continue
            raise ValueError(
                "Файлы сборки изменились во время экспорта. Повтори выгрузку."
            ) from None
        directory = stat.S_ISDIR(st.st_mode)
        if _reparse(st) or not _allowed(parts, worlds, directory):
            continue
        if catalog_folders is not None and not _catalog_path(parts, directory, catalog_folders, configs):
            continue
        _name_parts("/".join(parts))
        if directory:
            for child in sorted(path.iterdir(), reverse=True):
                if not child.name.startswith("."):
                    stack.append((child, (*parts, child.name)))
        elif stat.S_ISREG(st.st_mode):
            yield path, parts, st


def _reference_only_mod(path, parts, profile):
    if parts[0] != "mods" or "quickskin" not in modfiles.mod_info(path, modfiles.file_stamp(path))[0]:
        return False
    # Quick Skin allows resolving an official dependency, but not shipping its
    # JAR in ZIPs. The launcher restores this exact supported release at startup.
    # Other releases or disabled mods must never be silently replaced/enabled.
    from . import game_wardrobe
    if (game_wardrobe.provider(profile["version"], profile["loader"]) == "quickskin"
            and path.suffix.lower() == ".jar"
            and game_wardrobe._matches(path, game_wardrobe._QUICKSKIN)):
        return True
    raise ValueError(
        "Quick Skin нельзя включать в ZIP: мод разрешено получать только из официального источника. "
        "Эту версию или отключённый файл BlockHarbor не сможет точно восстановить при запуске. "
        "Удали Quick Skin из отдельной копии сборки перед выгрузкой; исходный файл сохранён."
    )


def export_build(store, pid, file, worlds=False, *, catalog_name=None, max_bytes=MAX_ZIP, include_configs=False):
    lifecycle.check()
    profile = store.profile(pid)
    root = store._profile_directory(store.instances, pid)
    meta = {
        "format": "BlockHarbor",
        "formatVersion": 1,
        "name": profile["name"] if catalog_name is None else catalog_name,
        "minecraft": profile["version"],
        "loader": profile["loader"],
        "worlds": worlds,
    }
    stamp = root / STAMP
    if stamp.is_file() and not _reparse(stamp.lstat()) and stamp.stat().st_size < 65536:
        saved = json.loads(stamp.read_text(encoding="utf-8"))
        if isinstance(saved, dict) and (saved.get("version"), saved.get("loader")) == (
            profile["version"],
            profile["loader"],
        ):
            meta["loaderVersion"] = _pin(saved.get("loader_version"))
    meta["packOptions"] = _pack_options(root)
    catalog = catalog_name is not None
    if catalog and worlds:
        raise ValueError("В публичную сборку нельзя включать миры.")
    sections = list(KINDS)
    if not catalog or include_configs:
        sections.extend(("config", "defaultconfigs"))
    if catalog and include_configs:
        meta["publicConfigs"] = True
    if worlds:
        sections.append("saves")
    seen = set()
    total = 0
    with zipfile.ZipFile(
        file, "w", compression=zipfile.ZIP_STORED, allowZip64=True
    ) as z:
        folders = _catalog_folders(root) if catalog else None
        if catalog:
            # Discover top-level pack files without traversing folder packs twice.
            names = [parts[1] for _, parts, _ in _walk(root, ["resourcepacks"], False, catalog_folders=set())]
            names.extend(name for kind, name in folders if kind == "resourcepacks")
            meta["packOptions"] = _catalog_options(meta["packOptions"], names)
        # A stable manifest timestamp lets interrupted publications resume.
        entry = zipfile.ZipInfo(MANIFEST) if catalog else MANIFEST
        manifest = json.dumps(meta, ensure_ascii=False).encode()
        if catalog and len(manifest) > 65536:
            raise ValueError("Манифест публичной сборки превышает 64 КБ.")
        z.writestr(entry, manifest)
        found_markers = set()
        for path, parts, st in _walk(root, sections, worlds, catalog_folders=folders, configs=include_configs):
            name = "/".join(parts)
            normal = unicodedata.normalize("NFC", name).casefold()
            if normal in seen:
                raise ValueError(
                    "В сборке есть пути, отличающиеся только регистром или написанием Unicode."
                )
            if _reference_only_mod(path, parts, profile):
                continue
            if catalog and file.tell() + st.st_size > max_bytes:
                raise ValueError("Публичная сборка превышает лимит каталога по размеру.")
            if (
                st.st_size > MAX_ADDON
                or len(seen) >= MAX_FILES - 1
                or total + st.st_size > MAX_TOTAL
            ):
                raise ValueError(
                    "Сборка превышает лимит ZIP: 8 ГБ, 512 МБ на файл или 40 000 файлов."
                )
            with path.open("rb") as src:
                if not os.path.samestat(st, os.fstat(src.fileno())):
                    raise ValueError("Файл сборки изменился во время экспорта.")
                if parts[0] in ("config", "defaultconfigs"):
                    if st.st_size > MAX_CONFIG:
                        raise ValueError("Файл настроек превышает 8 МБ.")
                    data = src.read(MAX_CONFIG + 1)
                    if len(data) != st.st_size:
                        raise ValueError("Файл настроек изменился во время экспорта.")
                    if _private_config(parts, data):
                        continue
                    z.writestr(zipfile.ZipInfo(name) if catalog else name, data)
                else:
                    with z.open(name, "w") as out:
                        left = st.st_size
                        while left:
                            lifecycle.check()
                            block = src.read(min(2**20, left))
                            if not block:
                                raise ValueError(
                                    "Файл сборки изменился во время экспорта."
                                )
                            out.write(block)
                            left -= len(block)
                        if src.read(1):
                            raise ValueError("Файл сборки изменился во время экспорта.")
            seen.add(normal)
            total += st.st_size
            if catalog and (marker := _folder_marker(parts)):
                found_markers.add(marker)
        if catalog and found_markers != folders:
            raise ValueError("Папки ресурс-паков или шейдеров изменились во время публикации.")
    if file.tell() > max_bytes:
        raise ValueError("Публичная сборка превышает лимит каталога по размеру." if catalog else "ZIP сборки превышает 8 ГБ.")
    file.seek(0)
    return (
        re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", profile["name"]).strip(". ")[:48]
        + ".zip"
    )


def _directory_limit(file):
    # Read the small EOCD before letting ZipFile allocate one object per entry.
    end = zipfile._EndRecData(file)
    if (
        not end
        or end[3] < 1
        or end[4] > MAX_FILES
        or end[5] > 24 * 1024**2
        or end[1]
        or end[2]
        or end[3] != end[4]
    ):
        raise ValueError("Недопустимый каталог ZIP или слишком много файлов.")
    file.seek(0)


def _members(archive, worlds, *, catalog=False, configs=False):
    paths = {}
    dirs = set()
    seen = set()
    rows = []
    size = 0
    if catalog:
        folders = {
            marker for item in archive.infolist() if not item.is_dir()
            if (marker := _folder_marker(item.orig_filename.split("/")))
        }
    for item in archive.infolist():
        lifecycle.check()
        name = item.orig_filename
        directory = name.endswith("/")
        norm = name[:-1] if directory else name
        parts = _name_parts(norm)
        key = tuple(unicodedata.normalize("NFC", p).casefold() for p in parts)
        mode = stat.S_IFMT(item.external_attr >> 16)
        if (
            mode not in (0, stat.S_IFREG, stat.S_IFDIR)
            or item.external_attr & 0x400
            or item.flag_bits & 1
            or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
        ):
            raise ValueError(
                "ZIP содержит ссылку, зашифрованный файл или неподдерживаемый тип записи."
            )
        if (
            mode == stat.S_IFDIR
            and not directory
            or directory
            and (mode == stat.S_IFREG or item.file_size)
        ):
            raise ValueError("Неоднозначный тип файла в архиве.")
        if key in seen or (key in dirs and not directory):
            raise ValueError("В ZIP повторяется имя файла или каталога.")
        for n in range(1, len(parts) + 1):
            stem = key[:n]
            if paths.setdefault(stem, parts[:n]) != parts[:n] or (
                n < len(parts) and stem in seen and stem not in dirs
            ):
                raise ValueError("В ZIP конфликтуют имена или регистр путей.")
            if n < len(parts) or directory:
                dirs.add(stem)
        seen.add(key)
        if name == MANIFEST:
            continue
        if not _allowed(parts, worlds, directory) or (catalog and not _catalog_path(parts, directory, folders, configs)):
            raise ValueError("В архиве есть путь вне состава сборки: " + norm[:140])
        if directory:
            continue
        size += item.file_size
        if (
            item.file_size > MAX_ADDON
            or size > MAX_TOTAL
            or (
                parts[0] in ("config", "defaultconfigs") and item.file_size > MAX_CONFIG
            )
            or (
                item.file_size > 8 * 1024**2
                and item.file_size > max(1, item.compress_size) * 350
            )
        ):
            raise ValueError("Архив превышает лимиты размера или степени сжатия.")
        rows.append((item, parts))
    return rows, size


def import_build(store, file, *, catalog_listing=None, max_bytes=MAX_ZIP):
    lifecycle.check()
    file.seek(0, 2)
    if not 0 < file.tell() <= max_bytes:
        raise ValueError("Нужен ZIP сборки размером до 8 ГБ.")
    file.seek(0)
    try:
        _directory_limit(file)
        with zipfile.ZipFile(file) as archive:
            try:
                info = archive.getinfo(MANIFEST)
            except KeyError:
                raise ValueError(
                    "В ZIP отсутствует blockharbor.json. Выбери архив, выгруженный из BlockHarbor."
                ) from None
            if (
                info.file_size > 65536
                or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or info.flag_bits & 1
                or len(archive.infolist()) > MAX_FILES
            ):
                raise ValueError(
                    "Неподдерживаемый манифест или слишком много файлов в ZIP."
                )
            meta = json.loads(archive.read(info))
            if (
                not isinstance(meta, dict)
                or meta.get("format") != "BlockHarbor"
                or type(meta.get("formatVersion")) is not int
                or meta["formatVersion"] != 1
            ):
                raise ValueError("Неизвестный формат архива BlockHarbor.")
            name, version, loader = input_profile(
                meta.get("name"), meta.get("minecraft"), meta.get("loader")
            )
            if catalog_listing is not None and (
                (name, version, loader) != tuple(catalog_listing[key] for key in ("name", "minecraft", "loader"))
                or meta.get("worlds") is not False
                or type(meta.get("publicConfigs", False)) is not bool
            ):
                raise ValueError("Описание публичной сборки расходится с её архивом.")
            pin = _pin(meta.get("loaderVersion"))
            options = meta.get("packOptions", [])
            if (
                not isinstance(options, list)
                or len(options) > 2
                or any(
                    not isinstance(line, str)
                    or chr(10) in line
                    or chr(13) in line
                    or line.partition(":")[0]
                    not in ("resourcePacks", "incompatibleResourcePacks")
                    or ":" not in line
                    for line in options
                )
            ):
                raise ValueError("Неверные параметры ресурс-паков в манифесте.")
            rows, size = _members(archive, meta.get("worlds") is True, catalog=catalog_listing is not None, configs=meta.get("publicConfigs") is True)
            if catalog_listing is not None:
                if size > max_bytes:
                    raise ValueError("Распакованная сборка превышает лимит каталога по размеру.")
                options = _catalog_options(options, (parts[1] for _, parts in rows if parts[0] == "resourcepacks"))
            if size > shutil.disk_usage(store.instances).free:
                raise ValueError("Недостаточно места на диске для распаковки.")
            existing = {p["name"].casefold() for p in store.all()}
            new_name = name
            n = 1
            while new_name.casefold() in existing:
                suffix = f" ({n + 1})"
                new_name = name[: 48 - len(suffix)] + suffix
                n += 1

            def unpack(folder):
                for item, parts in rows:
                    lifecycle.check()
                    target = folder.joinpath(*parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item) as src, target.open("xb") as out:
                        if parts[0] in ("config", "defaultconfigs"):
                            data = src.read(MAX_CONFIG + 1)
                            if _private_config(parts, data):
                                raise ValueError(
                                    "В ZIP содержится файл с личными параметрами."
                                )
                            out.write(data)
                        else:
                            while block := src.read(2**20):
                                lifecycle.check()
                                out.write(block)
                        if out.tell() != item.file_size:
                            raise ValueError("Файл из архива извлечён не полностью.")
                if pin:
                    (folder / STAMP).write_text(
                        json.dumps(
                            {
                                "version": version,
                                "loader": loader,
                                "loader_version": pin,
                            }
                        ),
                        encoding="utf-8",
                    )
                if options:
                    (folder / "options.txt").write_text(
                        "\n".join(options) + "\n", encoding="utf-8"
                    )

            pid = store.create(new_name, version, loader, prepare=unpack)
            return {"id": pid, "name": new_name}
    except (
        zipfile.BadZipFile,
        UnicodeError,
        struct.error,
        NotImplementedError,
        RuntimeError,
    ) as exc:
        raise ValueError(
            "Не удалось прочитать ZIP сборки. Проверь целостность архива."
        ) from exc


class Downloads:
    """Short-lived, single-use browser downloads. Authenticated preparation owns the temporary file."""

    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()
        self.timers = {}
        self.closed = False

    def issue(self, file, name):
        ticket = secrets.token_urlsafe(24)
        cookie = secrets.token_urlsafe(24)
        with self.lock:
            lifecycle.check()
            if self.closed:
                raise lifecycle.OperationCancelled("Лаунчер закрыт.")
            if len(self.items) >= 8:
                raise ValueError("Сначала скачай уже подготовленные архивы.")
            self.items[ticket] = (cookie, file, name)
            timer = threading.Timer(300, self.discard, args=(ticket,))
            timer.daemon = True
            self.timers[ticket] = timer
            timer.start()
        return ticket, cookie

    def discard(self, ticket):
        with self.lock:
            item = self.items.pop(ticket, None)
            timer = self.timers.pop(ticket, None)
            if timer:
                timer.cancel()
        if item:
            item[1].close()

    def take(self, ticket, cookie):
        with self.lock:
            found = self.items.get(ticket)
            if not found or not hmac.compare_digest(found[0], cookie):
                raise ValueError("Ссылка на ZIP истекла. Снова нажми «Выгрузить».")
            timer = self.timers.pop(ticket, None)
            if timer:
                timer.cancel()
            return self.items.pop(ticket)[1:]

    def close(self):
        with self.lock:
            self.closed = True
            for timer in self.timers.values():
                timer.cancel()
            files = [row[1] for row in self.items.values()]
            self.items.clear()
            self.timers.clear()
        for file in files:
            file.close()
