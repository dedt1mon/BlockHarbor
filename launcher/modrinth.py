"""Browse compatible Modrinth Java projects and install verified releases per instance."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass
from functools import lru_cache
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import catalog_filters, compatibility, lifecycle
from .content_icons import MAX_IMAGE
from .modfiles import file_hash, file_stamp, mod_info, publish_new
from .store import KINDS, MAX_ADDON, clean_filename, verify_pack

_API = "https://api.modrinth.com/v2"
_TYPES = {"mods": "mod", "shaderpacks": "shader", "resourcepacks": "resourcepack"}
_PAGE = 12
_ICON_URL = re.compile(
    r"https://cdn\.modrinth\.com/data/([A-Za-z0-9]{1,32})/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.(?i:png|jpe?g|webp|gif|bmp|svgz?|rgb|avif|ico)"
)
_HEADERS = {"User-Agent": "BlockHarbor/2.4.8 (Minecraft Java desktop launcher)"}


class CatalogError(ValueError):
    pass


class PlanConflict(CatalogError):
    pass


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9]{1,32}", value):
        raise CatalogError("Недопустимый идентификатор проекта или версии Modrinth.")
    return value


def _category(kind, loader):
    if kind not in _TYPES:
        raise CatalogError("Выбери категорию дополнения.")
    if kind == "mods" and loader == "vanilla":
        raise CatalogError(
            "Для установки модов выбери сборку с загрузчиком, например Fabric или Forge."
        )
    return _TYPES[kind]


def _allowed(url, hosts):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc.lower() not in hosts:
        raise CatalogError("Modrinth вернул неподдерживаемый адрес загрузки.")


class _Redirect(HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _allowed(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_API_HOSTS = {"api.modrinth.com"}
_CDN_HOSTS = {"cdn.modrinth.com", "cdn-raw.modrinth.com"}


def _open(url, hosts, body=None, *, timeout=30):
    lifecycle.check()
    _allowed(url, hosts)
    try:
        return build_opener(_Redirect(hosts)).open(
            Request(
                url,
                data=body,
                headers=(
                    {**_HEADERS, "Content-Type": "application/json"}
                    if body
                    else _HEADERS
                ),
            ),
            timeout=timeout,
        )
    except HTTPError as exc:
        code = exc.code
        exc.close()
        if code == 429:
            raise CatalogError(
                "Modrinth временно ограничил запросы. Подожди немного и повтори."
            ) from None
        if code == 404:
            raise CatalogError(
                "Этот проект или файл больше не доступен на Modrinth."
            ) from None
        raise CatalogError(
            f"Modrinth вернул HTTP {code}. Повтори попытку позже."
        ) from None
    except (OSError, URLError) as exc:
        raise CatalogError(
            "Не удалось подключиться к Modrinth. Проверь интернет и повтори попытку."
        ) from exc


@lru_cache(maxsize=48)
def _get_cached(url, period):
    with lifecycle.response(_open(url, _API_HOSTS)) as response:
        return _read_response(response)


def _read_response(response):
    data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise CatalogError("Ответ каталога слишком большой. Уточни поиск.")
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise CatalogError(
            "Modrinth вернул нечитаемый ответ. Попробуй ещё раз."
        ) from exc


def _get(path, **params):
    lifecycle.check()
    suffix = "?" + urlencode(params) if params else ""
    return _get_cached(_API + path + suffix, int(time.monotonic() // 300))


def _icon_url(value, project):
    match = _ICON_URL.fullmatch(value) if isinstance(value, str) else None
    return value if match and match[1] == project else ""


def dependency_suggestions(query):
    if not isinstance(query, str) or len(query) > 120:
        raise CatalogError("Название зависимости слишком длинное.")
    if not query.strip():
        return {"hits": []}
    result = _get("/search", query=query.strip(), limit=8)
    return {
        "hits": [
            {
                "id": _id(h["project_id"]),
                "title": h["title"],
                "author": h.get("author", ""),
            }
            for h in result["hits"]
        ]
    }


def search(profile, kind, query, offset, filters=None, sort="relevance"):
    category = _category(kind, profile["loader"])
    if len(query) > 180:
        raise CatalogError("Поисковый запрос длиннее 180 символов.")
    offset = int(offset)
    if not 0 <= offset <= 100_000:
        raise CatalogError("Недопустимая страница каталога.")
    facets = [["project_type:" + category], ["versions:" + profile["version"]]]
    if kind == "mods":
        facets.append(["categories:" + profile["loader"]])
    facets.extend(
        catalog_filters.facets(kind, {} if filters is None else filters, sort)
    )
    result = _get(
        "/search",
        query=query.strip(),
        facets=json.dumps(facets, separators=(",", ":")),
        limit=_PAGE,
        offset=offset,
        index=sort,
    )
    return {
        "hits": [
            {
                "id": _id(h["project_id"]),
                "title": h["title"],
                "author": h.get("author", ""),
                "description": h.get("description", ""),
                "downloads": h.get("downloads", 0),
                "icon": _icon_url(h.get("icon_url"), h["project_id"]),
            }
            for h in result["hits"]
        ],
        "offset": offset,
        "limit": _PAGE,
        "total": result["total_hits"],
    }


@dataclass(frozen=True)
class Package:
    version: str
    project: str
    kind: str
    number: str
    name: str
    channel: str
    published: str
    filename: str
    size: int
    sha512: str
    url: str
    deps: tuple
    icon: str = ""
    title: str = ""
    description: str = ""


def _package(v, profile, kind, project, icon="", metadata=None):
    if v["project_id"] != project or profile["version"] not in v["game_versions"]:
        return None
    if kind == "mods" and profile["loader"] not in v["loaders"]:
        return None
    if (
        v.get("status", "listed") not in ("listed", "archived", "unlisted")
        or v.get("environment") == "dedicated_server_only"
    ):
        return None
    files = [
        f
        for f in v["files"]
        if f["filename"].lower().endswith(KINDS[kind]) and not f.get("file_type")
    ]
    if not files:
        return None
    f = next((f for f in files if f.get("primary")), files[0])
    name = clean_filename(f["filename"])
    sha = str(f.get("hashes", {}).get("sha512", "")).lower()
    _allowed(f["url"], _CDN_HOSTS)
    if not re.fullmatch("[a-f0-9]{128}", sha) or not 0 < int(f["size"]) <= MAX_ADDON:
        return None
    return Package(
        _id(v["id"]),
        project,
        kind,
        v["version_number"],
        v["name"],
        v["version_type"],
        v["date_published"],
        name,
        int(f["size"]),
        sha,
        f["url"],
        tuple(v.get("dependencies", [])),
        _icon_url(icon, project),
        str((metadata or {}).get("title", ""))[:180],
        str((metadata or {}).get("description", ""))[:500],
    )


def _project_kind(project, metadata=None):
    metadata = metadata if metadata is not None else _get("/project/" + _id(project))
    if metadata["id"] != project:
        raise CatalogError("Modrinth вернул сведения о другом проекте.")
    for kind, project_type in _TYPES.items():
        if metadata["project_type"] == project_type:
            return kind
    raise CatalogError(
        "Для этой зависимости нужен неподдерживаемый тип проекта Modrinth."
    )


def _releases(profile, kind, project):
    _category(kind, profile["loader"])
    metadata = _get("/project/" + _id(project))
    if _project_kind(project, metadata) != kind:
        raise CatalogError("Выбери проект из текущей категории дополнений.")
    params = {
        "game_versions": json.dumps([profile["version"]]),
        "include_changelog": "false",
    }
    if kind == "mods":
        params["loaders"] = json.dumps([profile["loader"]])
    raw = _get("/project/" + project + "/version", **params)
    items = [
        p
        for v in raw
        if (p := _package(v, profile, kind, project, metadata.get("icon_url", ""), metadata))
    ]
    return sorted(items, key=lambda p: p.published, reverse=True)


def _files(folder, kind):
    return [
        f
        for f in folder.iterdir()
        if f.suffix.lower() == KINDS[kind] and not f.is_symlink() and f.is_file()
    ]


def _installed(files, release):
    for file in files:
        try:
            s = file_stamp(file)
        except FileNotFoundError:
            continue
        if s[0] == release.size and file_hash(file, s, "sha512") == release.sha512:
            return file
    return None


def versions(store, pid, kind, project):
    profile = store.profile(pid)
    releases = _releases(profile, kind, _id(project))
    existing = _files(store.content_folder(pid, kind), kind)
    return {
        "versions": [
            {
                "id": r.version,
                "number": r.number,
                "name": r.name,
                "type": r.channel,
                "published": r.published,
                "filename": r.filename,
                "size": r.size,
                "installed": bool(_installed(existing, r)),
            }
            for r in releases
        ]
    }


def _plan(store, profile, kind, project, version):
    # Resolve the whole graph before touching the game. Unpinned dependencies reuse a
    # compatible local release and otherwise take the newest stable compatible file.
    pid = profile["id"]
    queue = deque([(project, version, kind)])
    chosen = {}
    constraints = {}
    while queue:
        project, pinned, expected_kind = queue.popleft()
        if pinned and project in constraints and constraints[project] != pinned:
            raise PlanConflict(
                "Проекты требуют разные версии одной зависимости. Выбери другую версию дополнения."
            )
        if pinned:
            constraints[project] = pinned
        if project in chosen and (not pinned or chosen[project].version == pinned):
            continue
        if len(chosen) >= 64 or len(queue) > 256:
            raise CatalogError("Слишком много зависимостей в одной установке.")
        category = expected_kind or _project_kind(project)
        compatible = _releases(profile, category, project)
        files = _files(store.content_folder(pid, category), category)
        present = [r for r in compatible if _installed(files, r)]
        if pinned:
            release = next((r for r in compatible if r.version == pinned), None)
            if release is None:
                # Version-pinned dependencies can be unlisted but remain downloadable.
                release = _package(
                    _get("/version/" + _id(pinned)), profile, category, project
                )
        else:
            release = (
                next(iter(present), None)
                or next((r for r in compatible if r.channel == "release"), None)
                or next(iter(compatible), None)
            )
        if release is None or (pinned and release.version != pinned):
            raise PlanConflict(
                f"На Modrinth нет совместимого файла для {_get('/project/' + project).get('title') or project} и этой сборки."
            )
        other = next((r for r in present if r.sha512 != release.sha512), None)
        if other:
            actual = _installed(files, other)
            actual_name = actual.name if actual else other.filename
            raise PlanConflict(
                f"Для установки {release.filename} нужно убрать другую версию ({actual_name}) или выбрать установленный выпуск."
            )
        chosen[project] = release
        for dep in release.deps:
            if dep.get("dependency_type") != "required":
                continue
            required, exact = dep.get("project_id"), dep.get("version_id")
            if exact:
                exact = _id(exact)
                if not required:
                    required = _get("/version/" + exact)["project_id"]
            if not required:
                external = dep.get("file_name")
                if external and any(f.name == external for f in _inventory(store, pid)):
                    continue
                raise PlanConflict(
                    f"{release.filename} требует внешний файл {external or 'без имени'}. Для него нужна ручная установка."
                )
            queue.append((_id(required), exact, None))
    # Only keep dependencies reachable from the final pinned choices.
    reachable = set()
    ordered = []

    def visit(key):
        if key in reachable:
            return
        reachable.add(key)
        for dep in chosen[key].deps:
            if dep.get("dependency_type") == "required" and (
                dep.get("project_id") or dep.get("version_id")
            ):
                nxt = (
                    dep.get("project_id")
                    or _get("/version/" + _id(dep["version_id"]))["project_id"]
                )
                visit(nxt)
        ordered.append(chosen[key])

    visit(next(iter(chosen)))
    return ordered


@lru_cache(maxsize=32)
def _versions_by_hash(digests, period):
    body = json.dumps({"algorithm": "sha512", "hashes": digests}).encode("ascii")
    with lifecycle.response(_open(_API + "/version_files", _API_HOSTS, body)) as response:
        return _read_response(response)


def _inventory(store, pid):
    return {
        path: file_stamp(path)
        for kind in _TYPES
        for path in _files(store.content_folder(pid, kind), kind)
    }


def _recognized(inventory):
    """Identify installed releases even when their files were renamed or imported manually."""
    by_digest = {}
    total = 0
    limited = False
    # Inspect smaller JARs first when a very large resource pack collection is present.
    for path, stamp in sorted(
        inventory.items(),
        key=lambda row: (row[0].suffix != ".jar", row[1][0], row[0].name),
    ):
        if len(by_digest) >= 1024 or total + stamp[0] > 4 * 1024**3:
            limited = True
            continue
        total += stamp[0]
        digest = file_hash(path, stamp, "sha512")
        by_digest.setdefault(digest, []).append(path.name)
    keys = sorted(by_digest)
    identified = []
    for start in range(0, len(keys), 32):
        chunk = tuple(keys[start : start + 32])
        found = _versions_by_hash(chunk, int(time.monotonic() // 300))
        for digest in chunk:
            v = found.get(digest)
            if not isinstance(v, dict) or not any(
                f.get("hashes", {}).get("sha512") == digest for f in v.get("files", [])
            ):
                continue
            identified.extend((v, filename) for filename in by_digest[digest])
    return identified, limited


def _catalog_report(releases, inventory):
    result = compatibility.report()
    existing, limited = _recognized(inventory)
    if limited:
        result["warnings"].append(
            "Каталог проверил до 1024 файлов и 4 ГБ данных. Для остальных файлов доступны проверки по данным загрузчика."
        )
    # (project id, exact version id, filename, dependencies, part of the install plan)
    items = [(r.project, r.version, r.filename, r.deps, True) for r in releases]
    items += [
        (v["project_id"], v["id"], filename, v.get("dependencies", []), False)
        for v, filename in existing
    ]

    def installed_name(project, version):
        return next(
            (
                f
                for p, v, f, _, planned in items
                if not planned
                and (not project or p == project)
                and (not version or v == version)
            ),
            "",
        )

    for r in releases[:-1]:
        filename = installed_name(r.project, r.version)
        result["required"].append(
            {
                "name": _get("/project/" + r.project).get("title") or r.name,
                "filename": filename or r.filename,
                "installed": bool(filename),
                "version": r.number,
            }
        )
    for project, version, filename, deps, planned in items:
        for dep in deps:
            typ = dep.get("dependency_type")
            target, pin = dep.get("project_id"), dep.get("version_id")
            external = dep.get("file_name") or ""
            if planned and typ == "required" and external and not (target or pin):
                row = {"name": external, "filename": external, "installed": True}
                if row not in result["required"]:
                    result["required"].append(row)
            elif typ == "incompatible":
                for p, v, f, _, is_planned in items:
                    if not (planned or is_planned) or (p, v) == (project, version):
                        continue
                    if (
                        (target or pin)
                        and (not target or p == target)
                        and (not pin or v == pin)
                    ):
                        pair = " и ".join(sorted((filename, f)))
                        result["warnings"].append(
                            f"Автор на Modrinth отмечает несовместимость: {pair}. Можно выбрать другой выпуск или убрать конфликтующий файл."
                        )
                if planned and not (target or pin) and external:
                    result["warnings"].append(
                        f"Автор {filename} указывает несовместимость с внешним файлом {external}."
                    )
            elif planned and typ in ("optional", "embedded"):
                try:
                    pinned = _get("/version/" + _id(pin)) if pin else {}
                    target = target or pinned.get("project_id")
                    meta = _get("/project/" + _id(target)) if target else {}
                except CatalogError:
                    pinned, meta = {}, {}
                found = installed_name(target, pin) if target or pin else ""
                row = {
                    "name": meta.get("title")
                    or target
                    or external
                    or "Внешняя зависимость",
                    "slug": meta.get("slug", ""),
                    "filename": found or external,
                    "installed": bool(found),
                }
                if pin:
                    row["version"] = pinned.get("version_number", pin)
                if row not in result[typ]:
                    result[typ].append(row)
    result["warnings"] = sorted(set(result["warnings"]))
    result["blocked"] = bool(result["conflicts"])
    return result


class NeedsReview(CatalogError):
    def __init__(self, request, token, warnings):
        super().__init__("Перед установкой проверь предупреждения.")
        self.review = {"request": request, "token": token, "warnings": warnings}


def _review_token(request, releases, inventory, warnings):
    if not warnings:
        return ""
    state = [
        request,
        [(r.project, r.version, r.sha512) for r in releases],
        sorted(
            (path.parent.name, path.name, stamp) for path, stamp in inventory.items()
        ),
        sorted(set(warnings)),
    ]
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()


def _staged_warnings(local, catalog):
    warnings = list(local["warnings"])

    # A recommendation already visible in the catalog is not a new finding.
    def key(value):
        return "".join(ch for ch in value.casefold() if ch.isalnum())

    advertised = {
        key(text)
        for row in catalog["optional"]
        for text in (row["name"], row.get("slug", ""))
        if text
    }
    for group in ("required", "optional"):
        for row in local[group]:
            if row["installed"] or (
                group == "optional" and key(row["id"]) in advertised
            ):
                continue
            relation = (
                "требует"
                if group == "required"
                else "рекомендует"
                if row["recommended"]
                else "поддерживает необязательное дополнение"
            )
            bound = "" if row["version"] == "*" else " " + row["version"]
            warnings.append(
                f"{row['source']} {relation} {row['name']}{bound}. В сборке подходящий файл не найден."
            )
    return warnings


def check(store, pid, kind, project, version):
    try:
        releases = _plan(store, store.profile(pid), kind, _id(project), _id(version))
    except PlanConflict as exc:
        result = compatibility.report()
        result.update(blocked=True, conflicts=[str(exc)], reviewToken="")
        return result
    inventory = _inventory(store, pid)
    result = _catalog_report(releases, inventory)
    request = {"id": pid, "kind": kind, "project": project, "version": version}
    result["reviewToken"] = _review_token(
        request, releases, inventory, result["warnings"]
    )
    return result


def manual_check(store, pid, kind, name, path):
    name = clean_filename(name)
    profile = store.profile(pid)
    if kind not in KINDS or Path(name).suffix.lower() != KINDS[kind]:
        raise CatalogError("Выбери файл соответствующей категории.")
    verify_pack(path, kind, profile["loader"])
    folder = store.content_folder(pid, kind)
    if os.path.lexists(folder / name):
        raise PlanConflict("Файл с таким именем уже есть в сборке.")
    if kind != "mods":
        return compatibility.report()
    return compatibility.local_report(
        profile, {f.name: f for f in _files(folder, kind)}, {name: path}
    )


def _require_no_conflicts(report):
    if report["blocked"]:
        raise PlanConflict(" ".join(report["conflicts"][:3]))


def _download(release, target, transfer=None, status=lambda _message: None):
    if transfer is not None:
        transfer.download(release.url, target, title=release.filename, hashes={"sha512": release.sha512}, size=release.size, max_size=MAX_ADDON, hosts=_CDN_HOSTS, category="modrinth", status=status)
        return
    digest = hashlib.sha512()
    with lifecycle.response(_open(release.url, _CDN_HOSTS)) as src, target.open("xb") as out:
        remaining = release.size
        while remaining:
            chunk = src.read(min(256 * 1024, remaining))
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining or src.read(1) or digest.hexdigest() != release.sha512:
            raise CatalogError(
                "Контрольная сумма или размер скачанного файла не совпали с данными Modrinth."
            )


def _publish_set(store, pid, releases, staged):
    lifecycle.check()
    # Recheck all destinations after downloading. Manual imports may have completed
    # during network I/O. The launcher's mutation lock covers just this local commit.
    pending = []
    mod_archives = _files(store.content_folder(pid, "mods"), "mods")
    known_mods = (
        {file: mod_info(file, file_stamp(file))[0] for file in mod_archives}
        if any(r.kind == "mods" for r in releases)
        else {}
    )
    for release in releases:
        folder = store.content_folder(pid, release.kind)
        files = _files(folder, release.kind)
        existing = _installed(files, release)
        if existing:
            continue
        src = staged.get(release.version)
        if src is None:
            raise CatalogError(
                "Файлы сборки изменились во время загрузки. Повтори установку."
            )
        target = folder / release.filename
        if os.path.lexists(target):
            raise CatalogError(
                f"Файл {release.filename} уже есть и отличается от версии Modrinth."
            )
        if release.kind == "mods":
            ids = mod_info(src, file_stamp(src))[0] - {""}
            for file, other_ids in known_mods.items():
                if ids & other_ids:
                    raise CatalogError(
                        f"Мод из {release.filename} уже представлен другим файлом ({file.name}). Удали конфликтующий файл перед установкой."
                    )
            known_mods[target] = ids
        pending.append((release, src, target))
    with lifecycle.guard():
        written = []
        try:
            for release, src, target in pending:
                publish_new(src, target)
                written.append((target, release))
        except OSError as error:
            for path, release in reversed(written):
                if _installed([path], release):
                    path.unlink()
            raise CatalogError(
                "Содержимое сборки изменилось или файл занят. Повтори установку."
            ) from error
    return len(written)


def _save_project_icons(store, pid, releases, status):
    # Project metadata was already fetched for compatibility. Artwork is optional,
    # so use a short total budget and never turn a successful install into an error.
    deadline = time.monotonic() + 6
    announced = False
    for release in releases:
        if not release.icon or time.monotonic() >= deadline:
            continue
        try:
            file = _installed(
                _files(store.content_folder(pid, release.kind), release.kind), release
            )
            if not file:
                continue
            stamp = file_stamp(file)
            if store.icons.icon(file, stamp):
                continue
            if not announced:
                status("Сохраняем изображения дополнений…")
                announced = True
            data = bytearray()
            with lifecycle.response(_open(
                release.icon,
                _CDN_HOSTS,
                timeout=min(3, max(0.1, deadline - time.monotonic())),
            )) as response:
                read = getattr(response, "read1", response.read)
                while time.monotonic() < deadline:
                    chunk = read(min(64 * 1024, MAX_IMAGE + 1 - len(data)))
                    if not chunk:
                        store.icons.remember_project(
                            file, stamp, bytes(data), release.project, release.version
                        )
                        break
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE:
                        break
        except lifecycle.OperationCancelled:
            raise
        except (OSError, ValueError, HTTPException):
            continue


def install(
    store, pid, kind, project, version, status, mutation_lock=None, acknowledgement="", *, before_publish=None, transfer=None, after_publish=None
):
    profile = store.profile(pid)
    status("Проверяем совместимость и обязательные зависимости…")
    releases = _plan(store, profile, kind, _id(project), _id(version))
    inventory = _inventory(store, pid)
    request = {"id": pid, "kind": kind, "project": project, "version": version}
    catalog = _catalog_report(releases, inventory)
    _require_no_conflicts(catalog)
    warnings = list(catalog["warnings"])
    if warnings and not acknowledgement:
        raise NeedsReview(
            request, _review_token(request, releases, inventory, warnings), warnings
        )
    with tempfile.TemporaryDirectory(
        dir=store.folder(pid), prefix=".modrinth-"
    ) as work:
        staged = {}
        for release in releases:
            files = _files(store.content_folder(pid, release.kind), release.kind)
            if _installed(files, release):
                continue
            status("Скачиваем " + release.filename + "…")
            tmp = Path(work) / release.version
            if transfer is None:
                _download(release, tmp)
            else:
                _download(release, tmp, transfer, status)
            verify_pack(tmp, release.kind, profile["loader"])
            staged[release.version] = tmp
        with mutation_lock or nullcontext():
            if _inventory(store, pid) != inventory:
                raise CatalogError(
                    "Файлы сборки изменились во время загрузки. Повтори проверку."
                )
            additions = {
                r.filename: staged[r.version]
                for r in releases
                if r.kind == "mods" and r.version in staged
            }
            if additions:
                active = {
                    f.name: f
                    for f in inventory
                    if f.parent == store.content_folder(pid, "mods")
                }
                inspected = compatibility.local_report(profile, active, additions)
                _require_no_conflicts(inspected)
                warnings.extend(_staged_warnings(inspected, catalog))
            warnings = sorted(set(warnings))
            token = _review_token(request, releases, inventory, warnings)
            if token and token != acknowledgement:
                raise NeedsReview(request, token, warnings)
            if staged and before_publish is not None:
                before_publish()
            written = _publish_set(store, pid, releases, staged)
    _save_project_icons(store, pid, releases, status)
    if after_publish is not None:
        after_publish(releases)
    if not written:
        return "Эта версия уже установлена в сборке."
    return f"Установлено файлов из Modrinth: {written}."
