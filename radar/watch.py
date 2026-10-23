"""Watch data/input/ and run ingestion + correlation whenever new files arrive.

python -m radar.watch [--poll 5] [--correlation rules|llm|both]
"""
import argparse
import time

from . import correlate, correlate_llm, ingest


def main(poll: float, mode: str) -> None:
    print(f"Watching for new supplier updates (every {poll}s, correlation={mode}). Ctrl+C to stop.")
    while True:
        if ingest.pending_files():
            time.sleep(1)  # let writers finish
            stats = ingest.run()
            if stats["signals"]:
                if mode in ("rules", "both"):
                    correlate.run()
                if mode in ("llm", "both"):
                    correlate_llm.run()
        time.sleep(poll)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--poll", type=float, default=5)
    ap.add_argument("--correlation", choices=["rules", "llm", "both"], default="both")
    a = ap.parse_args()
    main(a.poll, a.correlation)
