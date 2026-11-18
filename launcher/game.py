"""Minecraft Java installations, isolated game directories and optional native login."""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from . import lifecycle, preparation

REDIRECT_URI = "http://localhost:43821/callback"
MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
LOADERS = ("vanilla", "fabric", "forge", "neoforge", "quilt")
META = {
    "fabric": "https://meta.fabricmc.net/v2",
    "quilt": "https://meta.quiltmc.org/v3",
}
STORE_FAMILY = "Microsoft.4297127D64EC6_8wekyb3d8bbwe"
STORE_PROFILES = "launcher_profiles_microsoft_store.json"
CLASSIC_PROFILES = "launcher_profiles.json"
STAMP = ".blockharbor-install.json"
Status = Callable[[str], None]


def _quiet(_message: str) -> None:
    pass


def _get_json(url: str):
    return preparation.json_document(url)


def list_versions() -> list[str]:
    return [v["id"] for v in _get_json(MANIFEST)["versions"] if v["type"] == "release"]


def _version_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,149}", value)
        or ".." in value
    ):
        raise ValueError("Недопустимый идентификатор версии Minecraft или загрузчика.")
    return value


def _check(version: str, loader: str) -> str:
    _version_id(version)
    loader = loader.lower()
    if loader not in LOADERS:
        raise ValueError("Неизвестный загрузчик модов.")
    return loader


def _atomic_json(path: Path, data: dict, expected: bytes | None = None) -> None:
    lifecycle.check()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".blockharbor-", delete=False
    ) as out:
        temporary = Path(out.name)
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if expected is not None and path.read_bytes() != expected:
            raise RuntimeError(
                "Профили Minecraft Launcher изменились. Закрой его и повтори операцию."
            )
        with lifecycle.guard():
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _install_lock(cache: Path):
    """Protect the shared libraries and profile list across BlockHarbor processes."""
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / ".blockharbor-install.lock").open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(
                "Уже идёт подготовка сборки. Дождись окончания и повтори."
            ) from exc
        yield  # Closing the handle releases both types of lock, including on errors.


