"""Offline advisory for mod JARs. Only loader metadata is read, never game code."""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache

from .modfiles import DESCRIPTORS, file_stamp, metadata_zip, read_archive_entry, read_descriptor

_LABELS = {
    "fabric": DESCRIPTORS[:1],
    "quilt": (DESCRIPTORS[1], DESCRIPTORS[0]),
    "forge": (DESCRIPTORS[2], "mcmod.info"),
    "neoforge": (DESCRIPTORS[3], DESCRIPTORS[2]),
}
_PLATFORM = {
    "java",
    "fabricloader",
    "quilt_loader",
    "forge",
    "neoforge",
    "fml",
    "mixinextras",
}
_KINDS = {
    "depends": "required",
    "recommends": "recommended",
    "suggests": "optional",
    "breaks": "hard",
    "conflicts": "soft",
}


def report():
    return {
        "blocked": False,
        "required": [],
        "optional": [],
        "embedded": [],
        "conflicts": [],
        "warnings": [],
    }


@dataclass
class Metadata:
    # id -> (display name, version); excludes mods for a dedicated server only.
    provided: dict = field(default_factory=dict)
    direct: set = field(default_factory=set)
    rules: list = field(default_factory=list)
    nested: list = field(default_factory=list)


def _pairs(data):
    return list(data.items())[:512] if isinstance(data, dict) else []


def _entries(data):
    return data[:512] if isinstance(data, list) else []


def _add(info, key, name, version):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key):
        return
    name = name[:180] if isinstance(name, str) and name else key
    version = version[:128] if isinstance(version, str) and "${" not in version else ""
    info.provided[key.lower()] = (name, version)


def _rule(info, relation, key, bounds):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key):
        return
    bounds = [bounds] if isinstance(bounds, str) else _entries(bounds)
    limits = tuple(v[:160] for v in bounds if isinstance(v, str))
    if limits:
        info.rules.append((relation, key.lower(), limits))


def _read(jar, loader):
    info = Metadata()
    for label in _LABELS.get(loader, ()):
        data = read_descriptor(jar, label)
        if not data:
            continue
        if label == "fabric.mod.json":
            if data.get("environment") == "server":
                break
            _add(info, data.get("id"), data.get("name"), data.get("version"))
            for key in _entries(data.get("provides")):
                _add(info, key, data.get("name"), data.get("version"))
            for prop, relation in _KINDS.items():
                for key, bounds in _pairs(data.get(prop)):
                    _rule(info, relation, key, bounds)
            info.nested = [
                x.get("file") for x in _entries(data.get("jars")) if isinstance(x, dict)
            ]
        elif label == "mcmod.info":
            for mod in data["modList"]:
                _add(info, mod.get("modid"), mod.get("name"), mod.get("version"))
                if mod.get("useDependencyInformation") is True:
                    # In FML the other lists specify load order only.
                    for dependency in _entries(mod.get("requiredMods")):
                        if isinstance(dependency, str):
                            key, _, bounds = dependency.partition("@")
                            _rule(info, "required", key, bounds or "*")
        elif label == "quilt.mod.json":
            q = data.get("quilt_loader", {})
            if not isinstance(q, dict):
                break
            _add(info, q.get("id"), q.get("metadata", {}).get("name"), q.get("version"))
            for item in _entries(q.get("provides")):
                if isinstance(item, dict):
                    _add(
                        info,
                        item.get("id"),
                        item.get("id"),
                        item.get("version", q.get("version")),
                    )
            for prop, relation in (("depends", "required"), ("breaks", "hard")):
                for item in _entries(q.get(prop)):
                    if isinstance(item, str):
                        _rule(info, relation, item, "*")
                    elif isinstance(item, dict) and not (
                        item.get("unless") or item.get("optional") or item.get("group")
                    ):
                        _rule(info, relation, item.get("id"), item.get("versions", "*"))
            info.nested = _entries(q.get("jars"))
        else:
            manifest_version = ""
            mods = _entries(data.get("mods"))
            if any(isinstance(mod, dict) and mod.get("version") == "${file.jarVersion}" for mod in mods):
                manifest = read_archive_entry(jar, "META-INF/MANIFEST.MF", 128 * 1024) or b""
                main = manifest.replace(b"\r\n", b"\n").partition(b"\n\n")[0].replace(b"\n ", b"")
                field = re.search(rb"(?im)^Implementation-Version:[ ]*([^\r\n]+)", main)
                if field:
                    manifest_version = field[1].decode("utf-8", "replace").strip()
            for mod in mods:
                if isinstance(mod, dict):
                    _add(
                        info,
                        mod.get("modId"),
                        mod.get("displayName"),
                        manifest_version if mod.get("version") == "${file.jarVersion}" else mod.get("version"),
                    )
            for owner, deps in _pairs(data.get("dependencies")):
                if owner.lower() not in info.provided:
                    continue
                for dep in _entries(deps):
                    if (
                        not isinstance(dep, dict)
                        or str(dep.get("side", "BOTH")).upper() == "SERVER"
                    ):
                        continue
                    typ = dep.get(
                        "type", "required" if dep.get("mandatory") else "optional"
                    )
                    relation = {
                        "required": "required",
                        "optional": "optional",
                        "incompatible": "hard",
                        "discouraged": "soft",
                    }.get(typ)
                    if relation:
                        _rule(
                            info,
                            relation,
                            dep.get("modId"),
                            dep.get("versionRange") or "*",
                        )
            jarjar = read_descriptor(jar, "META-INF/jarjar/metadata.json")
            info.nested = [
                x.get("path")
                for x in _entries(jarjar.get("jars"))
                if isinstance(x, dict)
            ]
        break
    return info


