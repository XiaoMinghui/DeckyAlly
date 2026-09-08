"""Read-only device probes and a single allowlisted recovery operation."""

import asyncio
import json
import os
import platform
import re
import shlex
import shutil
from pathlib import Path

SERVICE = "inputplumber.service"
BUS = "org.shadowblip.InputPlumber"
IFACE = "org.shadowblip.Input.CompositeDevice"
PREFIX = "/org/shadowblip/InputPlumber"


async def run(*args, timeout=5):
    """No shell; reap subprocesses on timeout, unload and suspend cancellation."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "LC_ALL": "C", "SYSTEMD_COLORS": "0"},
        )
    except OSError as exc:
        return {"code": -1, "out": "", "error": str(exc)}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        return {"code": proc.returncode, "out": out.decode(errors="replace")[:131072],
                "error": err.decode(errors="replace")[:4096]}
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return {"code": -1, "out": "", "error": "command timed out"}


def read(path):
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return ""


def key_values(value):
    return dict(line.split("=", 1) for line in value.splitlines() if "=" in line)


def host_info(root=Path("/")):
    dmi = root / "sys/class/dmi/id"
    os_data = key_values(read(root / "etc/os-release"))
    data = {
        "system": platform.system(),
        "os_id": os_data.get("ID", "").strip('"'),
        "os_version": os_data.get("VERSION_ID", "").strip('"'),
        "kernel": platform.release(),
        "vendor": read(dmi / "sys_vendor"),
        "board": read(dmi / "board_name"),
        "product": read(dmi / "product_name"),
    }
    data["supported"] = (
        data["system"] == "Linux" and data["os_id"] == "steamos"
        and data["vendor"] == "ASUSTeK COMPUTER INC."
        and (data["board"] == "RC72LA" or bool(re.search(r"\bRC72LA(?:_|\b)", data["product"])))
    )
    return data


def hardware(root=Path("/")):
    usb = []
    for item in (root / "sys/bus/usb/devices").glob("*"):
        if read(item / "idVendor").lower() == "0b05" and read(item / "idProduct").lower() == "1b4c":
            usb.append(item.name)
    hid = []
    for item in (root / "sys/class/hidraw").glob("hidraw*"):
        props = key_values(read(item / "device/uevent"))
        parts = props.get("HID_ID", "").split(":")
        try:
            matches = len(parts) == 3 and int(parts[1], 16) == 0x0B05 and int(parts[2], 16) == 0x1B4C
        except ValueError:
            matches = False
        if matches:
            hid.append("/dev/" + item.name)
    return {"usb": sorted(usb), "hidraw": sorted(hid)}


def parse_property(raw):
    """busctl JSON preferred; textual fallback supports older systemd builds."""
    try:
        value = json.loads(raw)
        if isinstance(value, dict) and "data" in value:
            return value["data"]
    except (ValueError, TypeError):
        pass
    tokens = shlex.split(raw)
    if len(tokens) == 2 and tokens[0] in ("s", "o"):
        return tokens[1]
    if len(tokens) >= 2 and tokens[0] in ("as", "ao"):
        if int(tokens[1]) == len(tokens[2:]):
            return tokens[2:]
    raise ValueError("Unsupported D-Bus property response")


class System:
    def __init__(self):
        self.host = host_info()
        self.tools = {name: shutil.which(name) for name in ("systemctl", "busctl", "dbus-monitor", "journalctl")}

    async def service(self, name=SERVICE):
        result = await run("systemctl", "show", name, "--no-pager",
                           "--property=LoadState,ActiveState,SubState,UnitFileState")
        values = key_values(result["out"])
        if result["code"] != 0 or not values.get("LoadState"):
            values["error"] = result["error"] or "Service status unavailable"
        return values

    async def property(self, path, name):
        args = ("busctl", "--system", "--auto-start=no", "--timeout=3", "get-property", BUS, path, IFACE, name)
        result = await run(*args)
        if result["code"]:
            raise ValueError(result["error"] or "D-Bus property unavailable")
        return parse_property(result["out"])

    async def composites(self):
        result = await run("busctl", "--system", "--auto-start=no", "--timeout=3", "--list", "tree", BUS)
        if result["code"]:
            return None, result["error"] or "InputPlumber D-Bus unavailable"
        paths = [line.strip() for line in result["out"].splitlines()
                 if re.fullmatch(re.escape(PREFIX) + r"/CompositeDevice\d+", line.strip())]
        if len(paths) > 16:
            return None, "Too many composite devices to inspect safely"
        devices = []
        try:
            for path in paths:
                name = await self.property(path, "Name")
                # Never mistake an external Xbox/PlayStation controller for the internal Ally X.
                if name != "ASUS ROG Ally X":
                    continue
                source, target, caps = await asyncio.gather(
                    self.property(path, "SourceDevicePaths"),
                    self.property(path, "TargetDevices"),
                    self.property(path, "TargetCapabilities"),
                )
                if not all(isinstance(value, list) and all(isinstance(x, str) for x in value)
                           for value in (source, target, caps)):
                    raise ValueError("Unexpected InputPlumber property types")
                devices.append({"path": path, "name": name, "sources": [x for x in source if x],
                                "targets": [x for x in target if x], "gamepad": any(x.startswith("Gamepad:") for x in caps)})
        except ValueError as exc:
            return None, str(exc)
        return devices, ""

    async def snapshot(self):
        service, devices = await asyncio.gather(self.service(), self.composites())
        return {"hardware": hardware(), "service": service, "devices": devices[0], "probe_error": devices[1]}

    async def restart(self):
        # Exact service, no frontend-supplied command, argument or device path.
        return await run("systemctl", "restart", SERVICE, timeout=20)

    async def journal(self):
        return await run("journalctl", "-b", "-u", SERVICE, "--since=-2min", "-n", "100",
                         "--no-pager", "-o", "short-monotonic", timeout=5)


def classify(snapshot, baseline=None):
    service = snapshot["service"]
    if service.get("error"):
        return "unknown", "service_probe_failed"
    if service.get("ActiveState") != "active":
        return "abnormal", "service_not_active"
    physical = snapshot["hardware"]
    if not physical["usb"] or not physical["hidraw"]:
        return "abnormal", "physical_device_missing"
    devices = snapshot["devices"]
    if devices is None:
        return "unknown", "inputplumber_probe_failed"
    if not devices:
        return "abnormal", "composite_device_missing"
    if len(devices) != 1:
        return "unknown", "ambiguous_composite_devices"
    device = devices[0]
    if not device["sources"] or not device["targets"] or not device["gamepad"]:
        return "abnormal", "input_chain_incomplete"
    if baseline:
        old = baseline["hardware"]
        if len(physical["hidraw"]) < len(old["hidraw"]):
            return "abnormal", "physical_interfaces_lost"
        old_devices = baseline.get("devices") or []
        # IDs change on resume: compare counts, never event/hidraw numbers.
        if len(old_devices) == 1 and len(device["sources"]) < len(old_devices[0]["sources"]):
            return "abnormal", "source_interfaces_lost"
    return "ready", "structure_ready_input_unverified"
