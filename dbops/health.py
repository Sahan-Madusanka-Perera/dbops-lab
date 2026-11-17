"""Run every check for the chosen engines, in parallel, and order the results."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module

from .config import ENGINES, Target, targets as all_targets
from .host import docker_stats, host_checks
from .model import Check, Status


def collect(engines: list[str] | None = None, env: dict | None = None) -> list[Check]:
    chosen = [tg for tg in all_targets(env) if tg.engine in (engines or ENGINES)]
    by_engine: dict[str, list[Target]] = {}
    for tg in chosen:
        by_engine.setdefault(tg.engine, []).append(tg)

    with ThreadPoolExecutor(max_workers=len(by_engine) + 1) as pool:
        stats_future = pool.submit(docker_stats)
        engine_futures = {e: pool.submit(import_module(f".engines.{e}", __package__).run, tgs)
                          for e, tgs in by_engine.items()}
        engine_checks = [c for e in by_engine for c in engine_futures[e].result()]
        try:
            stats = stats_future.result()
        except (subprocess.SubprocessError, OSError) as e:
            stats = None
            stats_error = str(e).strip()[:120] or "docker stats failed"

    # Per instance: reachable, then cpu / memory / storage, then the engine's own checks.
    ordered: list[Check] = []
    for tg in chosen:
        own = [c for c in engine_checks if c.target == tg.name]
        ordered += [c for c in own if c.name == "reachable"]
        if stats is None:
            ordered.append(Check(tg.name, "host metrics", Status.UNKNOWN, detail=stats_error))
        else:
            ordered += host_checks(tg, stats)
        ordered += [c for c in own if c.name != "reachable"]

    # Checks about a group rather than one instance (the Mongo replica set) go last.
    names = {tg.name for tg in chosen}
    ordered += [c for c in engine_checks if c.target not in names]
    return ordered
