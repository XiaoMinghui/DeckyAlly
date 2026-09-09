import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py_modules"))

from decky_ally.lighting import (
    Lighting, LightingHardware, hex_rgb, load_settings, rgb_hex, validate_settings,
)


class HardwareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.led = root / "led"
        self.led.mkdir()
        (self.led / "brightness").write_text("128")
        (self.led / "max_brightness").write_text("255")
        (self.led / "multi_index").write_text("rgb rgb rgb rgb")
        (self.led / "multi_intensity").write_text("16711680 16711680 16711680 16711680")
        self.power = root / "power"
        battery = self.power / "BAT1"
        battery.mkdir(parents=True)
        (battery / "type").write_text("Battery")
        (battery / "capacity").write_text("73")
        self.hardware = LightingHardware(self.led, self.power)

    def tearDown(self):
        self.tmp.cleanup()

    def test_detect_and_read_current_state(self):
        self.assertEqual(self.hardware.info()["zones"], 4)
        state = self.hardware.read_state()
        self.assertEqual(state, {"enabled": True, "color": "#ff0000", "brightness": 50})

    def test_static_color_packs_each_rgb_zone(self):
        self.hardware.write_frame([(1, 2, 3)], 25)
        packed = str((1 << 16) | (2 << 8) | 3)
        self.assertEqual((self.led / "multi_intensity").read_text(), " ".join([packed] * 4))
        self.assertEqual((self.led / "brightness").read_text(), "64")

    def test_wave_can_write_four_independent_zones(self):
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
        self.hardware.write_frame(colors, 100)
        self.assertEqual((self.led / "multi_intensity").read_text(),
                         "16711680 65280 255 16777215")

    def test_separate_color_channels_are_supported(self):
        (self.led / "multi_index").write_text("red green blue")
        (self.led / "multi_intensity").write_text("10 20 30")
        self.assertEqual(self.hardware.info()["zones"], 1)
        self.assertEqual(self.hardware.read_state()["color"], "#0a141e")
        self.hardware.write_frame([(40, 50, 60)], 100)
        self.assertEqual((self.led / "multi_intensity").read_text(), "40 50 60")

    def test_separate_channels_can_write_multiple_zones(self):
        (self.led / "multi_index").write_text("red green blue red green blue")
        (self.led / "multi_intensity").write_text("0 0 0 0 0 0")
        self.assertEqual(self.hardware.info()["zones"], 2)
        self.hardware.write_frame([(1, 2, 3), (4, 5, 6)], 100)
        self.assertEqual((self.led / "multi_intensity").read_text(), "1 2 3 4 5 6")

    def test_off_only_changes_brightness(self):
        before = (self.led / "multi_intensity").read_text()
        self.hardware.write_off()
        self.assertEqual((self.led / "brightness").read_text(), "0")
        self.assertEqual((self.led / "multi_intensity").read_text(), before)

    def test_missing_or_unsupported_metadata_is_unavailable(self):
        (self.led / "multi_index").write_text("amber")
        self.assertFalse(self.hardware.info()["available"])
        (self.led / "multi_index").unlink()
        self.assertIn("multi_index", self.hardware.info()["error"])

    def test_battery_capacity_uses_power_supply_type(self):
        self.assertEqual(self.hardware.battery_capacity(), 73)


class SettingsTests(unittest.TestCase):
    def test_color_helpers(self):
        self.assertEqual(hex_rgb("#12abef"), (0x12, 0xAB, 0xEF))
        self.assertEqual(rgb_hex((18, 171, 239)), "#12abef")

    def test_validation(self):
        for invalid in (
            {"color": "red"}, {"color": "#gg0000"}, {"brightness": True},
            {"brightness": 101}, {"speed": -1}, {"effect": "shell"}, {"extra": 1},
        ):
            with self.assertRaises(ValueError):
                validate_settings(invalid)
        self.assertEqual(validate_settings({"color": "#AABBCC"})["color"], "#aabbcc")
        self.assertFalse(validate_settings({"effect": "off"})["enabled"])

    def test_missing_config_is_unmanaged_and_broken_config_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lighting.json"
            settings, configured, error = load_settings(path)
            self.assertFalse(configured)
            self.assertEqual(error, "")
            self.assertTrue(settings["enabled"])
            path.write_text("broken")
            settings, configured, error = load_settings(path)
            self.assertFalse(configured)
            self.assertFalse(settings["enabled"])
            self.assertTrue(error)


