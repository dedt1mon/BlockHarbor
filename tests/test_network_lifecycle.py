import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from launcher import aliases, lifecycle, modrinth, pack_hub
from launcher.store import Store


class NetworkLifecycleTests(unittest.TestCase):
    def test_closed_window_never_starts_a_skin_loader_request(self):
        scope = lifecycle.CancellationScope()
        with lifecycle.bind(scope), patch.object(aliases, "urlopen") as opened:
            scope.cancel()
            with self.assertRaises(lifecycle.OperationCancelled):
                aliases._download(aliases.CUSTOM_SKIN_LOADER)
        opened.assert_not_called()

    def test_cancel_at_end_of_download_preserves_existing_file(self):
        content = b"verified new addon"
        scope = lifecycle.CancellationScope()

        class CancelOnEof(io.BytesIO):
            def read(self, size=-1):
                value = super().read(size)
                if not value:
                    scope.cancel()
                return value

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "addon.jar"
            target.write_bytes(b"existing addon")
            hashes = {name: hashlib.new(name, content).hexdigest() for name in ("sha1", "sha512")}
            with lifecycle.bind(scope), patch.object(modrinth, "_open", return_value=CancelOnEof(content)):
                with self.assertRaises(lifecycle.OperationCancelled):
                    pack_hub.download_verified("https://cdn.modrinth.com/data/Example/addon.jar", target, hashes, len(content))
            self.assertEqual(target.read_bytes(), b"existing addon")
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["addon.jar"])

    def test_cancelled_pack_does_not_try_mirrors_or_publish_a_build(self):
        content = b"verified addon"
        row = {"path": "mods/addon.jar", "fileSize": len(content),
               "hashes": {name: hashlib.new(name, content).hexdigest() for name in ("sha1", "sha512")},
               "downloads": ["https://cdn.modrinth.com/data/Example/addon.jar",
                             "https://cdn-raw.modrinth.com/data/Example/addon.jar"]}
        source = io.BytesIO()
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(pack_hub.INDEX, json.dumps({"formatVersion": 1, "game": "minecraft", "versionId": "1.0",
                "name": "Cancelled pack", "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.16.0"}, "files": [row]}))
        source.seek(0)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            original = store.create("Existing", "1.20.1", "vanilla")
            hub = pack_hub.PackHub(store)
            preview = hub.stage_import(source)
            folders = set(store.instances.iterdir())
            with patch.object(pack_hub, "download_verified", side_effect=lifecycle.OperationCancelled("closed")) as download:
                with self.assertRaises(lifecycle.OperationCancelled):
                    hub.install_token(preview["token"])
            download.assert_called_once()
            self.assertEqual(store.get("active"), original)
            self.assertEqual(set(store.instances.iterdir()), folders)
            self.assertEqual(len(store.all()), 1)


if __name__ == "__main__":
    unittest.main()
