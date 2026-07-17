import tempfile
import unittest
import zipfile
from copy import copy
from pathlib import Path

from scripts.package_skill import PackageError, build_archive, verify_archive


ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = """---
name: relay-artifacts
description: Transfer generated images and project attachments through a relay.
---

# Relay Artifacts

Use the bundled script for artifact jobs.
"""


class SkillPackageTests(unittest.TestCase):
    def make_skill(self, root: Path) -> Path:
        skill = root / "relay-artifacts"
        (skill / "scripts").mkdir(parents=True)
        (skill / "references").mkdir()
        (skill / "assets").mkdir()
        (skill / "SKILL.md").write_text(SKILL_MD)
        (skill / "scripts" / "relay_artifacts.py").write_text(
            "#!/usr/bin/env python3\nprint('ready')\n"
        )
        (skill / "references" / "api-contract.md").write_text(
            "Use https://relay.example.com/v1 with ${RELAY_API_KEY}.\n"
        )
        (skill / "assets" / "config.example.json").write_text(
            '{"api_key": "REPLACE_WITH_PRIVATE_RELAY_KEY"}\n'
        )
        return skill

    def test_archive_is_reproducible_and_has_one_top_level_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = self.make_skill(root)
            first = root / "first.zip"
            second = root / "second.zip"

            first_digest = build_archive(skill, first)
            second_digest = build_archive(skill, second)

            self.assertEqual(first_digest, second_digest)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            verify_archive(first, skill)
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
            self.assertTrue(all(name.startswith("relay-artifacts/") for name in names))
            self.assertIn("relay-artifacts/SKILL.md", names)

    def test_repository_skill_builds_and_verifies(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "relay-artifacts.zip"
            build_archive(ROOT / "skills" / "relay-artifacts", output)
            verify_archive(output, ROOT / "skills" / "relay-artifacts")

    def test_transient_cache_files_are_not_packaged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = self.make_skill(root)
            cache = skill / "scripts" / "__pycache__"
            cache.mkdir()
            (cache / "relay_artifacts.cpython-312.pyc").write_bytes(b"cache")
            (skill / ".DS_Store").write_bytes(b"cache")
            output = root / "skill.zip"

            build_archive(skill, output)

            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
            self.assertFalse(any("__pycache__" in name for name in names))
            self.assertFalse(any(name.endswith(".DS_Store") for name in names))

    def test_public_ip_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = self.make_skill(root)
            public_address = ".".join(("8", "8", "8", "8"))
            (skill / "references" / "api-contract.md").write_text(
                f"http://{public_address}:8317\n"
            )

            with self.assertRaisesRegex(PackageError, "public IPv4"):
                build_archive(skill, root / "skill.zip")

    def test_literal_credentials_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = self.make_skill(root)
            examples = (
                '{"api_key": "actual-secret-value"}\n',
                '{"api_key": "hunter2"}\n',
                '{"api_key": "ACTUALSECRET"}\n',
                '{"api_key": "ACTUAL_SECRET"}\n',
                '{"input_folder_token": "actual-folder-token"}\n',
            )
            for example in examples:
                with self.subTest(example=example):
                    (skill / "assets" / "config.example.json").write_text(example)
                    with self.assertRaisesRegex(PackageError, "literal credential"):
                        build_archive(skill, root / "skill.zip")

    def test_private_config_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = self.make_skill(root)
            (skill / "config.json").write_text("{}\n")

            with self.assertRaisesRegex(PackageError, "private configuration"):
                build_archive(skill, root / "skill.zip")

    def test_environment_file_variant_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = self.make_skill(root)
            (skill / ".env.local").write_text("RELAY_API_KEY=actual-secret-value\n")

            with self.assertRaisesRegex(PackageError, "private configuration"):
                build_archive(skill, root / "skill.zip")

    def test_literal_python_credential_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = self.make_skill(root)
            (skill / "scripts" / "relay_artifacts.py").write_text(
                'api_key = "actual-secret-value"\n'
            )

            with self.assertRaisesRegex(PackageError, "literal credential"):
                build_archive(skill, root / "skill.zip")

    def test_prefixed_python_credential_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = self.make_skill(root)
            (skill / "scripts" / "relay_artifacts.py").write_text(
                'DEFAULT_API_KEY = "actual-secret-value"\n'
            )

            with self.assertRaisesRegex(PackageError, "literal credential"):
                build_archive(skill, root / "skill.zip")

    def test_case_insensitive_path_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = self.make_skill(root)
            (skill / "skill.md").write_text("collision\n")

            with self.assertRaisesRegex(PackageError, "portable path collision"):
                build_archive(skill, root / "skill.zip")

    def test_trailing_dot_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = self.make_skill(root)
            (skill / "notes.").write_text("non-portable\n")

            with self.assertRaisesRegex(PackageError, "non-portable path component"):
                build_archive(skill, root / "skill.zip")

    def test_archive_with_wrong_top_level_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "wrong-root.zip"
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr("other-skill/SKILL.md", SKILL_MD)

            with self.assertRaisesRegex(PackageError, "missing top-level relay-artifacts"):
                verify_archive(output)

    def test_archive_with_changed_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = self.make_skill(root)
            valid = root / "valid.zip"
            changed = root / "changed.zip"
            build_archive(skill, valid)

            with zipfile.ZipFile(valid) as source:
                members = [(copy(info), source.read(info)) for info in source.infolist()]
            with zipfile.ZipFile(changed, "w") as destination:
                for info, data in members:
                    if info.filename == "relay-artifacts/SKILL.md":
                        info.external_attr = (0o100600 << 16)
                    destination.writestr(info, data)

            with self.assertRaisesRegex(PackageError, "non-reproducible mode"):
                verify_archive(changed)


if __name__ == "__main__":
    unittest.main()
