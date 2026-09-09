"""ROG Ally joystick-ring lighting through the kernel multicolor LED interface."""

import asyncio
import colorsys
import json
import math
from pathlib import Path
import time

from .recovery import atomic_json


LED_PATH = Path("/sys/class/leds/ally:rgb:joystick_rings")
EFFECTS = {"static", "pulse", "spectrum", "wave", "flash", "battery", "off"}
ANIMATED_EFFECTS = EFFECTS - {"static", "off"}
DEFAULTS = {
    "enabled": True,
    "color": "#ff0000",
    "brightness": 100,
    "effect": "static",
    "speed": 50,
}


def boot_time():
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


def sleep_offset():
    return boot_time() - time.monotonic()


def validate_settings(values):
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise ValueError("Unknown lighting setting")
    result = {**DEFAULTS, **values}
    if type(result["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if not isinstance(result["color"], str) or len(result["color"]) != 7 or result["color"][0] != "#":
        raise ValueError("color must use #rrggbb")
    try:
        int(result["color"][1:], 16)
    except ValueError as exc:
        raise ValueError("color must use #rrggbb") from exc
    result["color"] = result["color"].lower()
    for key in ("brightness", "speed"):
        if type(result[key]) is not int or not 0 <= result[key] <= 100:
            raise ValueError(f"{key} must be an integer from 0 to 100")
    if result["effect"] not in EFFECTS:
        raise ValueError("Unsupported lighting effect")
    if result["effect"] == "off":
        result["enabled"] = False
    elif result["enabled"] and result["effect"] not in EFFECTS - {"off"}:
        result["effect"] = "static"
    return result


def load_settings(path):
    path = Path(path)
    if not path.exists():
        return dict(DEFAULTS), False, ""
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        return validate_settings(values), True, ""
    except (OSError, ValueError, TypeError) as exc:
        # Invalid lighting settings must not cause an unexpected hardware write.
        return {**DEFAULTS, "enabled": False}, False, str(exc)


def hex_rgb(value):
    value = validate_settings({"color": value})["color"]
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


def rgb_hex(color):
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(value))) for value in color))


