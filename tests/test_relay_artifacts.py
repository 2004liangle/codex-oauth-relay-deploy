import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "skills" / "relay-artifacts" / "scripts" / "relay_artifacts.py"
SPEC = importlib.util.spec_from_file_location("relay_artifacts_portable", MODULE_PATH)
relay_artifacts = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = relay_artifacts
SPEC.loader.exec_module(relay_artifacts)


class HostToolsContractTests(unittest.TestCase):
    @staticmethod
    def transparent_generate_args():
        return SimpleNamespace(
            model="gpt-image-2",
            quality="high",
            size="1024x1024",
            output_format="png",
            n=1,
            compression=None,
            background="transparent",
            cutout_model="isnet-anime",
            moderation="auto",
            output_name="character.png",
            prompt="只保留人物",
            prompt_file=None,
            request_id="portable-transparent-01",
            wait=False,
            wait_timeout=60,
            poll_interval=1,
            download_dir=None,
            overwrite=False,
        )

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

    def test_transparent_cutout_options_are_forwarded_to_the_artifact_service(self):
        args = SimpleNamespace(
            model="gpt-image-2",
            quality="high",
            size="1024x1024",
            output_format="png",
            n=1,
            compression=None,
            background="transparent",
            cutout_model="isnet-anime",
            moderation="auto",
            output_name="character.png",
        )
        parameters = relay_artifacts.image_parameters(args, "只保留人物")
        self.assertEqual(parameters["background"], "transparent")
        self.assertEqual(parameters["background_removal_model"], "isnet-anime")

    def test_transparent_cutout_rejects_non_png_output(self):
        args = SimpleNamespace(
            model="gpt-image-2",
            quality="high",
            size="1024x1024",
            output_format="webp",
            n=1,
            compression=None,
            background="transparent",
            cutout_model=None,
            moderation="auto",
            output_name=None,
        )
        with self.assertRaises(relay_artifacts.ToolError):
            relay_artifacts.image_parameters(args, "只保留人物")

    def test_transparent_job_requires_server_side_alpha_validation(self):
        parameters = {
            "background": "transparent",
            "output_format": "png",
            "background_removal_model": "isnet-anime",
        }
        with self.assertRaises(relay_artifacts.ToolError) as caught:
            relay_artifacts.check_transparent_output({}, parameters)
        self.assertEqual(caught.exception.code, "transparent_output_unsupported")

        capabilities = {
            "transparent_output": {
                "format": "png",
                "models": ["isnet-general-use", "isnet-anime"],
                "default_model": "isnet-general-use",
                "alpha_validation": True,
            }
        }
        relay_artifacts.check_transparent_output(capabilities, parameters)

    def test_transparent_job_rejects_unadvertised_cutout_model(self):
        capabilities = {
            "transparent_output": {
                "format": "png",
                "models": ["isnet-general-use"],
                "default_model": "isnet-general-use",
                "alpha_validation": True,
            }
        }
        with self.assertRaises(relay_artifacts.ToolError) as caught:
            relay_artifacts.check_transparent_output(
                capabilities,
                {
                    "background": "transparent",
                    "background_removal_model": "isnet-anime",
                },
            )
        self.assertEqual(caught.exception.code, "unsupported_cutout_model")

    def test_generate_checks_transparent_capability_before_submit(self):
        class FakeRelay:
            def __init__(self, capabilities):
                self._capabilities = capabilities
                self.submissions = []

            def capabilities(self):
                return self._capabilities

            def submit(self, payload):
                self.submissions.append(payload)
                return {"request_id": payload["request_id"], "status": "queued"}

        supported = {
            "operations": ["image.generate"],
            "transparent_output": {
                "format": "png",
                "models": ["isnet-general-use", "isnet-anime"],
                "default_model": "isnet-general-use",
                "alpha_validation": True,
            },
        }
        relay = FakeRelay(supported)
        with mock.patch.object(relay_artifacts, "load_config", return_value=object()), mock.patch.object(
            relay_artifacts, "RelayClient", return_value=relay
        ):
            relay_artifacts.command_generate(self.transparent_generate_args())
        self.assertEqual(len(relay.submissions), 1)
        self.assertEqual(
            relay.submissions[0]["parameters"]["background_removal_model"],
            "isnet-anime",
        )

        old_relay = FakeRelay({"operations": ["image.generate"]})
        with mock.patch.object(relay_artifacts, "load_config", return_value=object()), mock.patch.object(
            relay_artifacts, "RelayClient", return_value=old_relay
        ), self.assertRaises(relay_artifacts.ToolError):
            relay_artifacts.command_generate(self.transparent_generate_args())
        self.assertEqual(old_relay.submissions, [])


if __name__ == "__main__":
    unittest.main()
