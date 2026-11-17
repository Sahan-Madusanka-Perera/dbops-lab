"""Run every engine's collector, and print the result for people, machines or psql."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module

from ..config import ENGINES, load_env
from . import Statement


def _module(engine: str):
    return import_module(f".{engine}", __package__)


def collect(engines: list[str] | None, top: int) -> list[tuple[str, list[Statement], str]]:
    """[(engine, statements, source or error)] in engine order. One engine failing does not stop the rest."""
    env, chosen = load_env(), engines or list(ENGINES)

    def one(engine):
        try:
            statements, source = _module(engine).collect(env, top)
            return engine, statements, source
        except Exception as e:  # noqa: BLE001 - reported per engine
            return engine, [], f"error: {str(e).strip().splitlines()[0][:160] if str(e).strip() else type(e).__name__}"

    with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
        return list(pool.map(one, chosen))


def reset(engines: list[str] | None) -> list[str]:
    env = load_env()
    out = []
    for engine in engines or ENGINES:
        try:
            out.append(f"{engine}: {_module(engine).reset(env)}")
        except Exception as e:  # noqa: BLE001
            out.append(f"{engine}: reset failed: {str(e).strip()[:160]}")
    return out


def as_text(results, top: int, width: int = 110) -> str:
    lines = [f"DBOps slow queries   top {top} per engine, ranked by total time", ""]
    for engine, statements, source in results:
        lines.append(f"{engine}   {source}")
        if not statements and not source.startswith("error"):
            lines.append("  nothing over the thresholds since the last reset (nothing ran? try  dbops workload)")
        for i, st in enumerate(statements, 1):
            lines.append(f"  #{i}  {st.calls:,} calls   total {st.total_ms:,.0f} ms   mean {st.mean_ms:.2f} ms   {st.work}")
            query = st.query if len(st.query) <= width else st.query[: width - 3] + "..."
            lines.append(f"      {query}")
            if st.plan:
                lines.append(f"      plan  {st.plan}")
            for note in st.notes:
                lines.append(f"      why   {note}")
            for fix in st.fixes:
                lines.append(f"      fix   {fix}")
        lines.append("")
    return "\n".join(lines).rstrip()


def as_json(results) -> str:
    return json.dumps([{"engine": e, "source": src, "statements": [s.to_dict() for s in sts]}
                       for e, sts, src in results], indent=2, default=str)


def as_ddl(results) -> str:
    """Every suggested fix, once, grouped by engine: a script to review, not to run blind."""
    lines = ["-- Suggested by dbops slow. Review each one; run the SQL ones in that engine's client.", ""]
    for engine, statements, _ in results:
        fixes = list(dict.fromkeys(f for st in statements for f in st.fixes))
        if fixes:
            lines.append(f"-- {engine}")
            lines += fixes
            lines.append("")
    return "\n".join(lines).rstrip()