class LightingHardware:
    """A narrowly scoped adapter for the single Ally RGB LED class device."""

    def __init__(self, led_path=LED_PATH, power_supply_path=Path("/sys/class/power_supply")):
        self.path = Path(led_path)
        self.power_supply_path = Path(power_supply_path)

    def info(self):
        files = {name: self.path / name for name in
                 ("brightness", "max_brightness", "multi_intensity", "multi_index")}
        missing = [name for name, path in files.items() if not path.is_file()]
        if missing:
            return {"available": False, "path": str(self.path), "zones": 0,
                    "max_brightness": 0, "error": "missing: " + ", ".join(missing)}
        try:
            indexes = files["multi_index"].read_text().split()
            maximum = int(files["max_brightness"].read_text().strip())
            values = files["multi_intensity"].read_text().split()
            if not indexes or len(indexes) != len(values) or maximum <= 0:
                raise ValueError("invalid LED metadata")
            if any(index.lower() not in {"rgb", "multi", "red", "green", "blue"} for index in indexes):
                raise ValueError("unsupported multi_index")
            packed = sum(index.lower() in {"rgb", "multi"} for index in indexes)
            channels = len(indexes) - packed
            zones = packed + ((channels + 2) // 3)
            return {"available": True, "path": str(self.path), "zones": zones,
                    "max_brightness": maximum, "error": ""}
        except (OSError, ValueError) as exc:
            return {"available": False, "path": str(self.path), "zones": 0,
                    "max_brightness": 0, "error": str(exc)}

    def _metadata(self):
        info = self.info()
        if not info["available"]:
            raise RuntimeError(info["error"] or "RGB device unavailable")
        indexes = (self.path / "multi_index").read_text().split()
        return indexes, info["max_brightness"]

    def read_state(self):
        indexes, maximum = self._metadata()
        raw = [int(value) for value in (self.path / "multi_intensity").read_text().split()]
        channels = {"red": 0, "green": 0, "blue": 0}
        packed = None
        for index, value in zip(indexes, raw):
            kind = index.lower()
            if kind in ("rgb", "multi") and packed is None:
                packed = ((value >> 16) & 255, (value >> 8) & 255, value & 255)
            elif kind in channels:
                channels[kind] = max(0, min(255, value))
        color = packed or (channels["red"], channels["green"], channels["blue"])
        brightness = int((int((self.path / "brightness").read_text().strip()) * 100 / maximum) + 0.5)
        return {"enabled": brightness > 0, "color": rgb_hex(color),
                "brightness": max(0, min(100, brightness))}

    def write_off(self):
        (self.path / "brightness").write_text("0")

    def write_frame(self, colors, brightness):
        indexes, maximum = self._metadata()
        if not colors:
            raise ValueError("At least one color is required")
        normalized = [tuple(max(0, min(255, int(channel))) for channel in color) for color in colors]
        if any(len(color) != 3 for color in normalized):
            raise ValueError("Colors must contain three channels")
        encoded = []
        packed_position = 0
        channel_position = 0
        for index in indexes:
            kind = index.lower()
            if kind in ("rgb", "multi"):
                r, g, b = normalized[packed_position % len(normalized)]
                encoded.append((r << 16) | (g << 8) | b)
                packed_position += 1
            elif kind == "red":
                r, _, _ = normalized[(channel_position // 3) % len(normalized)]
                encoded.append(r)
                channel_position += 1
            elif kind == "green":
                _, g, _ = normalized[(channel_position // 3) % len(normalized)]
                encoded.append(g)
                channel_position += 1
            elif kind == "blue":
                _, _, b = normalized[(channel_position // 3) % len(normalized)]
                encoded.append(b)
                channel_position += 1
        # Set color first so turning brightness up never flashes the previous color.
        (self.path / "multi_intensity").write_text(" ".join(str(value) for value in encoded))
        scaled = int(max(0, min(100, brightness)) * maximum / 100 + 0.5)
        (self.path / "brightness").write_text(str(scaled))

    def battery_capacity(self):
        for supply in sorted(self.power_supply_path.glob("*")):
            try:
                if (supply / "type").read_text().strip() == "Battery":
                    return max(0, min(100, int((supply / "capacity").read_text().strip())))
            except (OSError, ValueError):
                continue
        return 50


class Lighting:
    def __init__(self, settings_dir, hardware=None):
        self.settings_path = Path(settings_dir) / "lighting.json"
        self.settings, self.configured, settings_error = load_settings(self.settings_path)
        self.hardware = hardware or LightingHardware()
        self.state = {"available": False, "device": self.hardware.info(),
                      "last_error": settings_error, "suspended": False}
        self.animation = None
        self.resume_task = None
        self.power_task = None
        self.lock = asyncio.Lock()
        self.configure_lock = asyncio.Lock()
        self.closed = False
        self.suspended = False
        self.offset = sleep_offset()

    def status(self):
        self.state["device"] = self.hardware.info()
        self.state["available"] = self.state["device"]["available"]
        if self.state["available"] and not self.configured:
            try:
                self.settings.update(self.hardware.read_state())
                self.settings["effect"] = "static" if self.settings["enabled"] else "off"
                self.state["last_error"] = ""
            except Exception as exc:
                self.state["last_error"] = str(exc)
        return {**self.state, **self.settings, "configured": self.configured}

    async def start(self):
        self.state["device"] = self.hardware.info()
        self.state["available"] = self.state["device"]["available"]
        self.power_task = asyncio.create_task(self._power_watch())
        if not self.state["available"]:
            return self.status()
        if self.configured:
            await self.apply()
        else:
            try:
                self.settings.update(self.hardware.read_state())
                if not self.settings["enabled"]:
                    self.settings["effect"] = "off"
            except Exception as exc:
                self.state["last_error"] = str(exc)
        return self.status()

    async def configure(self, patch):
        if not isinstance(patch, dict):
            raise ValueError("Lighting settings must be an object")
        async with self.configure_lock:
            merged = {**self.settings, **patch}
            if "effect" in patch:
                merged["enabled"] = patch["effect"] != "off"
            # Enabling after choosing Off returns to a useful static color.
            if patch.get("enabled") is True and merged["effect"] == "off":
                merged["effect"] = "static"
            candidate = validate_settings(merged)
            atomic_json(self.settings_path, candidate)
            self.settings = candidate
            self.configured = True
            self.state["last_error"] = ""
            await self.apply()
            return self.status()

    async def _cancel_animation(self):
        task = self.animation
        self.animation = None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def apply(self):
        async with self.lock:
            await self._cancel_animation()
            self.state["device"] = self.hardware.info()
            self.state["available"] = self.state["device"]["available"]
            if not self.state["available"] or self.suspended or self.closed:
                return
            try:
                if not self.settings["enabled"] or self.settings["effect"] == "off":
                    self.hardware.write_off()
                elif self.settings["effect"] == "static":
                    self.hardware.write_frame([hex_rgb(self.settings["color"])], self.settings["brightness"])
                else:
                    self.animation = asyncio.create_task(self._animate(self.settings["effect"]))
                self.state["last_error"] = ""
            except Exception as exc:
                self.state["last_error"] = str(exc)

    def _delay(self):
        return 0.15 - self.settings["speed"] * 0.0013

    async def _animate(self, effect):
        phase = 0.0
        failures = 0
        try:
            while not self.closed and not self.suspended and self.settings["enabled"]:
                try:
                    brightness = self.settings["brightness"]
                    base = hex_rgb(self.settings["color"])
                    if effect == "pulse":
                        factor = 0.1 + 0.9 * ((math.sin(phase) + 1) / 2)
                        self.hardware.write_frame([base], int(brightness * factor))
                        phase += 0.1
                    elif effect == "spectrum":
                        color = tuple(int(value * 255) for value in colorsys.hsv_to_rgb((phase % 360) / 360, 1, 1))
                        self.hardware.write_frame([color], brightness)
                        phase += 2
                    elif effect == "wave":
                        count = max(1, self.state["device"].get("zones", 4))
                        colors = [tuple(int(value * 255) for value in colorsys.hsv_to_rgb(
                            ((phase + zone * 360 / count) % 360) / 360, 1, 1)) for zone in range(count)]
                        self.hardware.write_frame(colors, brightness)
                        phase += 3
                    elif effect == "flash":
                        if int(phase) % 2:
                            self.hardware.write_off()
                        else:
                            self.hardware.write_frame([base], brightness)
                        phase += 1
                    elif effect == "battery":
                        capacity = self.hardware.battery_capacity()
                        color = (int(255 * (1 - max(0, capacity - 50) / 50)), 255, 0) if capacity >= 50 \
                            else (255, int(255 * capacity / 50), 0)
                        self.hardware.write_frame([color], brightness)
                    failures = 0
                    self.state["last_error"] = ""
                except Exception as exc:
                    failures += 1
                    self.state["last_error"] = str(exc)
                    if failures >= 3:
                        return
                delay = 5 if effect == "battery" else self._delay() * (3 if effect == "flash" else 1)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

    def power_state(self, sleeping):
        if self.closed:
            return
        self.suspended = bool(sleeping)
        self.state["suspended"] = self.suspended
        if self.resume_task and not self.resume_task.done():
            self.resume_task.cancel()
        if sleeping:
            self.resume_task = asyncio.create_task(self._cancel_animation())
        else:
            self.resume_task = asyncio.create_task(self._resume_after_settle())

    async def _power_watch(self):
        """Detect suspend gaps and LED driver reloads without using the frontend."""
        try:
            while not self.closed:
                await asyncio.sleep(1)
                current = sleep_offset()
                if current - self.offset > 0.75:
                    self.power_state(False)
                self.offset = current
                device = self.hardware.info()
                was_available = self.state["available"]
                self.state["device"] = device
                self.state["available"] = device["available"]
                if was_available and not device["available"]:
                    await self._cancel_animation()
                elif not was_available and device["available"]:
                    if self.configured:
                        await self.apply()
                    else:
                        try:
                            self.settings.update(self.hardware.read_state())
                            self.settings["effect"] = "static" if self.settings["enabled"] else "off"
                            self.state["last_error"] = ""
                        except Exception as exc:
                            self.state["last_error"] = str(exc)
        except asyncio.CancelledError:
            raise

    async def _resume_after_settle(self):
        await asyncio.sleep(2)
        if self.suspended or self.closed:
            return
        if self.configured:
            await self.apply()
            return
        self.state["device"] = self.hardware.info()
        self.state["available"] = self.state["device"]["available"]
        if self.state["available"]:
            try:
                self.settings.update(self.hardware.read_state())
                if not self.settings["enabled"]:
                    self.settings["effect"] = "off"
                self.state["last_error"] = ""
            except Exception as exc:
                self.state["last_error"] = str(exc)

    async def stop(self):
        self.closed = True
        if self.power_task and not self.power_task.done():
            self.power_task.cancel()
            await asyncio.gather(self.power_task, return_exceptions=True)
        if self.resume_task and not self.resume_task.done():
            self.resume_task.cancel()
            await asyncio.gather(self.resume_task, return_exceptions=True)
        await self._cancel_animation()
