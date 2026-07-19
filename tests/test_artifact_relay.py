import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "artifact-relay" / "artifact_relay.py"
SPEC = importlib.util.spec_from_file_location("artifact_relay", MODULE_PATH)
artifact_relay = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = artifact_relay
SPEC.loader.exec_module(artifact_relay)


PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def input_manifest(token, name, data, role=None, mime_type="image/png"):
    result = {
        "file_token": token,
        "name": name,
        "mime_type": mime_type,
        "size_bytes": len(data),
        "sha256": digest(data),
    }
    if role:
        result["role"] = role
    return result


class FakeDrive:
    def __init__(self, files=None):
        self.files = files or {}
        self.downloads = []
        self.uploads = []

    def download(self, manifest, destination, job_root):
        self.downloads.append((dict(manifest), destination, job_root))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.files[manifest["file_token"]])

    def upload(self, path, job_root):
        self.uploads.append((path.name, path.read_bytes(), job_root))
        return f"uploadedtoken{len(self.uploads):04d}"


class FakeImageBackend:
    def __init__(self):
        self.generate_calls = []
        self.edit_calls = []
        self.cutout_calls = []

    def generate(self, parameters, output_dir, request_id):
        self.generate_calls.append((dict(parameters), request_id))
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / "generated.png"
        target.write_bytes(PNG)
        return [target]

    def edit(self, parameters, inputs, output_dir, request_id):
        self.edit_calls.append((dict(parameters), list(inputs), request_id))
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / "edited.png"
        target.write_bytes(PNG + b"-edited")
        return [target]

    def cutout(self, parameters, inputs, output_dir, request_id):
        self.cutout_calls.append((dict(parameters), list(inputs), request_id))
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / "cutout.png"
        target.write_bytes(PNG + b"-cutout")
        return [target]


class ArtifactRelayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = artifact_relay.Config(
            api_key="test-relay-key",
            state_dir=self.root / "state",
            upstream_base_url="http://127.0.0.1:9/v1",
            upstream_api_key="test-relay-key",
            lark_cli="lark-cli",
            lark_home=self.root,
            lark_identity="bot",
            input_target_type="folder",
            input_target_token="inputfoldertoken",
            output_target_type="folder",
            output_target_token="secretoutputfoldertoken",
        )
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.close()
        self.temporary.cleanup()

    def service(self, drive=None, backend=None, start_workers=True):
        service = artifact_relay.ArtifactService(
            self.config,
            drive=drive or FakeDrive(),
            image_backend=backend or FakeImageBackend(),
            start_workers=start_workers,
        )
        self.services.append(service)
        return service

    def wait_for_status(self, service, request_id, statuses, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = service.get(request_id)
            if job["status"] in statuses:
                return job
            time.sleep(0.01)
        self.fail(f"job {request_id} did not reach {statuses}: {service.get(request_id)}")

    def generation_request(self, request_id="generate01"):
        return {
            "request_id": request_id,
            "operation": "image.generate",
            "parameters": {"model": "gpt-image-2", "prompt": "test", "n": 1},
            "inputs": [],
        }

    def test_default_worker_count_is_two(self):
        self.assertEqual(self.config.worker_count, 2)

    def test_transparent_output_requires_png(self):
        request = self.generation_request("transparent-format")
        request["parameters"].update({"background": "transparent", "output_format": "webp"})
        with self.assertRaises(artifact_relay.ApiError) as caught:
            artifact_relay.validate_request(request, self.config.max_input_bytes)
        self.assertEqual(caught.exception.code, "invalid_parameters")

    def test_cutout_model_requires_transparent_background(self):
        request = self.generation_request("cutout-model")
        request["parameters"]["background_removal_model"] = "isnet-anime"
        with self.assertRaises(artifact_relay.ApiError) as caught:
            artifact_relay.validate_request(request, self.config.max_input_bytes)
        self.assertEqual(caught.exception.code, "invalid_parameters")

    def test_unhashable_background_options_are_rejected_cleanly(self):
        for name in ("background", "background_removal_model"):
            request = self.generation_request(f"bad-{name.replace('_', '-')}")
            request["parameters"][name] = ["transparent"]
            with self.subTest(name=name), self.assertRaises(artifact_relay.ApiError) as caught:
                artifact_relay.validate_request(request, self.config.max_input_bytes)
            self.assertEqual(caught.exception.code, "invalid_parameters")

    def test_capabilities_exposes_only_input_target_and_manual_retention(self):
        service = self.service(start_workers=False)
        value = service.capabilities()
        self.assertEqual(value["protocol_version"], "1.0")
        self.assertEqual(value["delivery"], "lark_drive")
        self.assertEqual(value["input_target"], {"type": "folder", "token": "inputfoldertoken"})
        self.assertEqual(value["identity"], "bot")
        self.assertEqual(value["retention"], "manual")
        self.assertEqual(value["transparent_output"]["format"], "png")
        self.assertTrue(value["transparent_output"]["alpha_validation"])
        self.assertEqual(value["cutout"]["provider"], "dreamina_agent")
        self.assertEqual(value["cutout"]["concurrency"], 1)
        self.assertIn("image.cutout", value["operations"])
        self.assertIn("artifact.handoff", value["operations"])
        self.assertNotIn("secretoutputfoldertoken", json.dumps(value))

    def test_cutout_requires_one_image_and_only_accepts_output_name(self):
        source = PNG + b"-source"
        request = {
            "request_id": "cutout-contract-01",
            "operation": "image.cutout",
            "parameters": {"output_name": "person.png"},
            "inputs": [input_manifest("cutouttoken01", "source.png", source, "image")],
        }
        validated = artifact_relay.validate_request(request, self.config.max_input_bytes)
        self.assertEqual(validated["operation"], "image.cutout")
        request["parameters"]["prompt"] = "different prompt"
        with self.assertRaises(artifact_relay.ApiError) as caught:
            artifact_relay.validate_request(request, self.config.max_input_bytes)
        self.assertEqual(caught.exception.code, "invalid_parameters")

        request["parameters"] = {}
        request["inputs"] = []
        with self.assertRaises(artifact_relay.ApiError) as caught:
            artifact_relay.validate_request(request, self.config.max_input_bytes)
        self.assertEqual(caught.exception.code, "invalid_inputs")

    def test_cutout_downloads_source_and_uploads_backend_result(self):
        source = PNG + b"-source"
        drive = FakeDrive({"cutouttoken01": source})
        backend = FakeImageBackend()
        service = self.service(drive, backend)
        service.submit(
            {
                "request_id": "cutout-service-01",
                "operation": "image.cutout",
                "parameters": {"output_name": "person.png"},
                "inputs": [input_manifest("cutouttoken01", "source.png", source, "image")],
            }
        )
        job = self.wait_for_status(service, "cutout-service-01", {"completed", "failed"})
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(len(backend.cutout_calls), 1)
        self.assertEqual(len(drive.downloads), 1)
        self.assertEqual(len(drive.uploads), 1)

    def test_generate_completes_with_integrity_manifest(self):
        drive = FakeDrive()
        backend = FakeImageBackend()
        service = self.service(drive, backend)
        submitted, created = service.submit(self.generation_request())
        self.assertTrue(created)
        self.assertEqual(submitted["status"], "queued")
        job = self.wait_for_status(service, "generate01", {"completed"})
        self.assertEqual(len(backend.generate_calls), 1)
        self.assertEqual(len(drive.uploads), 1)
        self.assertEqual(
            job["outputs"],
            [
                {
                    "file_token": "uploadedtoken0001",
                    "name": "generated.png",
                    "mime_type": "image/png",
                    "size_bytes": len(PNG),
                    "sha256": digest(PNG),
                }
            ],
        )
        self.assertIsNone(job["error"])

    def test_transparent_job_runs_cutout_validates_alpha_and_only_then_uploads(self):
        from PIL import Image

        opaque = self.root / "opaque-upstream.png"
        Image.new("RGB", (100, 100), (220, 220, 220)).save(opaque, format="PNG")
        helper = self.root / "fake-cutout.py"
        helper.write_text(
            """from pathlib import Path
import sys
from PIL import Image

source = Path(sys.argv[sys.argv.index("--input") + 1])
output = Path(sys.argv[sys.argv.index("--output") + 1])
with Image.open(source) as image:
    result = image.convert("RGBA")
alpha = Image.new("L", result.size, 255)
alpha.paste(0, (0, 0, result.width // 2, result.height))
result.putalpha(alpha)
result.save(output, format="PNG")
""",
            encoding="utf-8",
        )
        config = artifact_relay.dataclasses.replace(
            self.config,
            background_removal_python=sys.executable,
            background_removal_script=helper,
            background_removal_model_dir=self.root / "models",
        )
        drive = FakeDrive()
        backend = artifact_relay.ImageBackend(
            config, artifact_relay.BackgroundRemover(config)
        )
        response = {
            "data": [
                {
                    "b64_json": artifact_relay.base64.b64encode(
                        opaque.read_bytes()
                    ).decode()
                }
            ]
        }
        service = artifact_relay.ArtifactService(
            config, drive=drive, image_backend=backend
        )
        self.services.append(service)
        request = self.generation_request("transparent-service-success")
        request["parameters"].update(
            {
                "background": "transparent",
                "output_format": "png",
                "background_removal_model": "isnet-anime",
            }
        )
        with mock.patch.object(backend, "_json_request", return_value=response):
            service.submit(request)
            job = self.wait_for_status(
                service, "transparent-service-success", {"completed", "failed"}
            )
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(len(drive.uploads), 1)
        uploaded = self.root / "uploaded-transparent.png"
        uploaded.write_bytes(drive.uploads[0][1])
        transparent, opaque_count, total = artifact_relay.png_alpha_counts(uploaded)
        self.assertGreaterEqual(transparent, total // 2)
        self.assertGreaterEqual(opaque_count, total // 2)

    def test_failed_cutout_never_uploads_or_completes(self):
        from PIL import Image

        opaque = self.root / "opaque-failure.png"
        Image.new("RGB", (100, 100), (220, 220, 220)).save(opaque, format="PNG")
        helper = self.root / "failing-cutout.py"
        helper.write_text("raise SystemExit(1)\n", encoding="utf-8")
        config = artifact_relay.dataclasses.replace(
            self.config,
            background_removal_python=sys.executable,
            background_removal_script=helper,
            background_removal_model_dir=self.root / "models",
        )
        drive = FakeDrive()
        backend = artifact_relay.ImageBackend(
            config, artifact_relay.BackgroundRemover(config)
        )
        response = {
            "data": [
                {
                    "b64_json": artifact_relay.base64.b64encode(
                        opaque.read_bytes()
                    ).decode()
                }
            ]
        }
        service = artifact_relay.ArtifactService(
            config, drive=drive, image_backend=backend
        )
        self.services.append(service)
        request = self.generation_request("transparent-service-failure")
        request["parameters"].update(
            {"background": "transparent", "output_format": "png"}
        )
        with mock.patch.object(backend, "_json_request", return_value=response):
            service.submit(request)
            job = self.wait_for_status(
                service, "transparent-service-failure", {"completed", "failed"}
            )
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "background_removal_failed")
        self.assertEqual(drive.uploads, [])

    def test_partial_multi_output_upload_is_recorded_and_terminal_failure_is_not_retryable(self):
        class MultiOutputBackend(FakeImageBackend):
            def generate(self, parameters, output_dir, request_id):
                output_dir.mkdir(parents=True, exist_ok=True)
                first = output_dir / "first.png"
                second = output_dir / "second.png"
                first.write_bytes(PNG + b"-first")
                second.write_bytes(PNG + b"-second")
                return [first, second]

        class FailingDrive(FakeDrive):
            def upload(self, path, job_root):
                if len(self.uploads) == 1:
                    raise artifact_relay.JobError("feishu_unavailable", "temporary failure", True)
                return super().upload(path, job_root)

        drive = FailingDrive()
        service = self.service(drive, MultiOutputBackend())
        service.submit(self.generation_request("partial01"))

        job = self.wait_for_status(service, "partial01", {"failed"})

        self.assertFalse(job["error"]["retryable"])
        self.assertEqual(job["error"]["code"], "feishu_unavailable")
        self.assertEqual(len(job["outputs"]), 1)
        self.assertEqual(job["outputs"][0]["name"], "first.png")
        self.assertEqual(job["outputs"][0]["file_token"], "uploadedtoken0001")
        upload_states = service.store.get_uploads("partial01")
        self.assertIsNotNone(upload_states[0]["manifest"])
        self.assertIsNone(upload_states[1]["manifest"])

    def test_all_output_paths_are_validated_before_any_upload(self):
        drive = FakeDrive()
        service = self.service(drive, start_workers=False)
        request = artifact_relay.validate_request(
            self.generation_request("validate01"), self.config.max_input_bytes
        )
        service.store.create(request)
        job_root = self.config.state_dir / "jobs" / "validate01"
        output_dir = job_root / "outputs"
        output_dir.mkdir(parents=True)
        valid = output_dir / "valid.png"
        valid.write_bytes(PNG)
        outside = self.root / "outside.png"
        outside.write_bytes(PNG)

        with self.assertRaises(artifact_relay.JobError) as caught:
            service._upload_outputs("validate01", [valid, outside], job_root)

        self.assertEqual(caught.exception.code, "invalid_output")
        self.assertEqual(drive.uploads, [])
        self.assertEqual(service.store.get_uploads("validate01"), [])

    def test_request_id_is_idempotent_and_conflicts_on_changed_payload(self):
        service = self.service()
        first, created = service.submit(self.generation_request("samejob01"))
        self.assertTrue(created)
        second, created = service.submit(self.generation_request("samejob01"))
        self.assertFalse(created)
        self.assertEqual(second["request_id"], first["request_id"])
        changed = self.generation_request("samejob01")
        changed["parameters"]["prompt"] = "different"
        with self.assertRaises(artifact_relay.ApiError) as caught:
            service.submit(changed)
        self.assertEqual(caught.exception.status, 409)

    def test_edit_downloads_images_and_mask_before_processing(self):
        first = PNG + b"-one"
        second = PNG + b"-two"
        mask = PNG + b"-mask"
        drive = FakeDrive({"tokenimage01": first, "tokenimage02": second, "tokenmask001": mask})
        backend = FakeImageBackend()
        service = self.service(drive, backend)
        request = {
            "request_id": "editjob01",
            "operation": "image.edit",
            "parameters": {"prompt": "combine", "n": 1},
            "inputs": [
                input_manifest("tokenimage01", "one.png", first, "image"),
                input_manifest("tokenimage02", "two.png", second, "image"),
                input_manifest("tokenmask001", "mask.png", mask, "mask"),
            ],
        }
        service.submit(request)
        job = self.wait_for_status(service, "editjob01", {"completed"})
        self.assertEqual(job["status"], "completed")
        self.assertEqual(len(drive.downloads), 3)
        _, inputs, _ = backend.edit_calls[0]
        self.assertEqual([item[0]["role"] for item in inputs], ["image", "image", "mask"])

    def test_download_integrity_mismatch_fails_without_processing(self):
        expected = PNG + b"-expected"
        drive = FakeDrive({"mismatchtoken": PNG + b"-actual"})
        backend = FakeImageBackend()
        service = self.service(drive, backend)
        request = {
            "request_id": "mismatch01",
            "operation": "image.edit",
            "parameters": {"prompt": "edit"},
            "inputs": [input_manifest("mismatchtoken", "source.png", expected, "image")],
        }
        service.submit(request)
        job = self.wait_for_status(service, "mismatch01", {"failed"})
        self.assertEqual(job["error"]["code"], "input_integrity_mismatch")
        self.assertEqual(backend.edit_calls, [])
        self.assertEqual(drive.uploads, [])

    def test_handoff_instruction_is_optional_and_local_publish_completes(self):
        source = b"attachment contents"
        drive = FakeDrive({"attachmenttoken": source})
        service = self.service(drive, FakeImageBackend())
        request = {
            "request_id": "handoff01",
            "operation": "artifact.handoff",
            "parameters": {},
            "inputs": [
                input_manifest(
                    "attachmenttoken",
                    "report.txt",
                    source,
                    "attachment",
                    "text/plain",
                )
            ],
        }
        service.submit(request)
        job = self.wait_for_status(service, "handoff01", {"ready_for_processing"})
        self.assertEqual(job["outputs"], [])
        ready = service.local_ready_jobs("handoff01")
        self.assertEqual(ready[0]["instruction"], "")
        result_file = self.root / "summary.txt"
        result_file.write_text("summary")
        completed = service.publish_local("handoff01", [result_file])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["outputs"][0]["sha256"], digest(b"summary"))

    def test_local_cli_service_does_not_mark_active_jobs_failed(self):
        store = artifact_relay.JobStore(self.config.state_dir)
        request = artifact_relay.validate_request(self.generation_request("running001"), self.config.max_input_bytes)
        store.create(request)
        store.update("running001", status="processing")
        service = artifact_relay.ArtifactService(
            self.config,
            store=store,
            drive=FakeDrive(),
            image_backend=FakeImageBackend(),
            start_workers=False,
        )
        self.services.append(service)
        self.assertEqual(store.get("running001")["status"], "processing")

    def test_server_restart_resumes_queued_but_not_inflight_jobs(self):
        store = artifact_relay.JobStore(self.config.state_dir)
        queued = artifact_relay.validate_request(self.generation_request("queued0001"), self.config.max_input_bytes)
        active = artifact_relay.validate_request(self.generation_request("active0001"), self.config.max_input_bytes)
        store.create(queued)
        store.create(active)
        store.update("active0001", status="processing")
        backend = FakeImageBackend()
        service = artifact_relay.ArtifactService(
            self.config,
            store=store,
            drive=FakeDrive(),
            image_backend=backend,
            start_workers=True,
        )
        self.services.append(service)
        queued_job = self.wait_for_status(service, "queued0001", {"completed"})
        self.assertEqual(queued_job["status"], "completed")
        active_job = service.get("active0001")
        self.assertEqual(active_job["status"], "failed")
        self.assertEqual(active_job["error"]["code"], "service_restarted")
        self.assertEqual(len(backend.generate_calls), 1)

    def test_lark_command_accepts_prefixed_json_and_applies_file_size_limit(self):
        completed = artifact_relay.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='Downloading...\n{"ok":true,"data":{"output":"inputs/file.bin"}}\n',
            stderr="",
        )
        drive = artifact_relay.LarkDrive(self.config)
        with mock.patch.object(artifact_relay.subprocess, "run", return_value=completed) as run:
            envelope = drive._run(["drive", "+download"], self.root, file_size_limit=1234)

        self.assertTrue(envelope["ok"])
        command = run.call_args.args[0]
        self.assertEqual(
            command[:4],
            [self.config.prlimit_cli, "--fsize=1234", "--", self.config.lark_cli],
        )

    def test_lark_command_reads_error_json_from_stdout_when_stderr_is_plain_text(self):
        completed = artifact_relay.subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=(
                'Progress: request failed\n'
                '{"ok":false,"error":{"type":"authorization","subtype":"denied"}}\n'
            ),
            stderr="request failed",
        )
        drive = artifact_relay.LarkDrive(self.config)
        with mock.patch.object(artifact_relay.subprocess, "run", return_value=completed):
            with self.assertRaises(artifact_relay.JobError) as caught:
                drive._run(["drive", "+download"], self.root)

        self.assertEqual(caught.exception.code, "feishu_denied")
        self.assertFalse(caught.exception.retryable)

    def test_failed_download_removes_partial_destination(self):
        destination = self.root / "job" / "inputs" / "source.bin"
        destination.parent.mkdir(parents=True)
        drive = artifact_relay.LarkDrive(self.config)

        def fail_run(arguments, cwd, file_size_limit=None):
            destination.write_bytes(b"partial")
            raise artifact_relay.JobError("feishu_unavailable", "download interrupted", True)

        with mock.patch.object(drive, "_run", side_effect=fail_run):
            with self.assertRaises(artifact_relay.JobError):
                drive.download(
                    {"file_token": "source01", "size_bytes": 7},
                    destination,
                    self.root / "job",
                )

        self.assertFalse(destination.exists())

    def test_http_capabilities_requires_the_relay_key(self):
        service = self.service(start_workers=False)
        server = artifact_relay.ArtifactHTTPServer(("127.0.0.1", 0), service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"{base}/v1/artifact-capabilities")
            self.assertEqual(caught.exception.code, 401)
            request = urllib.request.Request(
                f"{base}/v1/artifact-capabilities",
                headers={"Authorization": "Bearer test-relay-key"},
            )
            with urllib.request.urlopen(request) as response:
                value = json.load(response)
            self.assertEqual(value["delivery"], "lark_drive")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class MultipartContractTests(unittest.TestCase):
    def test_edit_uses_repeated_image_array_fields_then_mask(self):
        captured = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format_string, *args):
                return

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                captured["body"] = self.rfile.read(length)
                payload = json.dumps(
                    {"data": [{"b64_json": artifact_relay.base64.b64encode(PNG).decode()}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = []
            for name in ("one.png", "two.png", "mask.png"):
                path = root / name
                path.write_bytes(PNG)
                paths.append(path)
            config = artifact_relay.Config(
                api_key="key",
                state_dir=root / "state",
                upstream_base_url=f"http://127.0.0.1:{server.server_port}/v1",
                upstream_api_key="key",
                lark_cli="lark-cli",
                lark_home=root,
                lark_identity="bot",
                input_target_type="folder",
                input_target_token="inputtoken",
                output_target_type="folder",
                output_target_token="outputtoken",
            )
            backend = artifact_relay.ImageBackend(config)
            manifests = [
                ({"role": "image", "mime_type": "image/png"}, paths[0]),
                ({"role": "image", "mime_type": "image/png"}, paths[1]),
                ({"role": "mask", "mime_type": "image/png"}, paths[2]),
            ]
            output = root / "outputs"
            backend.edit({"prompt": "edit", "n": 1}, manifests, output, "request001")
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        body = captured["body"]
        first = body.find(b'name="image[]"; filename="one.png"')
        second = body.find(b'name="image[]"; filename="two.png"')
        mask = body.find(b'name="mask"; filename="mask.png"')
        self.assertGreaterEqual(first, 0)
        self.assertGreater(second, first)
        self.assertGreater(mask, second)
        self.assertEqual(body.count(b'name="image[]"'), 2)


class BackgroundRemovalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = artifact_relay.Config(
            api_key="key",
            state_dir=self.root / "state",
            upstream_base_url="http://127.0.0.1:9/v1",
            upstream_api_key="key",
            lark_cli="lark-cli",
            lark_home=self.root,
            lark_identity="bot",
            input_target_type="folder",
            input_target_token="inputtoken",
            output_target_type="folder",
            output_target_token="outputtoken",
            background_removal_python="/test/python",
            background_removal_script=self.root / "remove_background.py",
            background_removal_model_dir=self.root / "models",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_existing_usable_alpha_is_preserved_without_processing(self):
        path = self.root / "already-transparent.png"
        path.write_bytes(PNG)
        remover = artifact_relay.BackgroundRemover(self.config)
        with mock.patch.object(artifact_relay, "png_alpha_counts", return_value=(1, 1, 2)), mock.patch.object(
            artifact_relay.subprocess, "run"
        ) as run:
            result = remover.ensure_transparent(path)
        self.assertEqual(result, path)
        self.assertEqual(path.read_bytes(), PNG)
        run.assert_not_called()

    def test_real_png_alpha_counts_require_substantial_background_and_foreground(self):
        from PIL import Image

        path = self.root / "real-rgba.png"
        image = Image.new("RGBA", (100, 100), (255, 255, 255, 255))
        for x in range(10):
            image.putpixel((x, 0), (255, 255, 255, 0))
        image.save(path, format="PNG")

        self.assertEqual(artifact_relay.png_alpha_counts(path), (10, 9990, 10000))

    def test_real_png_dimensions_are_bounded_before_decode(self):
        from PIL import Image

        path = self.root / "too-wide.png"
        Image.new("RGBA", (artifact_relay.MAX_CUTOUT_EDGE + 1, 1), (0, 0, 0, 0)).save(
            path, format="PNG"
        )
        with self.assertRaises(artifact_relay.JobError) as caught:
            artifact_relay.png_alpha_counts(path)
        self.assertEqual(
            caught.exception.code, "transparent_output_dimensions_unsupported"
        )

    def test_local_transparent_intent_is_not_forwarded_to_unsupported_upstream(self):
        parameters = artifact_relay.ImageBackend._upstream_parameters(
            {
                "background": "transparent",
                "background_removal_model": "isnet-anime",
                "prompt": "cut out",
            }
        )
        self.assertEqual(parameters["background"], "auto")
        self.assertNotIn("background_removal_model", parameters)

    def test_negligible_alpha_does_not_bypass_background_removal(self):
        path = self.root / "almost-opaque.png"
        path.write_bytes(PNG)
        processed = PNG + b"-rgba"

        def fake_run(command, **kwargs):
            output = Path(command[command.index("--output") + 1])
            output.write_bytes(processed)
            return artifact_relay.subprocess.CompletedProcess(command, 0, "", "")

        remover = artifact_relay.BackgroundRemover(self.config)
        with mock.patch.object(
            artifact_relay,
            "png_alpha_counts",
            side_effect=[(1, 9999, 10000), (100, 100, 10000)],
        ), mock.patch.object(artifact_relay.subprocess, "run", side_effect=fake_run) as run:
            result = remover.ensure_transparent(path)
        self.assertEqual(result, path)
        run.assert_called_once()

    def test_almost_empty_transparent_output_is_rejected(self):
        path = self.root / "almost-empty.png"
        path.write_bytes(PNG)
        remover = artifact_relay.BackgroundRemover(self.config)
        with mock.patch.object(
            artifact_relay, "png_alpha_counts", return_value=(10000, 1, 10000)
        ), mock.patch.object(artifact_relay.subprocess, "run") as run:
            with self.assertRaises(artifact_relay.JobError) as caught:
                remover.ensure_transparent(path)
        self.assertEqual(caught.exception.code, "transparent_output_empty")
        run.assert_not_called()

    def test_opaque_output_is_replaced_with_verified_rgba_png(self):
        path = self.root / "opaque.png"
        path.write_bytes(PNG)
        processed = PNG + b"-rgba"

        def fake_run(command, **kwargs):
            output = Path(command[command.index("--output") + 1])
            output.write_bytes(processed)
            return artifact_relay.subprocess.CompletedProcess(command, 0, "", "")

        remover = artifact_relay.BackgroundRemover(self.config)
        with mock.patch.object(
            artifact_relay, "png_alpha_counts", side_effect=[None, (1, 1, 2)]
        ), mock.patch.object(artifact_relay.subprocess, "run", side_effect=fake_run) as run:
            result = remover.ensure_transparent(path, "isnet-anime")
        self.assertEqual(result, path)
        self.assertEqual(path.read_bytes(), processed)
        self.assertIn("isnet-anime", run.call_args.args[0])
        child_environment = run.call_args.kwargs["env"]
        self.assertEqual(child_environment["U2NET_HOME"], str(self.root / "models"))
        self.assertNotIn("ARTIFACT_RELAY_API_KEY", child_environment)

    def test_failed_background_removal_never_returns_an_output(self):
        path = self.root / "opaque.png"
        path.write_bytes(PNG)
        remover = artifact_relay.BackgroundRemover(self.config)
        completed = artifact_relay.subprocess.CompletedProcess([], 1, "", "failed")
        with mock.patch.object(artifact_relay, "png_alpha_counts", return_value=None), mock.patch.object(
            artifact_relay.subprocess, "run", return_value=completed
        ):
            with self.assertRaises(artifact_relay.JobError) as caught:
                remover.ensure_transparent(path)
        self.assertEqual(caught.exception.code, "background_removal_failed")

    def test_image_backend_routes_transparent_outputs_through_remover(self):
        class FakeRemover:
            def __init__(self):
                self.calls = []

            def ensure_transparent(self, path, requested_model=None):
                self.calls.append((path, requested_model))
                return path

        remover = FakeRemover()
        backend = artifact_relay.ImageBackend(self.config, remover)
        response = {"data": [{"b64_json": artifact_relay.base64.b64encode(PNG).decode()}]}
        paths = backend._save_response_images(
            response,
            {
                "background": "transparent",
                "background_removal_model": "isnet-anime",
                "n": 1,
            },
            self.root / "outputs",
            "transparent01",
        )
        self.assertEqual(paths, [self.root / "outputs" / "transparent01.png"])
        self.assertEqual(remover.calls[0][1], "isnet-anime")


class DreaminaAgentCutoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.png"
        from PIL import Image

        Image.new("RGB", (100, 100), (220, 220, 220)).save(self.source, format="PNG")
        self.runner = self.root / "runner.py"
        self.browser = Path(sys.executable)
        self.config = artifact_relay.Config(
            api_key="secret-api-key",
            state_dir=self.root / "state",
            upstream_base_url="http://127.0.0.1:9/v1",
            upstream_api_key="secret-upstream-key",
            lark_cli="lark-cli",
            lark_home=self.root,
            lark_identity="bot",
            input_target_type="folder",
            input_target_token="inputtoken",
            output_target_type="folder",
            output_target_token="outputtoken",
            dreamina_node=sys.executable,
            dreamina_runner=self.runner,
            dreamina_browser=self.browser,
            dreamina_profile_dir=self.root / "profile",
            dreamina_diagnostics_dir=self.root / "diagnostics",
            dreamina_timeout_seconds=60,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_runner_result_must_be_a_real_transparent_png(self):
        self.runner.write_text(
            """from pathlib import Path
import sys
from PIL import Image
output = Path(sys.argv[sys.argv.index('--output') + 1])
image = Image.new('RGBA', (100, 100), (255, 0, 0, 255))
alpha = Image.new('L', image.size, 255)
alpha.paste(0, (0, 0, 50, 100))
image.putalpha(alpha)
image.save(output, format='PNG')
print('{\"ok\":true}')
""",
            encoding="utf-8",
        )
        destination = self.root / "result.png"
        result = artifact_relay.DreaminaAgentCutout(self.config).cutout(
            self.source, destination
        )
        self.assertEqual(result, destination)
        transparent, opaque, total = artifact_relay.png_alpha_counts(destination)
        self.assertEqual((transparent, opaque, total), (5000, 5000, 10000))

    def test_login_failure_is_public_and_never_creates_an_output(self):
        self.runner.write_text(
            "print('{\"ok\":false,\"code\":\"login_required\"}')\nraise SystemExit(21)\n",
            encoding="utf-8",
        )
        destination = self.root / "result.png"
        with self.assertRaises(artifact_relay.JobError) as caught:
            artifact_relay.DreaminaAgentCutout(self.config).cutout(
                self.source, destination
            )
        self.assertEqual(caught.exception.code, "dreamina_login_required")
        self.assertFalse(destination.exists())

    def test_cutout_subprocess_environment_does_not_receive_relay_secrets(self):
        self.runner.write_text("raise SystemExit(1)\n", encoding="utf-8")
        completed = artifact_relay.subprocess.CompletedProcess(
            [], 1, '{"ok":false,"code":"submit_failed"}\n', ""
        )
        with mock.patch.object(
            artifact_relay.subprocess, "run", return_value=completed
        ) as run, self.assertRaises(artifact_relay.JobError):
            artifact_relay.DreaminaAgentCutout(self.config).cutout(
                self.source, self.root / "result.png"
            )
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("ARTIFACT_RELAY_API_KEY", environment)
        self.assertNotIn("ARTIFACT_RELAY_UPSTREAM_API_KEY", environment)
        self.assertNotIn("secret-api-key", json.dumps(environment))

    def test_timeout_before_submission_is_not_reported_as_unknown_completion(self):
        before_submit = json.dumps(
            {"event": "cutout_failed", "kind": "timeout", "stage": "page_load"}
        )
        after_submit = json.dumps(
            {"event": "cutout_failed", "kind": "timeout", "stage": "result_wait"}
        )
        ambiguous_submit = json.dumps(
            {
                "event": "cutout_failed",
                "kind": "completion_unknown",
                "stage": "single_submit",
            }
        )
        self.assertEqual(
            artifact_relay.DreaminaAgentCutout._runner_error_code(before_submit),
            "dreamina_page_changed",
        )
        self.assertEqual(
            artifact_relay.DreaminaAgentCutout._runner_error_code(after_submit),
            "dreamina_completion_unknown",
        )
        self.assertEqual(
            artifact_relay.DreaminaAgentCutout._runner_error_code(ambiguous_submit),
            "dreamina_completion_unknown",
        )


if __name__ == "__main__":
    unittest.main()
