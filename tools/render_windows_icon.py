"""Regenerate the PNG and native ICO from our polygon-only SVG. Requires Pillow."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

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
    patterns = {}

    def points(path):
        if path.tag.rsplit("}", 1)[-1] != "path":
            raise ValueError("The icon supports polygon paths only")
        return [(round(x * scale), round(y * scale)) for x, y in polygon(path.attrib["d"])]

    for child in root:
        if child.tag.rsplit("}", 1)[-1] == "defs":
            for pattern in child:
                if (pattern.tag.rsplit("}", 1)[-1] != "pattern"
                        or set(pattern.attrib) - {"id", "width", "height", "patternUnits"}
                        or pattern.attrib.get("patternUnits") != "userSpaceOnUse"
                        or float(pattern.attrib["width"]) != width
                        or float(pattern.attrib["height"]) != height):
                    raise ValueError("The icon supports full-canvas user-space patterns only")
                layer = Image.new("RGBA", image.size)
                for face in pattern:
                    ImageDraw.Draw(layer).polygon(points(face), fill=face.attrib["fill"])
                patterns[pattern.attrib["id"]] = layer

    for node in root:
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "defs":
            continue
        if tag != "path":
            raise ValueError("Unexpected element in icon.svg")
        fill = node.attrib["fill"]
        if fill.startswith("url("):
            match = re.fullmatch(r"url\(#([\w-]+)\)", fill)
            if not match or match[1] not in patterns:
                raise ValueError("Unknown icon fill pattern")
            layer = patterns[match[1]].copy()
            mask = Image.new("L", image.size)
            ImageDraw.Draw(mask).polygon(points(node), fill=255)
            layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
            image.alpha_composite(layer)
        else:
            ImageDraw.Draw(image).polygon(points(node), fill=fill)
    image.resize((96, 96), Image.Resampling.LANCZOS).save(UI / "icon.png")
    image.save(UI / "icon.ico", sizes=[(n, n) for n in SIZES], bitmap_format="bmp")


if __name__ == "__main__":
    main()
