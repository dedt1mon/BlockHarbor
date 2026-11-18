"""Reviewable, exact build comparison and invitations carried by the users.

A share code contains a compressed descriptor, not a promise of an online room.
Steam remains the transport and admission authority for the actual game session.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import struct
import tempfile
import threading
import time
import zipfile
import zlib
from pathlib import Path

from . import build_archive, e4steam, modrinth, room_bridge, steam_network
from .modfiles import file_hash, file_stamp, mod_info, read_descriptor
from .pack_hub import _hashes, _path, _size, _unique_name, _url, _validate_paths, download_verified
from .store import KINDS, clean_filename, input_profile, require_idle_files, verify_pack

MAX_DESCRIPTOR = 4 * 1024**2
MAX_CONTENT = 8 * 1024**3
MAX_ROWS = 4000
INVITE_RE = re.compile(r"(?:s|d)-[a-z0-9]{1,64}-[a-z0-9]{1,64}\.steam\Z")


def validate_address(value):
    value = value.strip().lower() if isinstance(value, str) else ""
    if not INVITE_RE.fullmatch(value):
        raise ValueError("Вставь адрес текущего мира e4steam: s-…-….steam. Его хозяин получает после открытия мира для Steam.")
    return value


def _text(value, length, default=""):
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > length or any(ord(c) < 32 for c in value):
        raise ValueError("Описание сборки содержит некорректный текст.")
    return value


def _decode(value):
    if isinstance(value, bytes):
        if len(value) > MAX_DESCRIPTOR:
            raise ValueError("Описание сборки превышает 4 МБ.")
        value = value.decode("utf-8-sig")
    if isinstance(value, str):
        if len(value) > MAX_DESCRIPTOR * 2:
            raise ValueError("Код сборки слишком длинный.")
        value = value.strip()
        if value.startswith("bhshare:"):
            encoded = value[8:]
            if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", encoded):
                raise ValueError("Код сборки повреждён.")
            try:
                compressed = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
                decoder = zlib.decompressobj()
                raw = decoder.decompress(compressed, MAX_DESCRIPTOR + 1)
                if len(raw) > MAX_DESCRIPTOR or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
                    raise ValueError("Код сборки превышает лимит или содержит лишние данные.")
                value = raw.decode("utf-8")
            except (ValueError, zlib.error, UnicodeError) as exc:
                raise ValueError("Не удалось прочитать код сборки.") from exc
        try:
            value = json.loads(value)
        except (ValueError, RecursionError) as exc:
            raise ValueError("Вставь код bhshare:… или выбери файл описания .bhshare.") from exc
    return validate_descriptor(value)


def validate_descriptor(value):
    if not isinstance(value, dict) or value.get("format") != "BlockHarborSharedBuild" or type(value.get("formatVersion")) is not int or value["formatVersion"] != 1:
        raise ValueError("Неизвестный формат описания сборки.")
    name, minecraft, loader = input_profile(value.get("name"), value.get("minecraft"), value.get("loader"))
    rows = value.get("files")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError("В описании сборки слишком много файлов.")
    sanitized, total = [], 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Повреждён список файлов сборки.")
        parts = _path(row.get("path"))
        if len(parts) != 2 or parts[0] not in KINDS or not parts[1].lower().endswith(KINDS[parts[0]]):
            raise ValueError("В описании допускаются только JAR/ZIP дополнений сборки.")
        size, hashes = _size(row.get("size")), _hashes(row.get("hashes"))
        total += size
        if total > MAX_CONTENT:
            raise ValueError("Сборка в описании превышает 8 ГБ.")
        role = row.get("role", "unknown")
        if role not in {"required", "client", "unknown"}:
            raise ValueError("Неизвестная роль дополнения в совместной игре.")
        ids = row.get("ids", [])
        if not isinstance(ids, list) or len(ids) > 128 or any(not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", key) for key in ids):
            raise ValueError("Некорректные идентификаторы модов.")
        urls = row.get("downloads", [])
        if not isinstance(urls, list) or len(urls) > 8:
            raise ValueError("Слишком много источников дополнения.")
        project, version = row.get("project", ""), row.get("version", "")
        sanitized.append({"path": "/".join(parts), "size": size, "hashes": hashes,
                          "title": _text(row.get("title"), 256, parts[1]), "number": _text(row.get("number"), 128),
                          "ids": sorted(set(ids)), "role": role, "downloads": [_url(url) for url in urls],
                          "project": modrinth._id(project) if project else "", "version": modrinth._id(version) if version else ""})
    _validate_paths([row["path"] for row in sanitized])
    if len({row["hashes"]["sha512"] for row in sanitized}) != len(sanitized):
        raise ValueError("В описании есть дубликаты одного дополнения.")
    result = {"format": "BlockHarborSharedBuild", "formatVersion": 1, "name": name, "minecraft": minecraft,
              "loader": loader, "loaderVersion": build_archive._pin(value.get("loaderVersion")), "files": sanitized}
    if value.get("invite"):
        invite = value["invite"]
        if not isinstance(invite, dict):
            raise ValueError("Приглашение в мир повреждено.")
        result["invite"] = {"address": validate_address(invite.get("address")), "label": _text(invite.get("label"), 80, name)}
    return result


def encode_descriptor(value):
    raw = json.dumps(validate_descriptor(value), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_DESCRIPTOR:
        raise ValueError("Описание сборки превышает 4 МБ.")
    return "bhshare:" + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii").rstrip("=")


def _metadata(path, kind):
    if kind != "mods":
        return {"title": path.stem, "number": "", "role": "client", "ids": []}
    ids, version = mod_info(path, file_stamp(path))
    result = {"title": path.stem, "number": version, "role": "unknown", "ids": sorted(ids - {""})}
    try:
        with zipfile.ZipFile(path) as archive:
            data = read_descriptor(archive, "fabric.mod.json")
            if data:
                result["title"] = str(data.get("name", path.stem))[:256]
                result["role"] = "client" if data.get("environment") == "client" else "unknown"
            else:
                data = read_descriptor(archive, "quilt.mod.json")
                loader = data.get("quilt_loader", {})
                if isinstance(loader, dict):
                    metadata = loader.get("metadata", {})
                    if isinstance(metadata, dict):
                        result["title"] = str(metadata.get("name", path.stem))[:256]
                    if data.get("minecraft", {}).get("environment") == "client":
                        result["role"] = "client"
    except (OSError, ValueError, zipfile.BadZipFile, AttributeError, TypeError):
        pass
    return result


def _pin(store, pid):
    profile = store.profile(pid)
    path = store.folder(pid) / build_archive.STAMP
    if path.is_file() and not path.is_symlink() and path.stat().st_size < 65536:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (data.get("version"), data.get("loader")) == (profile["version"], profile["loader"]):
                return build_archive._pin(data.get("loader_version"))
        except (OSError, ValueError):
            pass
    return None


class SharedBuilds:
    ACTIONS = {"shareCreate", "shareCompare", "shareApply", "shareRoomState", "shareInvite", "shareInspect", "shareAddServer"}
    READ_ACTIONS = {"shareRoomState"}
    JOBS = {"shareApply"}

    def __init__(self, store, transfers=None, before_change=None, game_running=None):
        self.store, self.transfers = store, transfers
        self.before_change = before_change or (lambda pid, label: None)
        self.game_running = game_running or (lambda pid: False)
        self.plans = {}
        self.lock = threading.RLock()

    def _inventory(self, pid):
        inventory = modrinth._inventory(self.store, pid)
        if len(inventory) > MAX_ROWS or sum(stamp[0] for stamp in inventory.values()) > MAX_CONTENT:
            raise ValueError("Сборка слишком велика для обмена описанием: максимум 4000 файлов и 8 ГБ.")
        rows = []
        for path, stamp in sorted(inventory.items(), key=lambda pair: str(pair[0])):
            if build_archive._reparse(path.lstat()) or path.resolve().parent != self.store.folder(pid).resolve() / path.parent.name:
                raise ValueError("В сборке обнаружена ссылка вместо обычного дополнения.")
            metadata = _metadata(path, path.parent.name)
            if path.name == room_bridge.FILENAME and room_bridge.installed(self.store.folder(pid)):
                metadata.update(role="client", title="BlockHarbor Room Status")
            rows.append({"path": path.parent.name + "/" + path.name, "size": stamp[0],
                         "hashes": {kind: file_hash(path, stamp, kind) for kind in ("sha1", "sha512")},
                         "project": "", "version": "", "downloads": [], **metadata})
        return rows

    @staticmethod
    def _fingerprint(profile, pin, rows):
        raw = [profile["version"], profile["loader"], pin, sorted((r["path"], r["hashes"]["sha512"]) for r in rows)]
        return hashlib.sha256(json.dumps(raw, separators=(",", ":")).encode()).hexdigest()

    def descriptor(self, pid, *, resolve=True):
        profile, folder = self.store.profile(pid), self.store.folder(pid)
        rows = self._inventory(pid)
        by_hash = {row["hashes"]["sha512"]: row for row in rows}
        if resolve and rows:
            try:
                for start in range(0, len(rows), 32):
                    digests = tuple(row["hashes"]["sha512"] for row in rows[start:start + 32])
                    found = modrinth._versions_by_hash(digests, int(time.monotonic() // 300))
                    for digest, version in found.items():
                        row = by_hash.get(digest)
                        if not row or not isinstance(version, dict):
                            continue
                        file = next((f for f in version.get("files", []) if f.get("hashes", {}).get("sha512") == digest and f.get("size") == row["size"]), None)
                        if not file:
                            continue
                        row.update(project=modrinth._id(version["project_id"]), version=modrinth._id(version["id"]),
                                   number=str(version.get("version_number", row["number"]))[:128], downloads=[_url(file["url"])])
                        environment = version.get("environment")
                        if environment in {"client_only", "client_only_server_optional", "singleplayer_only"}:
                            row["role"] = "client"
                        elif environment == "client_and_server":
                            row["role"] = "required"
            except (ValueError, OSError):
                pass  # Exact local hashes remain useful when the catalog is offline.
        # Built-in components have pinned upstream sources even when not on Modrinth.
        for row in rows:
            if row["downloads"] or not row["path"].startswith("mods/"):
                continue
            path = folder.joinpath(*_path(row["path"]))
            for asset in e4steam._plan(profile["version"], profile["loader"]):
                if row["size"] == asset.size and file_hash(path, file_stamp(path), asset.algorithm) == asset.digest:
                    row["downloads"] = [_url(asset.urls[0])]
                    row["role"] = "required"
                    break
        value = {"format": "BlockHarborSharedBuild", "formatVersion": 1, "name": profile["name"],
                 "minecraft": profile["version"], "loader": profile["loader"], "loaderVersion": _pin(self.store, pid), "files": rows}
        return validate_descriptor(value)

    def compare(self, pid, source):
        descriptor = _decode(source)
        # The launcher's observer does not change the game's shared protocol and
        # must never become a required multiplayer mod through a remote descriptor.
        for row in descriptor["files"]:
            if "blockharbor_room_status" in row["ids"]:
                row["role"] = "client"
        profile = self.store.profile(pid) if pid else {"id": "", "name": "Новая сборка", "version": "", "loader": "vanilla"}
        rows, pin = (self._inventory(pid), _pin(self.store, pid)) if pid else ([], None)
        local_hashes = {row["hashes"]["sha512"]: row for row in rows}
        required = [row for row in descriptor["files"] if row["role"] != "client"]
        missing, different, matched, optional, used = [], [], [], [], set()
        for wanted in required:
            digest = wanted["hashes"]["sha512"]
            if digest in local_hashes:
                matched.append(wanted)
                used.add(local_hashes[digest]["path"])
                continue
            identities = set(wanted["ids"])
            candidates = [row for row in rows if row["path"] == wanted["path"] or (identities and identities.intersection(row["ids"]))]
            if len(candidates) > 1:
                raise ValueError("В локальной сборке несколько копий мода «" + wanted["title"] + "». Сначала убери дубликаты.")
            if candidates:
                if candidates[0]["path"] in used:
                    raise ValueError("Два дополнения приглашения заменяют один локальный мод. Проверь состав сборки хозяина.")
                used.add(candidates[0]["path"])
                different.append({"wanted": wanted, "current": candidates[0]})
            else:
                missing.append(wanted)
        for row in descriptor["files"]:
            if row["role"] == "client" and row["hashes"]["sha512"] not in local_hashes:
                optional.append(row)
        extra = [row for row in rows if row["path"] not in used]
        downloads = missing + [item["wanted"] for item in different]
        same_base = (profile["version"], profile["loader"]) == (descriptor["minecraft"], descriptor["loader"])
        manual = [row for row in (downloads if same_base else required) if not row["downloads"]]
        pin_change = bool(descriptor["loaderVersion"] and pin and descriptor["loaderVersion"] != pin)
        warnings = []
        unknown = [row for row in required if row["role"] == "unknown"]
        if unknown:
            warnings.append(f"Для {len(unknown)} модов сторона работы не указана. Они включены в обязательное сравнение; совпадение файлов само по себе не гарантирует отсутствие конфликтов.")
        if any(row["role"] != "client" for row in extra):
            warnings.append("В твоей сборке есть дополнительные моды с неизвестной или общей ролью. Они будут сохранены и могут влиять на совместимость.")
        if not same_base:
            warnings.append("Версия Minecraft или загрузчик отличаются. Создай отдельную сборку; моды исходной сборки в неё не переносятся." if pid else "Будет создана новая сборка по описанию друга.")
        if pin_change:
            warnings.append("У хозяина закреплена другая версия загрузчика. Для точного совпадения будет применена его версия.")
        # Prevent a downloaded file from silently overwriting an unrelated local file.
        replacements = {item["current"]["path"] for item in different}
        for wanted in downloads:
            collision = next((row for row in rows if row["path"].casefold() == wanted["path"].casefold()), None)
            if collision and collision["path"] not in replacements:
                raise ValueError("Имя файла из приглашения занято другим локальным дополнением: " + wanted["path"])
        token = secrets.token_urlsafe(24)
        result = {"token": token, "name": descriptor["name"], "minecraft": descriptor["minecraft"], "loader": descriptor["loader"],
                  "loaderVersion": descriptor["loaderVersion"], "missing": missing, "different": different, "matching": len(matched),
                  "extra": extra, "optional": optional, "manual": manual, "warnings": warnings,
                  "sameBase": same_base, "pinChange": pin_change, "canApply": not manual,
                  "downloadBytes": sum(row["size"] for row in downloads), "invite": descriptor.get("invite"),
                  "ready": same_base and not pin_change and not downloads}
        with self.lock:
            self.plans = {key: val for key, val in self.plans.items() if val["expires"] > time.monotonic()}
            if len(self.plans) >= 12:
                self.plans.pop(next(iter(self.plans)))
            self.plans[token] = {"id": pid, "descriptor": descriptor, "profile": profile, "pin": pin,
                                 "fingerprint": self._fingerprint(profile, pin, rows), "result": result, "expires": time.monotonic() + 1800}
        return result

    def _copy_personal(self, source, destination, *, with_content=True):
        total, count = 0, 0
        sections = ["config", "defaultconfigs", "options.txt", *(KINDS if with_content else [])]
        stack = [(source / name, destination / name) for name in sections]
        while stack:
            path, output = stack.pop()
            if not path.exists():
                continue
            st = path.lstat()
            if build_archive._reparse(st):
                raise ValueError("В сборке есть перенаправленная папка или файл; копирование остановлено.")
            if stat.S_ISDIR(st.st_mode):
                output.mkdir(parents=True, exist_ok=True)
                stack.extend((entry, output / clean_filename(entry.name)) for entry in path.iterdir() if not entry.name.startswith("."))
            elif stat.S_ISREG(st.st_mode):
                count, total = count + 1, total + st.st_size
                if count > 40_000 or total > MAX_CONTENT:
                    raise ValueError("Копия сборки превышает лимиты: 40 000 файлов или 8 ГБ.")
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, output)

    def apply(self, token, *, mode="clone", status=lambda message: None):
        with self.lock:
            plan = self.plans.get(token)
        if not plan or plan["expires"] <= time.monotonic():
            raise ValueError("Сравнение устарело. Сравни сборки ещё раз.")
        if mode not in {"clone", "existing"}:
            raise ValueError("Выбери отдельную сборку или обновление текущей.")
        pid, descriptor, result = plan["id"], plan["descriptor"], plan["result"]
        profile = self.store.profile(pid) if pid else plan["profile"]
        folder = self.store.folder(pid) if pid else None
        if pid and self.game_running(pid):
            raise ValueError("Закрой эту сборку перед синхронизацией.")
        if folder:
            require_idle_files(folder)
        if pid and self._fingerprint(profile, _pin(self.store, pid), self._inventory(pid)) != plan["fingerprint"]:
            raise ValueError("Сборка изменилась после сравнения. Сравни её ещё раз.")
        if mode == "existing" and not result["sameBase"]:
            raise ValueError("Для другой версии Minecraft или загрузчика нужна отдельная сборка.")
        wanted = result["missing"] + [row["wanted"] for row in result["different"]]
        if mode == "clone" and not result["sameBase"]:
            wanted = [row for row in descriptor["files"] if row["role"] != "client"]
        if any(not row["downloads"] for row in wanted):
            raise ValueError("Не для всех файлов есть проверяемый источник. Попроси у друга ZIP сборки или добавь указанные файлы вручную.")
        needed = sum(row["size"] for row in wanted)
        if mode == "clone" and pid:
            needed += sum(row["size"] for row in self._inventory(pid))
        if needed + 64 * 1024**2 > shutil.disk_usage(self.store.root).free:
            raise ValueError("Недостаточно места для подготовки общей сборки.")
        temp = Path(tempfile.mkdtemp(dir=self.store.root, prefix="share-sync-"))
        cleanup = True
        try:
            staged = {}
            for number, row in enumerate(wanted, 1):
                status(f"Сверяем сборку друга: {number}/{len(wanted)} · {row['title']}")
                path = temp / (str(number) + Path(row["path"]).suffix)
                error = None
                for url in row["downloads"]:
                    try:
                        download_verified(url, path, row["hashes"], row["size"], transfers=self.transfers, status=status, title=row["title"])
                        error = None
                        break
                    except (ValueError, OSError) as exc:
                        if type(exc).__name__ == "TransferCancelled":
                            raise
                        error = exc
                if error:
                    raise error
                verify_pack(path, row["path"].split("/")[0], descriptor["loader"])
                staged[row["path"]] = path
            # The user may edit files manually while network requests are running.
            if pid and self._fingerprint(self.store.profile(pid), _pin(self.store, pid), self._inventory(pid)) != plan["fingerprint"]:
                raise ValueError("Сборка изменилась во время загрузки. Сравни её ещё раз.")
            if folder:
                require_idle_files(folder)
            status("Применяем проверенный состав сборки…")
            effective_pin = descriptor["loaderVersion"] or (plan["pin"] if result["sameBase"] else None)
            pin_data = {"version": descriptor["minecraft"], "loader": descriptor["loader"], "loader_version": effective_pin}
            if mode == "clone":
                name = _unique_name(self.store, (descriptor["name"][:36] + " · с друзьями")[:48])

                def prepare(destination):
                    if folder:
                        self._copy_personal(folder, destination, with_content=result["sameBase"])
                    if result["sameBase"]:
                        for row in result["different"]:
                            destination.joinpath(*_path(row["current"]["path"])).unlink(missing_ok=True)
                    for relative, path in staged.items():
                        target = destination.joinpath(*_path(relative))
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists():
                            raise ValueError("В копии сборки уже есть другой файл с именем " + relative)
                        shutil.copy2(path, target)
                    if effective_pin:
                        (destination / build_archive.STAMP).write_text(json.dumps(pin_data), encoding="utf-8")

                new_id = self.store.create(name, descriptor["minecraft"], descriptor["loader"], prepare=prepare)
            else:
                self.before_change(pid, "Синхронизация со сборкой друга")
                removed, written = [], []
                old_stamp = folder / build_archive.STAMP
                if os.path.lexists(old_stamp) and (old_stamp.is_symlink() or not old_stamp.is_file() or old_stamp.stat().st_size > 65536):
                    raise ValueError("Сведения о загрузчике перенаправлены или повреждены. Исправь файл сборки перед синхронизацией.")
                stamp_data = old_stamp.read_bytes() if old_stamp.exists() else None
                try:
                    for number, row in enumerate(result["different"]):
                        target = folder.joinpath(*_path(row["current"]["path"]))
                        backup = temp / f"old-{number}"
                        target.rename(backup)
                        removed.append((target, backup))
                    for relative, path in staged.items():
                        target = folder.joinpath(*_path(relative))
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists():
                            raise ValueError("Имя файла уже занято: " + relative)
                        # Link/no-replace publication: never overwrite a manual file.
                        from .modfiles import publish_new
                        publish_new(path, target)
                        written.append(target)
                    if descriptor["loaderVersion"]:
                        stamp_temp = temp / "stamp.json"
                        stamp_temp.write_text(json.dumps(pin_data), encoding="utf-8")
                        os.replace(stamp_temp, old_stamp)
                except BaseException:
                    try:
                        for path in reversed(written):
                            path.unlink(missing_ok=True)
                        for target, backup in reversed(removed):
                            if target.exists():
                                raise ValueError("Во время восстановления появился другой файл.")
                            backup.rename(target)
                        if stamp_data is not None:
                            old_stamp.write_bytes(stamp_data)
                        else:
                            old_stamp.unlink(missing_ok=True)
                    except BaseException as recovery:
                        cleanup = False
                        raise ValueError("Восстановление не завершилось. Исходные файлы сохранены в " + str(temp)) from recovery
                    raise
                new_id, name = pid, profile["name"]
            with self.lock:
                self.plans.pop(token, None)
            return {"id": new_id, "name": name, "invite": descriptor.get("invite")}
        finally:
            if cleanup:
                shutil.rmtree(temp, ignore_errors=True)

    def room_state(self, pid):
        profile = self.store.profile(pid)
        # A log line is evidence of the last local event, not live participant presence.
        folder = self.store.folder(pid).resolve()
        running = bool(self.game_running(pid))
        current = room_bridge.status(folder, game_running=running)
        capability = {"bridgeSupported": room_bridge.supported(profile["version"], profile["loader"]),
                      "bridgeInstalled": room_bridge.installed(folder)}
        if current:
            phase = {"ACTIVE": "Сеанс Steam активен.", "CREATING": "e4steam создаёт сеанс.", "RECONNECTING": "e4steam восстанавливает соединение.",
                     "CLOSING": "Сеанс закрывается.", "CLOSED": "Сеанс закрыт.", "FAILED": "e4steam сообщил об ошибке сеанса.",
                     "NONE": "Мир ещё не открыт для друзей.", "UNAVAILABLE": "API e4steam пока не предоставил активный сеанс."}[current["state"]]
            return {"gameRunning": running, "lastEvent": current["state"].lower(), "lastEventTime": current["sampledAt"] / 1000,
                    "message": phase, "liveParticipants": current["names"], "peerCount": current["peers"], "capacity": current["capacity"],
                    "statusSource": "e4steam-api", "room": current, **capability,
                    "note": "Статус получен из текущего процесса Minecraft. Показаны имена только подтверждённых e4steam участников."}
        try:
            event, updated = steam_network._latest(folder)
            description = steam_network._LOG_TEXT[event]
        except (OSError, ValueError):
            event, updated, description = "unknown", None, "Журнал e4steam недоступен."
        return {"gameRunning": running, "lastEvent": event, "lastEventTime": updated,
                "message": description, "liveParticipants": None,
                "statusSource": "latest.log", **capability,
                "note": "Свежий статус из игры пока недоступен. Журнал показывает последний локальный сигнал; список участников подтверждается в Minecraft."}

    def add_server(self, pid, address, label="Мир друга"):
        """Add only an explicit e4steam invitation; preserve all existing NBT tags."""
        address = validate_address(address)
        label = _text(label, 80, "Мир друга") or "Мир друга"
        folder = self.store.folder(pid)
        if self.game_running(pid):
            raise ValueError("Закрой Minecraft, чтобы он не перезаписал список серверов.")
        require_idle_files(folder)
        path, backup = folder / "servers.dat", folder / ".blockharbor-servers.bak"
        for item in (path, backup):
            if item.is_symlink() or item.resolve().parent != folder.resolve():
                raise ValueError("Файл списка серверов перенаправлен.")
        existed = path.exists()
        if existed:
            if path.stat().st_size > 2 * 1024**2:
                raise ValueError("Список серверов слишком большой для автоматического изменения.")
            original = path.read_bytes()
            raw = original
            if raw.startswith(b"\x1f\x8b"):
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as zipped:
                    raw = zipped.read(2 * 1024**2 + 1)
                if len(raw) > 2 * 1024**2:
                    raise ValueError("Распакованный список серверов слишком велик.")
            tree = _NBT(raw).read()
        else:
            original, tree = b"", (10, b"", [])
        if tree[0] != 10:
            raise ValueError("Неизвестный формат servers.dat.")
        server_tags = [tag for tag in tree[2] if tag[1] == b"servers"]
        if len(server_tags) > 1:
            raise ValueError("В servers.dat повторяется список серверов.")
        if server_tags:
            tag = server_tags[0]
            if tag[0] != 9 or (tag[2][0] != 10 and not (tag[2][0] == 0 and not tag[2][1])):
                raise ValueError("Неизвестный формат списка серверов.")
            servers = tag[2][1]
            if tag[2][0] == 0:
                tree[2][tree[2].index(tag)] = (9, b"servers", (10, servers))
        else:
            servers = []
            tree[2].append((9, b"servers", (10, servers)))
        for server in servers:
            if any(kind == 8 and name == b"ip" and data == address.encode("ascii") for kind, name, data in server):
                return {"ok": True, "message": "Этот мир уже есть в списке серверов.", "address": address}
        if len(servers) >= 2048:
            raise ValueError("В Minecraft уже слишком много сохранённых серверов.")
        servers.append([(8, b"name", _modified_utf8(label)), (8, b"ip", address.encode("ascii"))])
        data = _NBT.encode(tree)
        self.before_change(pid, "Добавление мира друга в список серверов")
        with tempfile.TemporaryDirectory(dir=folder, prefix=".servers-") as temporary:
            stage = Path(temporary) / "servers.dat"
            stage.write_bytes(data)
            if path.is_symlink() or (path.read_bytes() != original if existed and path.exists() else path.exists() != existed):
                raise ValueError("Список серверов изменился во время подготовки. Повтори добавление.")
            if existed:
                old = Path(temporary) / "previous.dat"
                old.write_bytes(original)
                os.replace(old, backup)
            os.replace(stage, path)
        return {"ok": True, "message": "Мир добавлен. Запусти сборку и открой «Сетевая игра». Адрес действует, пока хозяин не закроет текущий сеанс.", "address": address}

    def handle(self, action, p, status=lambda message: None):
        if action in {"shareCreate", "shareInvite"}:
            value = self.descriptor(p["id"])
            if action == "shareInvite":
                value["invite"] = {"address": validate_address(p.get("address")), "label": _text(p.get("label"), 80, value["name"])}
            value = validate_descriptor(value)
            return {"descriptor": value, "code": encode_descriptor(value), "name": value["name"] + ".bhshare",
                    "manual": sum(not row["downloads"] and row["role"] != "client" for row in value["files"])}
        if action == "shareCompare":
            return self.compare(p.get("id", ""), p.get("descriptor", p.get("code", "")))
        if action == "shareInspect":
            value = _decode(p.get("descriptor", p.get("code", "")))
            return {"descriptor": value, "name": value["name"], "invite": value.get("invite")}
        if action == "shareApply":
            result = self.apply(p["token"], mode=p.get("mode", "clone"), status=status)
            return "Сборка «" + result["name"] + "» подготовлена. Личные дополнения исходной сборки сохранены."
        if action == "shareRoomState":
            return self.room_state(p["id"])
        if action == "shareAddServer":
            return self.add_server(p["id"], p.get("address"), p.get("label", "Мир друга"))
        raise ValueError("Неизвестное действие совместной сборки.")


def _modified_utf8(value):
    """NBT uses Java's modified UTF-8, including surrogate pairs for emoji."""
    result = bytearray()
    encoded = value.encode("utf-16-be")
    for pos in range(0, len(encoded), 2):
        code = int.from_bytes(encoded[pos:pos + 2], "big")
        if 1 <= code < 128:
            result.append(code)
        elif code < 2048:
            result.extend((0xC0 | code >> 6, 0x80 | code & 63))
        else:
            result.extend((0xE0 | code >> 12, 0x80 | code >> 6 & 63, 0x80 | code & 63))
    return bytes(result)


