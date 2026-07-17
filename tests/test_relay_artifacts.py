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
    def test_skill_docs_and_command_help_are_simplified_chinese(self):
        skill_text = (ROOT / "skills" / "relay-artifacts" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        reference_text = (
            ROOT / "skills" / "relay-artifacts" / "references" / "api-contract.md"
        ).read_text(encoding="utf-8")
        help_text = relay_artifacts.build_parser().format_help()

        self.assertIn("# 飞书中转图片与附件", skill_text)
        self.assertIn("description: 通过已配置的", skill_text)
        self.assertIn("# 中转文件接口说明", reference_text)
        self.assertIn("通过飞书云盘中转图片和附件任务", help_text)
        self.assertIn("用法:", help_text)
        self.assertIn("选项", help_text)
        self.assertNotIn("# Relay Artifacts", skill_text)
        self.assertNotIn("usage:", help_text)
        self.assertNotIn("self-test", help_text)

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