@lru_cache(maxsize=768)
def _metadata(path, stamp, loader):
    result = Metadata()
    try:
        with metadata_zip(path) as jar:
            result = _read(jar, loader)
            result.direct.update(result.provided)
            left = 16 * 1024 * 1024
            for name in result.nested[:128]:
                if not isinstance(name, str) or name not in jar.NameToInfo:
                    continue
                entry = jar.getinfo(name)
                if entry.file_size > min(left, 4 * 1024 * 1024):
                    continue
                left -= entry.file_size
                try:
                    raw = read_archive_entry(jar, name, min(left + entry.file_size, 4 * 1024 * 1024))
                    if raw is None:
                        continue
                    with metadata_zip(io.BytesIO(raw)) as inner:
                        child = _read(inner, loader)
                        result.provided.update({k: v for k, v in child.provided.items() if k not in result.provided})
                        result.rules.extend(child.rules)
                except (ValueError, RuntimeError, OSError, zipfile.BadZipFile):
                    continue
    except (
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        RuntimeError,
        zipfile.BadZipFile,
    ):
        pass
    return result


def _numeric(value):
    if re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,9}){0,7}", value):
        seq = tuple(map(int, value.split(".")))
        return seq + (0,) * (8 - len(seq))
    return None


def _matches(version, expr):
    # Confirm only constraints whose ordering is unambiguous. Preserve opaque
    # loader/prerelease ranges for the user instead of guessing their meaning.
    expr = expr.strip()
    if expr in ("", "*", "(,)"):
        return True
    if not version:
        return None
    if expr == version or expr == "=" + version or expr == "[" + version + "]":
        return True
    actual = _numeric(version.split("+", 1)[0])
    interval = re.fullmatch(r"(\[|\()([^,]*),([^,]*)(\]|\))", expr)
    if interval:
        left, low, high, right = interval.groups()
        start, end = _numeric(low), _numeric(high)
        if actual is None or (low and start is None) or (high and end is None):
            return None
        return (not low or actual > start or (actual == start and left == "[")) and (
            not high or actual < end or (actual == end and right == "]")
        )
    if expr.startswith("[") and expr.endswith("]"):
        expr = expr[1:-1]
    if " " in expr:
        outcomes = [_matches(version, part) for part in expr.split()]
        return False if False in outcomes else None if None in outcomes else True
    match = re.fullmatch(r"(<=|>=|=|<|>|~|\^)?([0-9]+(?:\.[0-9]+)*(?:\.[xX*])?)", expr)
    if not match or actual is None:
        return None
    op, target = match.groups()
    if target.endswith((".x", ".X", ".*")):
        prefix = target.rsplit(".", 1)[0]
        base = _numeric(prefix)
        if op not in (None, "=") or base is None:
            return None
        n = prefix.count(".") + 1
        return actual[:n] == base[:n]
    expected = _numeric(target)
    if expected is None:
        return None
    if op in ("^", "~"):
        width = 1 if op == "^" else 2
        return actual >= expected and actual[:width] == expected[:width]
    return {
        None: actual == expected,
        "=": actual == expected,
        "<": actual < expected,
        ">": actual > expected,
        "<=": actual <= expected,
        ">=": actual >= expected,
    }[op]


