"""Regenerate the native ICO from our polygon-only SVG. Requires Pillow."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw

UI = Path(__file__).resolve().parents[1] / "ui"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def polygon(data):
    """Read the line commands used by icon.svg, rejecting unsupported artwork."""
    tokens = re.findall(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+)", data)
    points = []
    x = y = 0.0
    command = None
    cursor = 0
    while cursor < len(tokens):
        if tokens[cursor].isalpha():
            command = tokens[cursor]
            cursor += 1
        if command in ("z", "Z"):
            break
        if command not in ("M", "m", "L", "l", "H", "h", "V", "v"):
            raise ValueError("The cube icon must use polygon commands only")
        relative = command.islower()
        first = float(tokens[cursor])
        cursor += 1
        if command.upper() in ("M", "L"):
            second = float(tokens[cursor])
            cursor += 1
            x, y = (x + first, y + second) if relative else (first, second)
            if command.upper() == "M":
                command = "l" if relative else "L"
        elif command.upper() == "H":
            x = x + first if relative else first
        else:
            y = y + first if relative else first
        points.append((x, y))
    return points


def main():
    root = ET.parse(UI / "icon.svg").getroot()
    _, _, width, height = map(float, root.attrib["viewBox"].split())
    scale = 2048 / width
    image = Image.new("RGBA", (2048, round(height * scale)))
    draw = ImageDraw.Draw(image)
    for path in root:
        if path.tag.rsplit("}", 1)[-1] != "path":
            raise ValueError("Unexpected element in icon.svg")
        points = [
            (round(x * scale), round(y * scale)) for x, y in polygon(path.attrib["d"])
        ]
        draw.polygon(points, fill=path.attrib["fill"])
    image = image.resize((256, 256), Image.Resampling.LANCZOS)
    image.save(UI / "icon.ico", sizes=[(n, n) for n in SIZES], bitmap_format="bmp")


if __name__ == "__main__":
    main()
