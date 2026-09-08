"""Headless recovery state machine. No UI action can trigger a restart."""

import asyncio
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time

from .system import classify, command_environment, read, run

DEFAULTS = {"enabled": True, "mode": "conditional", "delay_seconds": 8}


def boot_time():
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


def sleep_offset():
    return boot_time() - time.monotonic()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def load_settings(path):
    if not Path(path).exists():
        return dict(DEFAULTS), ""
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(values, dict):
            raise ValueError("settings must be an object")
        return validate_settings(values), ""
    except (OSError, ValueError, TypeError) as exc:
        # A broken config must not silently re-enable an opt-out.
        return {**DEFAULTS, "enabled": False}, str(exc)


def validate_settings(values):
    if set(values) - set(DEFAULTS):
        raise ValueError("Unknown setting")
    result = {**DEFAULTS, **values}
    if type(result["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    # v0.1.0-v0.1.1 exposed an always-restart policy. Normalize persisted
    # settings so upgrading cannot keep disrupting a healthy controller.
    if result["mode"] == "always":
        result["mode"] = "conditional"
    elif result["mode"] != "conditional":
        raise ValueError("Unsupported recovery mode")
    if type(result["delay_seconds"]) is not int or not 3 <= result["delay_seconds"] <= 15:
        raise ValueError("Delay must be an integer from 3 to 15 seconds")
    return result


class Recovery:
    def __init__(self, system, settings_dir, log_dir, runtime_dir):
        self.system = system
        self.settings_path = Path(settings_dir) / "settings.json"
        self.settings, settings_error = load_settings(self.settings_path)
        self.runtime_path = Path(runtime_dir) / "recovery.json"
        self.boot_id = read("/proc/sys/kernel/random/boot_id")
        self.last_attempt = -1000000.0
        self.runtime_error = ""
        if self.runtime_path.exists():
            try:
                state = json.loads(self.runtime_path.read_text(encoding="utf-8"))
                if state.get("boot_id") == self.boot_id:
                    self.last_attempt = float(state["last_attempt"])
                    if not math.isfinite(self.last_attempt) or self.last_attempt < 0 or self.last_attempt > boot_time():
                        raise ValueError("Invalid recovery timestamp")
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self.runtime_error = str(exc)
        self.logger = logging.getLogger(f"deckyally.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        self.log_path = Path(log_dir) / "recovery.jsonl"
        self.handler = RotatingFileHandler(self.log_path, maxBytes=512 * 1024, backupCount=3, encoding="utf-8")
        self.logger.addHandler(self.handler)
        self.state = {"phase": "starting", "reason": "", "last_result": None, "last_check": None,
                      "host": system.host, "tools": system.tools, "monitor": "starting",
                      "log_path": str(self.log_path), "settings_error": settings_error,
                      "screen_locked": True}
        self.baseline = None
        self.cycle = None
        self.cycle_source = None
        self.pending_resume_source = None
        self.tasks = []
        self.baseline_probe = None
        self.closed = False
        self.suspended = False
        # Stay inert until the Steam UI explicitly reports its lock state.
        self.screen_locked = True
        self.lock_state_known = False
        self.lock_revision = 0
        self.unlocked = asyncio.Event()
        self.last_resume = -1000000.0
        self.offset = sleep_offset()
        self.monitor_proc = None
        self.lock_stream = None

    def record(self, event, **fields):
        self.logger.info(json.dumps({"time": time.time(), "event": event, **fields}, ensure_ascii=False))

    def phase(self, name, reason=""):
        self.state.update(phase=name, reason=reason)

    def status(self):
        return {**self.state, "settings": dict(self.settings)}

    async def configure(self, patch):
        if not isinstance(patch, dict):
            raise ValueError("Settings must be an object")
        candidate = validate_settings({**self.settings, **patch})
        # Save first so the UI never displays a setting that failed to persist.
        atomic_json(self.settings_path, candidate)
        self.settings = candidate
        self.state["settings_error"] = ""
        await self.cancel_cycle()
        if self.state["phase"] != "unsupported":
            self.phase("locked" if self.screen_locked else ("idle" if candidate["enabled"] else "disabled"))
        self.record("settings_changed", settings=candidate)
        return self.status()

    def set_lock_state(self, locked):
        if type(locked) is not bool:
            raise ValueError("Lock state must be boolean")
        changed = not self.lock_state_known or locked != self.screen_locked
        self.lock_state_known = True
        if not changed:
            return self.status()
        self.screen_locked = locked
        self.lock_revision += 1
        self.state["screen_locked"] = locked
        unsupported = self.state["phase"] == "unsupported"
        if locked:
            self.unlocked.clear()
            if self.baseline_probe and not self.baseline_probe.done():
                self.baseline_probe.cancel()
            if self.cycle and not self.cycle.done():
                self.pending_resume_source = self.cycle_source or self.pending_resume_source
                self.cycle.cancel()
            if not unsupported:
                self.phase("locked")
        else:
            self.unlocked.set()
            source = self.pending_resume_source
            self.pending_resume_source = None
            if source and self.settings["enabled"] and not self.suspended and not unsupported:
                previous = self.cycle
                self.cycle_source = source
                self.cycle = asyncio.create_task(self.after_previous(previous, source))
            elif not (self.cycle and not self.cycle.done()) and not unsupported:
                self.phase("idle" if self.settings["enabled"] else "disabled")
        self.record("lock_state", locked=locked)
        return self.status()

    async def start(self):
        self.record("loaded", host=self.system.host, settings=self.settings)
        if not self.system.host["supported"]:
            self.phase("unsupported", "requires_steamos_ally_x_rc72la")
            self.state["monitor"] = "unavailable"
            return
        if os.geteuid() != 0 or not self.system.tools.get("systemctl"):
            self.phase("unsupported", "requires_root_and_systemctl")
            self.state["monitor"] = "unavailable"
            return
        # Hold one lock for the backend lifetime, including plugin reload overlap.
        import fcntl
        self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_stream = (self.runtime_path.parent / "backend.lock").open("a")
        try:
            fcntl.flock(self.lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_stream.close()
            self.lock_stream = None
            self.phase("unsupported", "another_backend_running")
            self.state["monitor"] = "unavailable"
            return
        self.phase("locked" if not self.lock_state_known or self.screen_locked
                   else ("idle" if self.settings["enabled"] else "disabled"))
        self.tasks = [asyncio.create_task(self.clock_watch()), asyncio.create_task(self.baseline_watch())]
        if self.system.tools.get("dbus-monitor"):
            self.tasks.append(asyncio.create_task(self.dbus_watch()))
        else:
            self.state["monitor"] = "clock_fallback"

    async def cancel_cycle(self):
        task = self.cycle
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def sleep_event(self, sleeping):
        if self.closed:
            return
        if sleeping:
            self.suspended = True
            # A wake may expose the lock screen before the Steam UI can report
            # it. Close the gate now and require a fresh UI state afterward.
            self.screen_locked = True
            self.lock_state_known = False
            self.lock_revision += 1
            self.unlocked.clear()
            self.state["screen_locked"] = True
            self.pending_resume_source = None
            if self.cycle and not self.cycle.done():
                self.cycle.cancel()
            if self.baseline_probe and not self.baseline_probe.done():
                self.baseline_probe.cancel()
            self.phase("sleeping")
            self.record("suspend", baseline=self.baseline)
        else:
            self.resume("logind")

    def resume(self, source):
        now = boot_time()
        self.offset = sleep_offset()
        was_sleeping = self.suspended
        self.suspended = False
        # logind and clock fallback can describe the same wake. There is no restart on load.
        previous = self.cycle
        if now - self.last_resume < 2 and not was_sleeping:
            return
        if previous and not previous.done() and not was_sleeping:
            return
        self.last_resume = now
        self.record("resume", source=source)
        if not self.settings["enabled"]:
            self.phase("disabled")
            return
        self.cycle_source = source
        if self.screen_locked:
            self.pending_resume_source = source
            self.phase("locked")
            return
        self.cycle = asyncio.create_task(self.after_previous(previous, source))

    async def after_previous(self, previous, source):
        if previous and not previous.done():
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
        await self.recover(source)

    async def wait_for_unlock_and_settle(self):
        """Do not inspect or restart anything until one full unlocked grace period."""
        while True:
            if self.screen_locked:
                self.phase("locked")
            await self.unlocked.wait()
            revision = self.lock_revision
            self.phase("waiting")
            await asyncio.sleep(self.settings["delay_seconds"])
            if not self.screen_locked and revision == self.lock_revision:
                return

    async def snapshot(self):
        try:
            value = await asyncio.wait_for(self.system.snapshot(), timeout=12)
        except Exception as exc:
            # Probe failure is uncertainty and must never trigger a disruptive restart.
            value = {"service": {"error": str(exc) or "probe timeout"},
                     "hardware": {"usb": [], "hidraw": []}, "devices": None,
                     "probe_error": str(exc) or "probe timeout"}
        health, reason = classify(value, self.baseline)
        self.state["last_check"] = {"time": time.time(), "health": health, "reason": reason}
        return value, health, reason

    async def baseline_watch(self):
        while not self.closed:
            if not self.suspended and not self.screen_locked and not (self.cycle and not self.cycle.done()):
                try:
                    self.baseline_probe = asyncio.create_task(self.snapshot())
                    value, health, _ = await self.baseline_probe
                    if (health == "ready" and not self.suspended and not self.screen_locked
                            and not (self.cycle and not self.cycle.done())):
                        self.baseline = value
                except asyncio.CancelledError:
                    if self.closed:
                        raise
                except Exception as exc:
                    self.record("baseline_probe_error", error=str(exc))
                finally:
                    self.baseline_probe = None
            await asyncio.sleep(60)

    async def guard(self):
        service = await self.system.service()
        if service.get("error"):
            return "service_probe_failed"
        if service.get("LoadState") != "loaded" or service.get("UnitFileState", "").startswith("masked"):
            return "service_missing_or_masked"
        active = service.get("ActiveState")
        previously_active = self.baseline and self.baseline["service"].get("ActiveState") == "active"
        if active not in ("active", "failed") and not (active == "inactive" and previously_active):
            return "service_not_managed_or_transitioning"
        # Do not compete with another controller manager, including template units.
        hhd = await run("systemctl", "list-units", "hhd*.service", "--state=active", "--no-legend", "--plain", "--no-pager")
        if hhd["code"]:
            return "conflict_probe_failed"
        if hhd["out"].strip():
            return "hhd_conflict"
        if self.runtime_error:
            return "runtime_state_invalid"
        if boot_time() - self.last_attempt < 45:
            return "cooldown"
        return ""

    async def finish(self, outcome, reason, **extra):
        result = {"time": time.time(), "outcome": outcome, "reason": reason, **extra}
        self.state["last_result"] = result
        self.phase(outcome, reason)
        self.record("result", **result)

    async def recover(self, source):
        try:
            await self.wait_for_unlock_and_settle()
            before, health, reason = await self.snapshot()
            self.record("before_recovery", source=source, health=health, reason=reason, snapshot=before)
            # Require three consecutive abnormal observations; unknown is never a failure.
            for _ in range(2):
                if health != "abnormal":
                    break
                await asyncio.sleep(2)
                before, health, reason = await self.snapshot()
                self.record("recheck", health=health, reason=reason, snapshot=before)
            if health != "abnormal":
                await self.finish("observed" if health == "ready" else "unknown", reason)
                return
            journal = await self.system.journal()
            self.record("journal_before", **journal)
            blocked = await self.guard()
            if blocked:
                await self.finish("skipped", blocked)
                return
            if self.suspended or self.screen_locked or not self.settings["enabled"]:
                return
            # Reserve before the systemd request: a crash/reload cannot bypass cooldown.
            self.last_attempt = boot_time()
            atomic_json(self.runtime_path, {"boot_id": self.boot_id, "last_attempt": self.last_attempt})
            self.phase("recovering")
            self.record("restart_requested", service="inputplumber.service", mode=self.settings["mode"])
            result = await self.system.restart()
            self.record("restart_finished", **result)
            self.phase("verifying")
            after = None
            for _ in range(3):
                await asyncio.sleep(2)
                after, health, reason = await self.snapshot()
                self.record("after_recovery", health=health, reason=reason, snapshot=after)
                if health == "ready":
                    break
            if result["code"] != 0:
                await self.finish("failed", "restart_command_failed", error=result["error"])
            elif health == "ready":
                await self.finish("attempted", "structure_ready_input_unverified")
            else:
                await self.finish("unknown" if health == "unknown" else "failed", reason)
            self.record("journal_after", **(await self.system.journal()))
        except asyncio.CancelledError:
            self.record("cycle_cancelled", note="An already submitted systemd job may still complete")
            raise
        except Exception as exc:
            await self.finish("failed", "recovery_error", error=str(exc))

    async def clock_watch(self):
        # CLOCK_BOOTTIME includes sleep; MONOTONIC does not. Wall clock/NTP changes are irrelevant.
        while not self.closed:
            await asyncio.sleep(2)
            current = sleep_offset()
            if current - self.offset > 0.75:
                self.resume("clock")
            self.offset = current

    async def dbus_watch(self):
        match = ("type='signal',sender='org.freedesktop.login1',path='/org/freedesktop/login1',"
                 "interface='org.freedesktop.login1.Manager',member='PrepareForSleep'")
        while not self.closed:
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "dbus-monitor", "--system", match, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=command_environment(),
                )
                self.monitor_proc = proc
                self.state["monitor"] = "logind_and_clock"
                awaiting = False
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    line = line.decode(errors="replace").strip()
                    if line.startswith("signal "):
                        awaiting = "member=PrepareForSleep" in line and "interface=org.freedesktop.login1.Manager" in line
                    elif awaiting and line in ("boolean true", "boolean false"):
                        self.sleep_event(line == "boolean true")
                        awaiting = False
            except OSError as exc:
                self.record("monitor_error", error=str(exc))
            finally:
                if proc:
                    if proc.returncode is None:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                    await proc.wait()
                self.monitor_proc = None
            self.state["monitor"] = "clock_fallback"
            self.record("monitor_disconnected", retry_seconds=10)
            await asyncio.sleep(10)

    async def stop(self):
        self.closed = True
        for task in self.tasks:
            task.cancel()
        await self.cancel_cycle()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.lock_stream:
            self.lock_stream.close()
        self.record("unloaded")
        self.logger.removeHandler(self.handler)
        self.handler.close()
