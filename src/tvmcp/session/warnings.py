"""First-use ToS/ban-risk warning for the opt-in `session` toolset.

Printed once per process to stderr the first time a session tool is invoked. The
`session` toolset authenticates with the owner's TradingView account cookie and may
violate TradingView's ToS; it is opt-in and never enabled by default.
"""

from __future__ import annotations

import sys

TOS_WARNING = (
    "\n[tvmcp] WARNING: the `session` toolset authenticates with YOUR TradingView "
    "account (TV_SESSIONID cookie) and uses it to read data TradingView's ToS "
    "considers non-public / non-display. Using it may violate TradingView's Terms "
    "of Service and risks account restrictions. It is an explicit opt-in, never "
    "enabled by default. This project is not affiliated with TradingView, Inc.\n"
)

_printed = False


def warn_once() -> None:
    global _printed
    if not _printed:
        print(TOS_WARNING, file=sys.stderr)
        _printed = True