from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "network-monitor" / "network_monitor.py"
SPEC = importlib.util.spec_from_file_location("codex_network_monitor", MODULE_PATH)
assert SPEC and SPEC.loader
network_monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = network_monitor
SPEC.loader.exec_module(network_monitor)


TOKEN = "test-internal-token-value"


def snapshot(
    rx: int,
    tx: int,
    *,
    boot_id: str = "boot-a",
    interface: str = "eth0",
    ifindex: int = 2,
):
    return network_monitor.CounterSnapshot(interface, ifindex, boot_id, rx, tx)


class ConfigTests(unittest.TestCase):
    def test_decimal_package_gb_is_converted_to_exact_bytes(self):
        config = network_monitor.Config.from_env(
            {
                "NETWORK_MONITOR_INTERNAL_TOKEN": TOKEN,
                "TRAFFIC_PACKAGE_TOTAL_GB": "1.25",
                "TRAFFIC_PACKAGE_START_AT": "2026-07-01T00:00:00+08:00",
                "TRAFFIC_PACKAGE_END_AT": "2026-08-01T00:00:00+08:00",
                "PURCHASED_BANDWIDTH_MBPS": "20.5",
            }
        )

        self.assertEqual(config.package.total_bytes, 1_250_000_000)
        self.assertEqual(config.package.purchased_bandwidth_mbps, 20.5)
        self.assertLess(config.package.start_at, config.package.end_at)

    def test_exact_bytes_and_gb_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError, "set only one"):
            network_monitor.Config.from_env(
                {
                    "NETWORK_MONITOR_INTERNAL_TOKEN": TOKEN,
                    "TRAFFIC_PACKAGE_TOTAL_BYTES": "1000",
                    "TRAFFIC_PACKAGE_TOTAL_GB": "1",
                }
            )

    def test_interface_traversal_and_short_token_are_rejected(self):
        for values in (
            {
                "NETWORK_INTERFACE": "../eth0",
                "NETWORK_MONITOR_INTERNAL_TOKEN": TOKEN,
            },
            {"NETWORK_MONITOR_INTERNAL_TOKEN": "too-short"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                network_monitor.Config.from_env(values)


class CounterReaderTests(unittest.TestCase):
    def test_reads_linux_sysfs_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            interface = root / "net" / "eth0"
            (interface / "statistics").mkdir(parents=True)
            (interface / "ifindex").write_text("7\n", encoding="ascii")
            (interface / "statistics" / "rx_bytes").write_text(
                "123\n", encoding="ascii"
            )
            (interface / "statistics" / "tx_bytes").write_text(
                "456\n", encoding="ascii"
            )
            boot_id = root / "boot_id"
            boot_id.write_text("boot-test\n", encoding="ascii")
            config = network_monitor.Config(
                internal_token=TOKEN,
                sys_class_net=root / "net",
                boot_id_path=boot_id,
            )

            value = network_monitor.CounterReader(config).read()

        self.assertEqual(value.ifindex, 7)
        self.assertEqual(value.boot_id, "boot-test")
        self.assertEqual(value.rx_bytes, 123)
        self.assertEqual(value.tx_bytes, 456)


class TrafficStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "traffic.sqlite3"
        self.store = network_monitor.TrafficStore(self.database)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_first_sample_is_baseline_and_increments_are_accumulated(self):
        initial = self.store.record(snapshot(10_000, 20_000), 1_000)
        current = self.store.record(snapshot(10_200, 20_400), 1_002)

        self.assertEqual(initial.delta_rx_bytes, 0)
        self.assertEqual(current.delta_rx_bytes, 200)
        self.assertEqual(current.delta_tx_bytes, 400)
        self.assertEqual(current.rx_bytes_per_second, 100)
        self.assertEqual(current.tx_bytes_per_second, 200)
        summary = self.store.summary(
            1_002,
            network_monitor.PackageConfig(),
            stale=False,
            collector_error=False,
        )
        self.assertEqual(summary["monitored"]["rx_bytes"], 200)
        self.assertEqual(summary["monitored"]["tx_bytes"], 400)
        self.assertEqual(summary["since_boot"]["rx_bytes"], 10_200)
        self.assertFalse(summary["package"]["configured"])

    def test_process_restart_preserves_gap_bytes_without_speed_spike(self):
        self.store.record(snapshot(1_000, 2_000), 1_000)
        self.store.record(snapshot(1_200, 2_400), 1_002)
        self.store.close()
        self.store = network_monitor.TrafficStore(self.database)

        resumed = self.store.record(
            snapshot(1_500, 3_000), 1_010, suppress_rate=True
        )
        live = self.store.record(snapshot(1_700, 3_400), 1_012)

        self.assertEqual(resumed.delta_rx_bytes, 300)
        self.assertEqual(resumed.delta_tx_bytes, 600)
        self.assertEqual(resumed.rx_bytes_per_second, 0)
        self.assertEqual(resumed.tx_bytes_per_second, 0)
        self.assertEqual(live.rx_bytes_per_second, 100)
        summary = self.store.summary(
            1_012,
            network_monitor.PackageConfig(),
            stale=False,
            collector_error=False,
        )
        self.assertEqual(summary["monitored"]["rx_bytes"], 700)
        self.assertEqual(summary["monitored"]["tx_bytes"], 1_400)

    def test_boot_interface_and_counter_changes_do_not_create_false_deltas(self):
        self.store.record(snapshot(1_000, 2_000), 1_000)
        self.store.record(snapshot(1_100, 2_200), 1_002)
        reboot = self.store.record(snapshot(20, 30, boot_id="boot-b"), 1_004)
        interface = self.store.record(
            snapshot(40, 50, boot_id="boot-b", ifindex=8), 1_006
        )
        reset = self.store.record(
            snapshot(10, 20, boot_id="boot-b", ifindex=8), 1_008
        )

        self.assertEqual(reboot.reason, "boot_changed")
        self.assertEqual(interface.reason, "interface_changed")
        self.assertEqual(reset.reason, "counter_reset")
        for result in (reboot, interface, reset):
            self.assertTrue(result.discontinuity)
            self.assertEqual(result.delta_rx_bytes, 0)
            self.assertEqual(result.rx_bytes_per_second, 0)
        summary = self.store.summary(
            1_008,
            network_monitor.PackageConfig(),
            stale=False,
            collector_error=False,
        )
        self.assertEqual(summary["monitored"]["rx_bytes"], 100)
        self.assertEqual(summary["monitored"]["reset_count"], 3)

    def test_package_uses_outbound_cycle_delta_and_manual_offset(self):
        self.store.record(snapshot(1_000, 2_000), 1_000)
        self.store.record(snapshot(1_100, 3_000), 1_010)
        self.store.record(snapshot(1_300, 5_000), 1_020)
        package = network_monitor.PackageConfig(
            total_bytes=10_000,
            start_at=1_010,
            end_at=2_000,
            tx_offset_bytes=500,
            purchased_bandwidth_mbps=1,
        )

        summary = self.store.summary(
            1_020, package, stale=False, collector_error=False
        )

        self.assertEqual(summary["package"]["metric"], "tx_bytes")
        self.assertEqual(summary["package"]["monitored_tx_bytes"], 2_000)
        self.assertEqual(summary["package"]["used_bytes"], 2_500)
        self.assertEqual(summary["package"]["remaining_bytes"], 7_500)
        self.assertEqual(summary["package"]["usage_ratio"], 0.25)
        self.assertTrue(summary["package"]["coverage_complete"])
        self.assertTrue(summary["bandwidth"]["configured"])

    def test_frontend_schema_compatibility_fields_are_stable(self):
        self.store.record(snapshot(1_000, 2_000), 1_000)
        self.store.record(snapshot(1_100, 2_200), 1_002)
        summary = self.store.summary(
            1_002,
            network_monitor.PackageConfig(total_bytes=100),
            stale=False,
            collector_error=False,
        )
        history = self.store.history("15m", 1_002)

        self.assertIsInstance(summary["interface"], str)
        self.assertIsInstance(summary["monitor_started_at"], str)
        self.assertIsInstance(summary["reset_count"], int)
        for section in ("current", "peak"):
            self.assertIn("rx_bytes_per_sec", summary[section])
            self.assertIn("tx_bytes_per_sec", summary[section])
            self.assertIn("total_bytes_per_sec", summary[section])
        self.assertIsInstance(summary["package"]["percent"], (int, float))
        self.assertGreater(summary["package"]["percent"], 100)
        self.assertIn("rx_bytes_per_sec", history["points"][-1])
        self.assertIn("tx_bytes_per_sec", history["points"][-1])
        self.assertIn("total_bytes_per_sec", history["points"][-1])

    def test_history_range_is_validated_and_points_are_bounded(self):
        self.store.record(snapshot(1_000, 2_000), 1_000)
        for index in range(1, 801):
            self.store.record(
                snapshot(1_000 + index * 10, 2_000 + index * 20),
                1_000 + index * 2,
            )

        history = self.store.history("1h", 2_601)

        self.assertLessEqual(len(history["points"]), 720)
        self.assertEqual(history["source_resolution"], "live")
        self.assertIn("tx_total_bytes", history["points"][-1])
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.store.history("forever", 2_601)


class FakeMonitor:
    def summary(self):
        return {"generated_at": "2026-07-18T00:00:00.000Z", "stale": False}

    def history(self, range_name):
        return {"range": range_name, "points": []}


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.server = network_monitor.MonitorHTTPServer(
            ("127.0.0.1", 0), FakeMonitor(), TOKEN
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, *, token=TOKEN, method="GET"):
        request = urllib.request.Request(
            self.base_url + path,
            method=method,
            headers={network_monitor.INTERNAL_TOKEN_HEADER: token},
        )
        return urllib.request.urlopen(request, timeout=5)

    def test_summary_history_health_and_head_are_no_store(self):
        with self.request("/summary") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertFalse(json.load(response)["stale"])
        with self.request("/history?range=7d") as response:
            self.assertEqual(json.load(response)["range"], "7d")
        with self.request("/healthz") as response:
            self.assertTrue(json.load(response)["ok"])
        with self.request("/summary", method="HEAD") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
        with self.request("/history?range=24h", method="HEAD") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")

    def test_token_is_required_and_compared_before_route_handling(self):
        for path in ("/summary", "/not-found"):
            with self.subTest(path=path), self.assertRaises(
                urllib.error.HTTPError
            ) as caught:
                self.request(path, token="incorrect-token-value")
            self.assertEqual(caught.exception.code, 403)
            self.assertEqual(caught.exception.headers["Cache-Control"], "no-store")

    def test_history_rejects_unknown_range_and_query_parameters(self):
        for path in ("/history?range=forever", "/history?extra=1"):
            with self.subTest(path=path), self.assertRaises(
                urllib.error.HTTPError
            ) as caught:
                self.request(path)
            self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
