import ctypes
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from launcher import windows_identity
from launcher.window import NativeFrame


class WindowsIdentityTest(unittest.TestCase):
    def test_native_structure_layout_is_pointer_safe(self):
        self.assertEqual(ctypes.sizeof(windows_identity.GUID), 16)
        self.assertEqual(ctypes.sizeof(windows_identity.PROPERTYKEY), 20)
        self.assertEqual(windows_identity.PROPVARIANT.value.offset, 8)
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(windows_identity.PROPVARIANT), 24)

    def test_relaunch_values_precede_identity_and_preserve_quoted_paths(self):
        with tempfile.TemporaryDirectory(prefix="launcher identity ") as directory:
            path = Path(directory) / "BlockHarbor.exe"
            values = windows_identity.property_values(path)
        self.assertEqual([key for key, value in values], [2, 4, 3, 5])
        self.assertEqual(values[-1][1], "BlockHarbor.Launcher")
        self.assertTrue(values[0][1].startswith('"'))
        self.assertTrue(values[0][1].endswith('"'))
        self.assertEqual(values[1][1], "BlockHarbor")
        self.assertTrue(values[2][1].endswith("BlockHarbor.exe,0"))

    def test_foreign_window_is_never_labeled(self):
        frame = object.__new__(NativeFrame)
        frame.current = Mock(return_value=False)
        with patch.object(windows_identity, "set_window_identity") as setter:
            self.assertFalse(frame.set_identity())
        setter.assert_not_called()

    def test_non_windows_is_safe_no_op(self):
        with patch.object(windows_identity.os, "name", "posix"):
            self.assertFalse(windows_identity.set_window_identity(100, "/BlockHarbor.exe"))
            self.assertFalse(windows_identity.mark_shortcut("/BlockHarbor.lnk", "/BlockHarbor.exe"))


if __name__ == "__main__":
    unittest.main()
