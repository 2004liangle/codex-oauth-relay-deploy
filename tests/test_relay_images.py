from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/relay-images/scripts/relay_images.py"
SPEC = importlib.util.spec_from_file_location("relay_images", SCRIPT)
assert SPEC and SPEC.loader
relay_images = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = relay_images
SPEC.loader.exec_module(relay_images)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlO7HQAAAAASUVORK5CYII="
)
PNG_B64 = base64.b64encode(PNG).decode("ascii")
SECRET = "test-secret-value"


def make_rgba_png(width: int, height: int, alpha: list[int]) -> bytes:
    assert len(alpha) == width * height

    def chunk(name: bytes, value: bytes) -> bytes:
        checksum = zlib.crc32(name + value) & 0xFFFFFFFF
        return len(value).to_bytes(4, "big") + name + value + checksum.to_bytes(4, "big")

    rows = bytearray()
    for row in range(height):
        rows.append(0)
        for column in range(width):
            rows.extend((255, 255, 255, alpha[row * width + column]))
    header = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes((8, 6, 0, 0, 0))
    )
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def write_fake_lark_cli(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import shutil
import sys

args = sys.argv[1:]

def value(flag):
    return args[args.index(flag) + 1]

if \"+upload\" in args:
    source = pathlib.Path(value(\"--file\"))
    data = source.read_bytes()
    token = \"boxUploaded\" + hashlib.sha256(data).hexdigest()[:12]
    store = os.environ.get(\"FAKE_LARK_STORE\")
    if store:
        pathlib.Path(store, token).write_bytes(data)
    print(json.dumps({\"ok\": True, \"identity\": value(\"--as\"), \"data\": {\"file_token\": token}}))
elif \"+download\" in args:
    marker = os.environ.get(\"FAKE_LARK_FAIL_ONCE\")
    if marker and not pathlib.Path(marker).exists():
        pathlib.Path(marker).write_text(\"failed\")
        print(json.dumps({\"ok\": False, \"error\": {\"type\": \"network\"}}), file=sys.stderr)
        raise SystemExit(1)
    source = pathlib.Path(os.environ[\"FAKE_LARK_DOWNLOAD\"])
    output = pathlib.Path(value(\"--output\"))
    shutil.copyfile(source, output)
    print(json.dumps({\"ok\": True, \"identity\": value(\"--as\"), \"data\": {\"output\": str(output)}}))
else:
    print(json.dumps({\"ok\": False, \"error\": {\"type\": \"usage\"}}), file=sys.stderr)
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


class FakeRelayHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    artifact_jobs: dict[str, dict[str, object]] = {}

    def log_message(self, format: str, *args: object) -> None:
        return

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)

    def _send_json(self, status: int, value: dict[str, object]) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", "req-test")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.requests.append({"method": "GET", "path": self.path, "authorization": self.headers.get("Authorization")})
        if self.headers.get("Authorization") != f"Bearer {SECRET}":
            self._send_json(401, {"error": {"message": "bad key"}})
        elif self.path == "/v1/models":
            self._send_json(200, {"data": [{"id": "gpt-image-2"}]})
        elif self.path == "/v1/artifact-capabilities":
            self._send_json(
                200,
                {
                    "delivery": "lark_drive",
                    "input_target": {"type": "wiki", "token": "wikiInputToken"},
                    "identity": "bot",
                    "operations": ["image.generate", "image.edit", "attachment.process"],
                    "retention": "manual",
                    "status_values": [
                        "queued",
                        "downloading",
                        "processing",
                        "uploading",
                        "ready_for_processing",
                        "completed",
                        "failed",
                    ],
                },
            )
        elif self.path.startswith("/v1/artifact-jobs/"):
            request_id = self.path.rsplit("/", 1)[-1]
            job = self.artifact_jobs.get(request_id)
            if job is None:
                self._send_json(404, {"error": {"message": "not found"}})
            else:
                completed = dict(job)
                completed["status"] = (
                    "ready_for_processing"
                    if job.get("operation") == "artifact.handoff"
                    else "completed"
                )
                self.artifact_jobs[request_id] = completed
                self._send_json(200, completed)
        else:
            self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        body = self._read_body()
        self.requests.append(
            {
                "method": "POST",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "body": body,
            }
        )
        if self.headers.get("Authorization") != f"Bearer {SECRET}":
            self._send_json(401, {"error": {"message": "bad key"}})
            return
        if self.path == "/v1/images/generations":
            request = json.loads(body)
            if not request.get("prompt"):
                self._send_json(400, {"error": {"message": "prompt is required"}})
                return
            self._send_json(200, {"data": [{"b64_json": PNG_B64}], "usage": {"total_tokens": 1}})
            return
        if self.path == "/v1/images/edits":
            if b'name="image[]"' not in body:
                self._send_json(400, {"error": {"message": "image is required"}})
                return
            self._send_json(200, {"data": [{"b64_json": PNG_B64}]})
            return
        if self.path == "/v1/artifact-jobs":
            request = json.loads(body)
            request_id = request["request_id"]
            job = {
                "request_id": request_id,
                "operation": request["operation"],
                "status": "queued",
                "created_at": "2026-07-17T00:00:00Z",
                "updated_at": "2026-07-17T00:00:00Z",
                "inputs": request.get("inputs", []),
                "outputs": [
                    {
                        "file_token": "boxOutputToken",
                        "name": "result.png",
                        "mime_type": "image/png",
                        "size_bytes": len(PNG),
                        "sha256": hashlib.sha256(PNG).hexdigest(),
                    }
                ],
                "error": None,
            }
            self.artifact_jobs[request_id] = job
            self._send_json(202, job)
            return
        self._send_json(404, {"error": {"message": "not found"}})


class RelayServer:
    def __enter__(self) -> "RelayServer":
        FakeRelayHandler.requests = []
        FakeRelayHandler.artifact_jobs = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRelayHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"


class ValidationTests(unittest.TestCase):
    def test_url_normalization_and_http_guard(self) -> None:
        value, parsed = relay_images.normalize_base_url("http://127.0.0.1:8317", False)
        self.assertEqual(value, "http://127.0.0.1:8317/v1")
        self.assertEqual(parsed.path, "/v1")
        with self.assertRaises(relay_images.RelayError):
            relay_images.normalize_base_url("http://example.com/v1", False)
        with self.assertRaises(relay_images.RelayError):
            relay_images.normalize_base_url("https://user:pass@example.com/v1", False)
        with self.assertRaises(relay_images.RelayError):
            relay_images.normalize_base_url("https://example.com/v1?key=value", False)
        with self.assertRaises(relay_images.RelayError):
            relay_images.normalize_base_url("https://example.com:notaport/v1", False)

    def test_secret_config_permissions_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text("{}", encoding="utf-8")
            config.chmod(0o644)
            with self.assertRaises(relay_images.RelayError):
                relay_images.read_config(config)

            private = Path(directory) / "private" / "config.json"
            relay_images.atomic_config_write(private, {"api_key": SECRET})
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(private.parent.stat().st_mode), 0o700)
            self.assertEqual(relay_images.read_config(private)["api_key"], SECRET)

            link = Path(directory) / "link.json"
            link.symlink_to(private)
            with self.assertRaises(relay_images.RelayError):
                relay_images.read_config(link)

    def test_size_validation(self) -> None:
        self.assertEqual(relay_images.validate_size("1536x1024"), "1536x1024")
        self.assertEqual(relay_images.validate_size("auto"), "auto")
        for value in ("1000x1000", "4096x1024", "3840x1024", "256x256"):
            with self.subTest(value=value), self.assertRaises(relay_images.RelayError):
                relay_images.validate_size(value)

    def test_decode_rejects_bad_base64_and_format(self) -> None:
        data, fmt = relay_images.decode_image(PNG_B64)
        self.assertEqual(data, PNG)
        self.assertEqual(fmt, "png")
        self.assertEqual(relay_images.image_dimensions(data, fmt), (1, 1))
        with self.assertRaises(relay_images.RelayError):
            relay_images.decode_image("not-base64")
        with self.assertRaises(relay_images.RelayError):
            relay_images.decode_image(base64.b64encode(b"plain text").decode("ascii"))

    def test_atomic_write_is_private_and_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "image.png"
            relay_images.atomic_write(target, PNG, False)
            self.assertEqual(target.read_bytes(), PNG)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            with self.assertRaises(relay_images.RelayError):
                relay_images.atomic_write(target, b"replacement", False)

    def test_sse_parser(self) -> None:
        stream = io.BytesIO(
            b"event: image_generation.completed\n"
            + f'data: {{"type":"image_generation.completed","b64_json":"{PNG_B64}"}}\n\n'.encode()
        )
        events = list(relay_images.sse_events(stream))
        self.assertEqual(events[0]["type"], "image_generation.completed")
        self.assertEqual(events[0]["b64_json"], PNG_B64)

        valid = [
            {"type": "image_generation.partial_image", "b64_json": PNG_B64},
            {"type": "image_generation.completed", "b64_json": PNG_B64},
        ]
        partials, final, _ = relay_images.collect_image_stream(valid, 1)
        self.assertEqual(len(partials), 1)
        self.assertEqual(final["encoded"], PNG_B64)
        with self.assertRaises(relay_images.RelayError):
            relay_images.collect_image_stream(valid, 0)
        with self.assertRaises(relay_images.RelayError):
            relay_images.collect_image_stream(
                valid + [{"type": "image_generation.completed", "b64_json": PNG_B64}],
                1,
            )

    def test_dimension_mismatch_warning(self) -> None:
        stderr = io.StringIO()
        original = relay_images.sys.stderr
        try:
            relay_images.sys.stderr = stderr
            relay_images.warn_dimension_mismatch("1024x1024", [{"width": 1254, "height": 1254}])
        finally:
            relay_images.sys.stderr = original
        self.assertIn("requested 1024x1024", stderr.getvalue())
        self.assertIn("1254x1254", stderr.getvalue())

    def test_format_mismatch_warning(self) -> None:
        stderr = io.StringIO()
        original = relay_images.sys.stderr
        try:
            relay_images.sys.stderr = stderr
            relay_images.warn_format_mismatch("jpeg", [{"format": "png"}])
        finally:
            relay_images.sys.stderr = original
        self.assertIn("requested jpeg", stderr.getvalue())
        self.assertIn("returned png", stderr.getvalue())

    def test_error_text_never_echoes_gateway_content(self) -> None:
        leaked = b"Authorization: Bearer leaked-secret data:image/png;base64,AAAA"
        self.assertEqual(relay_images.clean_error_text(leaked), "non-JSON response")
        value = json.dumps({"error": {"message": leaked.decode()}}).encode()
        self.assertEqual(relay_images.clean_error_text(value), "request rejected")
        value = json.dumps({"error": {"code": SECRET, "type": SECRET}}).encode()
        self.assertEqual(relay_images.clean_error_text(value), "request rejected")
        digest = relay_images.request_id_digest({"x-request-id": SECRET})
        self.assertIsNotNone(digest)
        self.assertNotIn(SECRET, digest or "")
        usage = relay_images.sanitized_usage(
            {"total_tokens": 3, "secret": SECRET, "input_tokens_details": {"text_tokens": 2}}
        )
        self.assertEqual(
            usage,
            {"total_tokens": 3, "input_tokens_details": {"text_tokens": 2}},
        )

    def test_input_count_mask_and_file_size_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            mask = Path(directory) / "mask.png"
            source.write_bytes(make_rgba_png(2, 1, [255, 255]))
            mask.write_bytes(make_rgba_png(2, 1, [0, 255]))
            files = relay_images.validate_edit_files([str(source)], str(mask))
            self.assertEqual([item.field for item in files], ["image[]", "mask"])
            with self.assertRaises(relay_images.RelayError):
                relay_images.validate_edit_files([str(source)] * 17, None)

            too_large = Path(directory) / "too-large.png"
            with too_large.open("wb") as handle:
                handle.write(PNG[:32])
                handle.truncate(relay_images.MAX_INPUT_BYTES)
            with self.assertRaises(relay_images.RelayError):
                relay_images.detect_input(str(too_large))

            mask.write_bytes(make_rgba_png(2, 1, [255, 255]))
            with self.assertRaises(relay_images.RelayError):
                relay_images.validate_edit_files([str(source)], str(mask))

            corrupt = bytearray(make_rgba_png(2, 1, [0, 255]))
            corrupt[-5] ^= 1
            mask.write_bytes(corrupt)
            with self.assertRaises(relay_images.RelayError):
                relay_images.validate_edit_files([str(source)], str(mask))

    def test_aggregate_multipart_limit(self) -> None:
        body = relay_images.MultipartBody({"prompt": "test"}, [])
        original = relay_images.MAX_EDIT_REQUEST_BYTES
        try:
            relay_images.MAX_EDIT_REQUEST_BYTES = len(body) - 1
            with self.assertRaises(relay_images.RelayError):
                relay_images.validate_edit_body_size(body)
        finally:
            relay_images.MAX_EDIT_REQUEST_BYTES = original

    def test_input_bytes_are_snapshotted_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            original = make_rgba_png(2, 1, [255, 255])
            replacement = make_rgba_png(3, 1, [255, 255, 255])
            source.write_bytes(original)
            files = relay_images.validate_edit_files([str(source)], None)
            source.write_bytes(replacement)
            body = relay_images.MultipartBody({"prompt": "test"}, files)
            uploaded = b"".join(body)
            self.assertIn(original, uploaded)
            self.assertNotIn(replacement, uploaded)

    def test_artifact_manifest_and_names_are_validated(self) -> None:
        self.assertEqual(relay_images.safe_artifact_name("../../bad\\name.png"), ".._.._bad_name.png")
        item = relay_images.normalized_artifact_entry(
            {
                "file_token": "boxValidToken",
                "name": "result.png",
                "mime_type": "image/png",
                "size_bytes": len(PNG),
                "sha256": hashlib.sha256(PNG).hexdigest().upper(),
            }
        )
        self.assertEqual(item["sha256"], hashlib.sha256(PNG).hexdigest())
        for field, value in (
            ("file_token", "../bad"),
            ("mime_type", "bad"),
            ("size_bytes", 0),
            ("sha256", "bad"),
        ):
            invalid = dict(item)
            invalid[field] = value
            with self.subTest(field=field), self.assertRaises(relay_images.RelayError):
                relay_images.normalized_artifact_entry(invalid)

    def test_artifact_job_schema_is_strict(self) -> None:
        job = relay_images.parse_artifact_job(
            {"request_id": "job-valid", "status": "queued"}, "job-valid"
        )
        self.assertEqual(job["status"], "queued")
        with self.assertRaises(relay_images.RelayError):
            relay_images.parse_artifact_job(
                {"request_id": "job-valid", "status": "mystery"}, "job-valid"
            )
        with self.assertRaises(relay_images.RelayError):
            relay_images.validate_request_id("../escape")
        with self.assertRaises(relay_images.RelayError):
            relay_images.validate_request_id("short")

    def test_lark_error_is_redacted_and_retryable(self) -> None:
        secret = "sensitive-upstream-message"
        stderr = json.dumps(
            {"ok": False, "error": {"type": "network", "message": secret}}
        )
        with self.assertRaises(relay_images.LarkCliError) as raised:
            relay_images.parse_lark_json("", stderr, 1)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn(secret, str(raised.exception))

    def test_explicit_lark_target_overrides_stored_opposite_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            lark_cli = root / "lark-cli"
            write_fake_lark_cli(lark_cli)
            relay_images.atomic_config_write(
                config,
                {
                    "lark_cli": str(lark_cli),
                    "lark_identity": "bot",
                    "lark_folder_token": "folderStoredToken",
                },
            )
            args = relay_images.argparse.Namespace(
                config=str(config),
                lark_cli=None,
                lark_identity=None,
                lark_profile=None,
                folder_token=None,
                wiki_token="wikiExplicitToken",
            )
            resolved = relay_images.resolve_lark_config(args, require_target=True)
            self.assertEqual(resolved.target_type, "wiki")
            self.assertEqual(resolved.target_token, "wikiExplicitToken")


class CliIntegrationTests(unittest.TestCase):
    def run_cli(
        self,
        base_url: str,
        *args: str,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["CODEX_RELAY_BASE_URL"] = base_url
        env["CODEX_RELAY_API_KEY"] = SECRET
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["python3", str(SCRIPT), *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

    def test_check_generate_and_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory, RelayServer() as relay:
            check = self.run_cli(relay.base_url, "check")
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertTrue(json.loads(check.stdout)["ok"])

            generated = Path(directory) / "generated.png"
            create = self.run_cli(
                relay.base_url,
                "generate",
                "--prompt",
                "A blue cup",
                "--output",
                str(generated),
            )
            self.assertEqual(create.returncode, 0, create.stderr)
            self.assertEqual(generated.read_bytes(), PNG)
            create_summary = json.loads(create.stdout)
            self.assertEqual(
                create_summary["request_id_sha256"],
                relay_images.request_id_digest({"x-request-id": "req-test"}),
            )
            self.assertEqual(create_summary["images"][0]["width"], 1)
            self.assertEqual(create_summary["images"][0]["height"], 1)

            source = Path(directory) / "source.png"
            source.write_bytes(PNG)
            edited = Path(directory) / "edited.png"
            edit = self.run_cli(
                relay.base_url,
                "edit",
                "--image",
                str(source),
                "--prompt",
                "Make it blue",
                "--output",
                str(edited),
            )
            self.assertEqual(edit.returncode, 0, edit.stderr)
            self.assertEqual(edited.read_bytes(), PNG)

            self.assertNotIn(SECRET, check.stdout + check.stderr + create.stdout + create.stderr + edit.stdout + edit.stderr)
            paths = [item["path"] for item in FakeRelayHandler.requests]
            self.assertIn("/v1/images/generations", paths)
            self.assertIn("/v1/images/edits", paths)
            self.assertNotIn("/v1/files", paths)

    def test_invalid_option_combinations(self) -> None:
        with RelayServer() as relay:
            cases = [
                ("--format", "png", "--compression", "80"),
                ("--background", "transparent"),
                ("--n", "11"),
                ("--partial-images", "4"),
                ("--model", "gpt-image-1.5"),
            ]
            for options in cases:
                with self.subTest(options=options):
                    result = self.run_cli(
                        relay.base_url,
                        "generate",
                        "--prompt",
                        "test",
                        "--dry-run",
                        *options,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_dry_run_does_not_read_config_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "unreadable.json"
            config.write_text(json.dumps({"base_url": "http://example.com/v1", "api_key": SECRET}))
            config.chmod(0)
            output = Path(directory) / "planned.png"
            env = os.environ.copy()
            env.pop("CODEX_RELAY_BASE_URL", None)
            env.pop("CODEX_RELAY_API_KEY", None)
            result = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--config",
                    str(config),
                    "generate",
                    "--prompt",
                    "A blue cup",
                    "--output",
                    str(output),
                    "--dry-run",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["output"]["final_paths"], [str(output)])
            self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_live_preflight_blocks_existing_output_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory, RelayServer() as relay:
            output = Path(directory) / "existing.png"
            output.write_bytes(PNG)
            request_count = len(FakeRelayHandler.requests)
            result = self.run_cli(
                relay.base_url,
                "generate",
                "--prompt",
                "A blue cup",
                "--output",
                str(output),
            )
            self.assertEqual(result.returncode, relay_images.EXIT_FILESYSTEM)
            self.assertEqual(len(FakeRelayHandler.requests), request_count)
            self.assertEqual(output.read_bytes(), PNG)

    def test_strict_output_saves_mismatch_and_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory, RelayServer() as relay:
            requested = Path(directory) / "strict.webp"
            result = self.run_cli(
                relay.base_url,
                "generate",
                "--prompt",
                "A blue cup",
                "--format",
                "webp",
                "--strict-output",
                "--output",
                str(requested),
            )
            self.assertEqual(result.returncode, relay_images.EXIT_RESPONSE, result.stderr)
            summary = json.loads(result.stdout)
            self.assertFalse(summary["ok"])
            self.assertFalse(summary["output_contract_met"])
            actual = Path(summary["images"][0]["path"])
            self.assertEqual(actual.suffix, ".png")
            self.assertEqual(actual.read_bytes(), PNG)

    def test_artifact_generate_polls_downloads_retries_and_commits_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory, RelayServer() as relay:
            root = Path(directory)
            lark = root / "lark-cli"
            remote = root / "remote.png"
            marker = root / "failed-once"
            output = root / "generated.png"
            write_fake_lark_cli(lark)
            remote.write_bytes(PNG)
            result = self.run_cli(
                relay.base_url,
                "artifact-generate",
                "--prompt",
                "A blue cup",
                "--request-id",
                "job-generate-1",
                "--output",
                str(output),
                "--lark-cli",
                str(lark),
                "--poll-interval",
                "0.01",
                "--wait-timeout",
                "5",
                "--timeout",
                "2",
                extra_env={
                    "FAKE_LARK_DOWNLOAD": str(remote),
                    "FAKE_LARK_FAIL_ONCE": str(marker),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_bytes(), PNG)
            self.assertTrue(marker.exists())
            self.assertFalse((root / ".generated.png.part").exists())
            summary = json.loads(result.stdout)
            self.assertEqual(summary["delivery"], "lark_drive")
            self.assertEqual(summary["request_id"], "job-generate-1")
            self.assertEqual(summary["images"][0]["sha256"], hashlib.sha256(PNG).hexdigest())
            paths = [item["path"] for item in FakeRelayHandler.requests]
            self.assertIn("/v1/artifact-capabilities", paths)
            self.assertIn("/v1/artifact-jobs", paths)
            self.assertIn("/v1/artifact-jobs/job-generate-1", paths)
            self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_artifact_edit_uploads_validated_input_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory, RelayServer() as relay:
            root = Path(directory)
            lark = root / "lark-cli"
            remote = root / "remote.png"
            source = root / "source.png"
            output = root / "edited.png"
            store = root / "store"
            store.mkdir()
            write_fake_lark_cli(lark)
            remote.write_bytes(PNG)
            source.write_bytes(PNG)
            result = self.run_cli(
                relay.base_url,
                "artifact-edit",
                "--image",
                str(source),
                "--prompt",
                "Make it blue",
                "--request-id",
                "job-edit-1",
                "--output",
                str(output),
                "--lark-cli",
                str(lark),
                "--poll-interval",
                "0.01",
                "--wait-timeout",
                "5",
                "--timeout",
                "2",
                extra_env={
                    "FAKE_LARK_DOWNLOAD": str(remote),
                    "FAKE_LARK_STORE": str(store),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_bytes(), PNG)
            artifact_posts = [
                item
                for item in FakeRelayHandler.requests
                if item["method"] == "POST" and item["path"] == "/v1/artifact-jobs"
            ]
            self.assertEqual(len(artifact_posts), 1)
            payload = json.loads(artifact_posts[0]["body"])
            self.assertEqual(payload["operation"], "image.edit")
            self.assertEqual(payload["inputs"][0]["role"], "image")
            self.assertEqual(payload["inputs"][0]["size_bytes"], len(PNG))
            self.assertEqual(payload["inputs"][0]["sha256"], hashlib.sha256(PNG).hexdigest())
            uploaded = list(store.iterdir())
            self.assertEqual(len(uploaded), 1)
            self.assertEqual(uploaded[0].read_bytes(), PNG)

    def test_artifact_dry_run_needs_no_relay_or_lark_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "planned.png"
            env = os.environ.copy()
            env.pop("CODEX_RELAY_BASE_URL", None)
            env.pop("CODEX_RELAY_API_KEY", None)
            result = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "artifact-generate",
                    "--prompt",
                    "A blue cup",
                    "--request-id",
                    "job-dry-run",
                    "--output",
                    str(output),
                    "--dry-run",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["payload"]["request_id"], "job-dry-run")
            self.assertFalse(output.exists())

    def test_live_artifact_failure_always_reports_request_id(self) -> None:
        result = self.run_cli(
            "http://127.0.0.1:1/v1",
            "artifact-generate",
            "--prompt",
            "A blue cup",
            "--request-id",
            "job-visible-1",
            "--request-retries",
            "0",
            "--timeout",
            "0.2",
        )
        self.assertEqual(result.returncode, relay_images.EXIT_NETWORK)
        self.assertIn("artifact request_id: job-visible-1", result.stderr)
        self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_artifact_handoff_returns_ready_for_processing(self) -> None:
        with tempfile.TemporaryDirectory() as directory, RelayServer() as relay:
            root = Path(directory)
            lark = root / "lark-cli"
            source = root / "report.txt"
            store = root / "store"
            store.mkdir()
            write_fake_lark_cli(lark)
            source.write_text("quarterly report", encoding="utf-8")
            result = self.run_cli(
                relay.base_url,
                "artifact-handoff",
                "--file",
                str(source),
                "--instruction",
                "Summarize this report",
                "--request-id",
                "job-handoff-1",
                "--lark-cli",
                str(lark),
                "--poll-interval",
                "0.01",
                "--wait-timeout",
                "5",
                "--timeout",
                "2",
                extra_env={"FAKE_LARK_STORE": str(store)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["status"], "ready_for_processing")
            artifact_posts = [
                item
                for item in FakeRelayHandler.requests
                if item["method"] == "POST" and item["path"] == "/v1/artifact-jobs"
            ]
            payload = json.loads(artifact_posts[0]["body"])
            self.assertEqual(payload["operation"], "artifact.handoff")
            self.assertEqual(payload["parameters"]["instruction"], "Summarize this report")
            self.assertEqual(len(payload["inputs"]), 1)

    def test_artifact_configure_preserves_relay_secret_without_echoing_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            lark = root / "lark-cli"
            token = "wikiPrivateTarget"
            write_fake_lark_cli(lark)
            relay_images.atomic_config_write(
                config,
                {"base_url": "https://relay.example/v1", "api_key": SECRET},
            )
            result = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--config",
                    str(config),
                    "artifact-configure",
                    "--lark-cli",
                    str(lark),
                    "--lark-as",
                    "bot",
                    "--wiki-token",
                    token,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            stored = relay_images.read_config(config)
            self.assertEqual(stored["api_key"], SECRET)
            self.assertEqual(stored["lark_wiki_token"], token)
            self.assertNotIn(SECRET, result.stdout + result.stderr)
            self.assertNotIn(token, result.stdout + result.stderr)

    def test_download_rejects_bad_manifest_hash_and_leaves_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lark_cli = root / "lark-cli"
            remote = root / "remote.png"
            part = root / ".result.png.part"
            write_fake_lark_cli(lark_cli)
            remote.write_bytes(PNG)
            old = os.environ.get("FAKE_LARK_DOWNLOAD")
            os.environ["FAKE_LARK_DOWNLOAD"] = str(remote)
            try:
                lark = relay_images.LarkConfig(str(lark_cli), "bot", None, None, None)
                entry = {
                    "file_token": "boxOutputToken",
                    "name": "result.png",
                    "mime_type": "image/png",
                    "size_bytes": len(PNG),
                    "sha256": "0" * 64,
                }
                with self.assertRaises(relay_images.RelayError):
                    relay_images.download_artifact_part(lark, entry, part, 2, 1)
                self.assertFalse(part.exists())
            finally:
                if old is None:
                    os.environ.pop("FAKE_LARK_DOWNLOAD", None)
                else:
                    os.environ["FAKE_LARK_DOWNLOAD"] = old


if __name__ == "__main__":
    unittest.main()
