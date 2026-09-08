import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py_modules"))

from decky_ally.recovery import Recovery, atomic_json, load_settings, validate_settings
from decky_ally.system import System, classify, hardware, host_info, parse_property, run

REAL_SLEEP = asyncio.sleep


def ready():
    return {
        "service": {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"},
        "hardware": {"usb": ["1-3"], "hidraw": ["/dev/hidraw2", "/dev/hidraw3"]},
        "devices": [{"name": "ASUS ROG Ally X", "path": "/org/shadowblip/InputPlumber/CompositeDevice0",
                     "sources": ["/dev/hidraw2", "/dev/hidraw3"], "targets": ["/target/gamepad0"], "gamepad": True}],
        "probe_error": "",
    }


def broken():
    value = ready()
    value["devices"] = []
    return value


class FakeSystem:
    host = {"supported": True, "product": "ROG Ally X RC72LA_RC72LA"}
    tools = {"systemctl": "/usr/bin/systemctl"}

    def __init__(self, snapshots=None):
        self.snapshots = snapshots or [ready()]
        self.index = 0
        self.restarts = 0
        self.status = deepcopy(ready()["service"])
        self.restart_result = {"code": 0, "out": "", "error": ""}

    async def snapshot(self):
        value = deepcopy(self.snapshots[min(self.index, len(self.snapshots) - 1)])
        self.index += 1
        return value

    async def service(self):
        return self.status

    async def journal(self):
        return {"code": 0, "out": "test journal", "error": ""}

    async def restart(self):
        self.restarts += 1
        return self.restart_result


class ProbeTests(unittest.TestCase):
    def test_structure_is_not_proof_of_input(self):
        self.assertEqual(classify(ready()), ("ready", "structure_ready_input_unverified"))

    def test_external_or_mouse_only_output_is_not_ready(self):
        value = ready()
        value["devices"][0]["gamepad"] = False
        self.assertEqual(classify(value)[0], "abnormal")

    def test_dbus_failure_is_unknown(self):
        value = ready()
        value["devices"] = None
        self.assertEqual(classify(value)[0], "unknown")

    def test_reenumeration_does_not_look_like_failure(self):
        value = ready()
        value["hardware"]["hidraw"] = ["/dev/hidraw8", "/dev/hidraw9"]
        value["devices"][0]["sources"] = ["/dev/hidraw8", "/dev/hidraw9"]
        self.assertEqual(classify(value, ready())[0], "ready")

    def test_lost_interface_and_lost_source_detected(self):
        value = ready()
        value["hardware"]["hidraw"].pop()
        self.assertEqual(classify(value, ready())[1], "physical_interfaces_lost")
        value = ready()
        value["devices"][0]["sources"].pop()
        self.assertEqual(classify(value, ready())[1], "source_interfaces_lost")

    def test_absent_hardware_and_service_failure(self):
        value = ready()
        value["hardware"]["usb"] = []
        self.assertEqual(classify(value)[1], "physical_device_missing")
        value["service"]["ActiveState"] = "failed"
        self.assertEqual(classify(value)[1], "service_not_active")

    def test_busctl_responses(self):
        self.assertEqual(parse_property('s "ASUS ROG Ally X"'), "ASUS ROG Ally X")
        self.assertEqual(parse_property('as 2 "/dev/hidraw2" "/dev/input/event3"'), ["/dev/hidraw2", "/dev/input/event3"])
        self.assertEqual(parse_property('as 0'), [])
        self.assertEqual(parse_property('{"type":"as","data":["/target/0"]}'), ["/target/0"])
        with self.assertRaises(ValueError):
            parse_property('as 2 "only-one"')

    def test_machine_and_hardware_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def put(path, value):
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(value, encoding="utf-8")
            put("etc/os-release", 'ID=steamos\nVERSION_ID="3.7.0"')
            put("sys/class/dmi/id/sys_vendor", "ASUSTeK COMPUTER INC.")
            put("sys/class/dmi/id/board_name", "RC72LA")
            put("sys/class/dmi/id/product_name", "ROG Ally X RC72LA_RC72LA")
            put("sys/bus/usb/devices/1-3/idVendor", "0b05")
            put("sys/bus/usb/devices/1-3/idProduct", "1b4c")
            put("sys/class/hidraw/hidraw8/device/uevent", "HID_ID=0003:00000B05:00001B4C")
            with patch("decky_ally.system.platform.system", return_value="Linux"):
                self.assertTrue(host_info(root)["supported"])
                put("etc/os-release", "ID=bazzite")
                self.assertFalse(host_info(root)["supported"])
                put("etc/os-release", "ID=steamos")
                put("sys/class/dmi/id/board_name", "RC73XA")
                put("sys/class/dmi/id/product_name", "ROG Xbox Ally X RC73XA")
                self.assertFalse(host_info(root)["supported"])
            self.assertEqual(hardware(root), {"usb": ["1-3"], "hidraw": ["/dev/hidraw8"]})

    def test_config_validation_and_broken_config_opt_out(self):
        for values in ({"enabled": "yes"}, {"mode": "shell"}, {"delay_seconds": True},
                       {"delay_seconds": 2}, {"command": "restart"}):
            with self.assertRaises(ValueError):
                validate_settings(values)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertTrue(load_settings(path)[0]["enabled"])
            path.write_text("{broken", encoding="utf-8")
            self.assertFalse(load_settings(path)[0]["enabled"])
            atomic_json(path, {"enabled": False})
            self.assertFalse(load_settings(path)[0]["enabled"])


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engines = []
        self.run_mock = AsyncMock(return_value={"code": 0, "out": "", "error": ""})
        self.run_patch = patch("decky_ally.recovery.run", self.run_mock)
        self.run_patch.start()
        async def fast_sleep(_):
            await REAL_SLEEP(0)
        self.sleep_patch = patch("decky_ally.recovery.asyncio.sleep", fast_sleep)
        self.sleep_patch.start()

    def engine(self, snapshots=None):
        system = FakeSystem(snapshots)
        root = Path(self.tmp.name)
        engine = Recovery(system, root / "settings", root / "logs", root / "data")
        self.engines.append(engine)
        return engine

    async def asyncTearDown(self):
        for engine in self.engines:
            await engine.stop()
        self.sleep_patch.stop()
        self.run_patch.stop()
        self.tmp.cleanup()

    async def test_default_covers_connected_but_unresponsive(self):
        engine = self.engine()
        await engine.recover("test")
        self.assertEqual(engine.system.restarts, 1)
        self.assertEqual(engine.state["last_result"]["outcome"], "attempted")
        self.assertIn("unverified", engine.state["reason"])

    async def test_conditional_leaves_normal_wake_alone(self):
        engine = self.engine()
        engine.settings["mode"] = "conditional"
        await engine.recover("test")
        self.assertEqual(engine.system.restarts, 0)

    async def test_transient_discovery_failure_self_recovers(self):
        engine = self.engine([broken(), ready()])
        engine.settings["mode"] = "conditional"
        await engine.recover("test")
        self.assertEqual(engine.system.restarts, 0)

    async def test_three_abnormal_samples_restart_once(self):
        engine = self.engine([broken(), broken(), broken(), ready()])
        engine.settings["mode"] = "conditional"
        await engine.recover("test")
        self.assertEqual(engine.system.restarts, 1)
        self.assertEqual(engine.system.index, 4)

    async def test_unknown_does_not_trigger_conditional_recovery(self):
        value = ready()
        value["devices"] = None
        engine = self.engine([value])
        engine.settings["mode"] = "conditional"
        await engine.recover("test")
        self.assertEqual(engine.state["phase"], "unknown")
        self.assertEqual(engine.system.restarts, 0)

    async def test_probe_timeout_does_not_block_always_mode(self):
        engine = self.engine()
        engine.system.snapshot = AsyncMock(side_effect=asyncio.TimeoutError)
        await engine.recover("test")
        self.assertEqual(engine.system.restarts, 1)
        self.assertEqual(engine.state["phase"], "unknown")

    async def test_missing_hardware_remains_failed_without_loop(self):
        value = ready()
        value["hardware"]["usb"] = []
        engine = self.engine([value])
        await engine.recover("test")
        self.assertEqual(engine.system.restarts, 1)
        self.assertEqual(engine.state["phase"], "failed")

    async def test_command_failure_is_not_reported_as_recovery(self):
        engine = self.engine()
        engine.system.restart_result = {"code": 1, "out": "", "error": "permission denied"}
        await engine.recover("test")
        self.assertEqual(engine.state["reason"], "restart_command_failed")

    async def test_service_guards(self):
        engine = self.engine()
        for service in ({"LoadState": "not-found"},
                        {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "masked"},
                        {"LoadState": "loaded", "ActiveState": "inactive"},
                        {"LoadState": "loaded", "ActiveState": "activating"},
                        {"error": "unavailable"}):
            engine.system.status = service
            await engine.recover("test")
        self.assertEqual(engine.system.restarts, 0)

    async def test_hhd_conflict_blocks_restart(self):
        engine = self.engine()
        self.run_mock.return_value = {"code": 0, "out": "hhd@deck.service loaded active running HHD", "error": ""}
        await engine.recover("test")
        self.assertEqual(engine.state["reason"], "hhd_conflict")
        self.assertEqual(engine.system.restarts, 0)

    async def test_cooldown_survives_backend_reload(self):
        engine = self.engine()
        await engine.recover("test")
        other = self.engine()
        await other.recover("test")
        self.assertEqual(other.system.restarts, 0)
        self.assertEqual(other.state["reason"], "cooldown")

    async def test_duplicate_wake_sources_coalesce(self):
        engine = self.engine()
        engine.resume("clock")
        engine.resume("logind")
        await engine.cycle
        self.assertEqual(engine.system.restarts, 1)

    async def test_second_suspend_cancels_first_cycle_then_allows_new_wake(self):
        engine = self.engine()
        engine.resume("logind")
        previous = engine.cycle
        engine.sleep_event(True)
        engine.sleep_event(False)
        self.assertIsNot(previous, engine.cycle)
        await engine.cycle
        self.assertEqual(engine.system.restarts, 1)

    async def test_disable_cancels_pending_cycle_and_persists(self):
        engine = self.engine()
        engine.resume("logind")
        await engine.configure({"enabled": False})
        self.assertEqual(engine.system.restarts, 0)
        engine.last_resume -= 5
        engine.resume("clock")
        self.assertEqual(engine.state["phase"], "disabled")
        self.assertFalse(load_settings(engine.settings_path)[0]["enabled"])

    async def test_unload_cancels_cycle(self):
        engine = self.engine()
        engine.resume("logind")
        await engine.stop()
        self.engines.remove(engine)
        self.assertEqual(engine.system.restarts, 0)
        self.assertTrue(engine.cycle.done())

    async def test_unsupported_host_starts_no_tasks_or_commands(self):
        engine = self.engine()
        engine.system.host = {"supported": False}
        await engine.start()
        self.assertEqual(engine.state["phase"], "unsupported")
        self.assertEqual(engine.tasks, [])
        self.run_mock.assert_not_called()

    async def test_clock_fallback_detects_sleep_not_ordinary_elapsed_time(self):
        engine = self.engine()
        engine.offset = 100
        detected = []
        def resumed(source):
            detected.append(source)
            engine.closed = True
        engine.resume = resumed
        with patch("decky_ally.recovery.sleep_offset", side_effect=[100, 100, 102]):
            await engine.clock_watch()
        self.assertEqual(detected, ["clock"])

    async def test_logind_monitor_parses_only_matching_signal(self):
        engine = self.engine()
        stream = asyncio.StreamReader()
        stream.feed_data(
            b"signal sender=:1.0 -> destination=(null destination) interface=org.freedesktop.DBus; member=NameAcquired\n"
            b"   boolean false\n"
            b"signal sender=:1.1 -> destination=(null destination) interface=org.freedesktop.login1.Manager; member=PrepareForSleep\n"
            b"   boolean true\n"
            b"signal sender=:1.1 -> destination=(null destination) interface=org.freedesktop.login1.Manager; member=PrepareForSleep\n"
            b"   boolean false\n"
        )
        class Process:
            stdout = stream
            returncode = None
            def kill(self):
                self.returncode = -9
            async def wait(self):
                return self.returncode
        proc = Process()
        events = []
        done = asyncio.Event()
        def received(value):
            events.append(value)
            if len(events) == 2:
                done.set()
        engine.sleep_event = received
        with patch("decky_ally.recovery.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as spawn:
            task = asyncio.create_task(engine.dbus_watch())
            await asyncio.wait_for(done.wait(), timeout=1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertIn("sender='org.freedesktop.login1'", spawn.call_args.args[2])
        self.assertEqual(events, [True, False])
        self.assertEqual(proc.returncode, -9)

    async def test_corrupted_runtime_blocks_restart(self):
        first = self.engine()
        first.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        first.runtime_path.write_text("broken", encoding="utf-8")
        engine = self.engine()
        await engine.recover("test")
        self.assertEqual(engine.system.restarts, 0)
        self.assertEqual(engine.state["reason"], "runtime_state_invalid")


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_reaps_process(self):
        result = await run(sys.executable, "-c", "import time; time.sleep(10)", timeout=0.1)
        self.assertEqual(result["code"], -1)
        self.assertEqual(result["error"], "command timed out")

    async def test_arguments_are_not_shell_interpolated(self):
        text = "$(echo bad); & literal"
        result = await run(sys.executable, "-c", "import sys; print(sys.argv[1])", text)
        self.assertEqual(result["out"].strip(), text)

    async def test_restart_has_only_fixed_service(self):
        with patch("decky_ally.system.run", new_callable=AsyncMock) as mocked:
            await System().restart()
            mocked.assert_awaited_once_with("systemctl", "restart", "inputplumber.service", timeout=20)

    async def test_composite_probe_excludes_external_controller(self):
        system = System()
        path = "/org/shadowblip/InputPlumber/CompositeDevice0"
        async def prop(_, name):
            return {"Name": "Sony DualSense", "SourceDevicePaths": ["/dev/hidraw20"]}[name]
        system.property = prop
        with patch("decky_ally.system.run", new_callable=AsyncMock,
                   return_value={"code": 0, "out": path, "error": ""}):
            devices, error = await system.composites()
            self.assertEqual(devices, [])
            self.assertEqual(error, "")

    async def test_complete_composite_query_and_empty_sources(self):
        system = System()
        path = "/org/shadowblip/InputPlumber/CompositeDevice0"
        properties = {"Name": "ASUS ROG Ally X", "SourceDevicePaths": [""],
                      "TargetDevices": ["/target/gamepad0"], "TargetCapabilities": ["Gamepad:Button:South"]}
        async def prop(_, name):
            return properties[name]
        system.property = prop
        with patch("decky_ally.system.run", new_callable=AsyncMock,
                   return_value={"code": 0, "out": path, "error": ""}):
            devices, error = await system.composites()
        self.assertEqual(error, "")
        snapshot = ready()
        snapshot["devices"] = devices
        self.assertEqual(classify(snapshot)[1], "input_chain_incomplete")


if __name__ == "__main__":
    unittest.main()
