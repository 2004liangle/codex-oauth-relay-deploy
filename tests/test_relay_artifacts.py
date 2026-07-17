import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "skills" / "relay-artifacts" / "scripts" / "relay_artifacts.py"
SPEC = importlib.util.spec_from_file_location("relay_artifacts_portable", MODULE_PATH)
relay_artifacts = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = relay_artifacts
SPEC.loader.exec_module(relay_artifacts)


class HostToolsContractTests(unittest.TestCase):
    def test_manifest_output_can_be_submitted_without_manual_json_rewriting(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "report.txt"
            source.write_text("portable host-tools contract", encoding="utf-8")
            envelope = relay_artifacts.command_manifest(
                SimpleNamespace(
                    file=str(source),
                    file_token="REMOTE_FILE_TOKEN",
                    role="attachment",
                )
            )

        manifests = relay_artifacts.input_manifests(
            [json.dumps(envelope)],
            allowed_roles=("attachment",),
            default_role="attachment",
        )

        self.assertEqual(len(manifests), 1)
        self.assertEqual(manifests[0]["file_token"], "REMOTE_FILE_TOKEN")
        self.assertEqual(manifests[0]["name"], "report.txt")
        self.assertEqual(manifests[0]["role"], "attachment")


if __name__ == "__main__":
    unittest.main()