def _record(instance: Path) -> dict:
    try:
        data = json.loads((instance / STAMP).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Повреждена запись об установке сборки.")
    return data


def _manifest_path(cache: Path, version: str) -> Path:
    return cache / "versions" / _version_id(version) / (version + ".json")


def _meta_profile(
    cache: Path, version: str, loader: str, pin: str | None
) -> tuple[str, str]:
    """Use the loader project's native launcher profile, also used by the official client."""
    url = f"{META[loader]}/versions/loader/{version}"
    if not pin:
        loaders = [row["loader"] for row in _get_json(url)]
        stable = [
            row["version"]
            for row in loaders
            if row.get("stable", True)
            and re.fullmatch(r"\d+(?:\.\d+)+", row["version"])
        ]
        if not stable:
            raise ValueError(f"Для Minecraft {version} нет стабильного {loader}.")
        # Quilt returns rows in mixed order. For both vendors choose the newest stable build.
        pin = max(stable, key=lambda v: tuple(int(n) for n in v.split(".")))
    _version_id(pin)
    installed = _version_id(f"{loader}-loader-{pin}-{version}")
    dest = _manifest_path(cache, installed)
    if dest.is_file():
        profile = json.loads(dest.read_text(encoding="utf-8"))
    else:
        profile = _get_json(f"{url}/{pin}/profile/json")
    if (
        profile.get("id") != installed
        or profile.get("inheritsFrom") != version
        or not profile.get("mainClass")
    ):
        raise ValueError("Метаданные загрузчика не совпали с выбранной версией.")
    if not dest.is_file():
        _atomic_json(dest, profile)
    return installed, pin


def _repair_loader_files(cache: Path, installed: str, status: Status) -> None:
    """Loader Maven metadata uses optional checksums that launcher-lib does not read."""
    from minecraft_launcher_lib.command import get_library_path

    libs_dir = (cache / "libraries").resolve()
    manifest = json.loads(_manifest_path(cache, installed).read_text(encoding="utf-8"))
    for entry in manifest["libraries"]:
        lifecycle.check()
        path = Path(get_library_path(entry["name"], str(cache))).resolve()
        if not path.is_relative_to(libs_dir):
            raise ValueError("Некорректный путь библиотеки загрузчика.")

        def valid(file: Path) -> bool:
            if not file.is_file() or not zipfile.is_zipfile(file):
                return False
            if entry.get("size") and file.stat().st_size != entry["size"]:
                return False
            expected = entry.get("sha256") or entry.get("sha1")
            if expected:
                algorithm = "sha256" if entry.get("sha256") else "sha1"
                with file.open("rb") as src:
                    return hashlib.file_digest(src, algorithm).hexdigest() == expected
            return True

        if valid(path):
            continue
        root = entry.get("url") or "https://libraries.minecraft.net"
        if urlsplit(root).scheme != "https":
            raise ValueError("Загрузчик указал неподдерживаемый адрес библиотеки.")
        url = root.rstrip("/") + "/" + path.relative_to(libs_dir).as_posix()
        status("Скачиваем " + path.name + "…")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temp = Path(handle.name)
        try:
            progress = (
                status
                if isinstance(status, preparation.Progress)
                else preparation.Progress(status)
            )
            preparation.Download(progress)(url, temp, sha1=entry.get("sha1"))

            if not valid(temp):
                raise ValueError(
                    "Не удалось проверить скачанную библиотеку "
                    + path.name
                    + ". Повтори загрузку."
                )
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def _callback(status: Status) -> dict:
    if isinstance(status, preparation.Progress):
        return status.callbacks()
    return preparation.Progress(status).callbacks()


def _prepare_files(version, cache, callbacks, status):
    import minecraft_launcher_lib as mc

    if isinstance(status, preparation.Progress):
        status.start()
    mc.install.install_minecraft_version(version, cache, callback=callbacks)


def _legacy_java(cache, status, callbacks):
    """Old releases such as 1.6.4 omit Mojang's Java component metadata."""
    import minecraft_launcher_lib as mc

    system = preparation.legacy_system_java()
    if system:
        return system
    component = "jre-legacy"
    java = mc.runtime.get_executable_path(component, cache)
    if java and preparation.runtime_problem(cache, component, java) is None:
        return java
    if isinstance(status, preparation.Progress):
        status.start()
    status("Устанавливаем Java 8 для этой версии Minecraft…")
    mc.runtime.install_jvm_runtime(component, cache, callback=callbacks, max_workers=6)
    java = mc.runtime.get_executable_path(component, cache)
    if not java or preparation.runtime_problem(cache, component, java):
        raise RuntimeError(
            "Установка Java 8 не завершена. Проверь подключение и повтори запуск."
        )
    return java


def _missing_install_file(cache: Path, base: str, installed: str) -> str | None:
    """Check the local launch dependencies. No requests or large file hashes on a repeat Play."""
    import minecraft_launcher_lib as mc

    metadata = json.loads(_manifest_path(cache, base).read_text(encoding="utf-8"))
    jar = _manifest_path(cache, base).with_suffix(".jar")
    expected = metadata.get("downloads", {}).get("client", {}).get("size")
    if not jar.is_file() or (expected is not None and jar.stat().st_size != expected):
        return jar.name
    component = metadata.get("javaVersion", {}).get("component")
    if component:
        java = mc.runtime.get_executable_path(component, cache)
        if not java:
            return f"Java ({component})"
        problem = preparation.runtime_problem(cache, component, java)
        if problem:
            return problem

    # Use the library's own resolved OS-specific classpath, including loader dependencies.
    args = mc.command.get_minecraft_command(
        installed, cache, {"username": "Player", "uuid": "0" * 32, "token": "0"}
    )
    sizes = {}
    for lib in metadata.get("libraries", []):
        downloads = lib.get("downloads", {})
        for entry in (
            downloads.get("artifact", {}),
            *downloads.get("classifiers", {}).values(),
        ):
            if "path" in entry and "size" in entry:
                sizes[cache / "libraries" / entry["path"]] = entry["size"]
    for flag in ("-cp", "-classpath", "--class-path", "-p", "--module-path"):
        if flag not in args:
            continue
        for library in args[args.index(flag) + 1].split(os.pathsep):
            if not library:
                continue
            path = Path(library)
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                return path.name
            if not size or size != sizes.get(path, size):
                return path.name
    if metadata.get("logging", {}).get("client"):
        log_id = metadata["logging"]["client"]["file"]["id"]
        if not (cache / "assets" / "log_configs" / log_id).is_file():
            return log_id
    if "assetIndex" in metadata:
        index = (
            cache / "assets" / "indexes" / (_version_id(metadata["assets"]) + ".json")
        )
        if not index.is_file():
            return index.name
        objects = json.loads(index.read_text(encoding="utf-8"))["objects"]
        for item in objects.values():
            lifecycle.check()
            key = item["hash"]
            if not re.fullmatch("[a-f0-9]{40}", key):
                return index.name
            obj = cache / "assets" / "objects" / key[:2] / key
            try:
                if obj.stat().st_size != item["size"]:
                    return obj.name
            except FileNotFoundError:
                return obj.name
    return None


def _install(
    version: str,
    loader: str,
    cache: Path,
    instance: Path,
    status: Status,
    official: bool,
) -> str:
    previous = _record(instance)
    same = previous.get("version") == version and previous.get("loader") == loader
    pin = previous.get("loader_version") if same else None
    callbacks = _callback(status)
    installed = version
    status(f"Подготовка Minecraft {version} · {loader}")
    if loader == "vanilla" and official:
        if (
            not same
            and not _manifest_path(cache, version).is_file()
            and version not in list_versions()
        ):
            raise ValueError(
                f"Версия {version} отсутствует в официальном списке релизов Minecraft."
            )
    elif loader in META:
        installed, pin = _meta_profile(cache, version, loader, pin)
    elif loader != "vanilla":
        import minecraft_launcher_lib as mc

        provider = mc.mod_loader.get_mod_loader(loader)
        if loader == "forge" and not pin:
            # Maven metadata is not consistently sorted. Forge publishes per-Minecraft recommendations.
            promos = _get_json(
                "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
            )["promos"]
            pin = promos.get(f"{version}-recommended") or promos.get(
                f"{version}-latest"
            )
        pin = _version_id(pin or provider.get_latest_loader_version(version))
        installed = _version_id(provider.get_installed_version(version, pin))
        ready = (
            same
            and previous.get("cache") == str(cache)
            and previous.get("id") == installed
            and _manifest_path(cache, installed).is_file()
        )
        if not ready:
            # Vanilla metadata specifies the Java generation required by this game.
            _prepare_files(version, cache, callbacks, status)
            metadata = json.loads(
                _manifest_path(cache, version).read_text(encoding="utf-8")
            )
            component = metadata.get("javaVersion", {}).get("component")
            java = (
                mc.runtime.get_executable_path(component, cache)
                if component
                else _legacy_java(cache, status, callbacks)
            )
            if not java:
                raise RuntimeError(
                    "Не удалось установить Java для этой версии. Проверь подключение и снова нажми «Играть»."
                )
            try:
                result = provider.install(
                    version, cache, loader_version=pin, java=java, callback=callbacks
                )
                lifecycle.check()
            except subprocess.CalledProcessError as exc:
                task = ""
                if isinstance(exc.cmd, (tuple, list)) and "--task" in exc.cmd:
                    index = exc.cmd.index("--task") + 1
                    if index < len(exc.cmd) and re.fullmatch(
                        r"[A-Z_]{1,48}", str(exc.cmd[index])
                    ):
                        task = " (" + str(exc.cmd[index]) + ")"
                raise RuntimeError(
                    f"Установщик {loader} завершился с кодом {exc.returncode}{task}. "
                    "Проверь доступ Java к интернету и снова нажми «Играть»."
                ) from None
            if result != installed or not _manifest_path(cache, installed).is_file():
                raise RuntimeError(
                    f"Установщик {loader} не создал профиль {installed}. Повтори установку."
                )
    if not official:
        import minecraft_launcher_lib as mc

        status("Проверяем локальные файлы игры…")
        if loader in META:
            _repair_loader_files(cache, installed, status)
        try:
            ready = _missing_install_file(cache, version, installed) is None
        except lifecycle.OperationCancelled:
            raise
        except (OSError, ValueError, KeyError, mc.exceptions.VersionNotFound):
            ready = False
        if not ready:
            # Re-run once if the vendor installer silently skipped a required file.
            # Verified downloads are reused, and incomplete bytes stay in temporary files.
            for attempt in range(2):
                _prepare_files(installed, cache, callbacks, status)
                missing = _missing_install_file(cache, version, installed)
                if not missing:
                    break
                if not attempt:
                    status("Восстанавливаем недостающий файл " + missing + "…")
            if missing:
                raise RuntimeError(
                    f"Загрузка не завершена. Не найден или повреждён файл {missing}. "
                    "Проверь интернет и снова нажми «Играть»."
                )
    java_override = None
    if not official:
        metadata = json.loads(
            _manifest_path(cache, version).read_text(encoding="utf-8")
        )
        if not metadata.get("javaVersion"):
            java_override = _legacy_java(cache, status, callbacks)
    instance.mkdir(parents=True, exist_ok=True)
    _atomic_json(
        instance / STAMP,
        {
            "version": version,
            "loader": loader,
            "loader_version": pin,
            "id": installed,
            "cache": str(cache),
            "java": java_override,
        },
    )
    return installed


def install(
    version: str, loader: str, cache: Path, instance: Path, status: Status = _quiet
) -> str:
    lifecycle.check()
    loader = _check(version, loader)
    cache = Path(cache).resolve()
    with _install_lock(cache), preparation.job(status) as progress:
        return _install(
            version, loader, cache, Path(instance).resolve(), progress, official=False
        )


def _profile_paths() -> list[Path]:
    if os.name == "nt":
        default = Path(os.environ["APPDATA"]) / ".minecraft"
        store = (
            Path(os.environ["LOCALAPPDATA"])
            / "Packages"
            / STORE_FAMILY
            / "LocalCache"
            / "Local"
            / "minecraft"
        )
        modern = [default / STORE_PROFILES, store / STORE_PROFILES]
        classic = [default / CLASSIC_PROFILES, store / CLASSIC_PROFILES]
        # A previous Store install can leave its profile file behind on a legacy-only PC.
        legacy_only = bool(_legacy_executables()) and not _store_app_id()
        candidates = (classic + modern) if legacy_only else (modern + classic)
    elif sys.platform == "darwin":
        candidates = [
            Path.home() / "Library/Application Support/minecraft" / CLASSIC_PROFILES
        ]
    else:
        candidates = [Path.home() / ".minecraft" / CLASSIC_PROFILES]
    return [path for path in candidates if path.is_file()]


def _check_official_closed() -> None:
    if os.name != "nt":
        return
    executable = Path(os.environ["SystemRoot"]) / "System32/tasklist.exe"
    result = lifecycle.run(
        [str(executable), "/FO", "CSV", "/NH"],
        capture_output=True,
        timeout=12,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=True,
    )
    processes = csv.reader(result.stdout.decode("ascii", errors="replace").splitlines())
    if any(
        row
        and row[0].lower()
        in ("minecraftlauncher.exe", "minecraft.exe", "minecraftlauncherbeta.exe")
        for row in processes
    ):
        raise RuntimeError(
            "Закрой официальный Minecraft Launcher (включая окно в трее), затем повтори подготовку сборки."
        )


def _memory(value: int) -> int:
    if type(value) is not int or not 512 <= value <= 65536:
        raise ValueError("Размер памяти для Java должен быть от 512 до 65536 МБ.")
    return value


def official_prepare(
    version: str,
    loader: str,
    instance: Path,
    label: str,
    status: Status = _quiet,
    *,
    memory_mb: int = 4096,
) -> str:
    """Register one installation without touching credentials or unrelated profile settings."""
    loader = _check(version, loader)
    memory = _memory(memory_mb)
    label = label.strip()
    if not label:
        raise ValueError("Укажи имя сборки.")
    paths = _profile_paths()
    if not paths:
        try:
            open_official()
        except (OSError, RuntimeError, subprocess.SubprocessError):
            raise RuntimeError(
                "Установи Minecraft Launcher с https://www.minecraft.net/download, открой раздел Java Edition и закрой лаунчер. Затем повтори подготовку сборки."
            ) from None
        raise RuntimeError(
            "Открыт Minecraft Launcher. Войди в аккаунт, открой раздел Java Edition, затем закрой лаунчер и снова нажми «Играть» в BlockHarbor."
        )
    path = paths[0]
    instance = Path(instance).resolve()
    key = (
        "blockharbor-"
        + hashlib.sha256(os.path.normcase(str(instance)).encode()).hexdigest()[:24]
    )
    cache = path.parent.resolve()
    with _install_lock(cache):
        existing = (
            json.loads(path.read_bytes().decode("utf-8-sig"))
            .get("profiles", {})
            .get(key, {})
        )
        record = _record(instance)
        if (
            record.get("version") == version
            and record.get("loader") == loader
            and record.get("cache") == str(cache)
            and existing.get("lastVersionId") == record.get("id")
            and existing.get("name") == label
            and existing.get("javaArgs") == f"-Xmx{memory}M"
            and existing.get("gameDir") == str(instance)
            and (loader == "vanilla" or _manifest_path(cache, record["id"]).is_file())
        ):
            status(f"Установка готова: {label}")
            return label
        _check_official_closed()
        with preparation.job(status) as progress:
            installed = _install(
                version, loader, cache, instance, progress, official=True
            )
        _check_official_closed()
        original = path.read_bytes()
        data = json.loads(original.decode("utf-8-sig"))
        profiles = data.setdefault("profiles", {})
        stamp = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        entry = profiles.setdefault(
            key, {"type": "custom", "created": stamp, "icon": "Grass"}
        )
        entry.update(
            name=label,
            lastVersionId=installed,
            gameDir=str(instance),
            lastUsed=stamp,
            javaArgs=f"-Xmx{memory}M",
        )
        _atomic_json(path, data, expected=original)
        verified = json.loads(path.read_text(encoding="utf-8"))["profiles"][key]
        if (
            verified["gameDir"] != str(instance)
            or verified["lastVersionId"] != installed
        ):
            raise RuntimeError(
                "Minecraft Launcher изменил созданный профиль. Закрой его и повтори."
            )
    status(f"Установка готова: {label}")
    return label


def _legacy_executables() -> list[Path]:
    paths = [
        Path(os.environ[root]) / "Minecraft Launcher/MinecraftLauncher.exe"
        for root in ("ProgramFiles(x86)", "ProgramFiles")
        if root in os.environ
    ]
    return [path for path in paths if path.is_file()]


def _store_app_id() -> str:
    # Resolve the installed manifest's App ID instead of guessing a version-specific path.
    system = Path(os.environ["SystemRoot"])
    script = "(Get-StartApps | Where-Object {$_.AppID -like 'Microsoft.4297127D64EC6_8wekyb3d8bbwe!*'} | Select-Object -First 1).AppID"
    args = [
        str(system / "System32/WindowsPowerShell/v1.0/powershell.exe"),
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]
    try:
        result = lifecycle.run(
            args,
            capture_output=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    appid = result.stdout.decode("ascii", errors="ignore").strip()
    return (
        appid
        if re.fullmatch(re.escape(STORE_FAMILY) + r"![\w.\-]+", appid, re.I)
        else ""
    )


def open_official() -> None:
    if os.name == "nt":
        system = Path(os.environ["SystemRoot"])
        appid = _store_app_id()
        if appid:
            subprocess.Popen(
                [str(system / "explorer.exe"), "shell:AppsFolder\\" + appid]
            )
            return
        for exe in _legacy_executables():
            subprocess.Popen([str(exe)])
            return
    elif sys.platform == "darwin":
        subprocess.run(["open", "-a", "Minecraft"], check=True)
        return
    else:
        exe = shutil.which("minecraft-launcher")
        if exe:
            subprocess.Popen([exe])
            return
    raise RuntimeError(
        "Minecraft Launcher не найден. Установи его с minecraft.net/download или открой вручную из меню «Пуск»."
    )


def login(client_id: str, status: Status = _quiet) -> dict:
    """Use Microsoft PKCE in the system browser. No tokens are written to disk."""
    import minecraft_launcher_lib as mc

    lifecycle.check()

    try:
        client_id = str(UUID(client_id.strip()))
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            "Укажи Application (client) ID зарегистрированного приложения Microsoft."
        ) from exc
    accounts = mc.microsoft_account
    url, state, verifier = accounts.get_secure_login_data(client_id, REDIRECT_URI)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # The authorization code is in the callback URL.

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != "/callback" or not hmac.compare_digest(
                query.get("state", [""])[0].encode(), state.encode()
            ):
                self.send_error(400, "Invalid callback")
                return
            if "error" in query:
                result["error"] = True
            elif query.get("code"):
                result["code"] = query["code"][0]
            else:
                self.send_error(400, "Missing code")
                return
            message = "Ответ получен. Вернись в BlockHarbor."
            body = message.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class LocalServer(HTTPServer):
        def get_request(self):
            sock, address = super().get_request()
            sock.settimeout(4)
            return sock, address

    try:
        port = urlsplit(REDIRECT_URI).port
        server = LocalServer(("127.0.0.1", port), Callback)
    except OSError as exc:
        raise RuntimeError(
            f"Порт {port} занят. Заверши предыдущий вход и повтори."
        ) from exc
    with server:
        server.timeout = 0.5
        status("Войди в Microsoft в открывшемся браузере.")
        if not webbrowser.open(url):
            raise RuntimeError("Не удалось открыть системный браузер для входа.")
        deadline = time.monotonic() + 180
        while not result and time.monotonic() < deadline:
            lifecycle.check()
            server.handle_request()
    lifecycle.check()
    if "code" not in result:
        raise RuntimeError("Вход отменён или время ожидания истекло. Попробуй ещё раз.")
    status("Проверка профиля Minecraft Java Edition…")
    try:
        with preparation.account_environment(accounts):
            account = accounts.complete_login(
                client_id, None, REDIRECT_URI, result["code"], verifier
            )
        lifecycle.check()
        if (
            not account.get("name")
            or not account.get("id")
            or not account.get("access_token")
        ):
            raise ValueError("Invalid Minecraft profile")
    except mc.exceptions.AccountNotOwnMinecraft:
        raise RuntimeError(
            "У аккаунта нет готового профиля Minecraft Java. Проверь покупку или подписку и создай профиль в официальном лаунчере."
        ) from None
    except mc.exceptions.AzureAppNotPermitted:
        raise RuntimeError(
            "У приложения нет доступа к Minecraft Services. Используй официальный запуск или одобренный Client ID."
        ) from None
    except lifecycle.OperationCancelled:
        raise
    except Exception:
        raise RuntimeError(
            "Сервис входа Microsoft или Xbox не выдал профиль Minecraft Java. Проверь Client ID, разрешения приложения и настройки аккаунта."
        ) from None
    # A refresh token is unnecessary when credentials last only for this application session.
    return {
        "name": account["name"],
        "id": account["id"],
        "access_token": account["access_token"],
        "client_id": client_id,
    }


def _java_offline_uuid(name: str) -> str:
    # Matches UUID.nameUUIDFromBytes used for local Java profiles.
    digest = hashlib.md5(
        ("OfflinePlayer:" + name).encode("utf-8"), usedforsecurity=False
    ).digest()
    return UUID(bytes=digest, version=3).hex


def launch(
    cache: Path,
    instance: Path,
    session: dict,
    nickname: str = "",
    memory_mb: int = 4096,
    status: Status = _quiet,
) -> subprocess.Popen:
    if (
        not session
        or not session.get("access_token")
        or not session.get("name")
        or not session.get("id")
    ):
        raise ValueError(
            "Для прямого запуска сначала войди в Microsoft с Minecraft Java."
        )
    if nickname and nickname != session["name"]:
        return launch_local(cache, instance, nickname, memory_mb, status)
    xuid = "0"
    try:
        jwt_payload = session["access_token"].split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(jwt_payload + "=" * (-len(jwt_payload) % 4))
        )
        if str(claims.get("xuid", "")).isdecimal():
            xuid = str(claims["xuid"])
    except (IndexError, TypeError, ValueError):
        pass  # Minecraft Services establishes identity; an opaque token may have no xuid.
    return _start_java(
        cache,
        instance,
        session["name"],
        UUID(session["id"]).hex,
        session["access_token"],
        session.get("client_id", ""),
        xuid,
        memory_mb,
        status,
    )


