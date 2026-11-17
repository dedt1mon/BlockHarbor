import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from launcher.singleton import SingleInstance


class SessionTests(unittest.TestCase):
    def test_redisplay_uses_first_session(self):
        with tempfile.TemporaryDirectory() as temp:
            visible = threading.Event()
            with SingleInstance(temp) as first:
                self.assertTrue(first.primary)
                first.monitor(visible.set, lambda: False, lambda: None)
                with SingleInstance(temp) as second:
                    self.assertFalse(second.primary)
                self.assertTrue(visible.is_set())
            self.assertEqual(
                [f.name for f in (Path(temp) / ".session").iterdir()], ["owner.lock"]
            )
            self.assertEqual((Path(temp) / ".session" / "owner.lock").read_bytes(), b"")

    def test_recovery_after_abrupt_primary_exit(self):
        # Launch and kill a genuine independent process holding the operating-system lock.
        with tempfile.TemporaryDirectory() as temp:
            worker = (
                "import sys,time; from launcher.singleton import SingleInstance; "
                "s=SingleInstance(sys.argv[1]); print(s.__enter__().primary,flush=True); time.sleep(60)"
            )
            project = Path(__file__).resolve().parents[1]
            with subprocess.Popen(
                [sys.executable, "-c", worker, temp],
                stdout=subprocess.PIPE,
                env={**os.environ, "PYTHONPATH": str(project)},
                text=True,
            ) as child:
                try:
                    self.assertEqual(child.stdout.readline().strip(), "True")
                    # A waiting second process can take over before the first has started monitoring.
                    timer = threading.Timer(0.25, child.kill)
                    timer.start()
                    try:
                        with SingleInstance(temp, timeout=4.0) as successor:
                            self.assertTrue(successor.primary)
                    finally:
                        timer.join()
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)

    def test_failed_window_reports_to_requester(self):
        with tempfile.TemporaryDirectory() as temp:

            def broken():
                raise OSError("browser")

            with SingleInstance(temp) as first:
                first.monitor(broken, lambda: False, lambda: None)
                with self.assertRaisesRegex(RuntimeError, "браузер"):
                    with SingleInstance(temp):
                        pass

    def test_idle_monitor_stops_and_lock_releases_on_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            stopped = threading.Event()
            with SingleInstance(temp) as first:
                first.monitor(lambda: None, lambda: True, stopped.set)
                self.assertTrue(stopped.wait(3))
            with SingleInstance(temp) as next_owner:
                self.assertTrue(next_owner.primary)
