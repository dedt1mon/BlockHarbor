"""Public catalog of opt-in BlockHarbor build snapshots."""

import hashlib
import json
import re
import shutil
import tempfile
import time
import unicodedata
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import build_archive, e4steam, lifecycle, version_ids
from .pack_hub import CATEGORIES
from .store import LOADERS, input_profile
from .version import VERSION

ORIGIN = "https://blockharbor-community-catalog.ded8timon.chatgpt.site"
CHUNK = 8 * 1024**2
MAX_ARCHIVE = 2 * 1024**3  # Existing archives remain readable during migration.
MAX_PUBLIC_UPLOAD = 256 * 1024**2  # New public snapshots are capped at 256 MiB.
PAGE = 20
MAX_JSON = 128 * 1024
MIRROR_INDEX = "https://github.com/dedt1mon/BlockHarbor/releases/download/community-mirror-index/index.json"
MIRROR_PREFIX = "https://github.com/dedt1mon/BlockHarbor/releases/download/community-mirror-"
MIRROR_JSON_LIMIT = 8 * 1024**2
MIRROR_MAX_AGE = 2 * 3600
MIRROR_SHARDS = 32
FALLBACK_HTTP = frozenset((0, 403, 408, 429, 500, 502, 503, 504, 521, 522, 523, 524))
ID = re.compile(r"BH-[A-F0-9]{12}\Z")


def _category(value, optional=False):
    if value not in CATEGORIES and not (optional and value == ""):
        raise ValueError("Выбери категорию сборки из списка.")
    return value


def _build_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value.upper()):
        raise ValueError("Нужен ID сборки в формате BH- и 12 символов.")
    return value.upper()


def _listing(value):
    if not isinstance(value, dict):
        raise ValueError("Каталог вернул некорректное описание сборки.")
    name, version, loader = input_profile(value.get("name"), value.get("minecraft"), value.get("loader"))
    size, digest = value.get("size"), value.get("sha256")
    if type(size) is not int or not 0 < size <= MAX_ARCHIVE or not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest):
        raise ValueError("Размер или контрольная сумма сборки в каталоге неверны.")
    expiry = value.get("expiresAt")
    if expiry is not None and (type(expiry) is not int or expiry <= 0):
        raise ValueError("Каталог вернул неверный срок публикации.")
    listing = {"id": _build_id(value.get("id")), "name": name, "minecraft": version, "loader": loader,
               "category": _category(value.get("category")), "size": size, "sha256": digest}
    if expiry is not None:
        listing["expiresAt"] = expiry
    return listing


def _expired(listing):
    """Fail closed for expired listings, including cached GitHub mirror pages."""
    expiry = listing.get("expiresAt")
    return expiry is not None and expiry <= time.time()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CatalogError(ValueError):
    def __init__(self, code, reason, *, reason_code="", blocked_until=0, retry_after=0):
        super().__init__(reason)
        self.code = code
        self.reason_code = reason_code
        self.blocked_until = blocked_until
        self.retry_after = retry_after