def launch_local(
    cache: Path,
    instance: Path,
    nickname: str,
    memory_mb: int = 4096,
    status: Status = _quiet,
) -> subprocess.Popen:
    """Local Java singleplayer/LAN identity; no Microsoft session or server authentication."""
    if not isinstance(nickname, str) or not re.fullmatch(
        r"[A-Za-z0-9_]{3,16}", nickname
    ):
        raise ValueError(
            "Ник должен содержать 3–16 латинских букв, цифр или знаков подчёркивания."
        )
    return _start_java(
        cache,
        instance,
        nickname,
        _java_offline_uuid(nickname),
        "0",
        "",
        "0",
        memory_mb,
        status,
    )


def _start_java(
    cache: Path,
    instance: Path,
    username: str,
    player_id: str,
    token: str,
    client_id: str,
    xuid: str,
    memory_mb: int,
    status: Status,
) -> subprocess.Popen:
    import minecraft_launcher_lib as mc

    lifecycle.check()

    cache, instance = Path(cache).resolve(), Path(instance).resolve()
    record = _record(instance)
    if record.get("cache") != str(cache) or not record.get("id"):
        raise ValueError("Сначала установи файлы этой сборки для прямого запуска.")
    installed = _version_id(record["id"])
    native_root = instance / ".blockharbor-natives"
    if native_root.is_symlink() or native_root.resolve().parent != instance:
        raise ValueError("Папка игровых библиотек перенаправлена.")
    native_root.mkdir(exist_ok=True)
    natives = Path(tempfile.mkdtemp(prefix="launch-", dir=native_root))
    try:
        # Provider installation can refresh its shared native cache for another
        # build. Every JVM loads its own extracted files, never those DLLs.
        mc.natives.extract_natives(installed, cache, natives)
        lifecycle.check()
    except BaseException:
        shutil.rmtree(natives, ignore_errors=True)
        raise
    options = {
        "username": username,
        "uuid": player_id,
        "token": token,
        "gameDirectory": str(instance),
        "nativesDirectory": str(natives),
        "jvmArguments": [f"-Xmx{_memory(memory_mb)}M"],
        "enableLoggingConfig": True,
        "launcherName": "BlockHarbor",
        "launcherVersion": "2.4.2",
    }
    if record.get("java"):
        options["executablePath"] = record["java"]
    try:
        command = mc.command.get_minecraft_command(installed, cache, options)
    except BaseException:
        shutil.rmtree(natives, ignore_errors=True)
        raise
    # Launcher-lib 8 leaves these placeholders unresolved in modern release metadata.
    command = [
        arg.replace("${clientid}", client_id).replace("${auth_xuid}", xuid)
        for arg in command
    ]
    status("Запускается Minecraft…")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    # The game writes logs/latest.log. Never log the command line which includes a token.
    try:
        from .diagnostics import game_stderr
        with game_stderr(instance) as stderr, lifecycle.guard():
            process = subprocess.Popen(
                command,
                cwd=instance,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                creationflags=flags,
            )
    except FileNotFoundError:
        shutil.rmtree(natives, ignore_errors=True)
        raise RuntimeError(
            "Исполняемый файл Java не найден. Проверь подключение и снова нажми «Играть» для восстановления файлов."
        ) from None
    except BaseException:
        shutil.rmtree(natives, ignore_errors=True)
        raise
    scope = lifecycle.current_scope()
    def clean_natives():
        while process.poll() is None:
            if scope:
                if scope.event.wait(0.5):
                    return  # Next idle launch cleans these private native files.
            else:
                time.sleep(0.5)
        if scope and scope.event.is_set():
            return
        if natives.resolve().parent == native_root.resolve() and not natives.is_symlink():
            shutil.rmtree(natives, ignore_errors=True)
    threading.Thread(target=clean_natives, daemon=True, name="blockharbor-native-files").start()
    try:
        exit_code = process.wait(timeout=1.25)
    except subprocess.TimeoutExpired:
        return process
    if isinstance(exit_code, int) and exit_code != 0:
        log = instance / "logs" / "latest.log"
        detail = (
            f"Журнал игры: {log}"
            if log.is_file()
            else "Журнал Minecraft ещё не создан. Проверь совместимость Java и модов этой сборки."
        )
        raise RuntimeError(
            f"Minecraft завершился при запуске (код {exit_code}). {detail}"
        )
    return process
