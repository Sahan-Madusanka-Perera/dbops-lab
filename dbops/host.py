"""CPU, memory and storage for each instance.

In the lab every instance is a container, so the container is the host: CPU and
memory come from `docker stats`, storage from `df` on the data directory inside it.
On a real server the same checks would read /proc and df directly.
"""

from __future__ import annotations

import json
import subprocess

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
