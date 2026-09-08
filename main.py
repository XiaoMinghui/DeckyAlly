"""Decky lifecycle adapter. Recovery is entirely owned by the Python backend."""

import sys
from pathlib import Path

import decky

sys.path.insert(0, str(Path(__file__).resolve().parent / "py_modules"))

from decky_ally.recovery import Recovery
from decky_ally.system import System


class Plugin:
    async def _main(self):
        self.recovery = Recovery(
            System(), decky.DECKY_PLUGIN_SETTINGS_DIR,
            decky.DECKY_PLUGIN_LOG_DIR, decky.DECKY_PLUGIN_RUNTIME_DIR,
        )
        await self.recovery.start()
        decky.logger.info("DeckyAlly backend initialized")

    async def get_status(self):
        if not hasattr(self, "recovery"):
            return {"phase": "starting"}
        return self.recovery.status()

    async def update_settings(self, settings: dict):
        if not hasattr(self, "recovery"):
            raise RuntimeError("Backend is starting")
        return await self.recovery.configure(settings)

    async def set_lock_state(self, locked: bool):
        if not hasattr(self, "recovery"):
            raise RuntimeError("Backend is starting")
        return self.recovery.set_lock_state(locked)

    async def _unload(self):
        if hasattr(self, "recovery"):
            await self.recovery.stop()

    async def _uninstall(self):
        # No system units, hooks or driver configuration are installed.
        pass
