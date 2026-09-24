"""Real concurrent connection-acquisition load test against the docker-compose
Postgres container, for DASH-STORY-028-LOADTEST.

Drives psycopg_pool.ConnectionPool with a fixed pool_max across a range of
concurrency levels, each worker thread repeatedly acquiring a connection,
running a trivial `SELECT 1`, and releasing it. Records connection-acquisition
latency (p50/p95/p99) and PoolTimeout/exhaustion counts per level.

Usage:
    python scripts/loadtest/connection_pool_loadtest.py --pool-max 20 --pool-min 2

Reads DASHANAN_POSTGRES_* from the environment (loaded from .env by the
caller) -- never a hardcoded credential (application-security-core).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import threading
import time
from dataclasses import dataclass, field

from psycopg_pool import ConnectionPool, PoolTimeout


@dataclass
class LevelResult:
    """Aggregated measurements for one concurrency level's run."""

    concurrency: int
    duration_s: float
    total_acquisitions: int
    successful_acquisitions: int
    timeouts: int
    other_errors: int
    latencies_ms: list[float] = field(default_factory=list)

    def summary(self) -> dict:
        """Return the p50/p95/p99 latency and exhaustion-rate summary for this level."""
        if self.latencies_ms:
            sorted_lat = sorted(self.latencies_ms)
            p50 = _percentile(sorted_lat, 50)
            p95 = _percentile(sorted_lat, 95)
            p99 = _percentile(sorted_lat, 99)
            mean = statistics.mean(sorted_lat)
        else:
            p50 = p95 = p99 = mean = None
        exhaustion_rate = (
            self.timeouts / self.total_acquisitions if self.total_acquisitions else 0.0
        )
        return {
            "concurrency": self.concurrency,
            "duration_s": self.duration_s,
            "total_acquisitions": self.total_acquisitions,
            "successful_acquisitions": self.successful_acquisitions,
            "timeouts": self.timeouts,
            "other_errors": self.other_errors,
            "exhaustion_rate": exhaustion_rate,
            "acquire_latency_ms_mean": mean,
            "acquire_latency_ms_p50": p50,
            "acquire_latency_ms_p95": p95,
            "acquire_latency_ms_p99": p99,
        }


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Return the given percentile (0-100) from an already-sorted list using linear interpolation."""
    if not sorted_values:
        return math.nan
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[int(rank)]
    frac = rank - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * frac


def _conninfo_from_env() -> str:
    """Build a psycopg conninfo string from the DASHANAN_POSTGRES_* environment variables."""
    host = os.environ["DASHANAN_POSTGRES_HOST"]
    port = os.environ["DASHANAN_POSTGRES_PORT"]
    dbname = os.environ["DASHANAN_POSTGRES_DATABASE"]
    user = os.environ["DASHANAN_POSTGRES_MIGRATION_USER"]
    password = os.environ["DASHANAN_POSTGRES_MIGRATION_PASSWORD"]
    return f"host={host} port={port} dbname={dbname} user={user} password={password}"


def run_level(
    pool: ConnectionPool,
    concurrency: int,
    duration_s: float,
    acquire_timeout_s: float,
    hold_ms: float = 0.0,
) -> LevelResult:
    """Drive `concurrency` worker threads against `pool` for `duration_s` seconds.

    Each worker loops: acquire a connection (timed), run a query that holds the
    connection for approximately `hold_ms` milliseconds (via `pg_sleep`, so the
    hold time is enforced server-side rather than by an unreliable client-side
    sleep), release, until the deadline. Returns the aggregated LevelResult.
    """
    result = LevelResult(
        concurrency=concurrency,
        duration_s=duration_s,
        total_acquisitions=0,
        successful_acquisitions=0,
        timeouts=0,
        other_errors=0,
    )
    lock = threading.Lock()
    stop_at = time.monotonic() + duration_s
    hold_s = hold_ms / 1000.0

    def worker() -> None:
        while time.monotonic() < stop_at:
            start = time.perf_counter()
            try:
                with pool.connection(timeout=acquire_timeout_s) as conn:
                    acquired_ms = (time.perf_counter() - start) * 1000.0
                    if hold_s > 0:
                        conn.execute("SELECT pg_sleep(%s)", (hold_s,)).fetchone()
                    else:
                        conn.execute("SELECT 1").fetchone()
                with lock:
                    result.total_acquisitions += 1
                    result.successful_acquisitions += 1
                    result.latencies_ms.append(acquired_ms)
            except PoolTimeout:
                with lock:
                    result.total_acquisitions += 1
                    result.timeouts += 1
            except Exception:
                with lock:
                    result.total_acquisitions += 1
                    result.other_errors += 1

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return result


def main() -> None:
    """Run the full concurrency sweep and print a JSON results summary to stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-min", type=int, default=2)
    parser.add_argument("--pool-max", type=int, default=20)
    parser.add_argument(
        "--concurrency-levels",
        type=int,
        nargs="+",
        default=[5, 10, 20, 32, 50, 100, 150],
    )
    parser.add_argument("--duration-s", type=float, default=8.0)
    parser.add_argument("--acquire-timeout-s", type=float, default=2.0)
    parser.add_argument(
        "--hold-ms",
        type=float,
        default=0.0,
        help="Simulated per-request work time held on the connection (server-side pg_sleep).",
    )
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    conninfo = _conninfo_from_env()
    pool = ConnectionPool(
        conninfo=conninfo,
        min_size=args.pool_min,
        max_size=args.pool_max,
        open=True,
        timeout=args.acquire_timeout_s,
    )
    pool.wait(timeout=15)

    results = []
    for concurrency in args.concurrency_levels:
        level_result = run_level(
            pool=pool,
            concurrency=concurrency,
            duration_s=args.duration_s,
            acquire_timeout_s=args.acquire_timeout_s,
            hold_ms=args.hold_ms,
        )
        summary = level_result.summary()
        results.append(summary)
        print(json.dumps(summary))

    pool.close()

    output = {
        "pool_min": args.pool_min,
        "pool_max": args.pool_max,
        "acquire_timeout_s": args.acquire_timeout_s,
        "duration_s_per_level": args.duration_s,
        "hold_ms": args.hold_ms,
        "levels": results,
    }
    if args.output:
        with open(args.output, "w") as f:
            json.dump(output, f, indent=2)
    print("---FINAL---")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