class FakeHardware:
    def __init__(self):
        self.frames = []
        self.off = 0
        self.capacity = 75
        self.available = True
        self.error = ""

    def info(self):
        return {"available": self.available, "path": "/sys/fake", "zones": 4,
                "max_brightness": 255, "error": self.error}

    def read_state(self):
        return {"enabled": True, "color": "#112233", "brightness": 42}

    def write_frame(self, colors, brightness):
        self.frames.append((colors, brightness))

    def write_off(self):
        self.off += 1

    def battery_capacity(self):
        return self.capacity


class LightingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hardware = FakeHardware()
        self.lighting = Lighting(Path(self.tmp.name), self.hardware)

    async def asyncTearDown(self):
        await self.lighting.stop()
        self.tmp.cleanup()

    async def test_first_start_adopts_hardware_without_writing(self):
        state = await self.lighting.start()
        self.assertEqual(state["color"], "#112233")
        self.assertEqual(state["brightness"], 42)
        self.assertFalse(state["configured"])
        self.assertEqual(self.hardware.frames, [])

    async def test_static_settings_persist_and_apply(self):
        await self.lighting.start()
        state = await self.lighting.configure({"color": "#abcdef", "brightness": 35})
        self.assertTrue(state["configured"])
        self.assertEqual(self.hardware.frames[-1], ([(171, 205, 239)], 35))
        saved = json.loads(self.lighting.settings_path.read_text())
        self.assertEqual(saved["color"], "#abcdef")

    async def test_off_and_reenable_use_static(self):
        await self.lighting.start()
        state = await self.lighting.configure({"effect": "off"})
        self.assertFalse(state["enabled"])
        self.assertEqual(self.hardware.off, 1)
        state = await self.lighting.configure({"enabled": True})
        self.assertTrue(state["enabled"])
        self.assertEqual(state["effect"], "static")
        self.assertTrue(self.hardware.frames)

    async def test_selecting_non_off_effect_enables_lighting(self):
        await self.lighting.start()
        await self.lighting.configure({"enabled": False})
        state = await self.lighting.configure({"effect": "pulse"})
        self.assertTrue(state["enabled"])
        self.assertEqual(state["effect"], "pulse")

    async def test_unavailable_device_reports_without_write(self):
        self.hardware.available = False
        self.hardware.error = "missing: brightness"
        state = await self.lighting.start()
        self.assertFalse(state["available"])
        await self.lighting.configure({"color": "#123456"})
        self.assertEqual(self.hardware.frames, [])

    async def test_status_adopts_device_that_appears_after_start(self):
        self.hardware.available = False
        state = await self.lighting.start()
        self.assertFalse(state["available"])
        self.hardware.available = True
        state = self.lighting.status()
        self.assertTrue(state["available"])
        self.assertEqual(state["color"], "#112233")
        self.assertEqual(state["brightness"], 42)
        self.assertEqual(self.hardware.frames, [])

    async def test_battery_effect_maps_green_to_red(self):
        await self.lighting.start()
        self.lighting.power_task.cancel()
        await asyncio.gather(self.lighting.power_task, return_exceptions=True)
        original_sleep = asyncio.sleep
        calls = 0
        async def stop_after_frame(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.lighting.closed = True
            await original_sleep(0)
        with patch("decky_ally.lighting.asyncio.sleep", stop_after_frame):
            await self.lighting.configure({"effect": "battery"})
            await self.lighting.animation
        self.assertEqual(self.hardware.frames[-1], ([(127, 255, 0)], 42))

    async def test_animation_stops_on_unload(self):
        await self.lighting.start()
        await self.lighting.configure({"effect": "spectrum"})
        task = self.lighting.animation
        await asyncio.sleep(0)
        await self.lighting.stop()
        self.assertTrue(task.done())

    async def test_suspend_gap_reapplies_saved_lighting(self):
        await self.lighting.start()
        await self.lighting.configure({"color": "#445566", "brightness": 60})
        self.hardware.frames.clear()
        self.lighting.offset = 100
        original_sleep = asyncio.sleep
        offsets = iter([100, 102])

        async def tick(_):
            await original_sleep(0)
            if len(self.hardware.frames) == 1:
                self.lighting.closed = True

        with patch("decky_ally.lighting.sleep_offset", side_effect=lambda: next(offsets, 102)), \
                patch("decky_ally.lighting.asyncio.sleep", tick):
            await self.lighting._power_watch()
            if self.lighting.resume_task:
                await self.lighting.resume_task
        self.assertEqual(self.hardware.frames, [([(68, 85, 102)], 60)])


if __name__ == "__main__":
    unittest.main()
