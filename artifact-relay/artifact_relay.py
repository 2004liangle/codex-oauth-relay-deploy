#!/usr/bin/env python3
"""Asynchronous Feishu-backed artifact delivery for the Codex OAuth relay.

The public HTTP surface is deliberately small. Remote callers may create and
inspect jobs, but they cannot choose a command to execute. Generic attachment
processing is a local handoff: a trusted process on the server publishes the
result with this module's local-only CLI.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import dataclasses
import datetime as dt
import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import queue
import re
import shutil
import signal
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence


PROTOCOL_VERSION = "1.0"
STATUS_VALUES = (
    "queued",
    "downloading",
    "processing",
    "uploading",
    "ready_for_processing",
    "completed",
    "failed",
)
TERMINAL_STATUSES = {"ready_for_processing", "completed", "failed"}
OPERATIONS = ("image.generate", "image.edit", "artifact.handoff")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}\Z")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{6,200}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MIME_RE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")
IMAGE_PARAMETER_NAMES = {
    "background",
    "model",
    "moderation",
    "n",
    "output_compression",
    "output_format",
    "prompt",
    "quality",
    "size",
    "user",
}
LOCAL_IMAGE_PARAMETER_NAMES = {"background_removal_model", "output_name"}
IMAGE_MIME = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
IMAGE_EXTENSION = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}
ACTIVE_STATUSES = {"queued", "downloading", "processing", "uploading"}
BACKGROUND_REMOVAL_MODELS = frozenset({"isnet-general-use", "isnet-anime"})
ALPHA_COVERAGE_DIVISOR = 1000
ALPHA_NEAR_OPAQUE = 240
ALPHA_NEAR_TRANSPARENT = 15
MAX_CUTOUT_EDGE = 8192
MAX_CUTOUT_PIXELS = 16_777_216


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def request_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclasses.dataclass(frozen=True)
class Config:
    api_key: str
    state_dir: Path
    upstream_base_url: str
    upstream_api_key: str
    lark_cli: str
    lark_home: Path
    lark_identity: str
    input_target_type: str
    input_target_token: str
    output_target_type: str
    output_target_token: str
    background_removal_python: str = "/opt/codex-artifact-relay/venv/bin/python"
    background_removal_script: Path = Path("/opt/codex-artifact-relay/remove_background.py")
    background_removal_model_dir: Path = Path("/var/lib/codex-artifact-relay/models")
    background_removal_model: str = "isnet-general-use"
    background_removal_timeout_seconds: int = 600
    prlimit_cli: str = "/usr/bin/prlimit"
    host: str = "127.0.0.1"
    port: int = 18318
    worker_count: int = 2
    max_request_bytes: int = 1024 * 1024
    max_input_bytes: int = 64 * 1024 * 1024
    max_upstream_response_bytes: int = 128 * 1024 * 1024
    lark_timeout_seconds: int = 900
    upstream_timeout_seconds: int = 3600

    @classmethod
    def from_env(cls) -> "Config":
        api_key = os.environ.get("ARTIFACT_RELAY_API_KEY", "")
        if not api_key:
            raise ValueError("ARTIFACT_RELAY_API_KEY is required")
        upstream_api_key = os.environ.get("ARTIFACT_RELAY_UPSTREAM_API_KEY", api_key)
        input_type, input_token = _target_from_env("INPUT")
        output_type, output_token = _target_from_env("OUTPUT")
        identity = os.environ.get("ARTIFACT_RELAY_LARK_IDENTITY", "bot")
        if identity not in {"bot", "user"}:
            raise ValueError("ARTIFACT_RELAY_LARK_IDENTITY must be bot or user")
        background_removal_model = os.environ.get(
            "ARTIFACT_RELAY_BACKGROUND_REMOVAL_MODEL", "isnet-general-use"
        )
        if background_removal_model not in BACKGROUND_REMOVAL_MODELS:
            raise ValueError(
                "ARTIFACT_RELAY_BACKGROUND_REMOVAL_MODEL must be isnet-general-use or isnet-anime"
            )
        return cls(
            api_key=api_key,
            state_dir=Path(os.environ.get("ARTIFACT_RELAY_STATE_DIR", "/var/lib/codex-artifact-relay")),
            upstream_base_url=os.environ.get(
                "ARTIFACT_RELAY_UPSTREAM_BASE_URL", "http://127.0.0.1:18317/v1"
            ).rstrip("/"),
            upstream_api_key=upstream_api_key,
            lark_cli=os.environ.get("ARTIFACT_RELAY_LARK_CLI", "lark-cli"),
            prlimit_cli=os.environ.get("ARTIFACT_RELAY_PRLIMIT", "/usr/bin/prlimit"),
            lark_home=Path(os.environ.get("ARTIFACT_RELAY_LARK_HOME", str(Path.home()))),
            lark_identity=identity,
            input_target_type=input_type,
            input_target_token=input_token,
            output_target_type=output_type,
            output_target_token=output_token,
            background_removal_python=os.environ.get(
                "ARTIFACT_RELAY_BACKGROUND_REMOVAL_PYTHON",
                "/opt/codex-artifact-relay/venv/bin/python",
            ),
            background_removal_script=Path(
                os.environ.get(
                    "ARTIFACT_RELAY_BACKGROUND_REMOVAL_SCRIPT",
                    "/opt/codex-artifact-relay/remove_background.py",
                )
            ),
            background_removal_model_dir=Path(
                os.environ.get(
                    "ARTIFACT_RELAY_BACKGROUND_REMOVAL_MODEL_DIR",
                    "/var/lib/codex-artifact-relay/models",
                )
            ),
            background_removal_model=background_removal_model,
            background_removal_timeout_seconds=env_int(
                "ARTIFACT_RELAY_BACKGROUND_REMOVAL_TIMEOUT", 600, 30, 3600
            ),
            host=os.environ.get("ARTIFACT_RELAY_HOST", "127.0.0.1"),
            port=env_int("ARTIFACT_RELAY_PORT", 18318, 1, 65535),
            worker_count=env_int("ARTIFACT_RELAY_WORKERS", 2, 1, 4),
            max_request_bytes=env_int(
                "ARTIFACT_RELAY_MAX_REQUEST_BYTES", 1024 * 1024, 1024, 8 * 1024 * 1024
            ),
            max_input_bytes=env_int(
                "ARTIFACT_RELAY_MAX_INPUT_BYTES", 64 * 1024 * 1024, 1024, 1024 * 1024 * 1024
            ),
            max_upstream_response_bytes=env_int(
                "ARTIFACT_RELAY_MAX_UPSTREAM_RESPONSE_BYTES",
                128 * 1024 * 1024,
                1024,
                1024 * 1024 * 1024,
            ),
            lark_timeout_seconds=env_int("ARTIFACT_RELAY_LARK_TIMEOUT", 900, 10, 7200),
            upstream_timeout_seconds=env_int(
                "ARTIFACT_RELAY_UPSTREAM_TIMEOUT", 3600, 10, 7200
            ),
        )


def _target_from_env(direction: str) -> tuple[str, str]:
    folder = os.environ.get(f"ARTIFACT_RELAY_FEISHU_{direction}_FOLDER_TOKEN", "")
    wiki = os.environ.get(f"ARTIFACT_RELAY_FEISHU_{direction}_WIKI_TOKEN", "")
    if bool(folder) == bool(wiki):
        raise ValueError(
            f"set exactly one ARTIFACT_RELAY_FEISHU_{direction}_FOLDER_TOKEN or "
            f"ARTIFACT_RELAY_FEISHU_{direction}_WIKI_TOKEN"
        )
    target_type, token = ("folder", folder) if folder else ("wiki", wiki)
    if not TOKEN_RE.fullmatch(token):
        raise ValueError(f"invalid Feishu {direction.lower()} target token")
    return target_type, token


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable


class JobError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def public_error(code: str, message: str, retryable: bool = False) -> dict[str, object]:
    return {"code": code, "message": message[:500], "retryable": retryable}


def safe_filename(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(character for character in name if ord(character) >= 32 and character != "\x7f")
    name = name.strip().strip(".")
    if not name:
        return fallback
    encoded = name.encode("utf-8")
    if len(encoded) <= 180:
        return name
    stem, suffix = os.path.splitext(name)
    suffix_bytes = suffix.encode("utf-8")[:20]
    budget = max(1, 180 - len(suffix_bytes))
    stem_bytes = stem.encode("utf-8")[:budget]
    while True:
        try:
            return stem_bytes.decode("utf-8") + suffix_bytes.decode("utf-8", "ignore")
        except UnicodeDecodeError:
            stem_bytes = stem_bytes[:-1]


def sniff_mime(path: Path, suggested: object = None) -> str:
    with path.open("rb") as handle:
        header = handle.read(16)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"PK\x03\x04"):
        guessed, _ = mimetypes.guess_type(path.name)
        return guessed or "application/zip"
    if isinstance(suggested, str) and MIME_RE.fullmatch(suggested):
        return suggested
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def file_manifest(path: Path, file_token: str, name: str | None = None, mime: object = None) -> dict[str, object]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return {
        "file_token": file_token,
        "name": name or path.name,
        "mime_type": sniff_mime(path, mime),
        "size_bytes": size,
        "sha256": digest.hexdigest(),
    }


def validate_request(value: object, max_input_bytes: int) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_request", "request body must be a JSON object")
    allowed = {"request_id", "operation", "parameters", "inputs"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ApiError(400, "invalid_request", f"unknown request fields: {', '.join(unknown)}")
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        raise ApiError(400, "invalid_request_id", "request_id must contain 8-128 safe characters")
    operation = value.get("operation")
    if operation not in OPERATIONS:
        raise ApiError(422, "unsupported_operation", "operation is not supported")
    parameters = value.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ApiError(400, "invalid_parameters", "parameters must be an object")
    inputs_value = value.get("inputs", [])
    if not isinstance(inputs_value, list):
        raise ApiError(400, "invalid_inputs", "inputs must be an array")
    inputs = [validate_input(item, index, max_input_bytes) for index, item in enumerate(inputs_value)]
    total = sum(int(item["size_bytes"]) for item in inputs)
    if total > max_input_bytes:
        raise ApiError(413, "inputs_too_large", "declared input size exceeds the service limit")
    validate_operation(operation, parameters, inputs)
    return {
        "request_id": request_id,
        "operation": operation,
        "parameters": parameters,
        "inputs": inputs,
    }


def validate_input(value: object, index: int, max_input_bytes: int) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_input", f"inputs[{index}] must be an object")
    allowed = {"file_token", "name", "mime_type", "size_bytes", "sha256", "role"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ApiError(400, "invalid_input", f"inputs[{index}] has unknown fields")
    token = value.get("file_token")
    name = value.get("name")
    size = value.get("size_bytes")
    digest = value.get("sha256")
    mime = value.get("mime_type")
    role = value.get("role")
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise ApiError(400, "invalid_input", f"inputs[{index}].file_token is invalid")
    if not isinstance(name, str) or safe_filename(name, "") != name:
        raise ApiError(400, "invalid_input", f"inputs[{index}].name must be a safe base name")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= max_input_bytes:
        raise ApiError(400, "invalid_input", f"inputs[{index}].size_bytes is invalid")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ApiError(400, "invalid_input", f"inputs[{index}].sha256 is invalid")
    if mime is not None and (not isinstance(mime, str) or not MIME_RE.fullmatch(mime)):
        raise ApiError(400, "invalid_input", f"inputs[{index}].mime_type is invalid")
    if role is not None and role not in {"image", "mask", "attachment"}:
        raise ApiError(400, "invalid_input", f"inputs[{index}].role is invalid")
    result: dict[str, object] = {
        "file_token": token,
        "name": name,
        "mime_type": mime or "application/octet-stream",
        "size_bytes": size,
        "sha256": digest,
    }
    if role is not None:
        result["role"] = role
    return result


def validate_operation(operation: str, parameters: dict[str, object], inputs: list[dict[str, object]]) -> None:
    if operation.startswith("image."):
        unknown = sorted(set(parameters) - IMAGE_PARAMETER_NAMES - LOCAL_IMAGE_PARAMETER_NAMES)
        if unknown:
            raise ApiError(400, "invalid_parameters", f"unsupported image parameters: {', '.join(unknown)}")
        prompt = parameters.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100_000:
            raise ApiError(400, "invalid_parameters", "parameters.prompt is required")
        count = parameters.get("n", 1)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
            raise ApiError(400, "invalid_parameters", "parameters.n must be between 1 and 10")
        output_name = parameters.get("output_name")
        if output_name is not None and (
            not isinstance(output_name, str) or safe_filename(output_name, "") != output_name
        ):
            raise ApiError(400, "invalid_parameters", "parameters.output_name must be a safe base name")
        background = parameters.get("background")
        if background is not None and (
            not isinstance(background, str) or background not in {"auto", "opaque", "transparent"}
        ):
            raise ApiError(400, "invalid_parameters", "parameters.background is invalid")
        removal_model = parameters.get("background_removal_model")
        if removal_model is not None and (
            not isinstance(removal_model, str) or removal_model not in BACKGROUND_REMOVAL_MODELS
        ):
            raise ApiError(
                400,
                "invalid_parameters",
                "parameters.background_removal_model must be isnet-general-use or isnet-anime",
            )
        if removal_model is not None and background != "transparent":
            raise ApiError(
                400,
                "invalid_parameters",
                "parameters.background_removal_model requires background=transparent",
            )
        if background == "transparent" and parameters.get("output_format", "png") != "png":
            raise ApiError(400, "invalid_parameters", "transparent output requires output_format=png")
    if operation == "image.generate" and inputs:
        raise ApiError(400, "invalid_inputs", "image.generate does not accept inputs")
    if operation == "image.edit":
        images = [item for item in inputs if item.get("role", "image") == "image"]
        masks = [item for item in inputs if item.get("role") == "mask"]
        attachments = [item for item in inputs if item.get("role") == "attachment"]
        if not 1 <= len(images) <= 16 or len(masks) > 1 or attachments:
            raise ApiError(400, "invalid_inputs", "image.edit requires 1-16 images and at most one mask")
    if operation == "artifact.handoff":
        if not 1 <= len(inputs) <= 32:
            raise ApiError(400, "invalid_inputs", "artifact.handoff requires 1-32 inputs")
        instruction = parameters.get("instruction")
        if instruction is not None and (not isinstance(instruction, str) or len(instruction) > 100_000):
            raise ApiError(400, "invalid_parameters", "parameters.instruction must be text")
        unknown = sorted(set(parameters) - {"instruction"})
        if unknown:
            raise ApiError(400, "invalid_parameters", "artifact.handoff only accepts instruction")


class JobStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.jobs_dir = state_dir / "jobs"
        self.database = state_dir / "jobs.sqlite3"
        self._lock = threading.RLock()
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    request_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    job_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_uploads (
                    request_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    manifest_json TEXT,
                    PRIMARY KEY (request_id, position),
                    FOREIGN KEY (request_id) REFERENCES jobs(request_id)
                )
                """
            )
        os.chmod(self.database, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def mark_interrupted(self) -> int:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT request_id, job_json FROM jobs WHERE status IN ('downloading','processing','uploading')"
            ).fetchall()
            for row in rows:
                job = json.loads(row["job_json"])
                job["status"] = "failed"
                job["updated_at"] = utc_now()
                job["error"] = public_error(
                    "service_restarted",
                    "service restarted while the job was active; create a new request_id after reviewing usage",
                    False,
                )
                connection.execute(
                    "UPDATE jobs SET status=?, updated_at=?, job_json=? WHERE request_id=?",
                    (job["status"], job["updated_at"], canonical_json(job), row["request_id"]),
                )
            return len(rows)

    def list_queued(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT request_id FROM jobs WHERE status='queued' ORDER BY created_at"
            ).fetchall()
        return [str(row["request_id"]) for row in rows]

    def create(self, request: dict[str, object]) -> tuple[dict[str, object], bool]:
        request_id = str(request["request_id"])
        digest = request_digest(request)
        now = utc_now()
        job: dict[str, object] = {
            "request_id": request_id,
            "operation": request["operation"],
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "inputs": request["inputs"],
            "outputs": [],
            "error": None,
        }
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT request_hash, job_json FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    raise ApiError(409, "request_id_conflict", "request_id already exists with different input")
                return json.loads(existing["job_json"]), False
            connection.execute(
                """INSERT INTO jobs
                   (request_id, request_hash, operation, request_json, job_json, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request_id,
                    digest,
                    request["operation"],
                    canonical_json(request),
                    canonical_json(job),
                    "queued",
                    now,
                    now,
                ),
            )
        return job, True

    def get(self, request_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT job_json FROM jobs WHERE request_id=?", (request_id,)).fetchone()
        return json.loads(row["job_json"]) if row else None

    def get_request(self, request_id: str) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_json FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
        if not row:
            raise KeyError(request_id)
        return json.loads(row["request_json"])

    def update(self, request_id: str, **changes: object) -> dict[str, object]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT job_json FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                raise KeyError(request_id)
            job = json.loads(row["job_json"])
            job.update(changes)
            job["updated_at"] = utc_now()
            connection.execute(
                "UPDATE jobs SET status=?, updated_at=?, job_json=? WHERE request_id=?",
                (job["status"], job["updated_at"], canonical_json(job), request_id),
            )
        return job

    def claim(self, request_id: str, expected: set[str], status: str) -> dict[str, object] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, job_json FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            if not row or row["status"] not in expected:
                connection.rollback()
                return None
            job = json.loads(row["job_json"])
            job["status"] = status
            job["updated_at"] = utc_now()
            connection.execute(
                "UPDATE jobs SET status=?, updated_at=?, job_json=? WHERE request_id=?",
                (status, job["updated_at"], canonical_json(job), request_id),
            )
            connection.commit()
            return job

    def prepare_uploads(self, request_id: str, relative_paths: Sequence[str]) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT position, relative_path FROM job_uploads WHERE request_id=? ORDER BY position",
                (request_id,),
            ).fetchall()
            existing = [str(row["relative_path"]) for row in rows]
            if existing and existing != list(relative_paths):
                connection.rollback()
                raise JobError("upload_state_conflict", "stored output upload state is inconsistent", False)
            if not existing:
                connection.executemany(
                    "INSERT INTO job_uploads (request_id, position, relative_path) VALUES (?, ?, ?)",
                    [(request_id, index, path) for index, path in enumerate(relative_paths)],
                )
            connection.commit()

    def get_uploads(self, request_id: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT position, relative_path, manifest_json
                   FROM job_uploads WHERE request_id=? ORDER BY position""",
                (request_id,),
            ).fetchall()
        return [
            {
                "position": int(row["position"]),
                "relative_path": str(row["relative_path"]),
                "manifest": json.loads(row["manifest_json"]) if row["manifest_json"] else None,
            }
            for row in rows
        ]

    def record_upload(self, request_id: str, position: int, manifest: Mapping[str, object]) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE job_uploads SET manifest_json=?
                   WHERE request_id=? AND position=? AND manifest_json IS NULL""",
                (canonical_json(manifest), request_id, position),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise JobError("upload_state_conflict", "output upload state changed unexpectedly", False)
            row = connection.execute(
                "SELECT job_json FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            if not row:
                connection.rollback()
                raise JobError("upload_state_conflict", "artifact job disappeared", False)
            job = json.loads(row["job_json"])
            manifests = connection.execute(
                """SELECT manifest_json FROM job_uploads
                   WHERE request_id=? AND manifest_json IS NOT NULL ORDER BY position""",
                (request_id,),
            ).fetchall()
            job["outputs"] = [json.loads(item["manifest_json"]) for item in manifests]
            job["updated_at"] = utc_now()
            connection.execute(
                "UPDATE jobs SET updated_at=?, job_json=? WHERE request_id=?",
                (job["updated_at"], canonical_json(job), request_id),
            )
            connection.commit()

    def list_ready(self, request_id: str | None = None) -> list[dict[str, object]]:
        query = "SELECT request_json, job_json FROM jobs WHERE status='ready_for_processing'"
        params: tuple[object, ...] = ()
        if request_id:
            query += " AND request_id=?"
            params = (request_id,)
        query += " ORDER BY created_at"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {"request": json.loads(row["request_json"]), "job": json.loads(row["job_json"])}
            for row in rows
        ]


class LarkDrive:
    def __init__(self, config: Config):
        self.config = config

    def _run(
        self,
        arguments: Sequence[str],
        cwd: Path,
        file_size_limit: int | None = None,
    ) -> dict[str, object]:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.config.lark_home),
                "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
            }
        )
        command = [self.config.lark_cli, *arguments]
        if file_size_limit is not None:
            command = [
                self.config.prlimit_cli,
                f"--fsize={file_size_limit}",
                "--",
                *command,
            ]
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.config.lark_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise JobError("feishu_unavailable", "Feishu file command failed", True) from exc
        preferred = result.stdout if result.returncode == 0 else result.stderr
        fallback = result.stderr if result.returncode == 0 else result.stdout
        try:
            envelope = extract_json_envelope(preferred)
        except ValueError:
            try:
                envelope = extract_json_envelope(fallback)
            except ValueError as exc:
                raise JobError(
                    "feishu_invalid_response",
                    "Feishu file command returned invalid JSON",
                    True,
                ) from exc
        if result.returncode != 0 or not isinstance(envelope, dict) or envelope.get("ok") is not True:
            error = envelope.get("error") if isinstance(envelope, dict) else None
            code = "feishu_error"
            retryable = True
            if isinstance(error, dict):
                subtype = error.get("subtype")
                if isinstance(subtype, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,80}", subtype):
                    code = f"feishu_{subtype}"
                if error.get("type") in {"authorization", "validation", "confirmation"}:
                    retryable = False
            raise JobError(code, "Feishu rejected the file operation", retryable)
        return envelope

    def download(self, manifest: Mapping[str, object], destination: Path, job_root: Path) -> None:
        relative = destination.relative_to(job_root)
        destination.unlink(missing_ok=True)
        try:
            self._run(
                [
                    "drive",
                    "+download",
                    "--as",
                    self.config.lark_identity,
                    "--file-token",
                    str(manifest["file_token"]),
                    "--output",
                    str(relative),
                    "--overwrite",
                    "--format",
                    "json",
                ],
                job_root,
                file_size_limit=int(manifest["size_bytes"]),
            )
        except JobError:
            destination.unlink(missing_ok=True)
            raise
        if not destination.is_file() or destination.is_symlink():
            raise JobError("feishu_download_missing", "Feishu download produced no regular file", True)

    def upload(self, path: Path, job_root: Path) -> str:
        relative = path.relative_to(job_root)
        target_flag = (
            "--folder-token" if self.config.output_target_type == "folder" else "--wiki-token"
        )
        envelope = self._run(
            [
                "drive",
                "+upload",
                "--as",
                self.config.lark_identity,
                "--file",
                str(relative),
                "--name",
                path.name,
                target_flag,
                self.config.output_target_token,
                "--format",
                "json",
            ],
            job_root,
        )
        token = find_file_token(envelope.get("data"))
        if not token:
            raise JobError("feishu_upload_missing_token", "Feishu upload returned no file token", True)
        return token


def extract_json_envelope(output: object) -> dict[str, object]:
    if not isinstance(output, str):
        raise ValueError("command output is not text")
    decoder = json.JSONDecoder()
    candidate: dict[str, object] | None = None
    for match in re.finditer(r"\{", output):
        try:
            value, _ = decoder.raw_decode(output[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("ok"), bool):
            candidate = value
    if candidate is None:
        raise ValueError("no JSON envelope found")
    return candidate


def find_file_token(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("file_token", "fileToken"):
            candidate = value.get(key)
            if isinstance(candidate, str) and TOKEN_RE.fullmatch(candidate):
                return candidate
        for nested in value.values():
            token = find_file_token(nested)
            if token:
                return token
    elif isinstance(value, list):
        for nested in value:
            token = find_file_token(nested)
            if token:
                return token
    return None


def png_alpha_counts(path: Path) -> tuple[int, int, int] | None:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise JobError(
            "background_removal_unavailable",
            "transparent output validation is not installed",
            False,
        ) from exc

    try:
        with Image.open(path) as image:
            if (
                image.width <= 0
                or image.height <= 0
                or image.width > MAX_CUTOUT_EDGE
                or image.height > MAX_CUTOUT_EDGE
                or image.width * image.height > MAX_CUTOUT_PIXELS
            ):
                raise JobError(
                    "transparent_output_dimensions_unsupported",
                    "transparent output dimensions exceed the local cutout limit",
                    False,
                )
            if image.format != "PNG":
                return None
            image.load()
            if "A" in image.getbands():
                alpha = image.getchannel("A")
            elif "transparency" in image.info:
                alpha = image.convert("RGBA").getchannel("A")
            else:
                return None
            histogram = alpha.histogram()
            total = image.width * image.height
    except JobError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, ValueError) as exc:
        raise JobError(
            "transparent_output_invalid",
            "transparent output could not be decoded as PNG",
            False,
        ) from exc

    transparent = sum(histogram[: ALPHA_NEAR_TRANSPARENT + 1])
    opaque = sum(histogram[ALPHA_NEAR_OPAQUE:])
    return transparent, opaque, total


class BackgroundRemover:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()

    def ensure_transparent(self, path: Path, requested_model: object = None) -> Path:
        alpha = png_alpha_counts(path)
        if alpha is not None:
            transparent, opaque, total = alpha
            required = max(1, (total + ALPHA_COVERAGE_DIVISOR - 1) // ALPHA_COVERAGE_DIVISOR)
            if transparent >= required and opaque >= required:
                return path
            if transparent >= required and opaque < required:
                raise JobError(
                    "transparent_output_empty",
                    "transparent output contains too few visible foreground pixels",
                    False,
                )

        model = (
            requested_model
            if isinstance(requested_model, str)
            else self.config.background_removal_model
        )
        if model not in BACKGROUND_REMOVAL_MODELS:
            raise JobError(
                "background_removal_unavailable",
                "the requested background removal model is unavailable",
                False,
            )

        destination = path.with_suffix(".png")
        temporary = destination.with_name(f".{destination.name}.cutout-{uuid.uuid4().hex}.png")
        environment = {
            "HOME": str(self.config.state_dir),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "OMP_NUM_THREADS": "2",
            "U2NET_HOME": str(self.config.background_removal_model_dir),
            "XDG_CACHE_HOME": str(self.config.state_dir / "cache"),
        }
        command = [
            self.config.background_removal_python,
            str(self.config.background_removal_script),
            "remove",
            "--model",
            model,
            "--input",
            str(path),
            "--output",
            str(temporary),
        ]
        try:
            with self._lock:
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=self.config.background_removal_timeout_seconds,
                    env=environment,
                    check=False,
                )
            if completed.returncode != 0 or not temporary.is_file():
                raise JobError(
                    "background_removal_failed",
                    "local background removal failed; output was not uploaded",
                    False,
                )
            alpha = png_alpha_counts(temporary)
            if alpha is None:
                raise JobError(
                    "transparent_output_unavailable",
                    "background removal did not produce a usable transparent PNG",
                    False,
                )
            required = max(
                1,
                (alpha[2] + ALPHA_COVERAGE_DIVISOR - 1) // ALPHA_COVERAGE_DIVISOR,
            )
            if alpha[0] < required or alpha[1] < required:
                raise JobError(
                    "transparent_output_unavailable",
                    "background removal did not produce a usable transparent PNG",
                    False,
                )
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            if destination != path and destination.exists():
                raise JobError(
                    "invalid_output",
                    "transparent PNG output path already exists",
                    False,
                )
            os.replace(temporary, destination)
            if destination != path:
                path.unlink(missing_ok=True)
            return destination
        except subprocess.TimeoutExpired as exc:
            raise JobError(
                "background_removal_timeout",
                "local background removal timed out; output was not uploaded",
                False,
            ) from exc
        except OSError as exc:
            raise JobError(
                "background_removal_unavailable",
                "local background removal could not be started",
                False,
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)


class ImageBackend:
    def __init__(self, config: Config, background_remover: BackgroundRemover | None = None):
        self.config = config
        self.background_remover = background_remover or BackgroundRemover(config)

    def generate(self, parameters: Mapping[str, object], output_dir: Path, request_id: str) -> list[Path]:
        payload = self._upstream_parameters(parameters)
        response = self._json_request("images/generations", payload)
        return self._save_response_images(response, parameters, output_dir, request_id)

    def edit(
        self,
        parameters: Mapping[str, object],
        inputs: Sequence[tuple[Mapping[str, object], Path]],
        output_dir: Path,
        request_id: str,
    ) -> list[Path]:
        fields = self._upstream_parameters(parameters)
        files = [
            ("mask" if manifest.get("role") == "mask" else "image[]", path, str(manifest["mime_type"]))
            for manifest, path in inputs
        ]
        response = self._multipart_request("images/edits", fields, files)
        return self._save_response_images(response, parameters, output_dir, request_id)

    @staticmethod
    def _upstream_parameters(parameters: Mapping[str, object]) -> dict[str, object]:
        result = {key: value for key, value in parameters.items() if key in IMAGE_PARAMETER_NAMES}
        if result.get("background") == "transparent":
            result["background"] = "auto"
        return result

    def _json_request(self, suffix: str, payload: Mapping[str, object]) -> dict[str, object]:
        body = canonical_json(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.upstream_base_url}/{suffix}",
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.upstream_api_key}",
                "Content-Type": "application/json",
                "User-Agent": "codex-artifact-relay/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.upstream_timeout_seconds) as response:
                data = read_bounded(response, self.config.max_upstream_response_bytes)
        except urllib.error.HTTPError as exc:
            message = parse_upstream_error(read_bounded(exc, 1024 * 1024))
            raise JobError("upstream_rejected", message, exc.code >= 500) from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise JobError(
                "upstream_completion_unknown",
                "image upstream connection failed; completion status is unknown",
                False,
            ) from exc
        return parse_upstream_json(data)

    def _multipart_request(
        self,
        suffix: str,
        fields: Mapping[str, object],
        files: Sequence[tuple[str, Path, str]],
    ) -> dict[str, object]:
        boundary = f"artifact-relay-{uuid.uuid4().hex}"
        parts: list[bytes | Path] = []
        for name, value in fields.items():
            rendered = str(value).lower() if isinstance(value, bool) else str(value)
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{rendered}\r\n"
                ).encode("utf-8")
            )
        for field, path, mime in files:
            header = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field}"; filename="{ascii_filename(path.name)}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode("ascii")
            parts.extend([header, path, b"\r\n"])
        parts.append(f"--{boundary}--\r\n".encode("ascii"))
        length = sum(part.stat().st_size if isinstance(part, Path) else len(part) for part in parts)
        if length > self.config.max_input_bytes + 1024 * 1024:
            raise JobError("inputs_too_large", "edit multipart body exceeds the service limit", False)
        parsed = urllib.parse.urlsplit(f"{self.config.upstream_base_url}/{suffix}")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise JobError("invalid_upstream", "configured upstream URL is invalid", False)
        connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        kwargs: dict[str, object] = {"timeout": self.config.upstream_timeout_seconds}
        if parsed.scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        connection = connection_class(parsed.hostname, parsed.port, **kwargs)
        path = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
        try:
            connection.putrequest("POST", path)
            connection.putheader("Accept", "application/json")
            connection.putheader("Authorization", f"Bearer {self.config.upstream_api_key}")
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(length))
            connection.putheader("User-Agent", "codex-artifact-relay/1.0")
            connection.endheaders()
            for part in parts:
                if isinstance(part, Path):
                    with part.open("rb") as handle:
                        while chunk := handle.read(1024 * 1024):
                            connection.send(chunk)
                else:
                    connection.send(part)
            response = connection.getresponse()
            data = read_bounded(response, self.config.max_upstream_response_bytes)
            if not 200 <= response.status < 300:
                raise JobError("upstream_rejected", parse_upstream_error(data), response.status >= 500)
            return parse_upstream_json(data)
        except JobError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise JobError(
                "upstream_completion_unknown",
                "image upstream connection failed; completion status is unknown",
                False,
            ) from exc
        finally:
            connection.close()

    def _save_response_images(
        self,
        response: Mapping[str, object],
        parameters: Mapping[str, object],
        output_dir: Path,
        request_id: str,
    ) -> list[Path]:
        items = response.get("data")
        if not isinstance(items, list) or not items:
            raise JobError("upstream_invalid_response", "image upstream response is missing data[]", False)
        requested = parameters.get("n", 1)
        if len(items) != requested:
            raise JobError("upstream_invalid_response", "image upstream returned an unexpected image count", False)
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        base_name = safe_filename(parameters.get("output_name"), request_id)
        stem = Path(base_name).stem or request_id
        paths: list[Path] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise JobError("upstream_invalid_response", "image upstream returned an invalid item", False)
            encoded = item.get("b64_json")
            if not isinstance(encoded, str) or not encoded:
                raise JobError("upstream_invalid_response", "image upstream response is missing Base64", False)
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise JobError("upstream_invalid_response", "image upstream returned invalid Base64", False) from exc
            image_format = sniff_image_format(data)
            suffix = f"-{index + 1}" if len(items) > 1 else ""
            path = output_dir / f"{stem}{suffix}{IMAGE_EXTENSION[image_format]}"
            atomic_write(path, data)
            if parameters.get("background") == "transparent":
                path = self.background_remover.ensure_transparent(
                    path, parameters.get("background_removal_model")
                )
            paths.append(path)
        return paths


def read_bounded(response: Any, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise JobError("response_too_large", "upstream response exceeds the service limit", False)
    return data


def parse_upstream_error(data: bytes) -> str:
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "image upstream rejected the request"
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:500]
    return "image upstream rejected the request"


def parse_upstream_json(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise JobError("upstream_invalid_response", "image upstream returned invalid JSON", False) from exc
    if not isinstance(value, dict):
        raise JobError("upstream_invalid_response", "image upstream returned a non-object", False)
    return value


def ascii_filename(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    return result[:120] or "image.bin"


def sniff_image_format(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise JobError("upstream_invalid_response", "decoded output is not PNG, JPEG, or WebP", False)


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.part-{uuid.uuid4().hex}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ArtifactService:
    def __init__(
        self,
        config: Config,
        store: JobStore | None = None,
        drive: LarkDrive | None = None,
        image_backend: ImageBackend | None = None,
        start_workers: bool = True,
    ):
        self.config = config
        self.store = store or JobStore(config.state_dir)
        self.drive = drive or LarkDrive(config)
        self.image_backend = image_backend or ImageBackend(config)
        self.queue: queue.Queue[str | None] = queue.Queue()
        self.workers: list[threading.Thread] = []
        if start_workers:
            self.store.mark_interrupted()
            for index in range(config.worker_count):
                worker = threading.Thread(
                    target=self._worker,
                    name=f"artifact-worker-{index + 1}",
                    daemon=True,
                )
                worker.start()
                self.workers.append(worker)
            for request_id in self.store.list_queued():
                self.queue.put(request_id)

    def capabilities(self) -> dict[str, object]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "delivery": "lark_drive",
            "input_target": {
                "type": self.config.input_target_type,
                "token": self.config.input_target_token,
            },
            "identity": self.config.lark_identity,
            "operations": list(OPERATIONS),
            "retention": "manual",
            "status_values": list(STATUS_VALUES),
            "max_input_bytes": self.config.max_input_bytes,
            "transparent_output": {
                "format": "png",
                "models": sorted(BACKGROUND_REMOVAL_MODELS),
                "default_model": self.config.background_removal_model,
                "alpha_validation": True,
            },
        }

    def submit(self, value: object) -> tuple[dict[str, object], bool]:
        request = validate_request(value, self.config.max_input_bytes)
        job, created = self.store.create(request)
        if created:
            self.queue.put(str(request["request_id"]))
        return job, created

    def get(self, request_id: str) -> dict[str, object]:
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise ApiError(404, "job_not_found", "artifact job was not found")
        job = self.store.get(request_id)
        if not job:
            raise ApiError(404, "job_not_found", "artifact job was not found")
        return job

    def _worker(self) -> None:
        while True:
            request_id = self.queue.get()
            try:
                if request_id is None:
                    return
                self._process(request_id)
            except Exception:
                # _process converts expected failures. Keep an unexpected bug from
                # killing the only worker and expose no sensitive exception text.
                if request_id is not None:
                    try:
                        self.store.update(
                            request_id,
                            status="failed",
                            error=public_error("internal_error", "artifact worker failed", False),
                        )
                    except Exception:
                        pass
            finally:
                self.queue.task_done()

    def _process(self, request_id: str) -> None:
        request = self.store.get_request(request_id)
        job_root = self.config.state_dir / "jobs" / request_id
        input_dir = job_root / "inputs"
        output_dir = job_root / "outputs"
        input_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        downloaded: list[tuple[dict[str, object], Path]] = []
        try:
            inputs = request["inputs"]
            if inputs:
                self.store.update(request_id, status="downloading", error=None)
                for index, raw_manifest in enumerate(inputs):
                    manifest = dict(raw_manifest)
                    destination = input_dir / f"{index + 1:02d}-{manifest['name']}"
                    self.drive.download(manifest, destination, job_root)
                    actual = file_manifest(
                        destination,
                        str(manifest["file_token"]),
                        str(manifest["name"]),
                        manifest.get("mime_type"),
                    )
                    if actual["size_bytes"] != manifest["size_bytes"] or actual["sha256"] != manifest["sha256"]:
                        raise JobError("input_integrity_mismatch", "downloaded input failed integrity checks", False)
                    if actual["size_bytes"] > self.config.max_input_bytes:
                        raise JobError("inputs_too_large", "downloaded input exceeds the service limit", False)
                    if "role" in manifest:
                        actual["role"] = manifest["role"]
                    downloaded.append((actual, destination))
                if sum(int(item[0]["size_bytes"]) for item in downloaded) > self.config.max_input_bytes:
                    raise JobError("inputs_too_large", "downloaded inputs exceed the service limit", False)
                self.store.update(request_id, inputs=[manifest for manifest, _ in downloaded])

            operation = str(request["operation"])
            if operation == "artifact.handoff":
                self.store.update(request_id, status="ready_for_processing", error=None)
                return

            self.store.update(request_id, status="processing", error=None)
            if operation == "image.generate":
                output_paths = self.image_backend.generate(
                    request["parameters"], output_dir, request_id
                )
            elif operation == "image.edit":
                output_paths = self.image_backend.edit(
                    request["parameters"], downloaded, output_dir, request_id
                )
            else:
                raise JobError("unsupported_operation", "operation is not supported", False)
            self._upload_outputs(request_id, output_paths, job_root)
        except JobError as exc:
            self.store.update(
                request_id,
                status="failed",
                error=public_error(exc.code, exc.message, False),
            )

    def _upload_outputs(self, request_id: str, paths: Sequence[Path], job_root: Path) -> None:
        if not paths:
            raise JobError("no_outputs", "processor produced no output files", False)
        output_root = (job_root / "outputs").resolve()
        resolved_paths: list[Path] = []
        relative_paths: list[str] = []
        for path in paths:
            resolved = path.resolve()
            if output_root not in resolved.parents or not resolved.is_file() or resolved.is_symlink():
                raise JobError("invalid_output", "processor output path is invalid", False)
            resolved_paths.append(resolved)
            relative_paths.append(str(resolved.relative_to(job_root)))
        if len(set(relative_paths)) != len(relative_paths):
            raise JobError("invalid_output", "processor returned a duplicate output path", False)

        self.store.prepare_uploads(request_id, relative_paths)
        self.store.update(request_id, status="uploading", error=None)
        upload_states = self.store.get_uploads(request_id)
        for position, (resolved, state) in enumerate(zip(resolved_paths, upload_states, strict=True)):
            manifest = state["manifest"]
            if manifest is not None:
                expected = file_manifest(resolved, str(manifest["file_token"]))
                if expected != manifest:
                    raise JobError(
                        "upload_state_conflict",
                        "stored output upload state does not match the local file",
                        False,
                    )
                continue
            token = self.drive.upload(resolved, job_root)
            self.store.record_upload(request_id, position, file_manifest(resolved, token))

        completed_states = self.store.get_uploads(request_id)
        outputs = [state["manifest"] for state in completed_states]
        if any(manifest is None for manifest in outputs):
            raise JobError("upload_state_conflict", "output upload state is incomplete", False)
        self.store.update(request_id, status="completed", outputs=outputs, error=None)

    def publish_local(self, request_id: str, files: Sequence[Path]) -> dict[str, object]:
        job = self.get(request_id)
        if job["status"] != "ready_for_processing":
            raise ApiError(409, "job_not_ready", "artifact job is not ready for local publishing")
        if not files:
            raise ApiError(400, "no_outputs", "at least one output file is required")
        job_root = self.config.state_dir / "jobs" / request_id
        output_dir = job_root / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        copied: list[Path] = []
        names: set[str] = set()
        for index, source in enumerate(files):
            source = source.resolve()
            if not source.is_file() or source.is_symlink():
                raise ApiError(400, "invalid_output", f"output {index + 1} is not a regular file")
            name = safe_filename(source.name, f"output-{index + 1}.bin")
            if name in names:
                name = f"{index + 1}-{name}"
            names.add(name)
            destination = output_dir / name
            temporary = output_dir / f".{name}.part-{uuid.uuid4().hex}"
            with source.open("rb") as reader, temporary.open("xb") as writer:
                os.chmod(temporary, 0o600)
                shutil.copyfileobj(reader, writer, 1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, destination)
            copied.append(destination)
        try:
            self._upload_outputs(request_id, copied, job_root)
        except JobError as exc:
            return self.store.update(
                request_id,
                status="failed",
                error=public_error(exc.code, exc.message, False),
            )
        return self.get(request_id)

    def local_ready_jobs(self, request_id: str | None = None) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for item in self.store.list_ready(request_id):
            request = item["request"]
            job = item["job"]
            root = self.config.state_dir / "jobs" / str(job["request_id"])
            inputs = []
            for index, manifest in enumerate(job["inputs"]):
                inputs.append(
                    {
                        **manifest,
                        "local_path": str(root / "inputs" / f"{index + 1:02d}-{manifest['name']}"),
                    }
                )
            results.append(
                {
                    "request_id": job["request_id"],
                    "operation": job["operation"],
                    "status": job["status"],
                    "instruction": request["parameters"].get("instruction", ""),
                    "inputs": inputs,
                    "output_directory": str(root / "outputs"),
                }
            )
        return results

    def close(self) -> None:
        for _ in self.workers:
            self.queue.put(None)
        for worker in self.workers:
            worker.join(timeout=5)


class ArtifactHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: ArtifactService):
        self.service = service
        super().__init__(address, ArtifactRequestHandler)


class ArtifactRequestHandler(BaseHTTPRequestHandler):
    server: ArtifactHTTPServer
    server_version = "artifact-relay"
    sys_version = ""

    def log_message(self, format_string: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/healthz":
                self._json(200, {"ok": True})
                return
            self._authorize()
            if path == "/v1/artifact-capabilities":
                self._json(200, self.server.service.capabilities())
                return
            prefix = "/v1/artifact-jobs/"
            if path.startswith(prefix) and path.count("/") == 3:
                request_id = urllib.parse.unquote(path[len(prefix) :])
                self._json(200, self.server.service.get(request_id))
                return
            raise ApiError(404, "not_found", "route was not found")
        except ApiError as exc:
            self._api_error(exc)

    def do_POST(self) -> None:
        try:
            self._authorize()
            path = urllib.parse.urlsplit(self.path).path
            if path != "/v1/artifact-jobs":
                raise ApiError(404, "not_found", "route was not found")
            content_type = self.headers.get_content_type()
            if content_type != "application/json":
                raise ApiError(415, "unsupported_media_type", "Content-Type must be application/json")
            length_header = self.headers.get("Content-Length")
            if length_header is None:
                raise ApiError(411, "length_required", "Content-Length is required")
            try:
                length = int(length_header)
            except ValueError as exc:
                raise ApiError(400, "invalid_request", "invalid Content-Length") from exc
            if length < 0 or length > self.server.service.config.max_request_bytes:
                raise ApiError(413, "request_too_large", "request body exceeds the service limit")
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ApiError(400, "invalid_json", "request body is not valid JSON") from exc
            job, created = self.server.service.submit(value)
            self._json(202 if created else 200, job)
        except ApiError as exc:
            self._api_error(exc)

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._api_error(ApiError(405, "method_not_allowed", "method is not allowed"))

    def _authorize(self) -> None:
        expected = f"Bearer {self.server.service.config.api_key}"
        supplied = self.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, expected):
            raise ApiError(401, "unauthorized", "valid relay key is required")

    def _api_error(self, error: ApiError) -> None:
        headers = {"WWW-Authenticate": 'Bearer realm="codex-artifact-relay"'} if error.status == 401 else None
        self._json(error.status, {"error": public_error(error.code, error.message, error.retryable)}, headers)

    def _json(
        self, status: int, value: object, headers: Mapping[str, str] | None = None
    ) -> None:
        payload = canonical_json(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if headers:
            for name, value in headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)


def run_server(config: Config) -> int:
    service = ArtifactService(config)
    server = ArtifactHTTPServer((config.host, config.port), service)
    stop = threading.Event()

    def handle_signal(_signum: int, _frame: object) -> None:
        if not stop.is_set():
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        service.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex relay Feishu artifact sidecar")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="run the loopback HTTP service")
    ready = subparsers.add_parser("list-ready", help="list local attachment handoff jobs")
    ready.add_argument("--request-id")
    publish = subparsers.add_parser("publish", help="publish local result files for a handoff job")
    publish.add_argument("--request-id", required=True)
    publish.add_argument("--file", action="append", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = Config.from_env()
        if args.command == "serve":
            return run_server(config)
        service = ArtifactService(config, start_workers=False)
        if args.command == "list-ready":
            print(json.dumps(service.local_ready_jobs(args.request_id), ensure_ascii=False, indent=2))
            return 0
        if args.command == "publish":
            result = service.publish_local(args.request_id, [Path(value) for value in args.file])
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "completed" else 1
    except (ApiError, ValueError) as exc:
        message = exc.message if isinstance(exc, ApiError) else str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
