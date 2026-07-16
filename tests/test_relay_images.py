from __future__ import annotations

import base64
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


class FakeRelayHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

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
        self._send_json(404, {"error": {"message": "not found"}})


class RelayServer:
    def __enter__(self) -> "RelayServer":
        FakeRelayHandler.requests = []
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


class CliIntegrationTests(unittest.TestCase):
    def run_cli(self, base_url: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["CODEX_RELAY_BASE_URL"] = base_url
        env["CODEX_RELAY_API_KEY"] = SECRET
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


if __name__ == "__main__":
    unittest.main()
