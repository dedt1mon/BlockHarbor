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
    # unittest records each failed subTest and can additionally report a cleanup
    # error for its parent. Count passing test cases by distinct parent identity,
    # rather than subtracting those records several times from testsRun.
    def case_id(test):
        return getattr(test, "test_case", test).id()

    failed_cases = {case_id(test) for test, _detail in result.failures + result.errors}
    nonpassing = failed_cases | {case_id(test) for test, _reason in result.skipped + result.expectedFailures}
    nonpassing.update(case_id(test) for test in result.unexpectedSuccesses)
    report = {"tests": result.testsRun, "passed": result.testsRun - len(nonpassing),
              "failedCases": len(failed_cases),
              "counting": "tests/passed/failedCases count test cases; failed/errors count unittest records, including subtests and cleanup",
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
