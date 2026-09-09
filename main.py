"""Decky lifecycle adapter for controller recovery and Ally lighting."""

import asyncio
import sys
from pathlib import Path

import decky

sys.path.insert(0, str(Path(__file__).resolve().parent / "py_modules"))

from decky_ally.lighting import Lighting
from decky_ally.recovery import Recovery
from decky_ally.system import System


class Plugin:
    async def _main(self):
        self.lighting = Lighting(decky.DECKY_PLUGIN_SETTINGS_DIR)
        self.recovery = Recovery(
            System(), decky.DECKY_PLUGIN_SETTINGS_DIR,
            decky.DECKY_PLUGIN_LOG_DIR, decky.DECKY_PLUGIN_RUNTIME_DIR,
        )
        try:
            await self.lighting.start()
        except Exception:
            # Lighting is optional; a driver or sysfs failure must not disable recovery.
            decky.logger.exception("DeckyAlly lighting failed to start")
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

    async def get_lighting_state(self):
        if not hasattr(self, "lighting"):
            return {"available": False, "last_error": "Backend is starting"}
        return self.lighting.status()

    async def update_lighting(self, settings: dict):
        if not hasattr(self, "lighting"):
            raise RuntimeError("Backend is starting")
        return await self.lighting.configure(settings)

    async def _unload(self):
        tasks = []
        if hasattr(self, "recovery"):
            tasks.append(self.recovery.stop())
        if hasattr(self, "lighting"):
            tasks.append(self.lighting.stop())
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                decky.logger.error("DeckyAlly shutdown failed: %s", result)

    async def _uninstall(self):
        # No system units, hooks or driver configuration are installed.
        pass
