import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from launcher import preparation


class NativeExtractionTests(unittest.TestCase):
    def test_parallel_native_archives_cannot_race_on_shared_directory(self):
        import minecraft_launcher_lib.install as install

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "natives"
            target.mkdir()
            archives = []
            for index in range(2):
                archive = root / f"natives-{index}.jar"
                with zipfile.ZipFile(archive, "w") as stream:
                    stream.writestr(f"META-INF/native-{index}.txt", f"native {index}")
                    stream.writestr(f"native-{index}.dll", f"library {index}")
                archives.append(archive)
            mkdir = zipfile.os.makedirs
            simultaneous = threading.Barrier(2)
            start = threading.Barrier(2)

            def raced_mkdir(path, *args, **kwargs):
                if Path(path).name == "META-INF":
                    # Without the adapter both ZipFile.extract calls observe
                    # absence and arrive here before either creates META-INF.
                    try:
                        simultaneous.wait(timeout=0.15)
                    except threading.BrokenBarrierError:
                        pass
                return mkdir(path, *args, **kwargs)

            def extract(archive):
                start.wait(timeout=3)
                return install.extract_natives_file(str(archive), str(target), {"exclude": []})

            with (preparation.environment(preparation.Progress(lambda _: None)),
                  patch.object(zipfile.os, "makedirs", side_effect=raced_mkdir),
                  ThreadPoolExecutor(max_workers=2) as workers):
                jobs = [workers.submit(extract, archive) for archive in archives]
                for job in jobs:
                    job.result(timeout=5)
            for index in range(2):
                self.assertEqual((target / "META-INF" / f"native-{index}.txt").read_text(), f"native {index}")
                self.assertEqual((target / f"native-{index}.dll").read_text(), f"library {index}")


if __name__ == "__main__":
    unittest.main()
