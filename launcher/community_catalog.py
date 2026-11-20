"""Public catalog of opt-in BlockHarbor build snapshots."""

import hashlib
import json
import re
import shutil
import tempfile
import unicodedata
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import build_archive, e4steam, lifecycle, version_ids
from .pack_hub import CATEGORIES
from .store import LOADERS, input_profile
from .version import VERSION

ORIGIN = "https://blockharbor-community-catalog.ded8timon.chatgpt.site"
CHUNK = 8 * 1024**2
MAX_ARCHIVE = 2 * 1024**3
PAGE = 20
MAX_JSON = 128 * 1024
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
    return {"id": _build_id(value.get("id")), "name": name, "minecraft": version, "loader": loader,
            "category": _category(value.get("category")), "size": size, "sha256": digest}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CatalogError(ValueError):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code


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
        timeout = 300 if path.endswith("/complete") else 60
        try:
            return lifecycle.response(self.opener.open(request, timeout=timeout))
        except HTTPError as exc:
            with exc:
                try:
                    reason = json.loads(exc.read(4096)).get("error")
                except (ValueError, AttributeError):
                    reason = ""
            if not isinstance(reason, str) or not reason:
                reason = "Каталог отклонил запрос."
            raise CatalogError(exc.code, f"{reason[:240]} (HTTP {exc.code})") from None
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


class CommunityCatalog:
    READ_ACTIONS = {"communitySearch", "communityPublication"}
    JOBS = {"communityInstall", "communityPublish", "communityUnpublish"}
    ACTIONS = READ_ACTIONS | JOBS

    def __init__(self, store, api=None):
        self.store = store
        self.api = api or CatalogAPI()
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
        page = self.api.json("GET", "builds?" + urlencode(filters))
        hits, total = page.get("hits"), page.get("total")
        if not isinstance(hits, list) or len(hits) > PAGE or type(total) is not int or total < len(hits):
            raise ValueError("Каталог вернул неверный список сборок.")
        return {"hits": [_listing(row) for row in hits], "total": total, "limit": PAGE, "offset": offset}

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
            build_archive.export_build(self.store, pid, stream, catalog_name=name, max_bytes=MAX_ARCHIVE, include_configs=include_configs)
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
            if listing:
                return listing

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
        items = [json.loads(row["listing"]) for row in rows]
        for item in items:
            item["build"] = item["id"]
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
        listing = _listing(self.api.json("GET", "builds/" + public_id))
        if listing["id"] != public_id:
            raise ValueError("Каталог вернул другую сборку.")
        if not allow_without_steam and not e4steam.support(listing["minecraft"], listing["loader"]):
            raise ValueError("Подтверди установку этой сборки без встроенной игры через Steam.")
        size = listing["size"]
        if shutil.disk_usage(self.store.root).free < size:
            raise ValueError("Недостаточно места на диске для архива сборки.")
        with tempfile.TemporaryFile(dir=self.store.root) as stream:
            digest = hashlib.sha256()
            with self.api.request("GET", "builds/" + public_id + "/archive") as response:
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
