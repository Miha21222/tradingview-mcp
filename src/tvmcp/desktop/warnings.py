"""First-use ToS/ban-risk warning for the opt-in `desktop` toolset.

Printed once per process to stderr the first time a desktop tool is invoked. The
`desktop` toolset automates the owner's logged-in TradingView Desktop app over CDP;
that automation may violate TradingView's ToS and is brittle by nature (breaks when
TradingView updates its UI). Opt-in, never enabled by default.
"""

from __future__ import annotations

import sys

TOS_WARNING = (
    "\n[tvmcp] WARNING: the `desktop` toolset automates YOUR logged-in TradingView "
    "Desktop app via Chrome DevTools Protocol. TradingView's ToS prohibits "
    "automated/non-display usage; using this toolset may risk account restrictions, "
    "and it can break whenever TradingView updates its UI. It is an explicit "
    "opt-in, never enabled by default. This project is not affiliated with "
    "TradingView, Inc.\n"
)

_printed = False


def warn_once() -> None:
    global _printed
    if not _printed:
        print(TOS_WARNING, file=sys.stderr)
        _printed = True
