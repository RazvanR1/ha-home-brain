"""Punctul de intrare al add-on-ului Home Brain."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import web

from .assistant import Assistant
from .automations import Automations
from .guard import Guard
from .ha import HAClient
from .notify import Notifier
from .web import build_app

OPTIONS = Path(os.environ.get("HB_OPTIONS", "/data/options.json"))


class Brain:
    def __init__(self, opts: dict) -> None:
        self.ha = HAClient()
        self.notifier = Notifier(self.ha, opts.get("notify_services", []), opts.get("ntfy_url", ""))
        self.guard = Guard(self.ha, self.notifier, opts.get("guard", {}))
        self.automations = Automations(self.ha, self.notifier, opts)
        self.assistant = Assistant(self.ha, opts.get("assistant", {}))


async def amain() -> None:
    opts = json.loads(OPTIONS.read_text()) if OPTIONS.exists() else {}
    logging.basicConfig(level=getattr(logging, str(opts.get("log_level", "info")).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    brain = Brain(opts)

    runner = web.AppRunner(build_app(brain))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("HB_PORT", "8099"))).start()
    logging.getLogger("home_brain").info("Home Brain pornit, panou pe portul 8099")

    await asyncio.gather(
        brain.ha.run_forever(),
        brain.guard.run_forever(),
        brain.automations.run_forever(),
    )


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