class _NBT:
    """Bounded NBT codec preserving unknown tags and exact string bytes."""
    def __init__(self, data):
        if len(data) > 2 * 1024**2:
            raise ValueError("Список серверов превышает 2 МБ.")
        self.source, self.count = io.BytesIO(data), 0

    def take(self, size):
        data = self.source.read(size)
        if len(data) != size:
            raise ValueError("Файл servers.dat повреждён.")
        return data

    def string(self):
        return self.take(struct.unpack(">H", self.take(2))[0])

    def length(self):
        size = struct.unpack(">i", self.take(4))[0]
        if not 0 <= size <= 2 * 1024**2:
            raise ValueError("Некорректная длина массива в servers.dat.")
        return size

    def payload(self, kind, depth=0):
        self.count += 1
        if depth > 32 or self.count > 100_000:
            raise ValueError("Список серверов слишком сложный для безопасного изменения.")
        if kind in {1, 2, 3, 4, 5, 6}:
            return self.take({1: 1, 2: 2, 3: 4, 4: 8, 5: 4, 6: 8}[kind])
        if kind in {7, 11, 12}:
            return self.take(self.length() * {7: 1, 11: 4, 12: 8}[kind])
        if kind == 8:
            return self.string()
        if kind == 9:
            child, size = self.take(1)[0], self.length()
            if size > 100_000 or (child == 0 and size):
                raise ValueError("Некорректный список NBT.")
            return child, [self.payload(child, depth + 1) for _ in range(size)]
        if kind == 10:
            result = []
            while child := self.take(1)[0]:
                name = self.string()
                result.append((child, name, self.payload(child, depth + 1)))
            return result
        raise ValueError("Неизвестный тип NBT в servers.dat.")

    def read(self):
        kind = self.take(1)[0]
        name = self.string()
        value = self.payload(kind)
        if self.source.read(1):
            raise ValueError("В servers.dat обнаружены лишние данные.")
        return kind, name, value

    @staticmethod
    def encode(tree):
        def string(value):
            if len(value) > 65535:
                raise ValueError("Строка NBT слишком длинная.")
            return struct.pack(">H", len(value)) + value

        def payload(kind, value):
            if kind in {1, 2, 3, 4, 5, 6}:
                return value
            if kind in {7, 11, 12}:
                return struct.pack(">i", len(value) // {7: 1, 11: 4, 12: 8}[kind]) + value
            if kind == 8:
                return string(value)
            if kind == 9:
                child, rows = value
                return bytes([child]) + struct.pack(">i", len(rows)) + b"".join(payload(child, row) for row in rows)
            if kind == 10:
                return b"".join(bytes([child]) + string(name) + payload(child, data) for child, name, data in value) + b"\0"
            raise ValueError("Неизвестный тип NBT.")

        return bytes([tree[0]]) + string(tree[1]) + payload(tree[0], tree[2])
