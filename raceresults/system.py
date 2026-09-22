"""CPU / RAM / disk / network / temperature meters and power-off."""

import os
import shutil
import subprocess
import threading
import time

import psutil

from . import db
from .state import state


class Sampler:
    """Samples every couple of seconds so network rates are accurate however many browsers are open."""

    def __init__(self, disk_path="/"):
        self._last_net = None
        self._disk_path = str(disk_path)
        psutil.cpu_percent(interval=None)  # prime: the first reading is always 0

    def sample(self):
        now = time.monotonic()
        counters = psutil.net_io_counters(pernic=True)
        rx = sum(c.bytes_recv for nic, c in counters.items() if nic != "lo")
        tx = sum(c.bytes_sent for nic, c in counters.items() if nic != "lo")
        down = up = 0.0
        if self._last_net:
            then, rx0, tx0 = self._last_net
            elapsed = max(now - then, 0.001)
            down, up = max(rx - rx0, 0) / elapsed, max(tx - tx0, 0) / elapsed
        self._last_net = (now, rx, tx)
        memory = psutil.virtual_memory()
        disk = disk_usage(self._disk_path)
        state.stats = {
            "cpu": psutil.cpu_percent(interval=None),
            "ram": memory.percent,
            "ram_used": memory.total - memory.available,
            "ram_total": memory.total,
            "disk": disk["percent"],
            "disk_used": disk["used"],
            "disk_total": disk["total"],
            "net_down": down,
            "net_up": up,
            "temp": cpu_temperature(),
        }


def disk_usage(path="/"):
    """Usage of the filesystem holding `path` (the data folder), as `df` reports it."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"percent": 0.0, "used": 0, "total": 0}
    # df's Use% ignores the blocks reserved for root, so the figure matches what the user sees in a shell.
    usable = usage.used + usage.free
    percent = round(100 * usage.used / usable, 1) if usable else 0.0
    return {"percent": percent, "used": usage.used, "total": usage.total}


def cpu_temperature():
    try:
        sensors = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        sensors = {}
    for name in ("cpu_thermal", "coretemp", "k10temp", "soc_thermal"):
        if sensors.get(name):
            return round(sensors[name][0].current, 1)
    for readings in sensors.values():
        if readings:
            return round(readings[0].current, 1)
    return None


def _systemctl():
    return os.environ.get("RACERESULTS_SYSTEMCTL") or shutil.which("systemctl") or "/usr/bin/systemctl"


def can_power_off():
    """True if the installer's sudo rule lets this user power off without a password."""
    if os.environ.get("RACERESULTS_FAKE_POWEROFF"):
        return True
    try:
        result = subprocess.run(["sudo", "-n", "-l", _systemctl(), "poweroff"],
                                capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def power_off(stop_workers):
    """Stop cleanly, then power the computer off (after the browser has had its reply)."""

    def run():
        time.sleep(1.5)
        state.activity("Powering off", "warn")
        stop_workers()
        try:
            db.checkpoint()
        except Exception:
            pass
        if os.environ.get("RACERESULTS_FAKE_POWEROFF"):
            state.activity("(test mode: power-off skipped)", "warn")
            return
        subprocess.run(["sudo", "-n", _systemctl(), "poweroff"], capture_output=True, timeout=30)

    threading.Thread(target=run, name="poweroff", daemon=True).start()
