"""Checks of the measurement machine (for `joulecode doctor` and as a pre-flight check)."""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def cpu_vendor() -> str | None:
    info = _read("/proc/cpuinfo") or ""
    for line in info.splitlines():
        if line.startswith("vendor_id"):
            return line.split(":", 1)[1].strip()
    return None


def gpu_utilization() -> list[int] | None:
    """GPU utilisation in per cent per card, None without nvidia-smi."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        return [int(x) for x in out.split()]
    except (subprocess.SubprocessError, ValueError):
        return None


def run_checks(backend_problems: dict[str, list[str]]) -> list[Check]:
    checks: list[Check] = []

    system = platform.system()
    release = platform.release()
    if system != "Linux":
        checks.append(Check("Operating system", "fail", f"{system} – energy measurement requires Linux."))
    elif "microsoft" in release.lower():
        checks.append(
            Check(
                "Operating system",
                "warn",
                f"WSL kernel ({release}) – no RAPL counters, cgroup back-end only.",
            )
        )
    else:
        checks.append(Check("Operating system", "ok", f"Linux {release}"))

    vendor = cpu_vendor()
    checks.append(
        Check(
            "CPU",
            "ok" if vendor == "GenuineIntel" else "warn",
            f"{vendor or 'unknown'}"
            + ("" if vendor == "GenuineIntel" else " – metrion only supports Intel."),
        )
    )

    cgroup_v2 = Path("/sys/fs/cgroup/cgroup.controllers").exists()
    checks.append(Check("cgroup", "ok" if cgroup_v2 else "warn", "v2" if cgroup_v2 else "v1 (outdated)"))

    try:
        import docker

        docker.from_env().ping()
        checks.append(Check("Docker", "ok", "daemon reachable"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("Docker", "fail", f"not reachable: {exc}"))

    governor = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    if governor is not None:
        checks.append(
            Check(
                "CPU governor",
                "ok" if governor == "performance" else "warn",
                governor
                + ("" if governor == "performance" else " – 'performance' reduces measurement noise."),
            )
        )
    no_turbo = _read("/sys/devices/system/cpu/intel_pstate/no_turbo")
    if no_turbo is not None:
        checks.append(
            Check(
                "Turbo Boost",
                "ok" if no_turbo == "1" else "warn",
                "off" if no_turbo == "1" else "on – better switch it off for reproducible measurements.",
            )
        )

    util = gpu_utilization()
    if util is None:
        checks.append(Check("GPU", "warn", "nvidia-smi not found (needed for Ollama)."))
    else:
        busy = any(u > 5 for u in util)
        checks.append(
            Check(
                "GPU",
                "warn" if busy else "ok",
                f"utilisation {util}%" + (" – is an LLM running right now?" if busy else ""),
            )
        )

    for name, problems in backend_problems.items():
        checks.append(Check(f"Backend {name}", "warn" if problems else "ok", "; ".join(problems) or "ready"))

    return checks
