"""Verified client-side skin loaders and Fabric 1.20.1 cosmetic aliases."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote
from urllib.request import Request, urlopen

from . import lifecycle
from .skins import SkinError, _write_inside, validate_nickname


@dataclass(frozen=True)
class ModRelease:
    title: str
    mod_ids: frozenset[str]
    url: str
    size: int
    sha512: str

    @property
    def filename(self) -> str:
        return unquote(self.url.rsplit("/", 1)[1])


FABRIC_API = ModRelease(
    "Fabric API",
    frozenset({"fabric-api"}),
    "https://cdn.modrinth.com/data/P7dR8mSH/versions/rvI2dfzR/fabric-api-0.92.12%2B1.20.1.jar",
    2137232,
    "de9da2344043b3e9ef9fb7dbfff5f2577b06000c4f73f2a37959a80123141169084cece19133bd299b45feaaa4648bdaca2ea085684888cc8c55cc9134a477ca",
)
NAME_PROTECTED = ModRelease(
    "NameProtected",
    frozenset({"nameprotect"}),
    "https://cdn.modrinth.com/data/iAZ0XYJw/versions/7YLKwp96/nameprotect-mc1.20.1-1.0.0.jar",
    24414,
    "83eaf4a5e57f3433f3d911a4a7ee9ed1109b3fb2018bd0391126eed9cf18a7b439f04ca88f310147dd8b9d4f6ec449aa458148cf55dc454f4518347f34679081",
)
CUSTOM_SKIN_LOADER = ModRelease(
    "CustomSkinLoader",
    frozenset({"customskinloader", "customskinloader-bootstrap"}),
    "https://cdn.modrinth.com/data/idMHQ4n2/versions/Fsrt5ueW/CustomSkinLoader_Fabric-14.27.jar",
    190419,
    "b29d25caf4418428ed07ec2e8645f58632518a907a66d2526c2edec894f1b0bc62c784abcca4cfccfdacb00ebef8cb73c0dec3679683923d09c999fb6fb22453",
)
_SKIN_RELEASES = json.loads(
    Path(__file__).with_name("skin_releases.json").read_text("utf-8")
)


def skin_release(version: str, loader: str) -> ModRelease | None:
    # The loader-specific 14.27 artifacts include their implementation. Prefer them
    # where available and use the universal release for newer Minecraft versions.
    for key in (
        "14.27-Fabric",
        "14.27-ForgeV2",
        "14.27-ForgeV1",
        "14.27-ForgeV3",
        "15.1-Universal",
    ):
        entry = _SKIN_RELEASES[key]
        if version not in entry["versions"] or loader not in entry["loaders"]:
            continue
        if (
            key == "15.1-Universal"
            and loader in ("fabric", "quilt")
            and tuple(map(int, version.split("."))) < (1, 14)
        ):
            continue
        return ModRelease(
            "CustomSkinLoader",
            CUSTOM_SKIN_LOADER.mod_ids,
            entry["url"],
            entry["size"],
            entry["sha512"],
        )
    return None


_INSTALL_LOCK = threading.RLock()


def _download(release: ModRelease) -> bytes:
    lifecycle.check()
    request = Request(
        release.url,
        headers={"User-Agent": "BlockHarbor/2.6.0 (Minecraft Java desktop launcher)"},
    )
    try:
        with lifecycle.response(urlopen(request, timeout=30)) as response:
            raw = response.read(release.size + 1)
    except OSError as exc:
        raise SkinError(
            f"Не удалось скачать {release.title} из каталога. Проверьте соединение."
        ) from exc
    if len(raw) != release.size or hashlib.sha512(raw).hexdigest() != release.sha512:
        raise SkinError(
            f"Загрузка {release.title} не прошла проверку SHA-512. Повторите позже."
        )
    return raw


def _fabric_metadata(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as jar:
            if (
                "fabric.mod.json" not in jar.NameToInfo
                and "customskinloader/CustomSkinLoader.class" in jar.NameToInfo
            ):
                return {"id": "customskinloader"}
            info = jar.getinfo("fabric.mod.json")
            if info.file_size > 1024 * 1024:
                return {}
            spec = json.loads(jar.read(info))
            return spec if isinstance(spec, dict) else {}
    except (KeyError, ValueError, zipfile.BadZipFile, OSError):
        return {}


def _existing(path: Path, spec: dict, release: ModRelease) -> bool:
    if (
        path.stat().st_size == release.size
        and hashlib.sha512(path.read_bytes()).hexdigest() == release.sha512
    ):
        return True
    # Other Fabric API releases for the same game version also satisfy NameProtected's "*" dependency.
    return (
        release == FABRIC_API
        and str(spec.get("version", "")).split("+", 1)[-1] == "1.20.1"
    )


def _ensure_mods(instance: Path, releases: tuple[ModRelease, ...]) -> list[Path]:
    mods = instance / "mods"
    if mods.resolve().parent != instance:
        raise SkinError("Папка модов находится за пределами сборки.")
    mods.mkdir(parents=True, exist_ok=True)
    active = [
        (p, _fabric_metadata(p))
        for p in mods.iterdir()
        if p.is_file() and p.suffix.lower() == ".jar"
    ]
    targets = []
    downloads = []
    for release in releases:
        lifecycle.check()
        matches = [(p, meta) for p, meta in active if meta.get("id") in release.mod_ids]
        if len(matches) > 1:
            raise SkinError(
                f"В сборке несколько версий {release.title}. Оставьте одну через раздел дополнений."
            )
        if matches:
            path, meta = matches[0]
            if path.is_symlink() or not _existing(path, meta, release):
                raise SkinError(
                    f"В сборке уже есть другая версия {release.title}: {path.name}. "
                    "Чтобы использовать предложенную версию, сначала удалите старую в разделе дополнений."
                )
            targets.append(path)
            continue
        path = mods / release.filename
        if path.exists() or path.is_symlink():
            raise SkinError(
                f"Файл {path.name} уже занят. Проверьте его в разделе дополнений."
            )
        targets.append(path)
        downloads.append((path, _download(release)))

    # Stage the full set before a short commit. Closing the window can cancel
    # staging, but cannot interrupt publication between its dependent JARs.
    staged = []
    try:
        for target, blob in downloads:
            lifecycle.check()
            with tempfile.NamedTemporaryFile(
                dir=mods, prefix=".download-", delete=False
            ) as out:
                temp = Path(out.name)
                staged.append((temp, target, blob))
                out.write(blob)
        with lifecycle.guard():
            written = []
            try:
                for temp, target, blob in staged:
                    if os.name == "nt":
                        os.rename(temp, target)  # Windows fails if the target exists.
                    else:
                        os.link(temp, target)
                    written.append((target, blob))
            except OSError:
                for target, blob in reversed(written):
                    if not target.is_symlink() and target.is_file() and target.read_bytes() == blob:
                        target.unlink()
                raise
    finally:
        for temp, _, _ in staged:
            temp.unlink(missing_ok=True)
    return targets


def ensure_skin_support(
    instance_dir: str | Path, version: str = "1.20.1", loader: str = "fabric"
) -> Path:
    """Install the matching client-side skin loader, or explain the unsupported pair."""
    release = skin_release(version, loader)
    if release is None:
        raise SkinError(
            f"Для локального скина в Minecraft {version} · {loader} нет совместимого загрузчика скинов."
        )
    with _INSTALL_LOCK:
        return _ensure_mods(Path(instance_dir).resolve(), (release,))[0]


def apply_alias(instance_dir: str | Path, real_name: str, nickname: str) -> Path:
    """Install NameProtected for Fabric 1.20.1 and preserve unrelated local rules.

    The mapping is purely visual on this client. To see the same alias together,
    friends can apply the same real name → nickname pair on their own clients.
    """
    username = validate_nickname(real_name).lower()
    alias = validate_nickname(nickname)
    instance = Path(instance_dir).resolve()
    config = instance / "config" / "nameprotect.json"
    with _INSTALL_LOCK:
        if config.is_symlink() or not config.resolve().is_relative_to(instance):
            raise SkinError("Файл псевдонимов находится за пределами сборки.")
        rules = {}
        if config.exists():
            try:
                if config.stat().st_size > 1024 * 1024:
                    raise ValueError()
                rules = json.loads(config.read_bytes())
                if not isinstance(rules, dict) or not all(
                    isinstance(v, str) for v in rules.values()
                ):
                    raise ValueError()
            except (OSError, ValueError) as exc:
                raise SkinError(
                    "Существующий nameprotect.json не удалось прочитать. Исправьте его перед изменением."
                ) from exc
        _ensure_mods(instance, (FABRIC_API, NAME_PROTECTED))
        for key in list(rules):
            if key.lower() == username:
                del rules[key]
        if alias.lower() != username or real_name.strip() != alias:
            rules[username] = alias
        encoded = (json.dumps(rules, indent=2, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        _write_inside(instance, config, encoded)
    return config