class CatalogAPI:
    def __init__(self, origin=ORIGIN):
        self.origin = origin.rstrip("/")
        self.opener = build_opener(_NoRedirect())

    def request(self, method, path, *, token=None, payload=None, block=None):
        lifecycle.check()
        headers = {"User-Agent": f"BlockHarbor/{VERSION}", "Accept-Encoding": "identity"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if payload is not None:
            block = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        elif block is not None:
            headers["Content-Type"] = "application/octet-stream"
        request = Request(self.origin + "/v1/" + path, data=block, headers=headers, method=method)
        # The registry streams and hashes the entire archive before acknowledging completion.
        timeout = (300 if path.endswith("/complete") else
                   12 if method == "GET" and not path.endswith("/archive") else
                   45 if method == "GET" else 60)
        try:
            return lifecycle.response(self.opener.open(request, timeout=timeout))
        except HTTPError as exc:
            with exc:
                try:
                    response = json.loads(exc.read(4096))
                except (ValueError, AttributeError):
                    response = {}
            if not isinstance(response, dict):
                response = {}
            reason = response.get("error")
            if not isinstance(reason, str) or not reason:
                reason = "Каталог отклонил запрос."
            reason_code = response.get("code", "")
            blocked_until = response.get("blockedUntil", 0)
            retry_after = response.get("retryAfterSeconds", 0)
            if reason_code != "PUBLISH_RATE_BAN" or exc.code != 429:
                reason_code, blocked_until, retry_after = "", 0, 0
            if type(blocked_until) is not int or blocked_until < 0:
                blocked_until = 0
            if type(retry_after) is not int or not 0 <= retry_after <= 5 * 3600:
                retry_after = 0
            raise CatalogError(
                exc.code, f"{reason[:240]} (HTTP {exc.code})",
                reason_code=reason_code, blocked_until=blocked_until,
                retry_after=retry_after,
            ) from None
        except (OSError, URLError, HTTPException) as exc:
            lifecycle.check()
            raise CatalogError(0, "Не удалось связаться с каталогом сборок. Проверь подключение и попробуй ещё раз.") from exc

    def json(self, method, path, **kwargs):
        with self.request(method, path, **kwargs) as response:
            raw = response.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ValueError("Ответ каталога превышает допустимый размер.")
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise ValueError("Не удалось прочитать ответ каталога сборок.") from exc
        if not isinstance(value, dict):
            raise ValueError("Каталог вернул некорректный ответ.")
        return value



class GitHubMirror:
    """Read-only copy of opt-in public builds, synced by GitHub Actions.

    Archive URLs are constructed from validated IDs rather than server data;
    redirects carry no publisher authorization and are TLS/host constrained.
    """

    def __init__(self, opener=None):
        self.opener = opener or build_opener()
        self._cache = None

    @staticmethod
    def archive_url(public_id):
        public_id = _build_id(public_id)
        shard = int(public_id[3:5], 16) % MIRROR_SHARDS
        return f"{MIRROR_PREFIX}{shard:02x}/{public_id}.zip"

    def _open(self, url, timeout):
        lifecycle.check()
        request = Request(url, headers={
            "User-Agent": f"BlockHarbor/{VERSION}",
            "Accept-Encoding": "identity",
        })
        try:
            opened = self.opener.open(request, timeout=timeout)
            final = urlsplit(opened.geturl())
            hostname = (final.hostname or "").lower()
            if final.scheme != "https" or not (hostname == "github.com" or hostname.endswith(".githubusercontent.com")):
                opened.close()
                raise CatalogError(0, "Резервный каталог вернул небезопасный адрес загрузки.")
            return lifecycle.response(opened)
        except (HTTPError, URLError, OSError, HTTPException) as exc:
            lifecycle.check()
            raise CatalogError(0, "Резервный каталог GitHub недоступен. Проверь подключение к GitHub.") from exc

    def _listings(self):
        cached = self._cache
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
        with self._open(MIRROR_INDEX, 15) as response:
            payload = response.read(MIRROR_JSON_LIMIT + 1)
        if len(payload) > MIRROR_JSON_LIMIT:
            raise CatalogError(0, "Резервный каталог вернул слишком большой список.")
        try:
            data = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise CatalogError(0, "Не удалось прочитать резервный каталог GitHub.") from exc
        if not isinstance(data, dict) or data.get("format") != 1 or type(data.get("generated")) is not int:
            raise CatalogError(0, "Резервный каталог имеет неизвестный формат.")
        age = time.time() - data["generated"]
        if not -300 <= age <= MIRROR_MAX_AGE:
            raise CatalogError(0, "Резервный каталог давно не обновлялся. Попробуй позже.")
        rows = data.get("items")
        if (not isinstance(rows, list) or len(rows) > 20000
                or type(data.get("total")) is not int or data["total"] != len(rows)):
            raise CatalogError(0, "Резервный каталог вернул неполные сведения.")
        items = [_listing(row) for row in rows]
        # The current mirror protocol must carry expiry information. Older
        # cached indexes cannot prove that a publication is still available.
        if any("expiresAt" not in item for item in items):
            raise CatalogError(0, "Резервный каталог ещё не синхронизировал сроки публикаций.")
        if len({item["id"] for item in items}) != len(items):
            raise CatalogError(0, "Резервный каталог содержит повторяющиеся сборки.")
        self._cache = (time.monotonic(), items)
        return items

    def search(self, filters):
        needle = filters["q"].strip().casefold()
        matching = [
            item for item in self._listings()
            if not _expired(item)
            and (not needle or needle == item["id"].casefold() or needle in item["name"].casefold())
            and (not filters["category"] or item["category"] == filters["category"])
            and (not filters["version"] or item["minecraft"] == filters["version"])
            and (not filters["loader"] or item["loader"] == filters["loader"])
        ]
        offset = filters["offset"]
        return {"hits": matching[offset:offset + PAGE], "total": len(matching),
                "limit": PAGE, "offset": offset, "source": "github"}

    def listing(self, public_id):
        public_id = _build_id(public_id)
        for item in self._listings():
            if item["id"] == public_id and not _expired(item):
                return item
        raise CatalogError(404, "Сборка не найдена в резервном каталоге.")

    def request_archive(self, public_id):
        return self._open(self.archive_url(public_id), 45)


class CommunityCatalog:
    READ_ACTIONS = {"communitySearch", "communityPublication"}
    JOBS = {"communityInstall", "communityPublish", "communityUnpublish"}
    ACTIONS = READ_ACTIONS | JOBS

    def __init__(self, store, api=None, mirror=None):
        self.store = store
        self.api = api or CatalogAPI()
        self.mirror = mirror if mirror is not None else GitHubMirror()
        with self.store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS community_uploads (
                origin TEXT NOT NULL, pid TEXT NOT NULL, fingerprint TEXT NOT NULL, upload TEXT NOT NULL,
                token TEXT NOT NULL, listing TEXT, PRIMARY KEY (origin,pid,fingerprint))""")

    def search(self, params):
        query = params.get("q", "")
        if not isinstance(query, str) or len(query) > 180:
            raise ValueError("Поисковый запрос длиннее 180 символов.")
        try:
            offset = int(params.get("offset", 0))
        except (ValueError, TypeError):
            raise ValueError("Неверная страница каталога.") from None
        if not 0 <= offset <= 100_000:
            raise ValueError("Неверная страница каталога.")
        version, loader = params.get("version", ""), params.get("loader", "")
        if version:
            version_ids.validate(version)
        if loader and loader not in LOADERS:
            raise ValueError("Неизвестный загрузчик сборки.")
        filters = {"q": query.strip(), "offset": offset, "category": _category(params.get("category", ""), True),
                   "version": version, "loader": loader}
        try:
            page = self.api.json("GET", "builds?" + urlencode(filters))
        except CatalogError as error:
            if error.code not in FALLBACK_HTTP:
                raise
            try:
                return self.mirror.search(filters)
            except (CatalogError, ValueError, OSError) as backup:
                raise CatalogError(0, "Каталог игроков недоступен через основной сервер и зеркало GitHub.") from backup
        hits, total = page.get("hits"), page.get("total")
        if not isinstance(hits, list) or len(hits) > PAGE or type(total) is not int or total < len(hits):
            raise ValueError("Каталог вернул неверный список сборок.")
        return {"hits": [_listing(row) for row in hits], "total": total, "limit": PAGE, "offset": offset, "source": "primary"}

    def _reconcile_legacy(self, pid, item):
        """Resolve a pre-retention publication without losing its owner token."""
        public_id = _build_id(item["id"])
        current = _listing(self.api.json("GET", "builds/" + public_id))
        if current["id"] != public_id or "expiresAt" not in current:
            raise CatalogError(0, "Сервер пока не подтвердил срок публикации.")
        with self.store.connection() as db:
            db.execute("""UPDATE community_uploads SET listing=?
                WHERE origin=? AND pid=? AND json_extract(listing,'$.id')=?""",
                (json.dumps(current, ensure_ascii=False), self.api.origin, pid, public_id))
        return current

    def _new_upload(self, pid, key, metadata):
        session = self.api.json("POST", "uploads", payload=metadata)
        upload, token = session.get("upload"), session.get("token")
        if (not isinstance(upload, str) or not re.fullmatch("[a-f0-9]{32}", upload)
                or not isinstance(token, str) or not re.fullmatch("[a-f0-9]{64}", token)
                or session.get("chunkSize") != CHUNK):
            raise ValueError("Каталог вернул некорректные параметры публикации.")
        with self.store.connection() as db:
            db.execute("REPLACE INTO community_uploads VALUES (?,?,?,?,?,NULL)", (self.api.origin, pid, key, upload, token))
        return upload, token

    def publish(self, pid, name, category, status, *, include_configs=False):
        if type(include_configs) is not bool:
            raise ValueError("Выбери, включать ли настройки модов в публикацию.")
        profile = self.store.profile(pid)
        if isinstance(name, str):
            name = unicodedata.normalize("NFC", name)
        name, version, loader = input_profile(name, profile["version"], profile["loader"])
        metadata = {"name": name, "minecraft": version, "loader": loader, "category": _category(category)}
        status("Собираем файлы дополнений для публикации…")
        with tempfile.TemporaryFile(dir=self.store.root) as stream:
            try:
                build_archive.export_build(
                    self.store, pid, stream, catalog_name=name,
                    max_bytes=MAX_PUBLIC_UPLOAD, include_configs=include_configs,
                )
            except ValueError as exc:
                if "лимит каталога" in str(exc):
                    raise ValueError(
                        "Сборка превышает лимит публикации 256 МиБ. Удали лишние файлы или уменьши сборку."
                    ) from exc
                raise
            digest = hashlib.sha256()
            while block := stream.read(CHUNK):
                lifecycle.check()
                digest.update(block)
            metadata.update(size=stream.tell(), sha256=digest.hexdigest())
            key = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
            args = (self.api.origin, pid, key)
            with self.store.connection() as db:
                old = db.execute("SELECT * FROM community_uploads WHERE origin=? AND pid=? AND fingerprint=?", args).fetchone()
            listing = json.loads(old["listing"]) if old and old["listing"] else None
            # Preserve pre-retention owner tokens: reconcile old publications
            # with the authoritative server instead of silently losing access.
            if listing and "expiresAt" not in listing:
                try:
                    listing = self._reconcile_legacy(pid, listing)
                except CatalogError as exc:
                    if exc.code != 404:
                        raise
                    listing = None
                    old = None
            if listing and not _expired(listing):
                return listing
            if listing:
                old = None
                listing = None

            # If the completion response was lost, recover the published ID
            # before sending another byte. A changed archive gets a new session.
            if old:
                upload, token = old["upload"], old["token"]
                try:
                    listing = self.api.json("POST", f"uploads/{upload}/complete", token=token)
                except CatalogError as exc:
                    if exc.code in (404, 410):
                        old = None
                    elif exc.code != 409:
                        raise
            if not old:
                upload, token = self._new_upload(pid, key, metadata)
            if listing is None:
                stream.seek(0)
                part = 0
                while block := stream.read(CHUNK):
                    lifecycle.check()
                    part += 1
                    status(f"Публикуем «{name}» · {stream.tell() * 100 // metadata['size']}%")
                    self.api.json("PUT", f"uploads/{upload}/parts/{part}", token=token, block=block)
                listing = self.api.json("POST", f"uploads/{upload}/complete", token=token)
            listing = _listing(listing)
            if _expired(listing):
                raise ValueError("Срок публикации истёк. Опубликуй сборку снова.")
            if any(listing[k] != v for k, v in metadata.items()):
                raise ValueError("Запись каталога не соответствует отправленной сборке.")
            with self.store.connection() as db:
                db.execute("UPDATE community_uploads SET listing=? WHERE origin=? AND pid=? AND fingerprint=?",
                           (json.dumps(listing, ensure_ascii=False), *args))
            return listing

    def publications(self, pid):
        self.store.profile(pid)
        with self.store.connection() as db:
            rows = db.execute("SELECT listing FROM community_uploads WHERE origin=? AND pid=? AND listing IS NOT NULL ORDER BY rowid DESC",
                              (self.api.origin, pid)).fetchall()
        items = []
        retired = []
        for row in rows:
            item = json.loads(row["listing"])
            if "expiresAt" not in item:
                try:
                    item = self._reconcile_legacy(pid, item)
                except CatalogError as exc:
                    if exc.code == 404:
                        retired.append(item["id"])
                        continue
                    # A blocked network must never erase the owner's ability
                    # to revoke a potentially still-live old publication.
                    item["statusUnverified"] = True
            if _expired(item):
                retired.append(item["id"])
                continue
            item["build"] = item["id"]
            items.append(item)
        if retired:
            with self.store.connection() as db:
                for public_id in retired:
                    db.execute("DELETE FROM community_uploads WHERE origin=? AND pid=? AND json_extract(listing,'$.id')=?",
                               (self.api.origin, pid, public_id))
        return {**(items[0] if items else {"build": "", "name": ""}), "publications": items}

    def unpublish(self, pid, public_id):
        self.store.profile(pid)
        public_id = _build_id(public_id)
        with self.store.connection() as db:
            rows = db.execute("SELECT token,listing,fingerprint FROM community_uploads WHERE origin=? AND pid=? AND listing IS NOT NULL",
                              (self.api.origin, pid)).fetchall()
        row = next((row for row in rows if json.loads(row["listing"])["id"] == public_id), None)
        if row is None:
            raise ValueError("Эта публикация не принадлежит выбранной сборке на этом устройстве.")
        try:
            self.api.json("DELETE", f"builds/{public_id}", token=row["token"])
        except CatalogError as exc:
            if exc.code != 404:
                raise
        with self.store.connection() as db:
            db.execute("DELETE FROM community_uploads WHERE origin=? AND pid=? AND fingerprint=?",
                       (self.api.origin, pid, row["fingerprint"]))
        return "Сборка " + public_id + " снята с публикации."

    def install(self, public_id, allow_without_steam, status):
        public_id = _build_id(public_id)
        from_mirror = False
        try:
            listing = _listing(self.api.json("GET", "builds/" + public_id))
        except CatalogError as error:
            if error.code not in FALLBACK_HTTP:
                raise
            listing = self.mirror.listing(public_id)
            from_mirror = True
        if listing["id"] != public_id:
            raise ValueError("Каталог вернул другую сборку.")
        if _expired(listing):
            raise CatalogError(404, "Срок публикации истёк. Попроси автора опубликовать сборку снова.")
        if not allow_without_steam and not e4steam.support(listing["minecraft"], listing["loader"]):
            raise ValueError("Подтверди установку этой сборки без встроенной игры через Steam.")
        size = listing["size"]
        if shutil.disk_usage(self.store.root).free < size:
            raise ValueError("Недостаточно места на диске для архива сборки.")
        with tempfile.TemporaryFile(dir=self.store.root) as stream:
            digest = hashlib.sha256()
            if from_mirror:
                source = self.mirror.request_archive(public_id)
            else:
                try:
                    source = self.api.request("GET", "builds/" + public_id + "/archive")
                except CatalogError as error:
                    if error.code not in FALLBACK_HTTP:
                        raise
                    mirror_listing = self.mirror.listing(public_id)
                    if any(listing[k] != mirror_listing[k] for k in ("id", "size", "sha256")):
                        raise ValueError("Резервный архив не совпадает с записью основного каталога.")
                    source = self.mirror.request_archive(public_id)
            with source as response:
                length = response.headers.get("Content-Length")
                if length is not None and length != str(size):
                    raise ValueError("Размер архива не совпадает с записью каталога.")
                while stream.tell() < size:
                    lifecycle.check()
                    block = response.read(min(1024**2, size - stream.tell()))
                    if not block:
                        break
                    stream.write(block)
                    digest.update(block)
                    status(f"Скачиваем «{listing['name']}» · {stream.tell() * 100 // size}%")
                if stream.tell() != size or response.read(1) or digest.hexdigest() != listing["sha256"]:
                    raise ValueError("Размер или контрольная сумма скачанной сборки не совпали.")
            status("Проверяем и устанавливаем сборку…")
            result = build_archive.import_build(self.store, stream, catalog_listing=listing, max_bytes=MAX_ARCHIVE)
        if allow_without_steam:
            e4steam.acknowledge_notice(self.store, result["id"], listing["minecraft"], listing["loader"])
        return result

    def handle(self, action, params, status=lambda msg: None):
        if action == "communitySearch":
            return self.search(params)
        if action == "communityPublication":
            return self.publications(params["id"])
        if action == "communityPublish":
            listing = self.publish(params["id"], params.get("name"), params.get("category"), status,
                                   include_configs=params.get("includeConfigs", False))
            return f"Сборка «{listing['name']}» опубликована. ID: {listing['id']}"
        if action == "communityInstall":
            result = self.install(params.get("build"), e4steam.notice_permission(params), status)
            return f"Сборка «{result['name']}» установлена."
        if action == "communityUnpublish":
            return self.unpublish(params["id"], params.get("build"))
        raise ValueError("Неизвестное действие каталога сборок.")
