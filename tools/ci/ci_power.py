"""Power state for the local CI tools: timing on battery is not comparable with a mains baseline.

See docs/ci-strategy.md, "Power state". States: `ac`, `battery`, `unknown` (a desktop, a runner or a failed query counts as
`ac` for every decision but is reported as unknown), plus whether a battery saver is on.

Duplication note: Sub0Math's shared perf gate has the same module (tools/perf/sub0perf/power.py). When the repository
being checked carries it, `power_state(repo)` uses it, so the two cannot disagree. Otherwise the self-contained fallback
below answers, with the same states and the same environment variables:

    SUB0PERF_FAKE_POWER=ac|battery|unknown|saver   replaces the query; for tests only (`saver` is mains with the saver on)
    SUB0MATH_PERF_ON_BATTERY=1                     measure on battery anyway (the run is flagged, never a baseline)
    SUB0MATH_PERF_POWER_STRICT=1                   fail instead of deferring

Standard library only; Windows, Linux and macOS.
"""
from __future__ import annotations

import ctypes
import importlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

AC, BATTERY, UNKNOWN = "ac", "battery", "unknown"
FAKE_VAR = "SUB0PERF_FAKE_POWER"
OVERRIDE_VAR = "SUB0MATH_PERF_ON_BATTERY"
STRICT_VAR = "SUB0MATH_PERF_POWER_STRICT"
SWITCH_VARS = (FAKE_VAR, OVERRIDE_VAR, STRICT_VAR)
UNTIMED_LABEL = "untimed (on battery)"


@dataclass(frozen=True)
class PowerState:
    state: str = UNKNOWN
    saver: bool = False
    source: str = ""
    mode_ac: str = ""  # Windows power-mode overlay on mains / on battery (reported, never changed); "" = unknown
    mode_dc: str = ""

    @property
    def untrusted(self) -> bool:
        """True when timings made now cannot be compared with a mains baseline."""
        return self.state == BATTERY or self.saver

    def describe(self) -> str:
        modes = f"power mode {self.mode_ac} (mains) / {self.mode_dc} (battery)" if self.mode_ac or self.mode_dc else ""
        extra = "; ".join(x for x in (self.source, modes) if x)
        return f"{self.state}{', battery saver' if self.saver else ''}" + (f" ({extra})" if extra else "")

    def as_dict(self) -> dict:
        return {"state": self.state, "saver": self.saver}


def truthy(value) -> bool:
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def override(environ: Optional[dict] = None) -> bool:
    return truthy((os.environ if environ is None else environ).get(OVERRIDE_VAR))


def switches_set(environ: Optional[dict] = None) -> list[str]:
    """The power switches present in the environment (tuning variables that must not be left on by mistake)."""
    environ = os.environ if environ is None else environ
    return [f"{v}={environ[v]}" for v in SWITCH_VARS if environ.get(v)]


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte), ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte), ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


# Windows power-mode overlays (the Settings > Power & battery slider), as in Sub0Math's sub0perf.power. No overlay value is Balanced.
_OVERLAYS = {"961cc777-2547-4f9d-8174-7d86181b8a7a": "Best power efficiency", "3af9b8d9-7c97-431d-ad78-34a8bfea439f": "Better performance",
             "ded574b5-45a0-4f42-8737-46345c09c238": "Best performance", "00000000-0000-0000-0000-000000000000": "Balanced"}


def _windows_modes() -> tuple[str, str]:
    """(mains, battery) power-mode overlay names from the registry (read only), '' where unreadable."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes") as key:
            names = []
            for value in ("ActiveOverlayAcPowerScheme", "ActiveOverlayDcPowerScheme"):
                try:
                    guid = str(winreg.QueryValueEx(key, value)[0]).strip("{}").lower()
                except FileNotFoundError:
                    guid = "00000000-0000-0000-0000-000000000000"
                names.append(_OVERLAYS.get(guid, guid))
            return names[0], names[1]
    except (OSError, ImportError, ValueError):
        return "", ""


def _windows() -> PowerState:
    status = _SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return PowerState(source="windows")
    ac, dc = _windows_modes()
    return PowerState({0: BATTERY, 1: AC}.get(status.ACLineStatus, UNKNOWN), status.SystemStatusFlag == 1, "windows", ac, dc)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _linux(root: str = "/sys/class/power_supply", profile: str = "/sys/firmware/acpi/platform_profile") -> PowerState:
    """Mains online means ac; a battery with no mains online means battery; a machine with no battery is ac."""
    if not Path(root).is_dir():
        return PowerState(source="linux")
    kinds = {p: _read(p / "type") for p in sorted(Path(root).glob("*"))}
    batteries = [p for p, k in kinds.items() if k == "Battery"]
    mains = [p for p, k in kinds.items() if k in ("Mains", "USB", "USB_C", "USB_PD")]
    saver = _read(Path(profile)) == "low-power"
    if any(_read(p / "online") == "1" for p in mains) or not batteries:
        return PowerState(AC, saver, "linux")
    return PowerState(BATTERY, saver, "linux")


def _macos() -> PowerState:
    batt = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    state = BATTERY if "'Battery Power'" in batt else AC if "'AC Power'" in batt else UNKNOWN
    full = subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=5).stdout
    return PowerState(state, any(line.split()[:2] == ["lowpowermode", "1"] for line in full.splitlines()), "macos")


def fake(value: str) -> PowerState:
    value = value.strip().lower()
    if value == "saver":
        return PowerState(AC, True, "fake")
    return PowerState(value if value in (AC, BATTERY, UNKNOWN) else UNKNOWN, False, "fake")


def _shared_module(repo: Optional[Path]):
    """Sub0Math's sub0perf.power, when the repository carries it (tools/perf/sub0perf); else None."""
    if repo is None or not (repo / "tools" / "perf" / "sub0perf" / "power.py").is_file():
        return None
    path = str(repo / "tools" / "perf")
    added = path not in sys.path
    if added:
        sys.path.insert(0, path)
    try:
        return importlib.import_module("sub0perf.power")
    except Exception:  # a broken or older copy: use the fallback  # noqa: BLE001
        return None
    finally:
        if added and path in sys.path:
            sys.path.remove(path)


def power_state(repo: Optional[Path] = None, environ: Optional[dict] = None) -> PowerState:
    """The machine's power state now. Never raises: a failed query is `unknown`."""
    environ = os.environ if environ is None else environ
    shared = _shared_module(repo)
    if shared is not None:
        try:
            found = shared.power_state(environ)
            return PowerState(found.state, bool(found.saver), f"{found.source}; sub0perf" if found.source else "sub0perf",
                              getattr(found, "mode_ac", ""), getattr(found, "mode_dc", ""))
        except Exception:  # noqa: BLE001
            pass
    if environ.get(FAKE_VAR):
        return fake(environ[FAKE_VAR])
    try:
        if sys.platform == "win32":
            return _windows()
        if sys.platform == "darwin":
            return _macos()
        return _linux()
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        return PowerState(source="none")
