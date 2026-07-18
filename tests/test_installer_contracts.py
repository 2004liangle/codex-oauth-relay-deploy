import hashlib
import ipaddress
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "install-codex-relay.sh").read_text()
BOOTSTRAP = (ROOT / "install.sh").read_text()
IMAGES_INSTALLER = (ROOT / "install-relay-images-skill.sh").read_text()
ARTIFACT_INSTALLER = (ROOT / "install-artifact-relay.sh").read_text()
UI_BUILDER = (ROOT / "usage-ui" / "build-plain-zh.sh").read_text()
NETWORK_MONITOR_PATH = ROOT / "network-monitor" / "network_monitor.py"


class UsageDashboardInstallerTests(unittest.TestCase):
    def test_release_entry_points_are_aligned(self):
        self.assertIn('DEPLOY_RELEASE_VERSION="1.4.0"', INSTALLER)
        self.assertIn('/releases/download/v1.4.0/install-codex-relay.sh', BOOTSTRAP)
        self.assertIn('VERSION="v1.4.0"', IMAGES_INSTALLER)

    def test_plain_chinese_ui_is_versioned_and_hash_pinned(self):
        version = re.search(r'^USAGE_UI_VERSION="([^"]+)"$', INSTALLER, re.MULTILINE)
        digest = re.search(r'^USAGE_UI_SHA256="([^"]+)"$', INSTALLER, re.MULTILINE)

        self.assertIsNotNone(version)
        self.assertEqual(version.group(1), "1.13.2-plain-zh.2")
        self.assertIsNotNone(digest)
        self.assertRegex(digest.group(1), r"^[0-9a-f]{64}$")
        self.assertIn("printf '%s  %s\\n' \"$USAGE_UI_SHA256\"", INSTALLER)

    def test_artifact_relay_defaults_to_two_bounded_workers(self):
        self.assertIn('WORKER_COUNT="${ARTIFACT_RELAY_WORKERS:-2}"', ARTIFACT_INSTALLER)
        self.assertIn("WORKER_COUNT >= 1 && WORKER_COUNT <= 4", ARTIFACT_INSTALLER)
        self.assertIn("ARTIFACT_RELAY_WORKERS=$WORKER_COUNT", ARTIFACT_INSTALLER)
        self.assertNotIn("ARTIFACT_RELAY_WORKERS=1", ARTIFACT_INSTALLER)

    def test_static_ui_and_api_routes_are_separate(self):
        api = INSTALLER.index("location ^~ /usage/api/")
        network_summary = INSTALLER.index("location = /usage/api/v1/server-network {")
        network_history = INSTALLER.index("location = /usage/api/v1/server-network/history {")
        assets = INSTALLER.index("location ^~ /usage/assets/")
        fallback = INSTALLER.index("location ^~ /usage/ {\n        return 404;")

        self.assertLess(api, fallback)
        self.assertLess(network_summary, api)
        self.assertLess(network_history, api)
        self.assertLess(assets, fallback)
        self.assertIn("location = /usage/key-overview", INSTALLER)
        self.assertIn("root $USAGE_UI_ROOT/current;", INSTALLER)
        self.assertIn("try_files \\$uri =404;", INSTALLER)
        self.assertIn("/usage/api/v1/status", INSTALLER)
        self.assertIn("/usage/assets/not-a-real-asset.js", INSTALLER)
        self.assertNotIn(
            "location ^~ /usage/ {\n        proxy_pass http://127.0.0.1:18081;",
            INSTALLER,
        )

    def test_network_monitor_release_asset_is_hash_pinned(self):
        digest = re.search(r'^NETWORK_MONITOR_SHA256="([0-9a-f]{64})"$', INSTALLER, re.MULTILINE)

        self.assertIsNotNone(digest)
        self.assertTrue(NETWORK_MONITOR_PATH.is_file())
        self.assertEqual(
            digest.group(1),
            hashlib.sha256(NETWORK_MONITOR_PATH.read_bytes()).hexdigest(),
        )
        self.assertIn('NETWORK_MONITOR_ASSET="codex-network-monitor.py"', INSTALLER)
        self.assertIn("/releases/download/v${DEPLOY_RELEASE_VERSION}/${NETWORK_MONITOR_ASSET}", INSTALLER)
        self.assertIn("printf '%s  %s\\n' \"$NETWORK_MONITOR_SHA256\"", INSTALLER)
        self.assertIn("python3 -m py_compile \"$WORK_DIR/$NETWORK_MONITOR_ASSET\"", INSTALLER)

    def test_network_monitor_runs_as_a_hardened_loopback_sidecar(self):
        self.assertIn("INTERNAL_PORTS=(18080 18081 18082 18317 18318)", INSTALLER)
        self.assertIn("--home-dir /var/lib/codex-network-monitor", INSTALLER)
        self.assertIn("User=codexnet", INSTALLER)
        self.assertIn("Group=codexnet", INSTALLER)
        self.assertIn("NETWORK_MONITOR_LISTEN_HOST=127.0.0.1", INSTALLER)
        self.assertIn("NETWORK_MONITOR_LISTEN_PORT=18082", INSTALLER)
        self.assertIn("NETWORK_MONITOR_DATABASE=/var/lib/codex-network-monitor/network-monitor.db", INSTALLER)
        self.assertIn("NoNewPrivileges=true", INSTALLER)
        self.assertIn("ProtectSystem=strict", INSTALLER)
        self.assertIn("CapabilityBoundingSet=", INSTALLER)
        self.assertIn("IPAddressDeny=any", INSTALLER)
        self.assertIn("IPAddressAllow=localhost", INSTALLER)
        self.assertIn("ReadWritePaths=/var/lib/codex-network-monitor", INSTALLER)
        self.assertIn("systemctl enable codex-network-monitor", INSTALLER)
        self.assertIn("systemctl restart codex-network-monitor", INSTALLER)

    def test_network_monitor_routes_require_keeper_admin_auth(self):
        self.assertIn("location = /_codex_network_monitor_auth {", INSTALLER)
        self.assertIn("internal;", INSTALLER)
        self.assertIn("proxy_pass http://127.0.0.1:18081/usage/api/v1/status;", INSTALLER)
        self.assertIn("proxy_method GET;", INSTALLER)
        self.assertIn("proxy_set_header Cookie \\$http_cookie;", INSTALLER)
        self.assertIn("proxy_set_header X-CPA-Usage-Keeper-Embed \\$http_x_cpa_usage_keeper_embed;", INSTALLER)
        self.assertIn("proxy_set_header X-CPA-Usage-Keeper-Embed-Session \\$http_x_cpa_usage_keeper_embed_session;", INSTALLER)
        self.assertEqual(INSTALLER.count("auth_request /_codex_network_monitor_auth;"), 2)
        self.assertIn("proxy_pass http://127.0.0.1:18082/summary;", INSTALLER)
        self.assertIn("proxy_pass http://127.0.0.1:18082/history;", INSTALLER)
        self.assertEqual(
            INSTALLER.count('proxy_set_header X-Codex-Network-Token "$NETWORK_MONITOR_INTERNAL_TOKEN";'),
            2,
        )
        summary_block = INSTALLER[
            INSTALLER.index("location = /usage/api/v1/server-network {"):
            INSTALLER.index("location = /usage/api/v1/server-network/history {")
        ]
        history_block = INSTALLER[
            INSTALLER.index("location = /usage/api/v1/server-network/history {"):
            INSTALLER.index("location ^~ /usage/api/")
        ]
        expected_method_guard = r"if (\$request_method !~ ^(GET|HEAD)\$) { return 405; }"
        self.assertIn(expected_method_guard, summary_block)
        self.assertIn(expected_method_guard, history_block)
        self.assertIn('add_header Cache-Control "no-store" always;', summary_block)
        self.assertIn('add_header Cache-Control "no-store" always;', history_block)
        self.assertIn('wait_for_http 401 "$LOCAL_URL/usage/api/v1/server-network"', INSTALLER)
        self.assertIn('wait_for_http 200 "$LOCAL_URL/usage/api/v1/server-network"', INSTALLER)
        self.assertIn("-H 'X-CPA-Usage-Keeper-Request: fetch'", INSTALLER)

    def test_network_monitor_config_is_repairable_and_not_publicly_opened(self):
        for target in (
            "/etc/systemd/system/codex-network-monitor.service",
            "/etc/codex-network-monitor",
            "/opt/codex-network-monitor",
            "/var/lib/codex-network-monitor",
        ):
            self.assertIn(target, INSTALLER)
        self.assertIn("SAVED_NETWORK_MONITOR_INTERNAL_TOKEN", INSTALLER)
        self.assertIn("NETWORK_MONITOR_PORT_CHECKED_MARKER", INSTALLER)
        self.assertIn("SAVED_TRAFFIC_PACKAGE_TOTAL_GB", INSTALLER)
        self.assertIn("TRAFFIC_PACKAGE_TX_OFFSET_BYTES", INSTALLER)
        self.assertIn("PURCHASED_BANDWIDTH_MBPS", INSTALLER)
        self.assertNotRegex(INSTALLER, r"ufw\s+allow\s+18082")
        self.assertNotRegex(INSTALLER, r"iptables[^\n]+18082")

    def test_ui_builder_pins_the_matching_upstream_commit(self):
        self.assertIn(
            'SOURCE_COMMIT="05573ca5aa701786b9ecf1b5af56e3cc31547ca8"',
            UI_BUILDER,
        )
        self.assertIn('SOURCE_VERSION="v1.13.2"', UI_BUILDER)
        self.assertIn("npm --prefix \"$SOURCE_DIR/web\" run test", UI_BUILDER)
        self.assertIn("npm --prefix \"$SOURCE_DIR/web\" run typecheck", UI_BUILDER)
        self.assertIn("gzip -n", UI_BUILDER)
        self.assertNotIn("s|__APP_BASE_PATH__|/usage|g", UI_BUILDER)
        self.assertIn('window.__APP_BASE_PATH__ = "/usage";', UI_BUILDER)
        self.assertIn('<base href="/usage/" />', UI_BUILDER)

    def test_repository_text_does_not_contain_a_public_ipv4_address(self):
        candidates = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            if path.suffix not in {"", ".md", ".py", ".sh", ".yaml", ".yml", ".patch"}:
                continue
            text = path.read_text(errors="ignore")
            for value in re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", text):
                try:
                    address = ipaddress.ip_address(value)
                except ValueError:
                    continue
                if address.is_global:
                    candidates.append((str(path.relative_to(ROOT)), value))

        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
