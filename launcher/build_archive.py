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
    r"account|auth(?:me|entication|orization|lib)?|credential|passw(?:or)?d|passwd|token|secret|webhook|api[_.-]?key|private|session|nickname|nameprotect|skin(?:ss|loader|restorer)|e4steam|^skins?$|^player(?:data|profiles?|skins?)$|^steam$|^logs?$|^cache$|^user(?:s|cache|name)?$",
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


def _walk(folder, sections, worlds):
    stack = [(folder / section, (section,)) for section in sections]
    while stack:
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
        _name_parts("/".join(parts))
        if directory:
            for child in sorted(path.iterdir(), reverse=True):
                if not child.name.startswith("."):
                    stack.append((child, (*parts, child.name)))
        elif stat.S_ISREG(st.st_mode):
            yield path, parts, st


def export_build(store, pid, file, worlds=False):
    profile = store.profile(pid)
    root = store._profile_directory(store.instances, pid)
    meta = {
        "format": "BlockHarbor",
        "formatVersion": 1,
        "name": profile["name"],
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
    sections = [*KINDS, "config", "defaultconfigs"]
    if worlds:
        sections.append("saves")
    seen = set()
    total = 0
    with zipfile.ZipFile(
        file, "w", compression=zipfile.ZIP_STORED, allowZip64=True
    ) as z:
        z.writestr(MANIFEST, json.dumps(meta, ensure_ascii=False).encode())
        for path, parts, st in _walk(root, sections, worlds):
            name = "/".join(parts)
            normal = unicodedata.normalize("NFC", name).casefold()
            if normal in seen:
                raise ValueError(
                    "В сборке есть пути, отличающиеся только регистром или написанием Unicode."
                )
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
                    z.writestr(name, data)
                else:
                    with z.open(name, "w") as out:
                        left = st.st_size
                        while left:
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
    if file.tell() > MAX_ZIP:
        raise ValueError("ZIP сборки превышает 8 ГБ.")
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


def _members(archive, worlds):
    paths = {}
    dirs = set()
    seen = set()
    rows = []
    size = 0
    for item in archive.infolist():
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
        if not _allowed(parts, worlds, directory):
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


def import_build(store, file):
    file.seek(0, 2)
    if not 0 < file.tell() <= MAX_ZIP:
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
            rows, size = _members(archive, meta.get("worlds") is True)
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
                            shutil.copyfileobj(src, out, 2**20)
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

    def issue(self, file, name):
        ticket = secrets.token_urlsafe(24)
        cookie = secrets.token_urlsafe(24)
        with self.lock:
            if len(self.items) >= 8:
                raise ValueError("Сначала скачай уже подготовленные архивы.")
            self.items[ticket] = (cookie, file, name)
        timer = threading.Timer(300, self.discard, args=(ticket,))
        timer.daemon = True
        timer.start()
        return ticket, cookie

    def discard(self, ticket):
        with self.lock:
            item = self.items.pop(ticket, None)
        if item:
            item[1].close()

    def take(self, ticket, cookie):
        with self.lock:
            found = self.items.get(ticket)
            if not found or not hmac.compare_digest(found[0], cookie):
                raise ValueError("Ссылка на ZIP истекла. Снова нажми «Выгрузить».")
            return self.items.pop(ticket)[1:]
