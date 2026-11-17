"""Minecraft Java profile changes and per-instance CustomSkinLoader textures."""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import tempfile
import uuid
from io import BytesIO
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image

Model = Literal["classic", "slim"]
_PROFILE = "https://api.minecraftservices.com/minecraft/profile"
_PNG = b"\x89PNG\r\n\x1a\n"
_SIZES = {(64, 32), (64, 64)}
_MAX_INPUT = 1024 * 1024


class SkinError(ValueError):
    """An actionable profile or skin error suitable for display."""


def validate_nickname(name: str) -> str:
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", name):
        raise SkinError(
            "Ник должен содержать от 3 до 16 латинских букв, цифр или знаков подчёркивания."
        )
    return name


def _load_skin(source: str | Path | bytes) -> Image.Image:
    if isinstance(source, bytes):
        raw = source
    else:
        try:
            with Path(source).open("rb") as stream:
                raw = stream.read(_MAX_INPUT + 1)
        except OSError as exc:
            raise SkinError("Не удалось открыть файл скина.") from exc
    if len(raw) > _MAX_INPUT:
        raise SkinError("PNG-файл скина превышает 1 МБ.")
    if len(raw) < 33 or raw[:8] != _PNG or raw[8:16] != b"\x00\x00\x00\rIHDR":
        raise SkinError("Выберите скин в формате PNG.")
    if struct.unpack(">II", raw[16:24]) not in _SIZES:
        raise SkinError(
            "Нужна развёртка скина Java Edition размером 64×64 или 64×32 пикселя."
        )
    try:
        with Image.open(BytesIO(raw), formats=["PNG"]) as png:
            if png.is_animated:
                raise SkinError("Выберите статичное изображение PNG.")
            png.verify()
        with Image.open(BytesIO(raw), formats=["PNG"]) as png:
            pixels = png.convert("RGBA")
            pixels.info.clear()
            return pixels
    except SkinError:
        raise
    except (OSError, SyntaxError, ValueError, struct.error) as exc:
        raise SkinError(
            "Не удалось прочитать пиксели. Проверьте целостность PNG-файла."
        ) from exc


def validate_skin(path: str | Path) -> tuple[int, int]:
    return _load_skin(path).size


def _encode_png(pixels: Image.Image) -> bytes:
    out = BytesIO()
    pixels.save(out, format="PNG")
    return out.getvalue()


def normalized_skin(source: str | Path | bytes) -> bytes:
    """Return a validated, static skin PNG containing only RGBA pixels."""
    return _encode_png(_load_skin(source))


def _check_model(model: Model) -> None:
    if model not in ("classic", "slim"):
        raise SkinError("Выберите модель Classic или Slim.")


