import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from launcher import aliases


class AliasTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.instance = Path(temp.name)
        self.mods = self.instance / "mods"
        self.mods.mkdir()

    @staticmethod
    def archive(mod_id, version):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as jar:
            jar.writestr(
                "fabric.mod.json", json.dumps({"id": mod_id, "version": version})
            )
        return out.getvalue()

    def test_preserves_rules_and_only_changes_requested_player(self):
        config = self.instance / "config" / "nameprotect.json"
        config.parent.mkdir()
        config.write_text('{"Original": "Earlier", "friend": "Green"}')
        with patch.object(aliases, "_ensure_mods") as ensure:
            result = aliases.apply_alias(self.instance, "Original", "New_Name")
            self.assertEqual(result.resolve(), config.resolve())
            self.assertEqual(
                json.loads(config.read_text()),
                {"original": "New_Name", "friend": "Green"},
            )
            aliases.apply_alias(self.instance, "Original", "Original")
            self.assertEqual(json.loads(config.read_text()), {"friend": "Green"})
            self.assertEqual(ensure.call_count, 2)

    def test_conflicting_id_is_detected_even_in_renamed_file(self):
        (self.mods / "another-name.jar").write_bytes(
            self.archive("nameprotect", "different-mod")
        )
        with (
            patch.object(aliases, "_download") as download,
            self.assertRaisesRegex(ValueError, "другая версия"),
        ):
            aliases._ensure_mods(self.instance, (aliases.NAME_PROTECTED,))
        download.assert_not_called()

    def test_reuses_compatible_fabric_api(self):
        present = self.mods / "my-api.jar"
        present.write_bytes(self.archive("fabric-api", "0.92.2+1.20.1"))
        with patch.object(aliases, "_download") as download:
            result = aliases._ensure_mods(self.instance, (aliases.FABRIC_API,))
        self.assertEqual(result, [present])
        download.assert_not_called()

    def test_atomic_install_and_retry(self):
        raw = self.archive("customskinloader", "14.27")
        release = aliases.ModRelease(
            "CSL",
            frozenset({"customskinloader"}),
            "https://cdn.modrinth.com/test.jar",
            len(raw),
            hashlib.sha512(raw).hexdigest(),
        )
        with patch.object(aliases, "_download", return_value=raw) as download:
            first = aliases._ensure_mods(self.instance, (release,))
            second = aliases._ensure_mods(self.instance, (release,))
        self.assertEqual(first, second)
        self.assertEqual(first[0].read_bytes(), raw)
        download.assert_called_once()

    def test_bad_download_never_lands_in_mods(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"Bad response"
        with (
            patch.object(aliases, "urlopen", return_value=response),
            self.assertRaisesRegex(ValueError, "SHA-512"),
        ):
            aliases.ensure_skin_support(self.instance)
        self.assertEqual(list(self.mods.iterdir()), [])

    def test_junction_mod_directory_is_rejected_even_inside_instance(self):
        with (patch.object(Path, "is_junction", lambda path: path == self.mods, create=True),
              patch.object(aliases, "_download") as download,
              self.assertRaisesRegex(ValueError, "пределами")):
            aliases._ensure_mods(self.instance, (aliases.NAME_PROTECTED,))
        download.assert_not_called()
