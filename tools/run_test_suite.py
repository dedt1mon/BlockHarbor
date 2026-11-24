"""Run actual unittest discovery with durable counts and useful hang diagnostics."""

import argparse
import faulthandler
import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TimedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.durations = []

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        self.durations.append({"test": test.id(), "seconds": round(time.monotonic() - self.started, 4)})
        super().stopTest(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "tests.json")
    args = parser.parse_args()
    faulthandler.enable()
    faulthandler.dump_traceback_later(180, repeat=True)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(suite)
    faulthandler.cancel_dump_traceback_later()
    report = {"tests": result.testsRun, "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped) - len(result.expectedFailures) - len(result.unexpectedSuccesses),
              "failed": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
              "expectedFailures": len(result.expectedFailures), "unexpectedSuccesses": len(result.unexpectedSuccesses),
              "failures": [{"test": test.id(), "traceback": detail} for test, detail in result.failures + result.errors],
              "skips": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
              "slowest": sorted(result.durations, key=lambda row: row["seconds"], reverse=True)[:20]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
