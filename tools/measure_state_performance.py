"""Reproducible local App initialization/state microbenchmark, not a UI benchmark."""

from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def measure(profiles, samples):
    # Imports and synthetic database preparation are outside the measured span.
    from launcher.store import Store
    from main import App

    with tempfile.TemporaryDirectory(prefix="bh-state-performance-") as directory:
        root = Path(directory)
        store = Store(root)
        for index in range(profiles):
            store.create(f"Synthetic build {index + 1}", "1.20.1", "vanilla")
        started = time.perf_counter()
        app = App(root)
        initialization = time.perf_counter() - started
        try:
            durations = []
            for _ in range(samples):
                started = time.perf_counter()
                snapshot = app.state()
                durations.append((time.perf_counter() - started) * 1000)
            encoded = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
            try:
                import resource
                peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                peak_kib = peak / 1024 if sys.platform == "darwin" else peak
            except ImportError:
                peak_kib = None
            return {
                "profiles": profiles,
                "samples": samples,
                "app_init_ms": round(initialization * 1000, 3),
                "state_first_ms": round(durations[0], 3),
                "state_median_ms": round(statistics.median(durations), 3),
                "state_p95_ms": round(sorted(durations)[math.ceil(samples * 0.95) - 1], 3),
                "state_max_ms": round(max(durations), 3),
                "state_json_bytes": len(encoded),
                "process_peak_rss_kib": peak_kib,
                "state_samples_ms": [round(value, 3) for value in durations],
            }
        finally:
            app.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=int, nargs="+", default=[1, 100])
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.profiles) < 1 or args.samples < 2:
        parser.error("Positive profile counts and at least two samples are required")
    if args.worker:
        print(json.dumps(measure(args.profiles[0], args.samples)))
        return
    results = []
    for count in args.profiles:
        raw = subprocess.check_output(
            [sys.executable, str(Path(__file__).resolve()), "--worker", "--profiles",
             str(count), "--samples", str(args.samples)],
            cwd=ROOT, text=True, encoding="utf-8", timeout=120,
        )
        results.append(json.loads(raw))
    report = {
        "platform": platform.system(),
        "python": platform.python_version(),
        "method": {
            "isolation": "Fresh Python process and temporary data directory per profile count",
            "data": "Empty synthetic Vanilla 1.20.1 profiles; no downloaded game or mods",
            "app_init": "App(root) only, after imports and synthetic Store preparation",
            "state": "Sequential App.state() calls, first included, no warm-up discarded",
            "percentile": "Nearest-rank p95 over all samples",
            "rss": "Process lifetime high-water mark including imports and Store preparation; not UI memory",
            "excludes": ["EXE/bootstrap startup", "network", "HTTP serialization timing",
                         "browser rendering", "game", "hardware-generalized performance claims"],
        },
        "results": results,
    }
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content)


if __name__ == "__main__":
    main()
