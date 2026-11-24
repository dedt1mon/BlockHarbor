"""Build an AMD64 COFF resource object for Go, using the Python standard library.

Resources follow Microsoft's PE/COFF and VS_VERSIONINFO formats. The Go linker
relocates data RVAs and includes the resulting .rsrc section in the native EXE.
"""

from __future__ import annotations

import struct
from pathlib import Path

VERSION = (2, 6, 9, 0)


def _aligned(data):
    return data + b"\0" * (-len(data) % 4)


def _block(key, value=b"", children=(), *, text=True):
    header = b"\0" * 6 + (key + "\0").encode("utf-16le")
    body = _aligned(header) + value
    for child in children:
        body = _aligned(body) + child
    count = len(value) // 2 if text else len(value)
    return struct.pack("<HHH", len(body), count, int(text)) + body[6:]


def version_resource():
    major, minor, patch, revision = VERSION
    high, low = (major << 16) | minor, (patch << 16) | revision
    fixed = struct.pack(
        "<13I", 0xFEEF04BD, 0x10000, high, low, high, low, 0x3F, 0, 0x40004, 1, 0, 0, 0
    )
    values = {
        "FileDescription": "BlockHarbor",
        "FileVersion": ".".join(map(str, VERSION)),
        "InternalName": "BlockHarbor",
        "OriginalFilename": "BlockHarbor.exe",
        "ProductName": "BlockHarbor",
        "ProductVersion": ".".join(map(str, VERSION[:3])),
    }
    strings = [_block(k, (v + "\0").encode("utf-16le")) for k, v in values.items()]
    translations = _block("Translation", struct.pack("<HH", 0x409, 1200), text=False)
    return _block(
        "VS_VERSION_INFO",
        fixed,
        [
            _block("StringFileInfo", children=[_block("040904b0", children=strings)]),
            _block("VarFileInfo", children=[translations]),
        ],
        text=False,
    )


def icon_resources(path):
    data = Path(path).read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    if reserved or kind != 1 or not 1 <= count <= 32:
        raise ValueError("Expected a multi-size Windows ICO file")
    group = bytearray(struct.pack("<HHH", reserved, kind, count))
    resources = {}
    for index in range(count):
        offset = 6 + index * 16
        width, height, colors, zero, planes, bits, size, location = struct.unpack_from(
            "<BBBBHHII", data, offset
        )
        if zero or location + size > len(data) or not size:
            raise ValueError("Invalid ICO image entry")
        group.extend(
            struct.pack(
                "<BBBBHHIH", width, height, colors, 0, planes, bits, size, index + 1
            )
        )
        resources[(3, index + 1, 0)] = data[location : location + size]
    resources[(14, 1, 0)] = bytes(group)
    return resources


def _resource_section(resources):
    tree = {}
    for (kind, name, language), content in sorted(resources.items()):
        tree.setdefault(kind, {}).setdefault(name, {})[language] = content
    section = bytearray()
    leaves = []

    def directory(entries):
        position = len(section)
        section.extend(struct.pack("<IIHHHH", 0, 0, 0, 0, 0, len(entries)))
        section.extend(b"\0" * (8 * len(entries)))
        for index, (key, value) in enumerate(sorted(entries.items())):
            target = position + 16 + index * 8
            if isinstance(value, dict):
                child = directory(value) | 0x80000000
            else:
                child = len(section)
                section.extend(b"\0" * 16)
                leaves.append((child, value))
            struct.pack_into("<II", section, target, key, child)
        return position

    directory(tree)
    relocations = []
    for entry, content in leaves:
        section.extend(b"\0" * (-len(section) % 4))
        struct.pack_into("<IIII", section, entry, len(section), len(content), 0, 0)
        section.extend(content)
        relocations.append(entry)
    return bytes(section), relocations


def write_object(icon, destination):
    resources = icon_resources(icon)
    resources[(16, 1, 0x409)] = version_resource()
    version = ".".join(map(str, VERSION))
    resources[(24, 1, 0)] = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity name="BlockHarbor" version="{version}" processorArchitecture="amd64" type="win32"/>
  <description>BlockHarbor</description>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security>
  </trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application><supportedOS Id="{{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}}"/></application>
  </compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
    <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
  </windowsSettings></application>
</assembly>'''.encode("utf-8")
    section, relocations = _resource_section(resources)
    raw_offset = 20 + 40
    relocation_offset = raw_offset + len(section)
    symbol_offset = relocation_offset + len(relocations) * 10
    # One section, one static section symbol, no optional header or string table entries.
    header = struct.pack("<HHIIIHH", 0x8664, 1, 0, symbol_offset, 1, 0, 0)
    section_header = struct.pack(
        "<8sIIIIIIHHI",
        b".rsrc",
        0,
        0,
        len(section),
        raw_offset,
        relocation_offset,
        0,
        len(relocations),
        0,
        0x40000040,
    )
    records = b"".join(struct.pack("<IIH", offset, 0, 3) for offset in relocations)
    symbol = struct.pack("<8sIhHBB", b".rsrc", 0, 1, 0, 3, 0)
    Path(destination).write_bytes(
        header + section_header + section + records + symbol + struct.pack("<I", 4)
    )
