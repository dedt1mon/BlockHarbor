"""Optional Microsoft sign-in via the genuine Windows Prism Launcher distribution."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import game, lifecycle
from .store import input_profile

# Official portable MinGW distribution includes Qt and its C++ runtime. Original
# project signatures, branding, Microsoft login, and user-facing dialogs are intact.
RELEASE = "11.1.1"
FILE = f"PrismLauncher-Windows-MinGW-w64-Portable-{RELEASE}.zip"
URL = (
    f"https://github.com/PrismLauncher/PrismLauncher/releases/download/{RELEASE}/{FILE}"
)
SIZE = 43_687_692
SHA256 = "05841d0b3bfc0a8212658457cc6035e1c29cd7ffae919464e874e384cbe8d062"
LOADER = {
    "forge": "net.minecraftforge",
    "neoforge": "net.neoforged",
    "fabric": "net.fabricmc.fabric-loader",
    "quilt": "org.quiltmc.quilt-loader",
}
REQUIRED = (
    "prismlauncher.exe",
    "platforms/qwindows.dll",
    "Qt6Core.dll",
    "jars/NewLaunch.jar",
    "tls/qschannelbackend.dll",
)
METADATA = "https://meta.prismlauncher.org/v1"
Status = Callable[[str], None]


def _engine(root: Path, status: Status) -> Path:
    lifecycle.check()
    runtime = root / f"engine-{RELEASE}"
    if runtime.exists():
        if all((runtime / name).is_file() for name in REQUIRED):
            return runtime / "prismlauncher.exe"
        raise ValueError(
            f"Файлы Prism повреждены. Закрой Prism, переименуй каталог {runtime} и повтори запуск."
        )
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        archive = Path(tmp) / FILE
        stage = Path(tmp) / "engine"
        digest = hashlib.sha256()
        size = 0
        status("Загружаем Prism с GitHub · 43,7 МБ")
        request = Request(URL, headers={"User-Agent": "BlockHarbor/2.4.0"})
        try:
            with lifecycle.response(urlopen(request, timeout=25)) as source, archive.open("wb") as dest:
                while block := source.read(512 * 1024):
                    lifecycle.check()
                    size += len(block)
                    if size > SIZE:
                        raise ValueError(
                            "Размер архива Prism отличается от официального релиза."
                        )
                    digest.update(block)
                    dest.write(block)
                    status(f"Загружаем Prism · {size * 100 // SIZE}%")
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(
                "Не удалось загрузить Prism с GitHub. Проверь соединение и повтори запуск."
            ) from exc
        if size != SIZE or digest.hexdigest() != SHA256:
            raise ValueError(
                "Архив Prism не прошёл проверку SHA-256. Повтори загрузку."
            )
        with zipfile.ZipFile(archive) as zipped:
            if any(name not in zipped.namelist() for name in REQUIRED):
                raise ValueError("Официальный архив Prism неполон.")
            # Verify the full upstream archive before extracting any member.
            for item in zipped.infolist():
                lifecycle.check()
                zipped.extract(item, stage)
        with lifecycle.guard():
            stage.rename(runtime)
    return runtime / "prismlauncher.exe"


def _rows(uid: str) -> list[dict]:
    return game._get_json(f"{METADATA}/{uid}/index.json")["versions"]


def _components(folder: Path, version: str, loader: str) -> list[dict]:
    components = [{"uid": "net.minecraft", "version": version, "important": True}]
    if not any(row["version"] == version for row in _rows("net.minecraft")):
        raise ValueError(
            f"В каталоге Prism нет Minecraft {version}. Проверь номер версии."
        )
    if loader == "vanilla":
        return components
    if loader in ("fabric", "quilt"):
        uid = "net.fabricmc.intermediary"
        if not any(row["version"] == version for row in _rows(uid)):
            raise ValueError(
                f"Для Minecraft {version} нет совместимых компонентов {loader}."
            )
        components.append({"uid": uid, "version": version, "dependencyOnly": True})
    uid = LOADER[loader]
    compatible = [
        row
        for row in _rows(uid)
        if all(
            dep.get("equals", version) == version
            for dep in row.get("requires", [])
            if dep["uid"] == "net.minecraft"
        )
    ]
    # Respect a loader already pinned by BlockHarbor direct launch.
    saved = game._record(folder)
    if (
        saved.get("version") == version
        and saved.get("loader") == loader
        and saved.get("loader_version")
    ):
        pin = saved["loader_version"]
        if loader == "forge":
            pin = pin.removeprefix(version + "-")
        selected = next((row for row in compatible if row["version"] == pin), None)
        if selected is None:
            raise ValueError(
                f"В Prism ещё нет выбранного загрузчика {loader} {pin} для Minecraft {version}."
            )
    else:
        stable = [row for row in compatible if row.get("type", "release") == "release"]
        # The Prism index is newest-first. Prefer the upstream recommended stable build.
        selected = next(
            (row for row in stable if row.get("recommended")), None
        ) or next(iter(stable), None)
        if selected is None:
            raise ValueError(
                f"В Prism нет стабильного {loader} для Minecraft {version}."
            )
    components.append({"uid": uid, "version": selected["version"], "important": True})
    return components


def _link_game(folder: Path, destination: Path) -> None:
    if os.name == "nt":
        import _winapi

        try:
            _winapi.CreateJunction(str(folder), str(destination))
        except OSError as exc:
            raise RuntimeError(
                "Не удалось привязать папку сборки. Для режима Microsoft нужен локальный диск с поддержкой точек соединения NTFS."
            ) from exc
    else:
        destination.symlink_to(folder, target_is_directory=True)


def _instance(
    data: Path, folder: Path, name: str, version: str, loader: str, memory_mb: int
) -> str:
    lifecycle.check()
    # An immutable wrapper avoids editing Prism's settings while its process may
    # still hold the instance in memory. Every wrapper shares the real gameDir.
    identity = json.dumps(
        [os.path.normcase(str(folder)), name, version, loader, memory_mb]
    )
    instance_id = "BlockHarbor-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    instances = data / "instances"
    instances.mkdir(parents=True, exist_ok=True)
    destination = instances / instance_id
    if destination.exists():
        if (
            not all(
                (destination / n).is_file() for n in ("instance.cfg", "mmc-pack.json")
            )
            or (destination / "minecraft").resolve() != folder
        ):
            raise ValueError(
                f"Повреждён профиль запуска Prism: {destination}. Папка мира находится в {folder}."
            )
        return instance_id
    components = _components(folder, version, loader)
    # Stage outside the instances directory: Prism's file watcher must see a
    # complete instance when a second CLI invocation forwards a launch by IPC.
    with tempfile.TemporaryDirectory(dir=data) as tmp:
        stage = Path(tmp) / instance_id
        stage.mkdir()
        _link_game(folder, stage / "minecraft")
        (stage / "mmc-pack.json").write_text(
            json.dumps({"formatVersion": 1, "components": components}, indent=2),
            "utf-8",
        )
        # Display name cannot inject new INI options; input_profile rejects control characters.
        (stage / "instance.cfg").write_text(
            f"[General]\nInstanceType=OneSix\nname=BlockHarbor · {name}\nOverrideMemory=true\nMinMemAlloc=512\nMaxMemAlloc={memory_mb}\n",
            "utf-8",
        )
        with lifecycle.guard():
            stage.rename(destination)
    return instance_id


def launch(
    data_root: Path,
    folder: Path,
    name: str,
    version: str,
    loader: str,
    memory_mb: int,
    status: Status = lambda _: None,
    *,
    on_game=None,
    on_waiting=None,
    on_done=None,
) -> subprocess.Popen:
    """Start the online instance. The CLI process is not a Minecraft lifetime handle."""
    lifecycle.check()
    if sys.platform != "win32":
        raise RuntimeError(
            "Готовая интеграция Microsoft через Prism предназначена для Windows x64."
        )
    name, version, loader = input_profile(name, version, loader)
    memory_mb = game._memory(memory_mb)
    folder = Path(folder).resolve(strict=True)
    root = Path(data_root).resolve() / "prism"
    runtime = _engine(root, status)
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    settings = data / "prismlauncher.cfg"
    if not settings.exists():
        with settings.open("x", encoding="utf-8") as config:
            config.write(
                "[General]\nAutomaticJavaDownload=true\nAutomaticJavaSwitch=true\nLanguage=ru\nApplicationTheme=dark\nIconTheme=pe_colored\nCloseAfterLaunch=false\nQuitAfterGameStop=true\n"
            )
    status(f"Готовим профиль Prism · {version} · {loader}")
    identifier = _instance(data, folder, name, version, loader, memory_mb)
    status("Запускаем сборку через Prism. При первом запуске войди в Microsoft.")
    try:
        with lifecycle.guard():
            process = subprocess.Popen(
                [str(runtime), "--dir", str(data), "--launch", identifier],
                cwd=runtime.parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except OSError as exc:
        raise RuntimeError(
            "Windows не удалось запустить компонент Prism. Открой диагностический журнал BlockHarbor и проверь разрешения каталога приложения."
        ) from exc
    try:
        code = process.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        code = None
    if code:
        raise RuntimeError(
            f"Prism завершился с кодом {code}. Подробности в {data / 'logs'}."
        )
    if on_game is not None:
        from . import prism_watch

        prism_watch.observe(data / "instances" / identifier, runtime, on_game, on_waiting=on_waiting, on_done=on_done)
    return process