def _any_match(version, bounds):
    states = [_matches(version, expr) for expr in bounds]
    return True if True in states else None if None in states else False


def local_report(profile, existing, additions):
    """Check only relations involving incoming files, including installed reverse rules."""
    result = report()
    loader = profile["loader"]
    providers = {"minecraft": [("", "Minecraft", profile["version"])]}
    mods = {}
    for name, path in [*existing.items(), *additions.items()]:
        meta = _metadata(path, file_stamp(path), loader)
        mods[name] = meta
        for key, (label, version) in meta.provided.items():
            providers.setdefault(key, []).append((name, label, version))
    for name, meta in mods.items():
        incoming = name in additions
        if incoming and not meta.provided:
            result["warnings"].append(
                f"{name}: в архиве нет читаемого описания зависимостей для этого загрузчика."
            )
        if incoming:
            # Multiple mods may bundle the same nested library.
            for key in meta.direct:
                for other, _, _ in providers[key]:
                    if other and other != name and key in mods[other].direct:
                        result["conflicts"].append(
                            f"{name} и {other} объявляют один мод ({key}). Оставь один из файлов."
                        )
        for relation, key, bounds in meta.rules:
            if key in _PLATFORM:
                continue
            matches = providers.get(key, [])
            if not incoming and not any(f in additions for f, _, _ in matches):
                continue
            label = matches[0][1] if matches else key
            expr = " / ".join(bounds)
            if relation in ("hard", "soft"):
                # When several mods embed different versions, the loader chooses a
                # provider. A definite failure requires all candidates to match.
                definite = all(_any_match(v, bounds) is True for _, _, v in matches)
                for file, _, v in matches:
                    if file == name or (not incoming and file not in additions):
                        continue
                    yes = _any_match(v, bounds)
                    if yes is False:
                        continue
                    description = file or label
                    if relation == "hard" and definite:
                        result["conflicts"].append(
                            f"{name} несовместим с {description} ({key} {expr}). Выбери другую версию или убери конфликтующий мод."
                        )
                    else:
                        result["warnings"].append(
                            f"{name}: возможна несовместимость с {description} ({key} {expr})."
                        )
                continue
            found = [f for f, _, v in matches if _any_match(v, bounds) is not False]
            if relation == "required" and found and not any(_any_match(v, bounds) is True for _, _, v in matches):
                result["warnings"].append(
                    f"{name} требует {key} {expr}. Не удалось подтвердить соответствие версии по описанию файла."
                )
            if incoming and key != "minecraft":
                group = "required" if relation == "required" else "optional"
                result[group].append(
                    {
                        "name": label,
                        "source": name,
                        "id": key,
                        "filename": found[0] if found else "",
                        "installed": bool(found),
                        "version": expr,
                        "recommended": relation == "recommended",
                    }
                )
            if (
                relation == "required"
                and matches
                and all(_any_match(v, bounds) is False for _, _, v in matches)
            ):
                other = matches[0][0] or label
                group = "conflicts" if key == "minecraft" else "warnings"
                result[group].append(
                    f"{name} требует {key} {expr}. Выбрана версия {profile['version']}."
                    if key == "minecraft"
                    else f"{name} требует {key} {expr}. У {other} другая версия."
                )
    for group in ("warnings", "conflicts"):
        result[group] = list(dict.fromkeys(result[group]))
    result["blocked"] = bool(result["conflicts"])
    return result
