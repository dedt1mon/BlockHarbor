"""Preview and reversibly reduce a modern Java instance's saved chunk distances."""

import json
import os
import re
import tempfile
from pathlib import Path

CAPS = {"renderDistance": 8, "simulationDistance": 5}
_OPTION = re.compile(
    rb"(?m)^(?:\xef\xbb\xbf)?(renderDistance|simulationDistance):([0-9]{1,5})[ \t]*(?=\r?$)"
)
_BACKUP = ".blockharbor-distances.json"


def supported(version):
    if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+){1,2}", version):
        return False
    return tuple(map(int, version.split("."))) >= (1, 18)


def _read(folder, name, limit):
    path = folder / name
    if path.resolve() != folder / name:
        raise ValueError("Файл настроек перенаправлен за пределы сборки.")
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except FileNotFoundError:
        return None
    if len(data) > limit:
        raise ValueError("Файл настроек слишком велик. Проверь его в папке сборки.")
    return data


def _replace(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        name = Path(tmp.name)
        try:
            tmp.write(data)
        except BaseException:
            tmp.close()
            name.unlink(missing_ok=True)
            raise
    try:
        os.replace(name, path)
    finally:
        name.unlink(missing_ok=True)


def _plan(folder, version):
    if not supported(version):
        raise ValueError(
            "Изменение дальности доступно для Minecraft Java 1.18 и новее."
        )
    data = _read(folder, "options.txt", 8 * 1024 * 1024)
    original = _read(folder, _BACKUP, 4096)
    backup = json.loads(original) if original is not None else {}
    if (
        not isinstance(backup, dict)
        or (original is not None and not backup)
        or backup.keys() - CAPS.keys()
        or any(
            type(v) is not int or not CAPS[k] < v < 100000 for k, v in backup.items()
        )
    ):
        raise ValueError(
            "Не удалось прочитать прежние значения дальности в этой сборке."
        )
    values = {}
    for match in _OPTION.finditer(data or b""):
        values[match[1].decode()] = int(match[2])
    if not data or not values:
        return {"mode": "unavailable", "current": {}, "changes": {}}, data
    changes = {
        k: backup[k] if backup else limit
        for k, limit in CAPS.items()
        if k in values
        and ((k in backup and values[k] != backup[k]) if backup else values[k] > limit)
    }
    return {
        "mode": "restore" if backup else "reduce",
        "current": values,
        "changes": changes,
    }, data


def preview(folder, version):
    return _plan(folder.resolve(), version)[0]


def apply(folder, version, expected):
    folder = folder.resolve()
    plan, data = _plan(folder, version)
    if plan != expected:
        raise ValueError("Параметры изменились. Открой «Дальность в игре» ещё раз.")
    mode, changes = plan["mode"], plan["changes"]
    if mode == "unavailable" or not changes and mode == "reduce":
        return
    backup_path = folder / _BACKUP
    if changes:
        # Only edit the effective last occurrence of each selected key.
        matches = {m[1].decode(): m for m in _OPTION.finditer(data)}
        updated = data
        for key in sorted(changes, key=lambda k: matches[k].start(2), reverse=True):
            a, b = matches[key].span(2)
            updated = updated[:a] + str(changes[key]).encode("ascii") + updated[b:]
        if mode == "reduce":
            before = {key: plan["current"][key] for key in changes}
            backup_data = json.dumps(before).encode("ascii")
            _replace(backup_path, backup_data)
        # If Minecraft or an editor saved the options while the dialog was open, keep its file.
        try:
            if _read(folder, "options.txt", 8 * 1024 * 1024) != data:
                raise ValueError(
                    "Параметры игры изменились. Проверь значения перед повтором."
                )
            _replace(folder / "options.txt", updated)
        except BaseException:
            if mode == "reduce" and _read(folder, _BACKUP, 4096) == backup_data:
                backup_path.unlink(missing_ok=True)
            raise
    if mode == "restore":
        backup_path.unlink(missing_ok=True)