def skin_preview(
    path: str | Path, model: Model = "classic", scale: int = 6
) -> Image.Image:
    """Render a front view from the player's actual texture, including clothing layers."""
    _check_model(model)
    if not isinstance(scale, int) or not 1 <= scale <= 20:
        raise SkinError("Допустимый масштаб предпросмотра — от 1 до 20.")
    source = _load_skin(path)
    modern = source.height == 64
    arm = 3 if model == "slim" and modern else 4
    canvas = Image.new("RGBA", (16, 32))

    def part(source_xy, target_xy, width, height, *, mirror=False):
        x, y = source_xy
        piece = source.crop((x, y, x + width, y + height))
        if mirror:
            piece = piece.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        canvas.alpha_composite(piece, target_xy)

    part((8, 8), (4, 0), 8, 8)
    part((20, 20), (4, 8), 8, 12)
    part((44, 20), (4 - arm, 8), arm, 12)
    part((36, 52) if modern else (44, 20), (12, 8), arm, 12, mirror=not modern)
    part((4, 20), (4, 20), 4, 12)
    part((20, 52) if modern else (4, 20), (8, 20), 4, 12, mirror=not modern)
    part((40, 8), (4, 0), 8, 8)
    if modern:
        part((20, 36), (4, 8), 8, 12)
        part((44, 36), (4 - arm, 8), arm, 12)
        part((52, 52), (12, 8), arm, 12)
        part((4, 36), (4, 20), 4, 12)
        part((4, 52), (8, 20), 4, 12)
    return canvas.resize((16 * scale, 32 * scale), Image.Resampling.NEAREST)


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _profile_request(
    route: str, token: str, method: str, data: bytes, content_type: str | None = None
) -> None:
    if not token or any(c.isspace() for c in token):
        raise SkinError("Войдите в профиль Minecraft Java и повторите.")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    request = Request(_PROFILE + route, headers=headers, method=method, data=data)
    try:
        with build_opener(_NoRedirects()).open(request, timeout=30) as response:
            if response.status not in (200, 204):
                raise SkinError(
                    f"Minecraft вернул неожиданный статус HTTP {response.status}."
                )
            if response.status == 200:
                raw = response.read(256 * 1024 + 1)
                if len(raw) > 256 * 1024:
                    raise SkinError("Сервис Minecraft вернул слишком большой ответ.")
                if raw:
                    profile = json.loads(raw)
                    if not isinstance(profile, dict) or not profile.get("id"):
                        raise SkinError(
                            "Сервис Minecraft не подтвердил изменение профиля."
                        )
    except HTTPError as exc:
        if exc.code == 401:
            message = "Сессия Minecraft истекла. Войдите снова."
        elif route.startswith("/name/") and exc.code in (400, 403, 409):
            message = "Minecraft отклонил ник. Проверьте доступность имени и срок в 30 дней после предыдущей смены."
        elif exc.code in (400, 415, 422):
            message = (
                "Сервис Minecraft не принял этот скин. Проверьте PNG и модель рук."
            )
        elif exc.code in (403, 404):
            message = "Сервис Minecraft отклонил запрос. Проверьте доступ аккаунта к Java Edition."
        elif exc.code == 429:
            message = "Слишком много запросов. Попробуйте позже."
        else:
            message = f"Сервис Minecraft вернул HTTP {exc.code}."
        exc.close()
        raise SkinError(message) from None
    except (URLError, TimeoutError, OSError) as exc:
        raise SkinError(
            "Не удалось связаться с сервисом профилей Minecraft. Проверьте подключение."
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SkinError(
            "Сервис Minecraft вернул нечитаемый ответ. Проверьте профиль перед повтором."
        ) from exc


def upload_skin(path: str | Path, model: Model, access_token: str) -> None:
    """Apply a skin to the signed-in Java profile. Pass a Minecraft services token."""
    _check_model(model)
    pixels = _load_skin(path)
    if pixels.height == 32 and model == "slim":
        raise SkinError(
            "Для старой развёртки 64×32 выберите Classic. Для Slim нужна развёртка 64×64."
        )
    boundary = uuid.uuid4().hex
    parts = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="variant"\r\n\r\n{model}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="skin.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode("ascii")
    body = parts + _encode_png(pixels) + f"\r\n--{boundary}--\r\n".encode("ascii")
    _profile_request(
        "/skins",
        access_token,
        "POST",
        body,
        f"multipart/form-data; boundary={boundary}",
    )


def change_name(name: str, access_token: str) -> None:
    """Change the actual Java profile name. Mojang applies its rename cooldown."""
    _profile_request("/name/" + validate_nickname(name), access_token, "PUT", b"")


def _write_inside(instance: Path, target: Path, data: bytes) -> None:
    if not target.parent.resolve().is_relative_to(instance):
        raise SkinError("Папка скина находится за пределами выбранной сборки.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".skin-", delete=False
        ) as stream:
            temp = Path(stream.name)
            stream.write(data)
        os.replace(temp, target)
    finally:
        if temp:
            temp.unlink(missing_ok=True)


def appearance_skin(source: str | Path | bytes, model: Model) -> bytes:
    _check_model(model)
    pixels = _load_skin(source)
    if model == "slim" and pixels.height == 32:
        raise SkinError("Для скина 64×32 выбери Classic. Модели Slim нужен скин 64×64.")
    return _encode_png(pixels)


def _same_texture(root: Path, file: Path, pixels: bytes) -> bool:
    if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
        raise SkinError("Папка скина находится за пределами выбранной сборки.")
    return (
        file.is_file()
        and file.stat().st_size == len(pixels)
        and file.read_bytes() == pixels
    )


def save_texture(root: Path, pixels: bytes) -> str:
    digest = hashlib.sha256(pixels).hexdigest()
    file = root / "skins" / (digest + ".png")
    if not _same_texture(root, file, pixels):
        _write_inside(root.resolve(), file, pixels)
    return digest


def install_local_skin(
    path: str | Path | bytes,
    instance_dir: str | Path,
    nickname: str,
    model: Model | None = None,
) -> Path:
    """Configure the loader's first matching local source without changing other players."""
    name = validate_nickname(nickname)
    pixels = normalized_skin(path) if model is None else appearance_skin(path, model)
    instance = Path(instance_dir).resolve()
    root = instance / "CustomSkinLoader"
    config = root / "CustomSkinLoader.json"
    # A separate path per player lets Classic and Slim coexist. The placeholder
    # prevents one player's texture from matching every other Minecraft profile.
    pattern = (
        "LauncherSkins/" + (name.lower() + "/" if model else "") + "skin-{USERNAME}.png"
    )
    texture = root / pattern.replace("{USERNAME}", name)
    source = {
        "name": "BlockHarbor " + name.lower() if model else "Launcher local skins",
        "type": "Legacy",
        "skin": pattern,
        "model": {"classic": "default", "slim": "slim", None: "auto"}[model],
        "checkPNG": True,
    }
    try:
        if config.exists():
            if (
                config.is_symlink()
                or not config.resolve().is_relative_to(instance)
                or config.stat().st_size > 2 * 1024 * 1024
            ):
                raise SkinError(
                    "Не удалось прочитать настройки CustomSkinLoader в этой сборке."
                )
            try:
                data = json.loads(config.read_text("utf-8-sig"))
                if not isinstance(data, dict) or not isinstance(
                    data.get("loadlist", []), list
                ):
                    raise ValueError()
            except (UnicodeError, ValueError) as exc:
                raise SkinError(
                    "Файл настроек CustomSkinLoader повреждён. Проверь CustomSkinLoader.json в папке сборки."
                ) from exc
        else:
            data = {
                "loadlist": [{"name": "Mojang", "type": "MojangAPI"}],
                "forceLoadAllTextures": False,
            }
        loadlist = data.get("loadlist", [])
        merged = [source] + [
            site
            for site in loadlist
            if not (isinstance(site, dict) and site.get("name") == source["name"])
        ]
        if not _same_texture(instance, texture, pixels):
            _write_inside(instance, texture, pixels)
        if merged != loadlist or not config.exists():
            data["loadlist"] = merged
            _write_inside(
                instance,
                config,
                json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"),
            )
    except OSError as exc:
        raise SkinError(
            "Не удалось записать скин в папку сборки. Проверь права записи."
        ) from exc
    return texture
