"""Emit the compatibility manifest required by the in-app updater."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

MAX_INSTALLER = 512 * 1024**2
VERSION = re.compile(r"(?:0|[1-9]\d{0,4})\.(?:0|[1-9]\d{0,4})\.(?:0|[1-9]\d{0,4})(?:-(?:alpha|beta|rc)\.(?:0|[1-9]\d{0,4}))?\Z")


def installer_metadata(installer, version):
    path = Path(installer)
    expected = "BlockHarbor-Setup-" + version + ".exe"
    if path.name != expected or path.is_symlink():
        raise ValueError("installer must be the regular release file " + expected)
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        if not 0 < before.st_size <= MAX_INSTALLER:
            raise ValueError("installer size must be between 1 byte and 512 MiB")
        while block := source.read(1024 * 1024):
            count += len(block)
            if count > MAX_INSTALLER:
                raise ValueError("installer changed while computing its checksum")
            digest.update(block)
        after = os.fstat(source.fileno())
    if count != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("installer changed while computing its checksum")
    return {"name": expected, "size": count, "sha256": digest.hexdigest()}


def manifest(version, *, data_schema=1, minimum=None, maximum=None, rollback_safe=True, installer=None):
    if not VERSION.fullmatch(version):
        raise ValueError("invalid release version")
    minimum = data_schema if minimum is None else minimum
    maximum = data_schema if maximum is None else maximum
    if (any(type(value) is not int for value in (data_schema, minimum, maximum))
        or not 1 <= data_schema <= 10000 or not 1 <= minimum <= maximum <= 10000
        or type(rollback_safe) is not bool):
        raise ValueError("invalid data schema")
    result = {
        "format": 1, "version": version, "dataSchema": data_schema,
        "minDataSchema": minimum, "maxDataSchema": maximum, "rollbackSafe": rollback_safe,
    }
    # Older updaters ignore additional fields. New clients can discover the
    # exact verified installer through GitHub's public latest/download link,
    # without a REST API quota or an extra executable-size probe.
    if installer is not None:
        result["installer"] = installer_metadata(installer, version)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-schema", type=int, default=1)
    parser.add_argument("--min-data-schema", type=int)
    parser.add_argument("--max-data-schema", type=int)
    parser.add_argument("--installer", type=Path, help="Include the built installer's exact name, size and SHA-256 for public release discovery")
    parser.add_argument("--no-rollback", action="store_true", help="Disable rollback for incompatible/irreversible data migrations")
    args = parser.parse_args()
    if args.installer is not None and args.output.resolve() == args.installer.resolve():
        parser.error("manifest output must not overwrite the installer")
    try:
        result = manifest(args.version, data_schema=args.data_schema,
                          minimum=args.min_data_schema, maximum=args.max_data_schema,
                          rollback_safe=not args.no_rollback, installer=args.installer)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
