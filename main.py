"""Entrypoint: speaks agent-fun/bars-replay/v1 via the kit's Python shim."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import makefun_bars  # noqa: E402
from strategy import SurgeBot  # noqa: E402

_BOT = SurgeBot()


def on_batch(state, batch):
    return _BOT.on_batch(state, batch)


if __name__ == "__main__":
    makefun_bars.run(on_batch)
