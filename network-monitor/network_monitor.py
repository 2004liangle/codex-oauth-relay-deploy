#!/usr/bin/env python3
"""Persistent host network counters exposed through a small loopback HTTP API."""

from __future__ import annotations

import hmac
import json
import logging
import math
import os
import re
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qs, urlsplit


LOGGER = logging.getLogger("codex-network-monitor")
INTERNAL_TOKEN_HEADER = "X-Codex-Network-Token"
HISTORY_RANGES = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "6h": 6 * 60 * 60,
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}
FINE_RETENTION_SECONDS = 48 * 60 * 60
ROLLUP_RETENTION_SECONDS = 400 * 24 * 60 * 60
MAX_HISTORY_POINTS = 720
INTERFACE_PATTERN = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")


def utc_iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_nonnegative_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed < 0:
        raise ValueError(f"{name} must not be negative")
    return parsed


def parse_nonnegative_decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a decimal number") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return parsed


def parse_optional_timestamp(value: str, name: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.timestamp()


@dataclass(frozen=True)
class PackageConfig:
    total_bytes: int = 0
    start_at: float | None = None
    end_at: float | None = None
    tx_offset_bytes: int = 0
    purchased_bandwidth_mbps: float = 0.0

    def validate(self) -> None:
        if self.total_bytes < 0 or self.tx_offset_bytes < 0:
            raise ValueError("package byte values must not be negative")
        if self.purchased_bandwidth_mbps < 0 or not math.isfinite(
            self.purchased_bandwidth_mbps
        ):
            raise ValueError("PURCHASED_BANDWIDTH_MBPS must be finite and non-negative")
        if (
            self.start_at is not None
            and self.end_at is not None
            and self.end_at <= self.start_at
        ):
            raise ValueError("TRAFFIC_PACKAGE_END_AT must be later than START_AT")


@dataclass(frozen=True)
class Config:
    interface: str = "eth0"
    database_path: Path = Path("/var/lib/codex-network-monitor/traffic.sqlite3")
    listen_host: str = "127.0.0.1"
    listen_port: int = 18082
    internal_token: str = ""
    sample_seconds: float = 2.0
    package: PackageConfig = PackageConfig()
    sys_class_net: Path = Path("/sys/class/net")
    boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id")

    def validate(self) -> None:
        if not INTERFACE_PATTERN.fullmatch(self.interface):
            raise ValueError("NETWORK_INTERFACE contains unsupported characters")
        if not self.listen_host:
            raise ValueError("NETWORK_MONITOR_LISTEN_HOST must not be empty")
        if not 1 <= self.listen_port <= 65535:
            raise ValueError("NETWORK_MONITOR_LISTEN_PORT must be between 1 and 65535")
        if len(self.internal_token) < 16:
            raise ValueError("NETWORK_MONITOR_INTERNAL_TOKEN must be at least 16 characters")
        if not 0.5 <= self.sample_seconds <= 60:
            raise ValueError("NETWORK_MONITOR_SAMPLE_SECONDS must be between 0.5 and 60")
        self.package.validate()

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Config":
        values = os.environ if environ is None else environ
        total_bytes_value = values.get("TRAFFIC_PACKAGE_TOTAL_BYTES", "").strip()
        total_gb_value = values.get("TRAFFIC_PACKAGE_TOTAL_GB", "").strip()
        if total_bytes_value and total_gb_value:
            raise ValueError(
                "set only one of TRAFFIC_PACKAGE_TOTAL_BYTES and TRAFFIC_PACKAGE_TOTAL_GB"
            )
        if total_bytes_value:
            total_bytes = parse_nonnegative_int(
                total_bytes_value, "TRAFFIC_PACKAGE_TOTAL_BYTES"
            )
        elif total_gb_value:
            total_bytes = int(
                parse_nonnegative_decimal(total_gb_value, "TRAFFIC_PACKAGE_TOTAL_GB")
                * Decimal(1_000_000_000)
            )
        else:
            total_bytes = 0

        package = PackageConfig(
            total_bytes=total_bytes,
            start_at=parse_optional_timestamp(
                values.get("TRAFFIC_PACKAGE_START_AT", ""),
                "TRAFFIC_PACKAGE_START_AT",
            ),
            end_at=parse_optional_timestamp(
                values.get("TRAFFIC_PACKAGE_END_AT", ""),
                "TRAFFIC_PACKAGE_END_AT",
            ),
            tx_offset_bytes=parse_nonnegative_int(
                values.get("TRAFFIC_PACKAGE_TX_OFFSET_BYTES", "0"),
                "TRAFFIC_PACKAGE_TX_OFFSET_BYTES",
            ),
            purchased_bandwidth_mbps=float(
                parse_nonnegative_decimal(
                    values.get("PURCHASED_BANDWIDTH_MBPS", "0"),
                    "PURCHASED_BANDWIDTH_MBPS",
                )
            ),
        )
        config = cls(
            interface=values.get("NETWORK_INTERFACE", "eth0"),
            database_path=Path(
                values.get(
                    "NETWORK_MONITOR_DATABASE",
                    "/var/lib/codex-network-monitor/traffic.sqlite3",
                )
            ),
            listen_host=values.get("NETWORK_MONITOR_LISTEN_HOST", "127.0.0.1"),
            listen_port=int(values.get("NETWORK_MONITOR_LISTEN_PORT", "18082")),
            internal_token=values.get("NETWORK_MONITOR_INTERNAL_TOKEN", ""),
            sample_seconds=float(values.get("NETWORK_MONITOR_SAMPLE_SECONDS", "2")),
            package=package,
        )
        config.validate()
        return config


@dataclass(frozen=True)
class CounterSnapshot:
    interface: str
    ifindex: int
    boot_id: str
    rx_bytes: int
    tx_bytes: int


@dataclass(frozen=True)
class SampleResult:
    delta_rx_bytes: int
    delta_tx_bytes: int
    rx_bytes_per_second: float
    tx_bytes_per_second: float
    discontinuity: bool
    reason: str | None


class CounterReader:
    def __init__(self, config: Config):
        self.interface = config.interface
        self.interface_path = config.sys_class_net / config.interface
        self.boot_id_path = config.boot_id_path

    @staticmethod
    def _read_nonnegative(path: Path) -> int:
        value = int(path.read_text(encoding="ascii").strip())
        if value < 0:
            raise ValueError(f"negative counter in {path}")
        return value

    def read(self) -> CounterSnapshot:
        return CounterSnapshot(
            interface=self.interface,
            ifindex=self._read_nonnegative(self.interface_path / "ifindex"),
            boot_id=self.boot_id_path.read_text(encoding="ascii").strip(),
            rx_bytes=self._read_nonnegative(
                self.interface_path / "statistics" / "rx_bytes"
            ),
            tx_bytes=self._read_nonnegative(
                self.interface_path / "statistics" / "tx_bytes"
            ),
        )


class TrafficStore:
    def __init__(self, database_path: Path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            database_path, timeout=10, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._last_prune_at = 0.0
        with self.lock:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS monitor_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    interface TEXT NOT NULL,
                    ifindex INTEGER NOT NULL,
                    boot_id TEXT NOT NULL,
                    last_rx_bytes INTEGER NOT NULL,
                    last_tx_bytes INTEGER NOT NULL,
                    accumulated_rx_bytes INTEGER NOT NULL,
                    accumulated_tx_bytes INTEGER NOT NULL,
                    monitor_started_at REAL NOT NULL,
                    last_sample_at REAL NOT NULL,
                    current_rx_bps REAL NOT NULL,
                    current_tx_bps REAL NOT NULL,
                    peak_rx_bps REAL NOT NULL,
                    peak_tx_bps REAL NOT NULL,
                    peak_total_bps REAL NOT NULL,
                    peak_rx_at REAL,
                    peak_tx_at REAL,
                    peak_total_at REAL,
                    reset_count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_samples (
                    ts_ms INTEGER PRIMARY KEY,
                    rx_bps REAL NOT NULL,
                    tx_bps REAL NOT NULL,
                    accumulated_rx_bytes INTEGER NOT NULL,
                    accumulated_tx_bytes INTEGER NOT NULL,
                    discontinuity INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS minute_samples (
                    bucket INTEGER PRIMARY KEY,
                    last_ts_ms INTEGER NOT NULL,
                    rx_avg_bps REAL NOT NULL,
                    tx_avg_bps REAL NOT NULL,
                    rx_peak_bps REAL NOT NULL,
                    tx_peak_bps REAL NOT NULL,
                    total_peak_bps REAL NOT NULL,
                    sample_count INTEGER NOT NULL,
                    accumulated_rx_bytes INTEGER NOT NULL,
                    accumulated_tx_bytes INTEGER NOT NULL
                );
                """
            )
            self.connection.commit()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def _state_locked(self) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM monitor_state WHERE singleton = 1"
        ).fetchone()

    @staticmethod
    def _discontinuity_reason(
        state: sqlite3.Row, snapshot: CounterSnapshot
    ) -> str | None:
        if state["boot_id"] != snapshot.boot_id:
            return "boot_changed"
        if (
            state["interface"] != snapshot.interface
            or state["ifindex"] != snapshot.ifindex
        ):
            return "interface_changed"
        if (
            snapshot.rx_bytes < state["last_rx_bytes"]
            or snapshot.tx_bytes < state["last_tx_bytes"]
        ):
            return "counter_reset"
        return None

    def record(
        self,
        snapshot: CounterSnapshot,
        sampled_at: float,
        *,
        suppress_rate: bool = False,
    ) -> SampleResult:
        with self.lock, self.connection:
            state = self._state_locked()
            if state is None:
                accumulated_rx = 0
                accumulated_tx = 0
                rx_rate = 0.0
                tx_rate = 0.0
                reset_count = 0
                started_at = sampled_at
                peak_rx = peak_tx = peak_total = 0.0
                peak_rx_at = peak_tx_at = peak_total_at = None
                delta_rx = delta_tx = 0
                reason = None
            else:
                sampled_at = max(sampled_at, state["last_sample_at"] + 0.001)
                reason = self._discontinuity_reason(state, snapshot)
                discontinuity = reason is not None
                if discontinuity:
                    delta_rx = delta_tx = 0
                    reset_count = state["reset_count"] + 1
                else:
                    delta_rx = snapshot.rx_bytes - state["last_rx_bytes"]
                    delta_tx = snapshot.tx_bytes - state["last_tx_bytes"]
                    reset_count = state["reset_count"]
                accumulated_rx = state["accumulated_rx_bytes"] + delta_rx
                accumulated_tx = state["accumulated_tx_bytes"] + delta_tx
                elapsed = sampled_at - state["last_sample_at"]
                if suppress_rate or discontinuity or elapsed <= 0:
                    rx_rate = tx_rate = 0.0
                else:
                    rx_rate = delta_rx / elapsed
                    tx_rate = delta_tx / elapsed
                started_at = state["monitor_started_at"]
                peak_rx = max(state["peak_rx_bps"], rx_rate)
                peak_tx = max(state["peak_tx_bps"], tx_rate)
                peak_total = max(state["peak_total_bps"], rx_rate + tx_rate)
                peak_rx_at = (
                    sampled_at if rx_rate > state["peak_rx_bps"] else state["peak_rx_at"]
                )
                peak_tx_at = (
                    sampled_at if tx_rate > state["peak_tx_bps"] else state["peak_tx_at"]
                )
                peak_total_at = (
                    sampled_at
                    if rx_rate + tx_rate > state["peak_total_bps"]
                    else state["peak_total_at"]
                )

            discontinuity = reason is not None
            self.connection.execute(
                """
                INSERT INTO monitor_state (
                    singleton, interface, ifindex, boot_id,
                    last_rx_bytes, last_tx_bytes,
                    accumulated_rx_bytes, accumulated_tx_bytes,
                    monitor_started_at, last_sample_at,
                    current_rx_bps, current_tx_bps,
                    peak_rx_bps, peak_tx_bps, peak_total_bps,
                    peak_rx_at, peak_tx_at, peak_total_at, reset_count
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    interface = excluded.interface,
                    ifindex = excluded.ifindex,
                    boot_id = excluded.boot_id,
                    last_rx_bytes = excluded.last_rx_bytes,
                    last_tx_bytes = excluded.last_tx_bytes,
                    accumulated_rx_bytes = excluded.accumulated_rx_bytes,
                    accumulated_tx_bytes = excluded.accumulated_tx_bytes,
                    monitor_started_at = excluded.monitor_started_at,
                    last_sample_at = excluded.last_sample_at,
                    current_rx_bps = excluded.current_rx_bps,
                    current_tx_bps = excluded.current_tx_bps,
                    peak_rx_bps = excluded.peak_rx_bps,
                    peak_tx_bps = excluded.peak_tx_bps,
                    peak_total_bps = excluded.peak_total_bps,
                    peak_rx_at = excluded.peak_rx_at,
                    peak_tx_at = excluded.peak_tx_at,
                    peak_total_at = excluded.peak_total_at,
                    reset_count = excluded.reset_count
                """,
                (
                    snapshot.interface,
                    snapshot.ifindex,
                    snapshot.boot_id,
                    snapshot.rx_bytes,
                    snapshot.tx_bytes,
                    accumulated_rx,
                    accumulated_tx,
                    started_at,
                    sampled_at,
                    rx_rate,
                    tx_rate,
                    peak_rx,
                    peak_tx,
                    peak_total,
                    peak_rx_at,
                    peak_tx_at,
                    peak_total_at,
                    reset_count,
                ),
            )
            ts_ms = int(sampled_at * 1000)
            self.connection.execute(
                """
                INSERT OR REPLACE INTO live_samples (
                    ts_ms, rx_bps, tx_bps, accumulated_rx_bytes,
                    accumulated_tx_bytes, discontinuity
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_ms,
                    rx_rate,
                    tx_rate,
                    accumulated_rx,
                    accumulated_tx,
                    int(discontinuity),
                ),
            )
            bucket = int(sampled_at // 60) * 60
            self.connection.execute(
                """
                INSERT INTO minute_samples (
                    bucket, last_ts_ms, rx_avg_bps, tx_avg_bps,
                    rx_peak_bps, tx_peak_bps, total_peak_bps, sample_count,
                    accumulated_rx_bytes, accumulated_tx_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(bucket) DO UPDATE SET
                    last_ts_ms = excluded.last_ts_ms,
                    rx_avg_bps = (
                        minute_samples.rx_avg_bps * minute_samples.sample_count
                        + excluded.rx_avg_bps
                    ) / (minute_samples.sample_count + 1),
                    tx_avg_bps = (
                        minute_samples.tx_avg_bps * minute_samples.sample_count
                        + excluded.tx_avg_bps
                    ) / (minute_samples.sample_count + 1),
                    rx_peak_bps = MAX(minute_samples.rx_peak_bps, excluded.rx_peak_bps),
                    tx_peak_bps = MAX(minute_samples.tx_peak_bps, excluded.tx_peak_bps),
                    total_peak_bps = MAX(
                        minute_samples.total_peak_bps, excluded.total_peak_bps
                    ),
                    sample_count = minute_samples.sample_count + 1,
                    accumulated_rx_bytes = excluded.accumulated_rx_bytes,
                    accumulated_tx_bytes = excluded.accumulated_tx_bytes
                """,
                (
                    bucket,
                    ts_ms,
                    rx_rate,
                    tx_rate,
                    rx_rate,
                    tx_rate,
                    rx_rate + tx_rate,
                    accumulated_rx,
                    accumulated_tx,
                ),
            )
            if sampled_at - self._last_prune_at >= 60:
                self.connection.execute(
                    "DELETE FROM live_samples WHERE ts_ms < ?",
                    (int((sampled_at - FINE_RETENTION_SECONDS) * 1000),),
                )
                self.connection.execute(
                    "DELETE FROM minute_samples WHERE bucket < ?",
                    (int(sampled_at - ROLLUP_RETENTION_SECONDS),),
                )
                self._last_prune_at = sampled_at

        return SampleResult(
            delta_rx_bytes=delta_rx,
            delta_tx_bytes=delta_tx,
            rx_bytes_per_second=rx_rate,
            tx_bytes_per_second=tx_rate,
            discontinuity=discontinuity,
            reason=reason,
        )

    def _totals_at_locked(
        self, timestamp: float, state: sqlite3.Row
    ) -> tuple[int, int, bool]:
        if timestamp <= state["monitor_started_at"]:
            return 0, 0, timestamp >= state["monitor_started_at"]
        if timestamp >= state["last_sample_at"]:
            return (
                state["accumulated_rx_bytes"],
                state["accumulated_tx_bytes"],
                True,
            )
        limit_ms = int(timestamp * 1000)
        live = self.connection.execute(
            """
            SELECT ts_ms, accumulated_rx_bytes, accumulated_tx_bytes
            FROM live_samples WHERE ts_ms <= ? ORDER BY ts_ms DESC LIMIT 1
            """,
            (limit_ms,),
        ).fetchone()
        minute = self.connection.execute(
            """
            SELECT last_ts_ms AS ts_ms, accumulated_rx_bytes,
                   accumulated_tx_bytes
            FROM minute_samples WHERE last_ts_ms <= ?
            ORDER BY last_ts_ms DESC LIMIT 1
            """,
            (limit_ms,),
        ).fetchone()
        candidates = [row for row in (live, minute) if row is not None]
        if candidates:
            row = max(candidates, key=lambda candidate: candidate["ts_ms"])
            return row["accumulated_rx_bytes"], row["accumulated_tx_bytes"], True
        earliest = self.connection.execute(
            """
            SELECT accumulated_rx_bytes, accumulated_tx_bytes
            FROM minute_samples ORDER BY last_ts_ms ASC LIMIT 1
            """
        ).fetchone()
        if earliest is None:
            return 0, 0, False
        return earliest["accumulated_rx_bytes"], earliest["accumulated_tx_bytes"], False

    def summary(
        self,
        now: float,
        package: PackageConfig,
        *,
        stale: bool,
        collector_error: bool,
    ) -> dict[str, object]:
        with self.lock:
            state = self._state_locked()
            if state is None:
                raise RuntimeError("monitor has not collected its initial sample")

            cycle_start = package.start_at
            cycle_end = package.end_at
            effective_start = max(
                state["monitor_started_at"],
                cycle_start if cycle_start is not None else state["monitor_started_at"],
            )
            effective_end = min(now, cycle_end) if cycle_end is not None else now
            if effective_end <= effective_start:
                tracked_tx = 0
                baseline_complete = cycle_start is None or state["monitor_started_at"] <= cycle_start
            else:
                _, start_tx, start_found = self._totals_at_locked(effective_start, state)
                _, end_tx, end_found = self._totals_at_locked(effective_end, state)
                tracked_tx = max(0, end_tx - start_tx)
                baseline_complete = (
                    start_found
                    and end_found
                    and (cycle_start is None or state["monitor_started_at"] <= cycle_start)
                )
            used_bytes = package.tx_offset_bytes + tracked_tx
            configured = package.total_bytes > 0
            remaining = max(0, package.total_bytes - used_bytes) if configured else None
            ratio = used_bytes / package.total_bytes if configured else None
            if cycle_start is not None and now < cycle_start:
                period_state = "not_started"
            elif cycle_end is not None and now >= cycle_end:
                period_state = "ended"
            else:
                period_state = "active"

            rx_rate = state["current_rx_bps"]
            tx_rate = state["current_tx_bps"]
            purchased_bps = package.purchased_bandwidth_mbps * 1_000_000 / 8
            return {
                "generated_at": utc_iso(now),
                "stale": stale,
                "collector_error": collector_error,
                "interface": state["interface"],
                "interface_details": {
                    "name": state["interface"],
                    "ifindex": state["ifindex"],
                },
                "monitor_started_at": utc_iso(state["monitor_started_at"]),
                "reset_count": state["reset_count"],
                "current": {
                    "rx_bytes_per_sec": round(rx_rate, 3),
                    "tx_bytes_per_sec": round(tx_rate, 3),
                    "total_bytes_per_sec": round(rx_rate + tx_rate, 3),
                    "rx_bytes_per_second": round(rx_rate, 3),
                    "tx_bytes_per_second": round(tx_rate, 3),
                    "total_bytes_per_second": round(rx_rate + tx_rate, 3),
                    "rx_mbps": round(rx_rate * 8 / 1_000_000, 6),
                    "tx_mbps": round(tx_rate * 8 / 1_000_000, 6),
                    "total_mbps": round((rx_rate + tx_rate) * 8 / 1_000_000, 6),
                },
                "peak": {
                    "rx_bytes_per_sec": round(state["peak_rx_bps"], 3),
                    "tx_bytes_per_sec": round(state["peak_tx_bps"], 3),
                    "total_bytes_per_sec": round(state["peak_total_bps"], 3),
                    "rx_bytes_per_second": round(state["peak_rx_bps"], 3),
                    "tx_bytes_per_second": round(state["peak_tx_bps"], 3),
                    "total_bytes_per_second": round(state["peak_total_bps"], 3),
                    "rx_at": utc_iso(state["peak_rx_at"]),
                    "tx_at": utc_iso(state["peak_tx_at"]),
                    "total_at": utc_iso(state["peak_total_at"]),
                },
                "monitored": {
                    "started_at": utc_iso(state["monitor_started_at"]),
                    "last_sample_at": utc_iso(state["last_sample_at"]),
                    "rx_bytes": state["accumulated_rx_bytes"],
                    "tx_bytes": state["accumulated_tx_bytes"],
                    "total_bytes": state["accumulated_rx_bytes"]
                    + state["accumulated_tx_bytes"],
                    "reset_count": state["reset_count"],
                },
                "since_boot": {
                    "rx_bytes": state["last_rx_bytes"],
                    "tx_bytes": state["last_tx_bytes"],
                    "total_bytes": state["last_rx_bytes"] + state["last_tx_bytes"],
                },
                "package": {
                    "configured": configured,
                    "metric": "tx_bytes",
                    "total_bytes": package.total_bytes if configured else 0,
                    "used_bytes": used_bytes,
                    "remaining_bytes": remaining if remaining is not None else 0,
                    "percent": round(ratio * 100, 6) if ratio is not None else 0,
                    "usage_ratio": round(ratio, 8) if ratio is not None else None,
                    "monitored_tx_bytes": tracked_tx,
                    "tx_offset_bytes": package.tx_offset_bytes,
                    "start_at": utc_iso(cycle_start),
                    "end_at": utc_iso(cycle_end),
                    "period_state": period_state,
                    "coverage_complete": baseline_complete,
                    "tracking_started_at": utc_iso(effective_start),
                },
                "bandwidth": {
                    "configured": package.purchased_bandwidth_mbps > 0,
                    "purchased_mbps": package.purchased_bandwidth_mbps
                    if package.purchased_bandwidth_mbps > 0
                    else None,
                    "current_tx_usage_ratio": round(tx_rate / purchased_bps, 8)
                    if purchased_bps > 0
                    else None,
                    "peak_tx_usage_ratio": round(
                        state["peak_tx_bps"] / purchased_bps, 8
                    )
                    if purchased_bps > 0
                    else None,
                },
            }

    @staticmethod
    def _aggregate_rows(
        rows: list[dict[str, float | int]], max_points: int
    ) -> list[dict[str, object]]:
        if not rows:
            return []
        chunk_size = max(1, math.ceil(len(rows) / max_points))
        points: list[dict[str, object]] = []
        for offset in range(0, len(rows), chunk_size):
            chunk = rows[offset : offset + chunk_size]
            weight = sum(int(row["weight"]) for row in chunk)
            rx_average = sum(float(row["rx_bps"]) * int(row["weight"]) for row in chunk) / weight
            tx_average = sum(float(row["tx_bps"]) * int(row["weight"]) for row in chunk) / weight
            last = chunk[-1]
            points.append(
                {
                    "timestamp": utc_iso(float(last["ts_ms"]) / 1000),
                    "rx_bytes_per_sec": round(rx_average, 3),
                    "tx_bytes_per_sec": round(tx_average, 3),
                    "total_bytes_per_sec": round(rx_average + tx_average, 3),
                    "rx_bytes_per_second": round(rx_average, 3),
                    "tx_bytes_per_second": round(tx_average, 3),
                    "total_bytes_per_second": round(rx_average + tx_average, 3),
                    "rx_peak_bytes_per_second": round(
                        max(float(row["rx_peak_bps"]) for row in chunk), 3
                    ),
                    "tx_peak_bytes_per_second": round(
                        max(float(row["tx_peak_bps"]) for row in chunk), 3
                    ),
                    "total_peak_bytes_per_second": round(
                        max(float(row["total_peak_bps"]) for row in chunk), 3
                    ),
                    "rx_total_bytes": int(last["accumulated_rx_bytes"]),
                    "tx_total_bytes": int(last["accumulated_tx_bytes"]),
                }
            )
        return points

    def history(self, range_name: str, now: float) -> dict[str, object]:
        if range_name not in HISTORY_RANGES:
            raise ValueError("unsupported history range")
        seconds = HISTORY_RANGES[range_name]
        cutoff_ms = int((now - seconds) * 1000)
        with self.lock:
            if seconds <= 6 * 60 * 60:
                source = "live"
                raw_rows = self.connection.execute(
                    """
                    SELECT ts_ms, rx_bps, tx_bps,
                           rx_bps AS rx_peak_bps, tx_bps AS tx_peak_bps,
                           rx_bps + tx_bps AS total_peak_bps,
                           accumulated_rx_bytes, accumulated_tx_bytes,
                           1 AS weight
                    FROM live_samples WHERE ts_ms >= ? ORDER BY ts_ms ASC
                    """,
                    (cutoff_ms,),
                ).fetchall()
            else:
                source = "minute"
                raw_rows = self.connection.execute(
                    """
                    SELECT last_ts_ms AS ts_ms, rx_avg_bps AS rx_bps,
                           tx_avg_bps AS tx_bps, rx_peak_bps, tx_peak_bps,
                           total_peak_bps, accumulated_rx_bytes,
                           accumulated_tx_bytes, sample_count AS weight
                    FROM minute_samples WHERE last_ts_ms >= ? ORDER BY last_ts_ms ASC
                    """,
                    (cutoff_ms,),
                ).fetchall()
            rows = [dict(row) for row in raw_rows]
        return {
            "generated_at": utc_iso(now),
            "range": range_name,
            "from": utc_iso(now - seconds),
            "to": utc_iso(now),
            "source_resolution": source,
            "points": self._aggregate_rows(rows, MAX_HISTORY_POINTS),
        }


class NetworkMonitor:
    def __init__(
        self,
        config: Config,
        store: TrafficStore,
        reader: CounterReader,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.store = store
        self.reader = reader
        self.clock = clock
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.status_lock = threading.Lock()
        self.last_error_at: float | None = None

    def sample(self, *, initial: bool = False) -> SampleResult:
        snapshot = self.reader.read()
        sampled_at = self.clock()
        result = self.store.record(snapshot, sampled_at, suppress_rate=initial)
        with self.status_lock:
            self.last_error_at = None
        return result

    def initialize(self) -> None:
        self.sample(initial=True)

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(
            target=self._run, name="network-counter-collector", daemon=True
        )
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.wait(self.config.sample_seconds):
            try:
                self.sample()
            except Exception:
                with self.status_lock:
                    self.last_error_at = self.clock()
                LOGGER.exception("network counter sample failed")

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=max(5, self.config.sample_seconds * 2))

    def summary(self) -> dict[str, object]:
        now = self.clock()
        with self.status_lock:
            collector_error = self.last_error_at is not None
        with self.store.lock:
            state = self.store._state_locked()
            if state is None:
                raise RuntimeError("monitor is not initialized")
            age = max(0.0, now - state["last_sample_at"])
        stale = collector_error or age > max(10.0, self.config.sample_seconds * 3)
        return self.store.summary(
            now,
            self.config.package,
            stale=stale,
            collector_error=collector_error,
        )

    def history(self, range_name: str) -> dict[str, object]:
        return self.store.history(range_name, self.clock())


class MonitorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        monitor: NetworkMonitor,
        internal_token: str,
    ):
        self.monitor = monitor
        self.internal_token = internal_token
        super().__init__(server_address, MonitorRequestHandler)


class MonitorRequestHandler(BaseHTTPRequestHandler):
    server: MonitorHTTPServer
    server_version = "CodexNetworkMonitor"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _authorized(self) -> bool:
        supplied = self.headers.get(INTERNAL_TOKEN_HEADER, "")
        return hmac.compare_digest(supplied, self.server.internal_token)

    def _send_json(
        self, status: int, value: dict[str, object], *, include_body: bool = True
    ) -> None:
        body = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def _handle_read(self, *, include_body: bool) -> None:
        if not self._authorized():
            self._send_json(
                403,
                {"error": {"code": "forbidden", "message": "forbidden"}},
                include_body=include_body,
            )
            return
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/summary":
                if parsed.query:
                    raise ValueError("summary does not accept query parameters")
                self._send_json(200, self.server.monitor.summary(), include_body=include_body)
                return
            if parsed.path == "/history":
                query = parse_qs(parsed.query, keep_blank_values=True)
                if set(query) - {"range"} or len(query.get("range", [])) > 1:
                    raise ValueError("invalid history query")
                range_name = query.get("range", ["24h"])[0]
                if range_name not in HISTORY_RANGES:
                    raise ValueError("unsupported history range")
                self._send_json(
                    200,
                    self.server.monitor.history(range_name),
                    include_body=include_body,
                )
                return
            if parsed.path == "/healthz" and not parsed.query:
                summary = self.server.monitor.summary()
                healthy = not bool(summary["stale"])
                self._send_json(
                    200 if healthy else 503,
                    {"ok": healthy, "stale": summary["stale"]},
                    include_body=include_body,
                )
                return
            self._send_json(
                404,
                {"error": {"code": "not_found", "message": "not found"}},
                include_body=include_body,
            )
        except ValueError as error:
            self._send_json(
                400,
                {"error": {"code": "invalid_request", "message": str(error)}},
                include_body=include_body,
            )
        except Exception:
            LOGGER.exception("request failed")
            self._send_json(
                503,
                {"error": {"code": "unavailable", "message": "temporarily unavailable"}},
                include_body=include_body,
            )

    def do_GET(self) -> None:
        self._handle_read(include_body=True)

    def do_HEAD(self) -> None:
        self._handle_read(include_body=False)

    def do_POST(self) -> None:
        self._send_json(
            405,
            {"error": {"code": "method_not_allowed", "message": "method not allowed"}},
        )


def serve(config: Config) -> None:
    store = TrafficStore(config.database_path)
    monitor = NetworkMonitor(config, store, CounterReader(config))
    monitor.initialize()
    monitor.start()
    server = MonitorHTTPServer(
        (config.listen_host, config.listen_port), monitor, config.internal_token
    )
    server.timeout = 0.5
    stopped = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    LOGGER.info(
        "listening on %s:%s for interface %s",
        config.listen_host,
        config.listen_port,
        config.interface,
    )
    try:
        while not stopped.is_set():
            server.handle_request()
    finally:
        server.server_close()
        monitor.stop()
        store.close()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("NETWORK_MONITOR_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = Config.from_env()
        serve(config)
    except (OSError, ValueError, sqlite3.Error) as error:
        LOGGER.error("startup failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
