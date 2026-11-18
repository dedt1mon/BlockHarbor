"""Emit the compatibility manifest required by the in-app updater."""

import argparse
import json
import re
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-schema", type=int, default=1)
    parser.add_argument("--min-data-schema", type=int)
    parser.add_argument("--max-data-schema", type=int)
    parser.add_argument("--no-rollback", action="store_true", help="Disable rollback for incompatible/irreversible data migrations")
    args = parser.parse_args()
    minimum = args.min_data_schema or args.data_schema
    maximum = args.max_data_schema or args.data_schema
    if not re.fullmatch(r"\d{1,5}\.\d{1,5}\.\d{1,5}(?:-(?:alpha|beta|rc)\.\d{1,5})?", args.version):
        parser.error("invalid release version")
    if not 1 <= args.data_schema <= 10000 or not 1 <= minimum <= maximum <= 10000:
        parser.error("invalid data schema")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "format": 1, "version": args.version, "dataSchema": args.data_schema,
        "minDataSchema": minimum, "maxDataSchema": maximum,
        "rollbackSafe": not args.no_rollback,
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
