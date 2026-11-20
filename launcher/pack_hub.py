"""Optional Modrinth modpacks and the standard, hash-verified .mrpack format.

Packs are prepared in an unpublished instance. The Store only creates a profile
after every download and override succeeds, so a failed import cannot leave a
half-installed build in the user's list.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
import unicodedata
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from . import build_archive, e4steam, lifecycle, modrinth
from .modfiles import file_hash, file_stamp
from .store import KINDS, MAX_ADDON, clean_filename, input_profile

INDEX = "modrinth.index.json"
RECEIPT = ".blockharbor-pack.json"
MAX_INDEX = 8 * 1024**2
MAX_PACK = 2 * 1024**3
MAX_TOTAL = 8 * 1024**3
MAX_FILES = 20_000
DOWNLOAD_HOSTS = {
    "cdn.modrinth.com", "cdn-raw.modrinth.com", "github.com",
    "raw.githubusercontent.com", "gitlab.com", "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}
LOADERS = {"fabric-loader": "fabric", "quilt-loader": "quilt", "forge": "forge", "neoforge": "neoforge"}
CATEGORIES = ("adventure", "challenging", "combat", "kitchen-sink", "lightweight", "magic", "multiplayer", "optimization", "quests", "technology")
SORTS = {"relevance", "downloads", "follows", "newest", "updated"}


def steam_variants(versions, loaders):
    """Conservative preflight from release metadata, before downloading its archive."""
    unknown = not isinstance(versions, list) or not isinstance(loaders, list)
    versions = versions if isinstance(versions, list) else []
    loaders = loaders if isinstance(loaders, list) else []
    valid_versions, valid_loaders = [], []
    for version in versions:
        try:
            input_profile("Модпак", version, "vanilla")
        except ValueError:
            unknown = True
        else:
            if version not in valid_versions:
                valid_versions.append(version)
    for loader in loaders:
        if not isinstance(loader, str) or loader not in {*LOADERS.values(), "vanilla"}:
            unknown = True
        elif loader not in valid_loaders:
            valid_loaders.append(loader)
    unknown = unknown or not valid_versions or not valid_loaders
    variants = [e4steam.compatibility(version, loader) for version in valid_versions for loader in valid_loaders]
    return {"supported": bool(variants) and not unknown and all(row["supported"] for row in variants),
            "unknown": bool(unknown), "variants": variants}


def _path(value):
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value:
        raise ValueError("Недопустимый путь в модпаке.")
    parts = tuple(clean_filename(p) for p in value.split("/"))
    if len(parts) > 24:
        raise ValueError("Слишком много вложенных папок в модпаке.")
    return parts


def _url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) <= 32 for c in value):
        raise ValueError("Модпак содержит некорректную ссылку загрузки.")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc.lower() not in DOWNLOAD_HOSTS or parsed.fragment:
        raise ValueError("Модпак использует неподдерживаемый сервер загрузки. Выбери сборку с файлами из разрешённых источников HTTPS.")
    return value


def _hashes(value):
    if not isinstance(value, dict):
        raise ValueError("В модпаке отсутствуют контрольные суммы файла.")
    result = {}
    for algorithm, length in (("sha1", 40), ("sha512", 128)):
        digest = value.get(algorithm)
        if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{%d}" % length, digest):
            raise ValueError("Для файла модпака нужны SHA-1 и SHA-512.")
        result[algorithm] = digest.lower()
    return result


def _size(value, maximum=MAX_ADDON):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("Недопустимый размер файла модпака.")
    return value


def _canonical(value):
    return unicodedata.normalize("NFC", value).casefold()


def _validate_paths(paths):
    """Detect Windows case aliases and file/directory collisions before I/O."""
    seen, spellings = set(), {}
    for value in paths:
        parts = _path(value)
        folded = _canonical(value)
        if folded in seen:
            raise ValueError("В модпаке повторяются пути или имена отличаются только регистром.")
        seen.add(folded)
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            key = _canonical(prefix)
            if key in spellings and spellings[key] != prefix:
                raise ValueError("В модпаке конфликтуют имена папок по регистру или Unicode.")
            spellings[key] = prefix
    for path in seen:
        pieces = path.split("/")
        if any("/".join(pieces[:i]) in seen for i in range(1, len(pieces))):
            raise ValueError("Файл модпака одновременно используется как папка.")


def _manifest(raw):
    if not isinstance(raw, dict) or type(raw.get("formatVersion")) is not int or raw["formatVersion"] != 1 or raw.get("game") != "minecraft":
        raise ValueError("Поддерживается формат .mrpack версии 1 для Minecraft.")
    dependencies = raw.get("dependencies")
    if not isinstance(dependencies, dict) or "minecraft" not in dependencies:
        raise ValueError("В модпаке не указана версия Minecraft.")
    if set(dependencies) - {"minecraft", *LOADERS}:
        raise ValueError("Модпак требует неподдерживаемый компонент: " + ", ".join(sorted(set(dependencies) - {"minecraft", *LOADERS})))
    loaders = [key for key in LOADERS if key in dependencies]
    if len(loaders) > 1:
        raise ValueError("Модпак одновременно требует несколько загрузчиков.")
    loader_key = loaders[0] if loaders else ""
    loader = LOADERS[loader_key] if loader_key else "vanilla"
    title = raw.get("name")
    if not isinstance(title, str) or not title.strip() or len(title) > 256:
        raise ValueError("Некорректное название модпака.")
    name, minecraft, loader = input_profile(title.strip()[:48], dependencies["minecraft"], loader)
    pin = build_archive._pin(dependencies.get(loader_key)) if loader_key else None
    version_id = raw.get("versionId")
    if not isinstance(version_id, str) or not 1 <= len(version_id) <= 256 or any(ord(c) < 32 for c in version_id):
        raise ValueError("Модпак не содержит корректного номера выпуска.")
    files = raw.get("files")
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise ValueError("Модпак содержит слишком много файлов.")
    validated, total = [], 0
    for row in files:
        if not isinstance(row, dict):
            raise ValueError("Некорректная запись файла в модпаке.")
        path = "/".join(_path(row.get("path")))
        size = _size(row.get("fileSize"))
        hashes = _hashes(row.get("hashes"))
        urls = row.get("downloads")
        if not isinstance(urls, list) or not 1 <= len(urls) <= 8:
            raise ValueError("Для файла модпака нет допустимых ссылок загрузки.")
        urls = [_url(url) for url in urls]
        env = row.get("env", {"client": "required", "server": "required"})
        if not isinstance(env, dict) or any(value not in {"required", "optional", "unsupported"} for value in env.values()) or set(env) - {"client", "server"}:
            raise ValueError("Неизвестная среда установки файла модпака.")
        env = {"client": env.get("client", "required"), "server": env.get("server", "required")}
        total += size
        if total > MAX_TOTAL:
            raise ValueError("Модпак превышает лимит распакованных данных: 8 ГБ.")
        validated.append({"path": path, "fileSize": size, "hashes": hashes, "downloads": urls, "env": env})
    _validate_paths([row["path"] for row in validated])
    return {"name": name, "title": title, "minecraft": minecraft, "loader": loader, "loaderVersion": pin,
            "versionId": version_id, "summary": str(raw.get("summary", ""))[:1000], "files": validated}


def _inspect(archive):
    """Check all ZIP entries, including ignored server overrides, before extraction."""
    try:
        with archive.open(INDEX) as stream:
            data = stream.read(MAX_INDEX + 1)
        if len(data) > MAX_INDEX:
            raise ValueError("Индекс модпака превышает 8 МБ.")
        manifest = _manifest(json.loads(data))
    except (KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("В корне архива .mrpack отсутствует или повреждён индекс содержимого.") from exc
    entries = archive.infolist()
    if len(entries) > MAX_FILES:
        raise ValueError("В ZIP модпака слишком много файлов.")
    all_names, layers, total = [], {"overrides": [], "client-overrides": []}, 0
    for entry in entries:
        directory = entry.is_dir()
        name = entry.filename[:-1] if directory else entry.filename
        parts = _path(name)
        mode = entry.external_attr >> 16
        if entry.external_attr & 0x400 or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
            raise ValueError("Ссылки и специальные файлы в модпаке не поддерживаются.")
        if entry.flag_bits & 1 or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ValueError("Зашифрованный файл или неподдерживаемое сжатие модпака.")
        if directory:
            continue
        all_names.append(entry.filename)
        if entry.filename == INDEX:
            if entry.file_size > MAX_INDEX:
                raise ValueError("Индекс модпака превышает 8 МБ.")
            continue
        _size(entry.file_size)
        total += entry.file_size
        if total > MAX_TOTAL:
            raise ValueError("Распакованный модпак превышает 8 ГБ.")
        if len(parts) > 1 and parts[0] in layers:
            layers[parts[0]].append((entry, "/".join(parts[1:])))
    _validate_paths(all_names)
    # Overlay precedence is explicit, but aliases across layers are not allowed.
    final_paths = {row["path"] for row in manifest["files"]}
    for layer in ("overrides", "client-overrides"):
        final_paths.update(path for _, path in layers[layer])
    _validate_paths(list(final_paths))
    manifest["overrideBytes"] = total
    return manifest, layers


def _unique_name(store, proposed):
    used = {row["name"].casefold() for row in store.all()}
    name, number = proposed, 2
    while name.casefold() in used:
        suffix = f" ({number})"
        name, number = proposed[:48 - len(suffix)] + suffix, number + 1
    return name


def download_verified(url, target, hashes, size, *, transfers=None, status=lambda message: None, title=None):
    """A transport adapter shared by pack imports and friend synchronization."""
    lifecycle.check()
    _url(url)
    if transfers is not None:
        return transfers.download(url, target, title=title or target.name, hashes=hashes,
                                  size=size, max_size=max(size, 1), hosts=DOWNLOAD_HOSTS,
                                  category="modpack", status=status)
    digest = {name: hashlib.new(name) for name in hashes}
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part-" + secrets.token_hex(4))
    try:
        with lifecycle.response(modrinth._open(url, DOWNLOAD_HOSTS)) as source, tmp.open("xb") as output:
            remaining = size
            while remaining:
                part = source.read(min(2**20, remaining))
                if not part:
                    break
                output.write(part)
                for value in digest.values():
                    value.update(part)
                remaining -= len(part)
            if remaining or source.read(1) or any(value.hexdigest() != hashes[name] for name, value in digest.items()):
                raise ValueError("Размер или контрольная сумма скачанного файла модпака не совпали.")
        with lifecycle.guard():
            os.replace(tmp, target)
        return target
    finally:
        tmp.unlink(missing_ok=True)


class PackHub:
    ACTIONS = {"packSearch", "packVersions", "packPreview", "packInstall", "packState", "packDiscard"}
    READ_ACTIONS = {"packSearch", "packVersions", "packState"}
    JOBS = {"packInstall"}

    def __init__(self, store, transfers=None):
        self.store, self.transfers = store, transfers
        self.staging = store.root / "pack-staging"
        if self.staging.is_symlink():
            raise ValueError("Папка подготовки модпаков перенаправлена.")
        self.staging.mkdir(exist_ok=True)
        self.previews = {}
        self.lock = threading.RLock()

    def search(self, p):
        query = p.get("q", "")
        if not isinstance(query, str) or len(query) > 180:
            raise ValueError("Поисковый запрос длиннее 180 символов.")
        offset, sort = int(p.get("offset", 0)), p.get("sort", "relevance")
        if not 0 <= offset <= 100_000 or sort not in SORTS:
            raise ValueError("Неверная страница или сортировка модпаков.")
        facets = [["project_type:modpack"]]
        version, loader, category = p.get("version", ""), p.get("loader", ""), p.get("category", "")
        if version:
            input_profile("Модпак", version, "vanilla")
            facets.append(["versions:" + version])
        if loader:
            if loader not in {*LOADERS.values(), "vanilla"}:
                raise ValueError("Неизвестный загрузчик модпака.")
            facets.append(["categories:" + loader])
        if category:
            if category not in CATEGORIES:
                raise ValueError("Неизвестная категория модпака.")
            facets.append(["categories:" + category])
        raw = modrinth._get("/search", query=query.strip(), facets=json.dumps(facets), limit=12, offset=offset, index=sort)
        return {"hits": [{"id": modrinth._id(h["project_id"]), "title": h["title"],
                          "description": h.get("description", ""), "author": h.get("author", ""),
                          "icon": modrinth._icon_url(h.get("icon_url"), h["project_id"]),
                          "downloads": h.get("downloads", 0), "versions": h.get("versions", [])[-12:]}
                         for h in raw.get("hits", [])], "total": raw.get("total_hits", 0), "offset": offset, "limit": 12}

    def versions(self, project):
        project = modrinth._id(project)
        meta = modrinth._get("/project/" + project)
        if meta.get("project_type") != "modpack" or meta.get("id") != project:
            raise ValueError("Выбери модпак из каталога.")
        rows = modrinth._get("/project/" + project + "/version", include_changelog="false")
        result = []
        for row in rows:
            if row.get("project_id") != project or row.get("status", "listed") not in {"listed", "archived", "unlisted"}:
                continue
            file = self._release_file(row)
            if file:
                versions, loaders = row.get("game_versions"), row.get("loaders")
                result.append({"id": modrinth._id(row["id"]), "name": row.get("name", ""), "number": row.get("version_number", ""),
                               "type": row.get("version_type", "release"), "published": row.get("date_published", ""),
                               "minecraft": [v for v in versions if isinstance(v, str)] if isinstance(versions, list) else [],
                               "loaders": [v for v in loaders if isinstance(v, str)] if isinstance(loaders, list) else [],
                               "size": file["size"], "steam": steam_variants(versions, loaders)})
        return {"project": project, "title": meta.get("title", project), "description": meta.get("description", ""),
                "versions": sorted(result, key=lambda row: row["published"], reverse=True)}

    @staticmethod
    def _release_file(row):
        files = [file for file in row.get("files", []) if isinstance(file, dict) and str(file.get("filename", "")).lower().endswith(".mrpack")]
        if not files:
            return None
        file = next((f for f in files if f.get("primary")), files[0])
        clean_filename(file["filename"])
        _size(file["size"], MAX_PACK)
        _url(file["url"])
        _hashes(file.get("hashes"))
        return file

    def prepare_release(self, project, version, status=lambda message: None):
        project, version = modrinth._id(project), modrinth._id(version)
        meta = modrinth._get("/project/" + project)
        row = modrinth._get("/version/" + version)
        if meta.get("project_type") != "modpack" or row.get("id") != version or row.get("project_id") != project:
            raise ValueError("Каталог вернул другую сборку или версию.")
        file = self._release_file(row)
        if not file:
            raise ValueError("У этого выпуска нет файла .mrpack.")
        with tempfile.TemporaryDirectory(dir=self.staging, prefix="fetch-") as temp:
            path = Path(temp) / "pack.mrpack"
            status("Загружаем описание и настройки модпака…")
            download_verified(file["url"], path, _hashes(file["hashes"]), file["size"], transfers=self.transfers, status=status, title=meta.get("title", "Модпак"))
            return self.stage_import(path, source_info={"project": project, "version": version})

    def _expire(self):
        now = time.monotonic()
        for token, value in list(self.previews.items()):
            if value["expires"] <= now and not value.get("installing"):
                self.previews.pop(token, None)
                shutil.rmtree(value["path"].parent, ignore_errors=True)

    def stage_import(self, source, *, source_info=None):
        """Own a copy of an uploaded archive; return the explicit install preview."""
        lifecycle.check()
        source_info = source_info or {}
        with self.lock:
            self._expire()
            if len(self.previews) >= 4:
                raise ValueError("Закрой предыдущие окна подготовки модпаков и повтори.")
            directory = Path(tempfile.mkdtemp(dir=self.staging, prefix="preview-"))
            target = directory / "pack.mrpack"
            try:
                if hasattr(source, "read"):
                    source.seek(0)
                    reader, close = source, False
                else:
                    reader, close = Path(source).open("rb"), True
                try:
                    total = 0
                    with target.open("xb") as output:
                        while chunk := reader.read(2**20):
                            lifecycle.check()
                            total += len(chunk)
                            if total > MAX_PACK:
                                raise ValueError("Файл .mrpack превышает 2 ГБ.")
                            output.write(chunk)
                finally:
                    if close:
                        reader.close()
                with target.open("rb") as stream:
                    build_archive._directory_limit(stream)
                    with zipfile.ZipFile(stream) as archive:
                        manifest, _ = _inspect(archive)
                token = secrets.token_urlsafe(24)
                self.previews[token] = {"path": target, "manifest": manifest, "expires": time.monotonic() + 1800,
                                        "source": source_info or {}, "digest": file_hash(target, file_stamp(target), "sha512")}
                return self.preview(token)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise

    def preview(self, token):
        with self.lock:
            self._expire()
            value = self.previews.get(token)
            if not value:
                raise ValueError("Подготовка модпака истекла. Выбери файл или выпуск ещё раз.")
            meta = value["manifest"]
            active = [row for row in meta["files"] if row["env"]["client"] != "unsupported"]
            return {"token": token, **{key: meta[key] for key in ("name", "title", "minecraft", "loader", "loaderVersion", "versionId", "summary")},
                    "steam": e4steam.compatibility(meta["minecraft"], meta["loader"]),
                    "files": len(active), "downloadBytes": sum(row["fileSize"] for row in active if row["env"]["client"] == "required"),
                    "optional": [{"path": row["path"], "size": row["fileSize"]} for row in active if row["env"]["client"] == "optional"],
                    "unsupported": len(meta["files"]) - len(active), "overrideBytes": meta["overrideBytes"]}

    def discard(self, token):
        with self.lock:
            value = self.previews.get(token)
            if value and value.get("installing"):
                value["discardRequested"] = True
                return
            value = self.previews.pop(token, None)
        if value:
            shutil.rmtree(value["path"].parent, ignore_errors=True)

    def install_token(self, token, *, name="", optional=(), status=lambda message: None, allow_without_steam=False):
        allowed = e4steam.notice_permission({"allowWithoutSteam": allow_without_steam})
        with self.lock:
            self.preview(token)
            value = self.previews[token]
            if value.get("installing"):
                raise ValueError("Этот модпак уже устанавливается.")
            value["installing"] = True
        success = False
        try:
            result = self._install_value(value, name, optional, status)
            if allowed:
                profile = self.store.profile(result["id"])
                e4steam.acknowledge_notice(self.store, profile["id"], profile["version"], profile["loader"])
            success = True
            return result
        finally:
            with self.lock:
                value["installing"] = False
                value["expires"] = time.monotonic() + 1800
                discard = success or value.get("discardRequested")
            if discard:
                self.discard(token)

    def _install_value(self, value, name, optional, status):
        path = value["path"]
        if file_hash(path, file_stamp(path), "sha512") != value["digest"]:
            raise ValueError("Файл модпака изменился после проверки. Открой его заново.")
        if not isinstance(optional, (list, tuple)) or any(not isinstance(item, str) for item in optional):
            raise ValueError("Неверный список дополнительных модов.")
        with path.open("rb") as stream:
            build_archive._directory_limit(stream)
            with zipfile.ZipFile(stream) as archive:
                manifest, layers = _inspect(archive)
                allowed_optional = {row["path"] for row in manifest["files"] if row["env"]["client"] == "optional"}
                if set(optional) - allowed_optional:
                    raise ValueError("Дополнительный файл не принадлежит этому модпаку.")
                selected = [row for row in manifest["files"] if row["env"]["client"] == "required" or row["path"] in optional]
                total = manifest["overrideBytes"] + sum(row["fileSize"] for row in selected)
                if total + 32 * 1024**2 > shutil.disk_usage(self.store.instances).free:
                    raise ValueError("Недостаточно места на диске для этого модпака.")
                title = input_profile(name or manifest["name"], manifest["minecraft"], manifest["loader"])[0]
                title = _unique_name(self.store, title)

                def prepare(folder):
                    for number, row in enumerate(selected, 1):
                        lifecycle.check()
                        status(f"Модпак: {number}/{len(selected)} · {row['path']}")
                        destination = folder.joinpath(*_path(row["path"]))
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        error = None
                        for url in row["downloads"]:
                            try:
                                download_verified(url, destination, row["hashes"], row["fileSize"], transfers=self.transfers, status=status, title=row["path"])
                                error = None
                                break
                            except lifecycle.OperationCancelled:
                                raise
                            except (OSError, ValueError) as exc:
                                if type(exc).__name__ == "TransferCancelled":
                                    raise
                                error = exc
                        if error:
                            raise error
                    status("Применяем настройки модпака…")
                    for layer in ("overrides", "client-overrides"):
                        for entry, relative in layers[layer]:
                            lifecycle.check()
                            destination = folder.joinpath(*_path(relative))
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(entry) as source, destination.open("wb") as output:
                                left = entry.file_size
                                while left:
                                    lifecycle.check()
                                    chunk = source.read(min(2**20, left))
                                    if not chunk:
                                        raise ValueError("Файл настроек модпака распакован не полностью.")
                                    output.write(chunk)
                                    left -= len(chunk)
                                if source.read(1):
                                    raise ValueError("Файл модпака превышает объявленный размер.")
                    if manifest["loaderVersion"]:
                        lifecycle.check()
                        (folder / build_archive.STAMP).write_text(json.dumps({"version": manifest["minecraft"], "loader": manifest["loader"], "loader_version": manifest["loaderVersion"]}), encoding="utf-8")
                    (folder / RECEIPT).write_text(json.dumps({"format": 1, "name": title, "version": manifest["versionId"], "source": value["source"], "files": selected}, ensure_ascii=False), encoding="utf-8")

                pid = self.store.create(title, manifest["minecraft"], manifest["loader"], prepare=prepare)
        return {"id": pid, "name": title}

    def import_pack(self, source, status=lambda message: None, *, name="", optional=()):
        preview = self.stage_import(source)
        try:
            return self.install_token(preview["token"], name=name, optional=optional, status=status)
        finally:
            self.discard(preview["token"])

    def export_pack(self, pid, output, status=lambda message: None):
        lifecycle.check()
        profile, folder = self.store.profile(pid), self.store.folder(pid)
        dependencies = {"minecraft": profile["version"]}
        if profile["loader"] != "vanilla":
            stamp = folder / build_archive.STAMP
            if not stamp.is_file() or stamp.is_symlink() or stamp.stat().st_size > 65536:
                raise ValueError("Один раз запусти эту сборку, чтобы закрепить точную версию загрузчика для .mrpack. Обычная выгрузка ZIP доступна уже сейчас.")
            record = json.loads(stamp.read_text(encoding="utf-8"))
            if (record.get("version"), record.get("loader")) != (profile["version"], profile["loader"]) or not record.get("loader_version"):
                raise ValueError("Запусти сборку, чтобы обновить сведения о загрузчике перед выгрузкой .mrpack.")
            dependencies[next(key for key, value in LOADERS.items() if value == profile["loader"])] = build_archive._pin(record["loader_version"])
        status("Проверяем источники дополнений для .mrpack…")
        inventory = modrinth._inventory(self.store, pid)
        recognized = {}
        try:
            identified, _ = modrinth._recognized(inventory)
            for version, filename in identified:
                local = next((path for path in inventory if path.name == filename), None)
                digest = file_hash(local, inventory[local], "sha512") if local else ""
                for file in version.get("files", []):
                    if digest and file.get("hashes", {}).get("sha512") == digest:
                        recognized[(filename, file.get("hashes", {}).get("sha512"))] = file
        except lifecycle.OperationCancelled:
            raise
        except (OSError, ValueError):
            pass  # A local-only pack is valid: unknown files become overrides.
        manifest = {"formatVersion": 1, "game": "minecraft", "versionId": time.strftime("%Y.%m.%d-%H%M%S", time.gmtime()),
                    "name": profile["name"], "summary": "Сборка BlockHarbor", "dependencies": dependencies, "files": []}
        with tempfile.TemporaryFile(dir=self.store.root) as portable:
            build_archive.export_build(self.store, pid, portable, False)
            with zipfile.ZipFile(portable) as source, zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for entry in source.infolist():
                    lifecycle.check()
                    if entry.filename == build_archive.MANIFEST:
                        continue
                    parts = _path(entry.filename)
                    local = folder.joinpath(*parts)
                    digest = file_hash(local, file_stamp(local), "sha512")
                    known = recognized.get((local.name, digest))
                    if known:
                        hashes = _hashes(known.get("hashes"))
                        if hashes["sha512"] == digest and known["size"] == entry.file_size:
                            manifest["files"].append({"path": entry.filename, "hashes": hashes, "downloads": [_url(known["url"])], "fileSize": entry.file_size})
                            continue
                    with source.open(entry) as reader, archive.open("overrides/" + entry.filename, "w") as writer:
                        while chunk := reader.read(2**20):
                            lifecycle.check()
                            writer.write(chunk)
                options = build_archive._pack_options(folder)
                if options:
                    archive.writestr("overrides/options.txt", "\n".join(options) + "\n")
                archive.writestr(INDEX, json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
        output.seek(0)
        return re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", profile["name"]).strip(". ")[:48] + ".mrpack"

    def handle(self, action, p, status=lambda message: None):
        if action == "packSearch":
            return self.search(p)
        if action == "packVersions":
            return self.versions(p["project"])
        if action == "packPreview":
            return self.prepare_release(p["project"], p["version"], status)
        if action == "packState":
            return self.preview(p["token"]) if p.get("token") else {"ready": True}
        if action == "packDiscard":
            self.discard(p.get("token"))
            return {"ok": True}
        if action == "packInstall":
            result = self.install_token(p["token"], name=p.get("name", ""), optional=p.get("optional", []), status=status,
                                        allow_without_steam=e4steam.notice_permission(p))
            return f"Модпак «{result['name']}» установлен в отдельную сборку."
        raise ValueError("Неизвестное действие модпака.")
