import ipaddress
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "install-codex-relay.sh").read_text()
BOOTSTRAP = (ROOT / "install.sh").read_text()
IMAGES_INSTALLER = (ROOT / "install-relay-images-skill.sh").read_text()
UI_BUILDER = (ROOT / "usage-ui" / "build-plain-zh.sh").read_text()


class UsageDashboardInstallerTests(unittest.TestCase):
    def test_release_entry_points_are_aligned(self):
        self.assertIn('DEPLOY_RELEASE_VERSION="1.3.0"', INSTALLER)
        self.assertIn('/releases/download/v1.3.0/install-codex-relay.sh', BOOTSTRAP)
        self.assertIn('VERSION="v1.3.0"', IMAGES_INSTALLER)

    def test_plain_chinese_ui_is_versioned_and_hash_pinned(self):
        version = re.search(r'^USAGE_UI_VERSION="([^"]+)"$', INSTALLER, re.MULTILINE)
        digest = re.search(r'^USAGE_UI_SHA256="([^"]+)"$', INSTALLER, re.MULTILINE)

        self.assertIsNotNone(version)
        self.assertEqual(version.group(1), "1.13.2-plain-zh.1")
        self.assertIsNotNone(digest)
        self.assertRegex(digest.group(1), r"^[0-9a-f]{64}$")
        self.assertIn("printf '%s  %s\\n' \"$USAGE_UI_SHA256\"", INSTALLER)

    def test_static_ui_and_api_routes_are_separate(self):
        api = INSTALLER.index("location ^~ /usage/api/")
        assets = INSTALLER.index("location ^~ /usage/assets/")
        fallback = INSTALLER.index("location ^~ /usage/ {\n        return 404;")

        self.assertLess(api, fallback)
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
