"""CPU, memory and storage for each instance.

In the lab every instance is a container, so the container is the host: CPU and
memory come from `docker stats`, storage from `df` on the data directory inside it.
On a server (a target with no container, M6) the same checks read /proc and df directly.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from . import thresholds as t
from .config import Target
from .model import Check, Status, grade


def _run(cmd: list[str], timeout: float = 15) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout).stdout


def parse_docker_stats(output: str, ncpu: int = 1) -> dict[str, dict[str, float]]:
    """`docker stats --format '{{json .}}'` lines -> {container: {cpu, mem}} in percent.

    Docker reports CPU as a percent of one core (400% = four cores busy), so it
    is divided by the core count to give a percent of the whole machine.
    """
    stats = {}
    for line in output.splitlines():
        if line.strip():
            row = json.loads(line)
            stats[row["Name"]] = {
                "cpu": float(row["CPUPerc"].rstrip("%")) / ncpu,
                "mem": float(row["MemPerc"].rstrip("%")),
                "mem_usage": row.get("MemUsage", ""),
            }
    return stats


def parse_df(output: str) -> tuple[float, str]:
    """`df -P <dir>` -> (percent used, "used of size" in human units)."""
    fields = output.strip().splitlines()[-1].split()
    size_kb, used_kb, pct = int(fields[1]), int(fields[2]), float(fields[4].rstrip("%"))
    return pct, f"{_human(used_kb)} of {_human(size_kb)}"


def _human(kb: int) -> str:
    gb = kb / 1024 / 1024
    return f"{gb:.1f} GB" if gb >= 1 else f"{kb / 1024:.0f} MB"


def docker_stats() -> dict[str, dict[str, float]]:
    """One call for every container: `docker stats` takes about two seconds however many you ask for."""
    ncpu = int(_run(["docker", "info", "--format", "{{.NCPU}}"]).strip() or 1)
    return parse_docker_stats(_run(["docker", "stats", "--no-stream", "--format", "{{json .}}"]), ncpu)


def host_checks(target: Target, stats: dict[str, dict[str, float]]) -> list[Check]:
    checks = []
    s = stats.get(target.container)
    if s is None:
        checks.append(Check(target.name, "container", Status.CRIT, detail=f"{target.container} is not running"))
        return checks

    checks.append(Check(target.name, "cpu", grade(s["cpu"], *t.CPU_PCT), round(s["cpu"], 1), "%"))
    checks.append(Check(target.name, "memory", grade(s["mem"], *t.MEMORY_PCT), round(s["mem"], 1), "%",
                        s["mem_usage"]))
    try:
        pct, detail = parse_df(_run(["docker", "exec", target.container, "df", "-P", target.data_dir]))
        checks.append(Check(target.name, "storage", grade(pct, *t.STORAGE_PCT), pct, "%",
                            f"{target.data_dir}: {detail}"))
    except (subprocess.SubprocessError, ValueError, IndexError) as e:
        checks.append(Check(target.name, "storage", Status.UNKNOWN, detail=str(e).strip()[:120]))
    return checks


# ---------------------------------------------------------------- on the machine itself (M6)


def parse_proc_stat(text: str) -> tuple[int, int]:
    """First line of /proc/stat -> (busy jiffies, total jiffies) across all CPUs."""
    fields = [int(v) for v in text.splitlines()[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)      # idle + iowait
    total = sum(fields[:8])                                       # guest time is already in user
    return total - idle, total


def parse_meminfo(text: str) -> tuple[float, str]:
    """/proc/meminfo -> (percent used, "used of total"). Used = total - MemAvailable, as free(1) does."""
    kb = {k: int(v.split()[0]) for k, v in (line.split(":", 1) for line in text.splitlines() if ":" in line)}
    total, used = kb["MemTotal"], kb["MemTotal"] - kb["MemAvailable"]
    return round(used / total * 100, 1), f"{_human(used)} of {_human(total)}"


def local_host_checks(target: Target, sample_s: float = 1.0) -> list[Check]:
    checks = []
    try:
        busy1, total1 = parse_proc_stat(Path("/proc/stat").read_text())
        time.sleep(sample_s)
        busy2, total2 = parse_proc_stat(Path("/proc/stat").read_text())
        cpu = (busy2 - busy1) / max(total2 - total1, 1) * 100
        checks.append(Check(target.name, "cpu", grade(cpu, *t.CPU_PCT), round(cpu, 1), "%"))
        mem, detail = parse_meminfo(Path("/proc/meminfo").read_text())
        checks.append(Check(target.name, "memory", grade(mem, *t.MEMORY_PCT), mem, "%", detail))
    except (OSError, ValueError, KeyError, IndexError) as e:
        checks.append(Check(target.name, "host metrics", Status.UNKNOWN, detail=f"/proc: {str(e)[:100]}"))
    try:
        pct, detail = parse_df(_run(["df", "-P", target.data_dir]))
        checks.append(Check(target.name, "storage", grade(pct, *t.STORAGE_PCT), pct, "%",
                            f"{target.data_dir}: {detail}"))
    except (subprocess.SubprocessError, OSError, ValueError, IndexError) as e:
        checks.append(Check(target.name, "storage", Status.UNKNOWN, detail=str(e).strip()[:120]))
    return checks
