"""Browse compatible Java add-ons from the community catalog and reviewed publisher releases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass, replace
from functools import lru_cache
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import catalog_filters, compatibility, lifecycle, trusted_catalog
from .content_icons import MAX_IMAGE
from .modfiles import file_hash, file_stamp, mod_info, publish_new
from .store import KINDS, MAX_ADDON, PackLoaderMismatch, clean_filename, verify_pack

_API = "https://api.modrinth.com/v2"
_TYPES = {"mods": "mod", "shaderpacks": "shader", "resourcepacks": "resourcepack"}
_PAGE = 12
_ICON_URL = re.compile(
    r"https://cdn\.modrinth\.com/data/([A-Za-z0-9]{1,32})/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.(?i:png|jpe?g|webp|gif|bmp|svgz?|rgb|avif|ico)"
)
_HEADERS = {"User-Agent": "BlockHarbor/2.6.0 (Minecraft Java desktop launcher)"}


class CatalogError(ValueError):
    pass


class CatalogNotFound(CatalogError):
    pass


class PlanConflict(CatalogError):
    pass


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9]{1,32}", value):
        raise CatalogError("Недопустимый идентификатор проекта или версии.")
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
        raise CatalogError("Каталог вернул неподдерживаемый адрес загрузки.")


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
                "Сервис временно ограничил запросы. Подожди немного и повтори."
            ) from None
        if code == 404:
            raise CatalogNotFound(
                "Этот проект или файл больше не доступен в каталоге."
            ) from None
        raise CatalogError(
            f"Каталог вернул HTTP {code}. Повтори попытку позже."
        ) from None
    except (OSError, URLError) as exc:
        raise CatalogError(
            "Не удалось подключиться к каталогу. Проверь интернет и повтори попытку."
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
            "Каталог вернул нечитаемый ответ. Попробуй ещё раз."
        ) from exc


def _get(path, **params):
    lifecycle.check()
    if path.startswith("/version/") and (reviewed := trusted_catalog.version(path.removeprefix("/version/"))):
        return reviewed
    if path.startswith("/project/") and (reviewed := trusted_catalog.project(path.removeprefix("/project/"))):
        return reviewed
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


def search(profile, kind, query, offset, filters=None, sort="relevance", source="community"):
    category = _category(kind, profile["loader"])
    if len(query) > 180:
        raise CatalogError("Поисковый запрос длиннее 180 символов.")
    offset = int(offset)
    if not 0 <= offset <= 100_000:
        raise CatalogError("Недопустимая страница каталога.")
    facets = [["project_type:" + category], ["versions:" + profile["version"]]]
    if kind == "mods":
        facets.append(["categories:" + profile["loader"]])
    extra_facets = catalog_filters.facets(kind, {} if filters is None else filters, sort)
    facets.extend(extra_facets)
    if source not in {"all", "github", "community"}:
        raise CatalogError("Неизвестный источник дополнений.")
    publisher = [] if source == "community" else trusted_catalog.search(profile, kind, category, query, extra_facets)
    if source == "github":
        ordered = _order_hits(publisher, sort)
        return {"hits": ordered[offset:offset + _PAGE], "offset": offset, "limit": _PAGE, "total": len(ordered)}

    # Pages refer to one combined, duplicate-free list. Skip publisher projects
    # on the remote side only if they satisfy this profile, text and filters.
    facets.extend([["project_id!=" + hit["id"]] for hit in publisher if not hit["id"].startswith("BHghP")])
    start = max(0, offset - len(publisher))
    try:
        result = _get(
            "/search", query=query.strip(), facets=json.dumps(facets, separators=(",", ":")),
            limit=_PAGE + len(publisher), offset=start, index=sort,
        )
    except CatalogError:
        if source != "all":
            raise
        ordered = _order_hits(publisher, sort)
        return {"hits": ordered[offset:offset + _PAGE], "offset": offset, "limit": _PAGE,
                "total": len(ordered), "partial": True,
                "message": "Основной каталог недоступен. Показаны подходящие выпуски авторов с GitHub."}
    remote = [
        {"id": _id(h["project_id"]), "title": h["title"], "author": h.get("author", ""),
         "description": h.get("description", ""), "downloads": h.get("downloads", 0),
         "icon": _icon_url(h.get("icon_url"), h["project_id"]), "follows": h.get("follows", 0),
         "newest": h.get("date_created", ""), "updated": h.get("date_modified", ""),
         "source": "community", "sourceTitle": "Основной каталог"}
        for h in result["hits"]
    ]
    if not publisher:
        return {"hits": remote, "offset": offset, "limit": _PAGE, "total": result["total_hits"]}
    for hit in publisher:
        # Opening a mixed result also lists newer releases from the main catalog.
        if not hit["id"].startswith("BHghP"):
            hit.update(source="all", sourceTitle="Каталог и GitHub автора")
    ordered = _order_hits(publisher + remote, sort)
    index = offset - start
    return {"hits": ordered[index:index + _PAGE], "offset": offset, "limit": _PAGE, "total": result["total_hits"] + len(publisher)}


def _order_hits(hits, sort):
    if sort == "relevance":
        return hits
    return sorted(hits, key=lambda hit: hit[sort], reverse=True)


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
    renderers: tuple = ()
    alternatives: tuple = ()


# Exclude development/source/installer artifacts even if a publisher did not
# label their file_type in the catalog. An ordinary API/library mod is allowed.
_NON_RUNTIME_JAR = re.compile(r"[-_.](?:sources?|javadoc|dev|deobf|installer)\.jar$", re.I)
_LOADER_NAMES = {"fabric", "quilt", "forge", "neoforge"}
_MAX_ARTIFACTS = 8


def _artifact(file, project, version):
    name = clean_filename(file["filename"])
    sha = str(file.get("hashes", {}).get("sha512", "")).lower()
    if not trusted_catalog.is_publisher_file(project, version, file):
        _allowed(file["url"], _CDN_HOSTS)
    size = int(file["size"])
    if not re.fullmatch("[a-f0-9]{128}", sha) or not 0 < size <= MAX_ADDON:
        return None
    return name, size, sha, file["url"]


def _artifact_rank(file, loader):
    # File names are a preference only; actual JAR metadata remains mandatory.
    tokens = set(re.split(r"[^a-z0-9]+", file["filename"].casefold()))
    named = tokens & _LOADER_NAMES
    if loader in named:
        match = 0
    elif not named or (loader == "quilt" and named == {"fabric"}):
        match = 1
    else:
        match = 2
    return match, not bool(file.get("primary"))


def _choices(release):
    yield release
    for filename, size, sha512, url in release.alternatives:
        yield replace(release, filename=filename, size=size, sha512=sha512,
                      url=url, alternatives=())


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
        f for f in v["files"]
        if f["filename"].lower().endswith(KINDS[kind])
        and f.get("file_type") in (None, "", "unknown")
        and (kind != "mods" or not _NON_RUNTIME_JAR.search(f["filename"]))
    ]
    if not files:
        return None
    if kind == "mods" and len(files) > 1:
        files.sort(key=lambda f: _artifact_rank(f, profile["loader"]))
    else:
        files.sort(key=lambda f: not bool(f.get("primary")))
    # Validate the preferred artifact and every bounded alternative before
    # constructing a download plan. Never infer a URL from the file name.
    artifacts = []
    for f in files[:_MAX_ARTIFACTS if kind == "mods" else 1]:
        artifact = _artifact(f, project, v.get("id"))
        if artifact and artifact not in artifacts:
            artifacts.append(artifact)
    if not artifacts:
        return None
    name, size, sha, url = artifacts[0]
    renderers = {"iris": "Iris", "optifine": "OptiFine", "canvas": "Canvas", "vanilla": "Vanilla"}
    return Package(
        _id(v["id"]),
        project,
        kind,
        v["version_number"],
        v["name"],
        v["version_type"],
        v["date_published"],
        name,
        size,
        sha,
        url,
        tuple(v.get("dependencies", [])),
        _icon_url(icon, project),
        str((metadata or {}).get("title", ""))[:180],
        str((metadata or {}).get("description", ""))[:500],
        tuple(renderers[r] for r in v["loaders"] if r in renderers) if kind == "shaderpacks" else (),
        tuple(artifacts[1:]),
    )


def _project_kind(project, metadata=None):
    metadata = metadata if metadata is not None else trusted_catalog.project(project) or _get("/project/" + _id(project))
    if metadata["id"] != project:
        raise CatalogError("Каталог вернул сведения о другом проекте.")
    for kind, project_type in _TYPES.items():
        if metadata["project_type"] == project_type:
            return kind
    raise CatalogError(
        "Для этой зависимости нужен неподдерживаемый тип проекта."
    )


def _releases(profile, kind, project, source="all"):
    if source not in {"all", "community", "github"}:
        raise CatalogError("Неизвестный источник дополнений.")
    bundled = trusted_catalog.project(project)
    if source == "community" or (not bundled and source == "all"):
        return _community_releases(profile, kind, project)
    if not bundled:
        return []
    _category(kind, profile["loader"])
    if _project_kind(project, bundled) != kind:
        raise CatalogError("Выбери проект из текущей категории дополнений.")
    author = [_package(v, profile, kind, project, bundled.get("icon_url", ""), bundled)
              for v in trusted_catalog.compatible(profile, kind, project)]
    author = [r for r in author if r is not None]
    if source == "github" or project.startswith("BHghP"):
        return author
    try:
        community = _community_releases(profile, kind, project)
    except CatalogError:
        if author:
            return author
        raise
    # Keep canonical community IDs for the normal and automatic update paths.
    # The GitHub-only view returns the publisher ID, which selects its own URL.
    hashes = {r.sha512 for r in community}
    return sorted(community + [r for r in author if r.sha512 not in hashes], key=lambda r: r.published, reverse=True)


def _community_releases(profile, kind, project):
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


def _prefer_installed(files, release, loader):
    """Keep a verified alternative already installed, including renamed files."""
    for candidate in _choices(release):
        path = _installed(files, candidate)
        if not path:
            continue
        if candidate.kind == "mods":
            try:
                verify_pack(path, candidate.kind, loader)
            except PackLoaderMismatch:
                continue
        return candidate
    return release


def versions(store, pid, kind, project, source="all"):
    profile = store.profile(pid)
    releases = _releases(profile, kind, _id(project), source=source)
    existing = _files(store.content_folder(pid, kind), kind)
    releases = [_prefer_installed(existing, r, profile["loader"]) for r in releases]
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
                "source": "github" if trusted_catalog.has_version(r.version) else "community",
                "renderers": list(r.renderers),
            }
            for r in releases
        ]
    }


def _selections(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 32:
        raise CatalogError("Выбери от 1 до 32 дополнений за одну установку.")
    selected = {}
    for row in items:
        if not isinstance(row, dict) or row.get("kind") not in _TYPES:
            raise CatalogError("Некорректная категория выбранного дополнения.")
        project = _id(row.get("project"))
        version = _id(row["version"]) if row.get("version") is not None else None
        item = {"kind": row["kind"], "project": project, "version": version}
        source = row.get("source", "all")
        if source not in ("all", "community", "github"):
            raise CatalogError("Неизвестный источник дополнений.")
        if source != "all":
            item["source"] = source
        if project in selected and selected[project] != item:
            raise PlanConflict("Один проект выбран несколько раз с разными параметрами.")
        selected[project] = item
    return list(selected.values())


def _plan(store, profile, kind, project, version):
    return _plan_items(store, profile, [{"project": project, "version": version, "kind": kind}])


def _plan_items(store, profile, items):
    # Rebuild only when a pin arrives from another branch. Constraints of a superseded
    # version must disappear with that version; otherwise an unused branch can block a basket.
    pid = profile["id"]
    roots = {item["project"]: item for item in items}
    inventory = _inventory(store, pid)
    by_kind = {kind: [p for p in inventory if p.parent.name == kind] for kind in _TYPES}
    filenames = {p.name for p in inventory}

    @lru_cache(maxsize=128)
    def candidates(project, category, source):
        if not category:
            category = _project_kind(project)
        releases = _releases(profile, category, project) if source == "all" else _releases(profile, category, project, source=source)
        releases = [_prefer_installed(by_kind[category], r, profile["loader"]) for r in releases]
        present = [(r, file) for r in releases if (file := _installed(by_kind[category], r))]
        return category, releases, present

    @lru_cache(maxsize=256)
    def exact_version(pin):
        return _get("/version/" + _id(pin))

    root_pins = {p: row["version"] for p, row in roots.items() if row["version"]}
    pins = dict(root_pins)
    for _ in range(32):
        targets = {p: {pin} for p, pin in root_pins.items()}
        chosen, edges, conflicts = {}, {}, []
        queue = deque(roots)
        while queue:
            project = queue.popleft()
            if project in chosen:
                continue
            if len(chosen) >= 128 or len(queue) > 512:
                raise CatalogError("Слишком много зависимостей в одной установке.")
            row = roots.get(project, {})
            pin = pins.get(project)
            source = "github" if pin and pin.startswith("BHghV") else row.get("source", "all")
            category, compatible, present = candidates(project, row.get("kind"), source)
            if pin:
                # One verified binary can have distinct IDs at the two providers.
                pin_id = trusted_catalog.canonical_version(pin)
                release = next((r for r in compatible if trusted_catalog.canonical_version(r.version) == pin_id), None)
                if release is None and source != "github":
                    release = _package(exact_version(pin), profile, category, project)
                    if release is not None:
                        release = _prefer_installed(by_kind[category], release, profile["loader"])
            else:
                stable = next((r for r in compatible if r.channel == "release"), None)
                release = present[0][0] if present else stable or next(iter(compatible), None)
            edges[project] = []
            chosen[project] = release
            if release is None or (pin and trusted_catalog.canonical_version(release.version) != pin_id):
                conflicts.append(f"В каталоге нет совместимого файла для {project} и этой сборки.")
                continue
            for installed, file in present:
                if installed.sha512 != release.sha512:
                    conflicts.append(f"Для установки {release.filename} нужно убрать другую версию ({file.name}) или выбрать установленный выпуск.")
                    break
            for dep in release.deps:
                if dep.get("dependency_type") != "required":
                    continue
                nxt, exact = dep.get("project_id"), dep.get("version_id")
                if exact:
                    exact = _id(exact)
                    if not nxt:
                        nxt = exact_version(exact)["project_id"]
                if not nxt:
                    external = dep.get("file_name")
                    if not external or external not in filenames:
                        conflicts.append(f"{release.filename} требует внешний файл {external or 'без имени'}. Для него нужна ручная установка.")
                    continue
                nxt = _id(nxt)
                edges[project].append(nxt)
                if exact:
                    targets.setdefault(nxt, set()).add(exact)
                queue.append(nxt)
        next_pins = {p: root_pins.get(p, min(versions)) for p, versions in targets.items()}
        if pins != next_pins:
            pins = next_pins
            continue
        if any(len({trusted_catalog.canonical_version(v) for v in versions}) > 1 for versions in targets.values()):
            raise PlanConflict("Проекты требуют разные версии одной зависимости. Выбери другой выпуск дополнения.")
        if conflicts:
            raise PlanConflict(conflicts[0])
        break
    else:
        raise PlanConflict("Не удалось согласовать версии общих зависимостей. Выбери другой выпуск.")
    visited, ordered = set(), []

    def visit(project):
        if project in visited:
            return
        visited.add(project)
        for child in edges[project]:
            visit(child)
        ordered.append(chosen[project])

    for project in roots:
        visit(project)
    return ordered


@lru_cache(maxsize=32)
def _versions_by_hash(digests, period):
    local = {digest: v for digest in digests if (v := trusted_catalog.by_hash(digest))}
    unknown = [digest for digest in digests if digest not in local]
    if not unknown:
        return local
    body = json.dumps({"algorithm": "sha512", "hashes": unknown}).encode("ascii")
    with lifecycle.response(_open(_API + "/version_files", _API_HOSTS, body)) as response:
        return {**_read_response(response), **local}


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


def _catalog_report(releases, inventory, roots=None):
    result = compatibility.report()
    try:
        existing, limited = _recognized(inventory)
    except CatalogError:
        if not releases or any(not r.version.startswith("BHghV") for r in releases):
            raise
        existing, limited = [], False
        result["warnings"].append("Сервис распознавания установленных файлов недоступен. Проверим описания модов в самих файлах.")
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
                and (not version or trusted_catalog.canonical_version(v) == trusted_catalog.canonical_version(version))
            ),
            "",
        )

    selected = {releases[-1].project} if roots is None else set(roots)
    for r in releases:
        if r.project in selected:
            continue
        existing_file = _installed([p for p in inventory if p.parent.name == r.kind], r)
        filename = existing_file.name if existing_file else installed_name(r.project, r.version)
        result["required"].append(
            {
                "name": r.title or r.name,
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
                        and (not pin or trusted_catalog.canonical_version(v) == trusted_catalog.canonical_version(pin))
                    ):
                        pair = " и ".join(sorted((filename, f)))
                        result["warnings"].append(
                            f"Автор отмечает несовместимость: {pair}. Можно выбрать другой выпуск или убрать конфликтующий файл."
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
    request = {"id": pid, "kind": kind, "project": project, "version": version}
    return check_selection(store, pid, [request], request=request)


def check_selection(store, pid, items, *, request=None):
    profile = compatibility.installation_profile(store, pid)
    try:
        selected = _selections(items)
        releases = _plan_items(store, profile, selected)
    except PlanConflict as exc:
        result = compatibility.report()
        result.update(blocked=True, conflicts=[str(exc)], reviewToken="")
        return result
    inventory = _inventory(store, pid)
    roots = {r["project"] for r in selected}
    result = _catalog_report(releases, inventory, roots)
    result["selected"] = [
        {"project": r.project, "name": r.title or r.name, "version": r.number, "filename": r.filename,
         "installed": bool(_installed([p for p in inventory if p.parent.name == r.kind], r))}
        for r in releases if r.project in roots
    ]
    by_project = {r.project: r for r in releases}
    result["items"] = [{**item, "version": by_project[item["project"]].version} for item in selected]
    request = request or {"id": pid, "items": result["items"]}
    result["reviewToken"] = _review_token(request, releases, inventory, result["warnings"])
    return result


# These are loader mod IDs whose official catalog slug differs from the ID in the JAR.
# For other IDs use an exact slug only. The downloaded archive must actually provide the ID.
_DEPENDENCY_SLUGS = {
    "fabric": "fabric-api", "fabric_api": "fabric-api", "cloth_config": "cloth-config",
    "cloth-config2": "cloth-config", "forgeconfigapiport": "forge-config-api-port",
    "architectury": "architectury-api", "roughlyenoughitems": "rei",
    "resourcefullib": "resourceful-lib", "resourcefulconfig": "resourceful-config",
    "balm-fabric": "balm",
}


def _unresolved(rows):
    details = "; ".join(
        f"{r['source']} требует {r['id']} {r['version']}" for r in rows[:6]
    )
    raise PlanConflict(
        details + ". Совместимый файл зависимости не найден. Добавь подходящую версию вручную "
        "или выбери другой выпуск мода."
    )


def _descriptor_dependencies(profile, missing):
    requirements = {}
    for row in missing:
        requirements.setdefault(row["id"], []).append(row)
    items = {}
    for mod_id, rows in requirements.items():
        slug = _DEPENDENCY_SLUGS.get(mod_id, mod_id)
        normalized = slug.replace("_", "-")
        for candidate in dict.fromkeys((slug, normalized, mod_id)):
            try:
                metadata = _get("/project/" + candidate)
            except CatalogNotFound:
                continue
            project = metadata.get("id")
            if not project or metadata.get("project_type") != "mod":
                continue
            releases = _releases(profile, "mods", _id(project))
            bounds = [row["version"].split(" / ") for row in rows]
            suitable = [
                release for release in releases
                if all(compatibility._any_match(release.number, group) is not False for group in bounds)
            ]
            if not suitable:
                continue
            picked = next((r for r in suitable if r.channel == "release"), suitable[0])
            if project in items and items[project]["version"] != picked.version:
                _unresolved(rows)
            items[project] = {"project": project, "kind": "mods", "version": picked.version}
            break
        else:
            _unresolved(rows)
    return list(items.values())


def manual_check(store, pid, kind, name, path):
    name = clean_filename(name)
    profile = compatibility.installation_profile(store, pid)
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


def manual_plan(store, pid, name, path):
    profile = compatibility.installation_profile(store, pid)
    local = manual_check(store, pid, "mods", name, path)
    _require_no_conflicts(local)
    missing = [row for row in local["required"] if not row["installed"]]
    selected = _descriptor_dependencies(profile, missing)
    releases = _plan_items(store, profile, selected)
    inventory = _inventory(store, pid)
    report = _catalog_report(releases, inventory, set()) if releases else compatibility.report()
    report["warnings"] = sorted(set(report["warnings"] + _staged_warnings(
        {**local, "required": []}, report)))
    request = {"id": pid, "file": name, "sha512": file_hash(path, file_stamp(path), "sha512"), "items": selected}
    report["reviewToken"] = _review_token(request, releases, inventory, report["warnings"])
    return report, request


def _require_no_conflicts(report):
    if report["blocked"]:
        raise PlanConflict(" ".join(report["conflicts"][:3]))


def _download(release, target, transfer=None, status=lambda _message: None):
    file = {"url": release.url, "filename": release.filename, "size": release.size, "hashes": {"sha512": release.sha512}}
    hosts = trusted_catalog.RELEASE_HOSTS if trusted_catalog.is_publisher_file(release.project, release.version, file) else _CDN_HOSTS
    _allowed(release.url, hosts)
    if transfer is not None:
        transfer.download(release.url, target, title=release.filename, hashes={"sha512": release.sha512},
                          size=release.size, max_size=MAX_ADDON, hosts=hosts, category="content", status=status)
        return
    digest = hashlib.sha512()
    with lifecycle.response(_open(release.url, hosts)) as src, target.open("xb") as out:
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
                "Контрольная сумма или размер скачанного файла не совпали с данными выпуска."
            )


def _download_compatible(release, target, loader, transfer=None, status=lambda _message: None):
    """Try only hash-verified runtime artifacts of the exact same release.

    Retry a different artifact solely on loader mismatch, never on integrity,
    transport, corruption or dependency failure. The chosen digest and file
    name then flow through the final review and commit.
    """
    rejected = []
    for candidate in _choices(release):
        lifecycle.check()
        status("Скачиваем " + candidate.filename + "…")
        if transfer is None:
            _download(candidate, target)
        else:
            _download(candidate, target, transfer, status)
        try:
            verify_pack(target, candidate.kind, loader)
        except PackLoaderMismatch as exc:
            rejected.append(candidate.filename + ": " + str(exc))
            target.unlink()
            continue
        except ValueError as exc:
            lifecycle.check()
            raise CatalogError(candidate.filename + ": " + str(exc)) from exc
        return candidate
    raise PlanConflict(
        (rejected[0] if rejected else release.filename)
        + (" Проверены также: " + ", ".join(row.split(": ", 1)[0] for row in rejected[1:]) + "." if len(rejected) > 1 else "")
        + " Выбери совместимый выпуск мода для " + loader + ". Установка не внесла изменений в сборку."
    )


def _publish_set(store, pid, releases, staged, publisher=None):
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
                f"Файл {release.filename} уже есть и отличается от выбранной версии."
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
    if publisher is not None:
        return publisher(pid, pending)
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
    request = {"id": pid, "kind": kind, "project": project, "version": version}
    return install_selection(
        store, pid, [request], status, mutation_lock, acknowledgement,
        before_publish=before_publish, transfer=transfer, after_publish=after_publish, request=request,
    )


def install_selection(
    store, pid, items, status, mutation_lock=None, acknowledgement="", *,
    before_publish=None, transfer=None, after_publish=None, request=None, local_mod=None,
    on_stage=lambda _stage, _release=None: None, publisher=None,
):
    profile = compatibility.installation_profile(store, pid)
    selected = _selections(items) if items or local_mod is None else []
    request = request or {"id": pid, "items": selected}
    roots = {r["project"] for r in selected}
    on_stage("dependencies")
    status("Проверяем совместимость и обязательные зависимости…")
    releases = _plan_items(store, profile, selected)
    inventory = _inventory(store, pid)
    catalog = _catalog_report(releases, inventory, roots) if releases else compatibility.report()
    _require_no_conflicts(catalog)
    warnings = list(catalog["warnings"])
    if warnings and not acknowledgement:
        raise NeedsReview(request, _review_token(request, releases, inventory, warnings), warnings)
    with tempfile.TemporaryDirectory(dir=store.folder(pid), prefix=".catalog-") as work:
        staged = {}
        # Check dependencies declared only inside JARs, including roots already present on disk.
        # Each new dependency goes through the same verified provider and loader checks.
        discovered = {}
        resolved = {}
        for _ in range(32):
            releases = [resolved.get((r.project, r.version), r) for r in releases]
            changed = False
            for i, release in enumerate(releases):
                if release.version in staged:
                    continue
                installed = _installed([p for p in inventory if p.parent.name == release.kind], release)
                if installed:
                    try:
                        verify_pack(installed, release.kind, profile["loader"])
                    except PackLoaderMismatch as exc:
                        raise PlanConflict(installed.name + ": " + str(exc) + " Удали несовместимый файл и повтори установку.") from exc
                    continue
                tmp = Path(work) / release.version
                on_stage("downloading", release)
                actual = _download_compatible(release, tmp, profile["loader"], transfer, status)
                changed |= actual.sha512 != release.sha512 or actual.filename != release.filename
                resolved[(actual.project, actual.version)] = actual
                releases[i] = actual
                staged[actual.version] = tmp
            # Name collisions and warnings must refer to the chosen binaries,
            # including any replacement of an incompatible primary JAR.
            by_name = {}
            for release in releases:
                key = (release.kind, release.filename.casefold())
                if key in by_name:
                    raise PlanConflict("Два выпуска используют одно имя файла: " + release.filename)
                by_name[key] = release.sha512
            if changed:
                catalog = _catalog_report(releases, inventory, roots)
                _require_no_conflicts(catalog)
            active = {p.name: p for p in inventory if p.parent.name == "mods"}
            additions = {}
            for r in releases:
                if r.kind != "mods":
                    continue
                path = staged.get(r.version) or _installed(list(active.values()), r)
                if path:
                    name = r.filename if r.version in staged else path.name
                    if name in active and active[name] != path:
                        raise PlanConflict("В сборке уже есть другой файл с именем " + name)
                    additions[name] = path
            if local_mod:
                name, path = local_mod
                if name.casefold() in {n.casefold() for n in [*active, *additions]}:
                    raise PlanConflict("Файл уже есть в сборке или плане установки: " + name)
                additions[name] = path
            on_stage("checking")
            inspected = compatibility.local_report(profile, active, additions)
            _require_no_conflicts(inspected)
            missing = [row for row in inspected["required"] if not row["installed"]]
            if not missing:
                break
            new_items = _descriptor_dependencies(profile, missing)
            occupied = {r.project: r.version for r in releases}
            for item in new_items:
                project = item["project"]
                if project in roots or occupied.get(project) == item["version"]:
                    _unresolved(missing)
                discovered[project] = item
            if len(discovered) > 64:
                raise PlanConflict("Превышен предел обязательных зависимостей. Проверь состав сборки.")
            releases = _plan_items(store, profile, selected + list(discovered.values()))
            releases = [resolved.get((r.project, r.version), r) for r in releases]
            catalog = _catalog_report(releases, inventory, roots)
            _require_no_conflicts(catalog)
        else:
            raise PlanConflict("Слишком много уровней зависимостей в описаниях модов.")
        warnings = list(catalog["warnings"])
        if discovered:
            names = [r.filename for r in releases if r.project in discovered]
            warnings.append("Также будут установлены обязательные зависимости из описаний модов: " + ", ".join(names))
        warnings.extend(_staged_warnings(inspected, catalog))
        with mutation_lock or nullcontext():
            if _inventory(store, pid) != inventory:
                raise CatalogError(
                    "Файлы сборки изменились во время загрузки. Повтори проверку."
                )
            warnings = sorted(set(warnings))
            token = _review_token(request, releases, inventory, warnings)
            if token and token != acknowledgement:
                raise NeedsReview(request, token, warnings)
            if local_mod:
                name, path = local_mod
                digest = file_hash(path, file_stamp(path), "sha512")
                manual = Package("local-" + digest, "", "mods", "", name, "", "", name, path.stat().st_size, digest, "", ())
                staged[manual.version] = path
            publish = releases + [manual] if local_mod else releases
            on_stage("installing")
            if staged and before_publish is not None:
                before_publish()
            written = _publish_set(store, pid, publish, staged, publisher)
    _save_project_icons(store, pid, releases, status)
    if after_publish is not None:
        after_publish(releases)
    if not written:
        return "Эта версия уже установлена в сборке."
    return f"Установлено файлов: {written}."
