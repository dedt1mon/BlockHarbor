"""Install the embedded client from the early Forge installer format."""

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path

from . import lifecycle, preparation


VERSIONS = {"1.5.2", "1.6.1", "1.6.2", "1.6.3", "1.6.4", "1.7.2"}
# SHA-1 of the publisher's recommended installers, also covering their embedded JAR.
INSTALLERS = {
    "1.5.2-7.8.1.738": "69cf98dbaa771545678fdb60b8a6a22019146dfb",
    "1.6.1-8.9.0.775": "3aa3f3155847fcf989bf2e3fb494e7d9cdeb592b",
    "1.6.2-9.10.1.871": "dc61161257a4fd9c22245952a14f9dd2b92d566d",
    "1.6.3-9.11.0.878": "f52020cba059232d9598480eb0ca2166a3bd80c5",
    "1.6.4-9.11.1.1345": "7a57c0febe74260bfdee1a5d4968982d8863a02c",
    "1.7.2-10.12.2.1161-mc172": "7b4103625b7a62253620031410e8f09cfcfd9195",
}


def client_jar(cache, version, installed):
    """Forge's installer strips META-INF from a private copy of the signed base JAR."""
    source = cache / "versions" / version / (version + ".jar")
    jar_id = installed + "-client"
    dest = cache / "versions" / jar_id / (jar_id + ".jar")
    if dest.is_file() and zipfile.is_zipfile(dest):
        return jar_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as scratch:
        temp = Path(scratch) / "client.jar"
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(temp, "w") as out:
            for member in original.infolist():
                lifecycle.check()
                if not member.filename.upper().startswith("META-INF/"):
                    out.writestr(member, original.read(member))
        with lifecycle.guard():
            os.replace(temp, dest)
    return jar_id


def profile(cache, installed, coordinate, metadata, status):
    from minecraft_launcher_lib.command import get_library_path

    progress = status if isinstance(status, preparation.Progress) else preparation.Progress(status)
    root = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{coordinate}/forge-{coordinate}"
    version, _, build = coordinate.partition("-")
    expected = INSTALLERS.get(coordinate)
    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(scratch) / "installer.jar"
        preparation.Download(progress)(root + "-installer.jar", archive, sha1=expected)
        with zipfile.ZipFile(archive) as src:
            info = json.loads(src.read("install_profile.json"))
            install, client = info["install"], info["versionInfo"]
            name = install.get("path")
            if version not in VERSIONS or install.get("minecraft") != version or name not in {
                "net.minecraftforge:minecraftforge:" + build, "net.minecraftforge:forge:" + coordinate,
            }:
                raise ValueError("Профиль архивного Forge не совпал с выбранной версией.")
            member = src.getinfo(install["filePath"])
            if not 0 < member.file_size < 32 * 1024**2:
                raise ValueError("Некорректный размер библиотеки архивного Forge.")
            payload = src.read(member)
    jar = Path(get_library_path(name, str(cache)))
    extension = ".zip" if version == "1.5.2" else ".jar"
    artifact = {
        "url": root + "-universal" + extension,
        "path": jar.relative_to(cache / "libraries").as_posix(),
        "size": len(payload),
        "sha1": hashlib.sha1(payload).hexdigest(),
    }
    if coordinate == "1.6.1-8.9.0.775":
        # Same entries and CRCs, but Forge recompressed the standalone JAR.
        # Record the published file so the normal repair path can restore it.
        artifact.update(size=1853061, sha1="8f01b4612dc486524a2d4192a229a885b8bf76f7")
    jar.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=jar.parent) as scratch:
        temp = Path(scratch) / "forge.jar"
        temp.write_bytes(payload)
        if not zipfile.is_zipfile(temp):
            raise ValueError("Повреждён архив Forge внутри установщика.")
        with lifecycle.guard():
            os.replace(temp, jar)
    # Let Mojang supply its original libraries and native classifier metadata.
    # The legacy Forge versionInfo repeats them without download descriptors.
    inherited = {entry["name"] for entry in metadata["libraries"]}
    libraries = [{"name": name, "downloads": {"artifact": artifact}}]
    libraries.extend(lib for lib in client["libraries"] if lib["name"] != name and lib["name"] not in inherited)
    return {
        "id": installed, "inheritsFrom": version,
        "jar": client_jar(cache, version, installed) if install.get("stripMeta") else version,
        "mainClass": client["mainClass"], "minecraftArguments": client["minecraftArguments"],
        "libraries": libraries,
    }
