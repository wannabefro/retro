#!/usr/bin/env python3
"""SessionStart hook: name any probation rule past its checkpoint, so retro verify gets run."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import ledger
from lib.session import due_summary


def _debug(message):
    if os.environ.get("RETRO_DEBUG"):
        print(message, file=sys.stderr)


def main():
    data = ledger.load()
    summary = due_summary(data)
    count = summary["due"]
    if count == 0:
        return
    noun = "rule" if count == 1 else "rules"
    print(f"retro: {count} {noun} past checkpoint. Run `retro verify` to check them.")


def run():
    try:
        main()
    except Exception as exc:
        _debug(f"session-start failed: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
