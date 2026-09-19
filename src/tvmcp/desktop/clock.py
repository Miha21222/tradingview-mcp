"""Sleep/monotonic-clock indirection for the desktop pipeline modules.

Python-side polling loops (marker settling, own-copy verification, Strategy
Tester waits) call `clock.sleep` / `clock.now` instead of `time.*` directly so
tests can swap in a fake clock that advances on sleep - the loops then run in
milliseconds while their deadlines still mean seconds.
"""

from __future__ import annotations

import time


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def now() -> float:
    return time.monotonic()
