"""Share a Java Edition cosmetic mapping between friends as bounded JSON data."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path

from . import aliases, skins

_CARD_FORMAT = "blockharbor.player"
_FIELDS = {"format", "version", "profileName", "nickname", "skinPNG"}
_MAX_CARD_BYTES = 64 * 1024  # A re-encoded 64×64 RGBA PNG fits with room to spare.


def build_card(
    real_name: str, nickname: str, skin_path_or_None: str | Path | None
) -> bytes:
    real_name = skins.validate_nickname(real_name)
    nickname = skins.validate_nickname(nickname)
    image = None
    if skin_path_or_None is not None:
        image = base64.b64encode(skins.normalized_skin(skin_path_or_None)).decode(
            "ascii"
        )
    fields = {
        "format": _CARD_FORMAT,
        "version": 1,
        "profileName": real_name,
        "nickname": nickname,
        "skinPNG": image,
    }
    return (json.dumps(fields, ensure_ascii=True, indent=2) + "\n").encode("utf-8")


def read_card(path: str | Path) -> tuple[str, str, bytes | None]:
    try:
        with Path(path).open("rb") as file:
            content = file.read(_MAX_CARD_BYTES + 1)
    except OSError as exc:
        raise ValueError("Не удалось прочитать карточку игрока.") from exc
    if len(content) > _MAX_CARD_BYTES:
        raise ValueError("Карточка игрока превышает 64 КБ.")
    try:
        fields = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(
            "Выберите карточку игрока .bhplayer, экспортированную из BlockHarbor."
        ) from exc
    if (
        not isinstance(fields, dict)
        or set(fields) != _FIELDS
        or fields["format"] != _CARD_FORMAT
    ):
        raise ValueError("Некорректный формат карточки BlockHarbor.")
    if type(fields["version"]) is not int or fields["version"] != 1:
        raise ValueError("Эта версия карточки пока не поддерживается.")
    if not isinstance(fields["profileName"], str) or not isinstance(
        fields["nickname"], str
    ):
        raise ValueError("Повреждено имя игрока в карточке.")
    name = skins.validate_nickname(fields["profileName"])
    nickname = skins.validate_nickname(fields["nickname"])
    encoded = fields["skinPNG"]
    if encoded is None:
        return name, nickname, None
    if not isinstance(encoded, str):
        raise ValueError("Повреждено изображение скина в карточке.")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Повреждено изображение скина в карточке.") from exc
    return name, nickname, skins.normalized_skin(image)


def apply_card(
    path: str | Path, instance_dir: str | Path, *, own_name: str | None = None
) -> str:
    """Install a friend's local mapping into the caller-selected Fabric 1.20.1 instance."""
    name, nickname, image = read_card(path)
    if own_name and name.casefold() == own_name.strip().casefold():
        raise ValueError(
            "В карточке указан ваш собственный Java-ник. Для него используйте раздел «Профиль»."
        )
    if image is not None:
        aliases.ensure_skin_support(instance_dir)
    aliases.apply_alias(instance_dir, name, nickname)
    if image is None:
        return f"В этой сборке игрок {name} будет отображаться как {nickname}."
    skins.install_local_skin(image, instance_dir, name)
    return f"Для игрока {name} в этой сборке установлены ник {nickname} и скин. Перезапустите игру."
