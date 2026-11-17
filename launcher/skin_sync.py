"""Prepare Skinss+ on both sides of a BlockHarbor multiplayer world."""

import hashlib
import json
import re
import shutil
import tempfile
import threading
from pathlib import Path

from PIL import Image

from . import modfiles, skins

_ROOT = Path(__file__).resolve().parent
_BUNDLED = _ROOT.parent / "bundled" / "skin-sync"
_CATALOG = json.loads((_ROOT / "skin_sync_catalog.json").read_text(encoding="utf-8"))
_LOCK = threading.RLock()


def support(version, loader):
    return f"{loader}/{version}" in _CATALOG


def _matches(path, release):
    if path.is_symlink() or not path.is_file():
        return False
    stamp = modfiles.file_stamp(path)
    return (
        stamp[0] == release["size"]
        and modfiles.file_hash(path, stamp, "sha512") == release["sha512"]
    )


def managed_file(instance, version, loader, filename):
    release = _CATALOG.get(f"{loader}/{version}")
    if not release or Path(filename).name != filename or "\\" in filename:
        return False
    mods = Path(instance).resolve() / "mods"
    return mods == mods.resolve() and _matches(mods / filename, release)


def managed_files(instance, version, loader, entries):
    release = _CATALOG.get(f"{loader}/{version}")
    if not release:
        return set()
    return {
        entry["name"]
        for entry in entries
        if entry["size"] == release["size"]
        and managed_file(instance, version, loader, entry["name"])
    }


def _install(instance, release):
    mods = instance / "mods"
    if mods.resolve() != mods:
        raise ValueError("Папка модов сборки перенаправлена.")
    mods.mkdir(parents=True, exist_ok=True)
    target = mods / release["file"]
    existing = None
    for path in mods.iterdir():
        if path.suffix.lower() != ".jar":
            continue
        same_name = path.name.casefold() == target.name.casefold()
        if not same_name and (
            not path.is_file()
            or "skinss" not in modfiles.mod_info(path, modfiles.file_stamp(path))[0]
        ):
            continue
        if not _matches(path, release) or existing is not None:
            raise ValueError(
                f"В папке mods есть другая копия Skinss+ ({path.name}). Перемести её из сборки и повтори."
            )
        existing = path
    if existing is not None:
        return existing
    source = _BUNDLED / release["file"]
    if not _matches(source, release):
        raise ValueError("Встроенный Skinss+ повреждён. Распакуй BlockHarbor заново.")
    with tempfile.TemporaryDirectory(dir=mods, prefix=".blockharbor-skins-") as tmp:
        stage = Path(tmp) / "skinss.part"
        shutil.copyfile(source, stage)
        if not _matches(stage, release):
            raise ValueError("Не удалось скопировать Skinss+ в сборку.")
        try:
            modfiles.publish_new(stage, target)
        except FileExistsError:
            if not _matches(target, release):
                raise
    return target


def _texture(source, model):
    skins._check_model(model)
    image = skins._load_skin(source)
    if image.height == 64:
        return skins._encode_png(image)
    if model == "slim":
        raise skins.SkinError(
            "Для скина 64×32 выбери Classic. Модели Slim нужен скин 64×64."
        )
    # The mod binds a raw texture. Minecraft's modern player model uses
    # independent left limbs, while the old UV layout mirrored the right limbs.
    modern = Image.new("RGBA", (64, 64))
    modern.paste(image)
    for x, y, dx, dy, height in (
        (4, 16, 20, 48, 4),
        (8, 16, 24, 48, 4),
        (0, 20, 24, 52, 12),
        (4, 20, 20, 52, 12),
        (8, 20, 16, 52, 12),
        (12, 20, 28, 52, 12),
        (44, 16, 36, 48, 4),
        (48, 16, 40, 48, 4),
        (40, 20, 40, 52, 12),
        (44, 20, 36, 52, 12),
        (48, 20, 32, 52, 12),
        (52, 20, 44, 52, 12),
    ):
        piece = image.crop((x, y, x + 4, y + height))
        modern.paste(piece.transpose(Image.Transpose.FLIP_LEFT_RIGHT), (dx, dy))
    hat_area = modern.crop((32, 0, 64, 32))
    if hat_area.getchannel("A").getextrema()[0] >= 128:
        # Fully opaque unused legacy overlay pixels historically mean no hat.
        hat_area.putalpha(0)
        modern.paste(hat_area, (32, 0))
    for rect in ((0, 0, 32, 16), (0, 16, 64, 32), (16, 48, 48, 64)):
        layer = modern.crop(rect)
        layer.putalpha(255)
        modern.paste(layer, rect[:2])
    return skins._encode_png(modern)


def prepare(instance_dir, version, loader, image=None, model="classic"):
    """Install the receiver even for players without a custom skin."""
    release = _CATALOG.get(f"{loader}/{version}")
    if release is None:
        return None
    instance = Path(instance_dir).resolve()
    with _LOCK:
        jar = _install(instance, release)
        if image is None:
            return jar
        pixels = _texture(image, model)
        root = instance / "config" / "skinss"
        settings = root / "settings.json"
        skin_dir = root / "skin"
        if settings.resolve() != settings or skin_dir.resolve() != skin_dir:
            raise ValueError("Папка скинов или файл настроек Skinss+ перенаправлены.")
        data = {}
        if settings.exists():
            if settings.stat().st_size > 1024 * 1024:
                raise ValueError("Файл настроек Skinss+ слишком велик.")
            try:
                data = json.loads(settings.read_text(encoding="utf-8-sig"))
            except (ValueError, UnicodeError) as error:
                raise ValueError(
                    "Не удалось прочитать настройки Skinss+. Проверь config/skinss/settings.json."
                ) from error
            if not isinstance(data, dict):
                raise ValueError("Проверь формат config/skinss/settings.json.")
        # Commit the selection after the immutable PNG is fully written.
        name = "blockharbor-" + hashlib.sha256(pixels).hexdigest() + ".png"
        target = skin_dir / name
        prior = data.get("selectedSkin", "")
        if not skins._same_texture(instance, target, pixels):
            skins._write_inside(instance, target, pixels)
        selected = {"selectedSkin": name}
        if (loader, version) == ("fabric", "1.21.8"):
            selected["slim"] = model == "slim"
        else:
            selected["skinModel"] = model
        if not all(data.get(k) == v for k, v in selected.items()):
            data.update(selected)
            skins._write_inside(
                instance,
                settings,
                (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(),
            )
        if (
            isinstance(prior, str)
            and prior != name
            and re.fullmatch(r"blockharbor-[a-f0-9]{64}\.png", prior)
        ):
            (skin_dir / prior).unlink(missing_ok=True)
        return jar
