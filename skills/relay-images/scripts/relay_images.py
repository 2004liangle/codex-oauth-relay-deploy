#!/usr/bin/env python3
"""Generate and edit images through the configured Codex OAuth relay."""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import getpass
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import ssl
import stat
import sys
import time
import urllib.parse
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence


EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_ROUTE = 4
EXIT_QUOTA = 5
EXIT_NETWORK = 6
EXIT_RESPONSE = 7
EXIT_FILESYSTEM = 9

MAX_PROMPT_CHARS = 32_000
MAX_INPUT_IMAGES = 16
MAX_INPUT_BYTES = 50_000_000
MAX_EDIT_REQUEST_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024 * 1024
MAX_MASK_DECODE_BYTES = 256 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
CONFIG_ENV = "CODEX_RELAY_IMAGES_CONFIG"
DEFAULT_CONFIG = Path("~/.config/relay-images/config.json").expanduser()
FORMAT_EXTENSIONS = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}
USAGE_FIELDS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "text_tokens",
    "image_tokens",
    "input_tokens_details",
    "output_tokens_details",
}


class RelayError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_USAGE):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class RelayConfig:
    base_url: str
    api_key: str
    allow_http: bool
    parsed_url: urllib.parse.SplitResult


@dataclass(frozen=True)
class InputFile:
    field: str
    path: Path
    mime: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes
    duration_ms: int


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def config_path(value: str | None) -> Path:
    raw = value or os.environ.get(CONFIG_ENV)
    return Path(raw).expanduser() if raw else DEFAULT_CONFIG


def read_config(path: Path) -> dict[str, object]:
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RelayError(f"secret config is not a regular file: {path}")
        mode = metadata.st_mode & 0o777
        if mode & 0o077:
            raise RelayError(
                f"config permissions are {mode:03o}; run chmod 600 {path} before use"
            )
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    except FileNotFoundError:
        return {}
    except RelayError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise RelayError(f"cannot read config {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise RelayError(f"config {path} must contain a JSON object")
    return value


def is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def normalize_base_url(raw: str, allow_http: bool) -> tuple[str, urllib.parse.SplitResult]:
    value = raw.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise RelayError("relay Base URL must start with http:// or https://")
    if not parsed.hostname or parsed.username or parsed.password:
        raise RelayError("relay Base URL must contain a host and no embedded credentials")
    try:
        parsed.port
    except ValueError as exc:
        raise RelayError("relay Base URL contains an invalid port") from exc
    if parsed.query or parsed.fragment:
        raise RelayError("relay Base URL must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    if path != "/v1" and not path.endswith("/v1"):
        raise RelayError("relay Base URL must end with /v1")
    parsed = parsed._replace(path=path, query="", fragment="")
    if parsed.scheme == "http" and not is_loopback(parsed.hostname) and not allow_http:
        raise RelayError(
            "remote HTTP exposes the key and images; configure HTTPS or explicitly use --allow-http"
        )
    return urllib.parse.urlunsplit(parsed), parsed


def resolve_config(args: argparse.Namespace, require_key: bool = True) -> RelayConfig:
    stored = read_config(config_path(args.config))
    explicit_base = args.base_url or os.environ.get("CODEX_RELAY_BASE_URL")
    raw_base = explicit_base or stored.get("base_url")
    key = os.environ.get("CODEX_RELAY_API_KEY") or stored.get("api_key") or ""
    stored_http_base = stored.get("allow_http_base_url")
    allow_http = bool(
        args.allow_http
        or (
            explicit_base is None
            and isinstance(raw_base, str)
            and isinstance(stored_http_base, str)
            and raw_base.rstrip("/") == stored_http_base.rstrip("/")
        )
    )
    if not isinstance(raw_base, str) or not raw_base.strip():
        raise RelayError(
            "relay Base URL is missing; run configure or set CODEX_RELAY_BASE_URL"
        )
    if require_key and (not isinstance(key, str) or not key):
        raise RelayError(
            "relay API key is missing; run configure or set CODEX_RELAY_API_KEY"
        )
    base_url, parsed = normalize_base_url(raw_base, allow_http)
    if parsed.scheme == "http" and not is_loopback(parsed.hostname or ""):
        eprint("warning: relay traffic is using unencrypted HTTP")
    return RelayConfig(base_url, str(key), allow_http, parsed)


def connection(config: RelayConfig, timeout: float) -> http.client.HTTPConnection:
    host = config.parsed_url.hostname
    assert host is not None
    port = config.parsed_url.port
    if config.parsed_url.scheme == "https":
        return http.client.HTTPSConnection(
            host,
            port or 443,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(host, port or 80, timeout=timeout)


def endpoint_path(config: RelayConfig, suffix: str) -> str:
    return f"{config.parsed_url.path.rstrip('/')}/{suffix.lstrip('/')}"


def read_limited(response: http.client.HTTPResponse) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(CHUNK_SIZE)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise RelayError("relay response exceeded the 256 MiB safety limit", EXIT_RESPONSE)
        chunks.append(chunk)


def headers_dict(response: http.client.HTTPResponse) -> dict[str, str]:
    return {key.lower(): value for key, value in response.getheaders()}


def request(
    config: RelayConfig,
    method: str,
    suffix: str,
    body: bytes | None,
    content_type: str | None,
    timeout: float,
) -> HttpResult:
    conn = connection(config, timeout)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.api_key}",
        "User-Agent": "relay-images/1.0",
    }
    if content_type:
        headers["Content-Type"] = content_type
    started = time.monotonic()
    try:
        conn.request(method, endpoint_path(config, suffix), body=body, headers=headers)
        response = conn.getresponse()
        result = HttpResult(
            response.status,
            headers_dict(response),
            read_limited(response),
            int((time.monotonic() - started) * 1000),
        )
        return result
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise RelayError(
            "relay network error; completion status is unknown",
            EXIT_NETWORK,
        ) from exc
    finally:
        conn.close()


def json_request(
    config: RelayConfig,
    suffix: str,
    payload: Mapping[str, object],
    timeout: float,
) -> HttpResult:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return request(config, "POST", suffix, body, "application/json", timeout)


def clean_error_text(body: bytes) -> str:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "non-JSON response" if body else "empty response"
    return "request rejected"


def raise_for_status(result: HttpResult, endpoint: str) -> None:
    if 200 <= result.status < 300:
        return
    detail = clean_error_text(result.body)
    if result.status in {401, 403}:
        raise RelayError(f"relay authentication failed ({result.status}): {detail}", EXIT_AUTH)
    if result.status in {404, 405}:
        raise RelayError(f"relay route {endpoint} is unavailable ({result.status}): {detail}", EXIT_ROUTE)
    if result.status == 413:
        raise RelayError("relay rejected the request as too large (413)", EXIT_ROUTE)
    if result.status == 429:
        retry_after = result.headers.get("retry-after")
        hint = (
            f"; retry after {retry_after} seconds"
            if retry_after and re.fullmatch(r"[0-9]{1,6}", retry_after)
            else ""
        )
        raise RelayError(f"relay quota or rate limit reached (429){hint}: {detail}", EXIT_QUOTA)
    if result.status >= 500:
        raise RelayError(
            f"relay/upstream failure ({result.status}); completion status may be unknown: {detail}",
            EXIT_NETWORK,
        )
    raise RelayError(f"image request failed ({result.status}): {detail}", EXIT_USAGE)


def parse_json_response(result: HttpResult, endpoint: str) -> dict[str, object]:
    raise_for_status(result, endpoint)
    try:
        value = json.loads(result.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RelayError("relay returned a non-JSON success response", EXIT_RESPONSE) from exc
    if not isinstance(value, dict):
        raise RelayError("relay returned an unexpected JSON value", EXIT_RESPONSE)
    return value


def sniff_image(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise RelayError("decoded output is not PNG, JPEG, or WebP", EXIT_RESPONSE)


def image_dimensions(data: bytes, fmt: str) -> tuple[int, int] | None:
    if fmt == "png" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if fmt == "jpeg":
        offset = 2
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(data):
                break
            length = int.from_bytes(data[offset : offset + 2], "big")
            if length < 2 or offset + length > len(data):
                break
            if marker in sof_markers and length >= 7:
                height = int.from_bytes(data[offset + 3 : offset + 5], "big")
                width = int.from_bytes(data[offset + 5 : offset + 7], "big")
                return width, height
            offset += length
    if fmt == "webp" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
        if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
        if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return width, height
    return None


def decode_image(value: object) -> tuple[bytes, str]:
    if not isinstance(value, str) or not value:
        raise RelayError("relay response is missing image Base64", EXIT_RESPONSE)
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RelayError("relay returned invalid image Base64", EXIT_RESPONSE) from exc
    return data, sniff_image(data)


def adjusted_output_path(
    path: Path,
    fmt: str,
    index: int,
    count: int,
    label: str | None,
    warn: bool = True,
) -> Path:
    extension = FORMAT_EXTENSIONS[fmt]
    if path.exists() and path.is_dir():
        path = path / f"image{extension}"
    stem = path.stem if path.suffix else path.name
    parent = path.parent
    suffix = ""
    if label:
        suffix += f"-{label}"
    if count > 1 or label == "partial":
        suffix += f"-{index + 1}"
    target = parent / f"{stem}{suffix}{extension}"
    if warn and path.suffix.lower() not in {"", extension, ".jpeg" if fmt == "jpeg" else extension}:
        eprint(f"warning: output format is {fmt}; writing {target.name}")
    return target


def atomic_write(path: Path, data: bytes, overwrite: bool) -> None:
    temp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.parent / f".{path.name}.part-{os.getpid()}-{secrets.token_hex(4)}"
        descriptor = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temp, path)
        else:
            try:
                os.link(temp, path)
            except FileExistsError as exc:
                raise RelayError(f"output already exists: {path}; use --overwrite", EXIT_FILESYSTEM) from exc
            finally:
                temp.unlink(missing_ok=True)
    except RelayError:
        raise
    except OSError as exc:
        try:
            if temp is not None:
                temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise RelayError(f"cannot write output {path}: {exc}", EXIT_FILESYSTEM) from exc


def save_images(
    encoded: Sequence[object],
    output: str | None,
    operation: str,
    overwrite: bool,
    label: str | None = None,
) -> list[dict[str, object]]:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    requested = Path(output).expanduser() if output else Path(f"{operation}-{timestamp}.png")
    decoded = [decode_image(item) for item in encoded]
    targets = [
        adjusted_output_path(requested, fmt, index, len(decoded), label)
        for index, (_, fmt) in enumerate(decoded)
    ]
    if not overwrite:
        existing = next((target for target in targets if target.exists()), None)
        if existing is not None:
            raise RelayError(f"output already exists: {existing}; use --overwrite", EXIT_FILESYSTEM)
    saved: list[dict[str, object]] = []
    for (data, fmt), target in zip(decoded, targets):
        atomic_write(target, data, overwrite)
        item: dict[str, object] = {
            "path": str(target.resolve()),
            "format": fmt,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        dimensions = image_dimensions(data, fmt)
        if dimensions:
            item["width"] = dimensions[0]
            item["height"] = dimensions[1]
        saved.append(item)
    return saved


def preflight_target(path: Path, overwrite: bool) -> None:
    if path.exists():
        if path.is_dir():
            raise RelayError(f"output target is a directory: {path}", EXIT_FILESYSTEM)
        if not overwrite:
            raise RelayError(f"output already exists: {path}; use --overwrite", EXIT_FILESYSTEM)
    ancestor = path.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise RelayError(f"output parent is not a directory: {ancestor}", EXIT_FILESYSTEM)
    if not os.access(ancestor, os.W_OK | os.X_OK):
        raise RelayError(f"output directory is not writable: {ancestor}", EXIT_FILESYSTEM)


def dry_run_output_plan(args: argparse.Namespace, operation: str) -> dict[str, object]:
    if not args.output:
        timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        args.output = f"{operation}-{timestamp}{FORMAT_EXTENSIONS[args.output_format]}"
    requested = Path(args.output).expanduser()
    final_count = 1 if args.stream or args.partial_images else args.n
    final_paths = [
        adjusted_output_path(requested, args.output_format, index, final_count, None)
        for index in range(final_count)
    ]
    partial_paths = [
        adjusted_output_path(requested, args.output_format, index, args.partial_images, "partial")
        for index in range(args.partial_images)
    ]
    possible_paths: set[Path] = set()
    for fmt in FORMAT_EXTENSIONS:
        possible_paths.update(
            adjusted_output_path(requested, fmt, index, final_count, None, warn=False)
            for index in range(final_count)
        )
        possible_paths.update(
            adjusted_output_path(requested, fmt, index, args.partial_images, "partial", warn=False)
            for index in range(args.partial_images)
        )
    for path in possible_paths:
        preflight_target(path, bool(args.overwrite))
    return {
        "overwrite": bool(args.overwrite),
        "final_paths": [str(path) for path in final_paths],
        "possible_partial_paths": [str(path) for path in partial_paths],
    }


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None and args.prompt_file is not None:
        raise RelayError("use either --prompt or --prompt-file, not both")
    if args.prompt_file is not None:
        try:
            prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise RelayError(f"cannot read prompt file: {exc}") from exc
    else:
        prompt = args.prompt or ""
    prompt = prompt.strip()
    if not prompt:
        raise RelayError("prompt is required")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise RelayError(f"prompt exceeds {MAX_PROMPT_CHARS} characters")
    return prompt


def validate_size(value: str) -> str:
    if value == "auto":
        return value
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", value)
    if not match:
        raise RelayError("size must be auto or WIDTHxHEIGHT")
    width, height = map(int, match.groups())
    if width % 16 or height % 16:
        raise RelayError("gpt-image-2 width and height must be multiples of 16")
    if max(width, height) > 3840:
        raise RelayError("gpt-image-2 maximum edge is 3840 pixels")
    if max(width, height) / min(width, height) > 3:
        raise RelayError("gpt-image-2 aspect ratio must not exceed 3:1")
    pixels = width * height
    if not 655_360 <= pixels <= 8_294_400:
        raise RelayError("gpt-image-2 size must contain 655360 to 8294400 pixels")
    if pixels > 3_686_400:
        eprint("warning: resolutions above 2560x1440 total pixels are experimental")
    return value


def image_options(args: argparse.Namespace) -> dict[str, object]:
    if not 1 <= args.n <= 10:
        raise RelayError("--n must be between 1 and 10")
    if not 0 <= args.partial_images <= 3:
        raise RelayError("--partial-images must be between 0 and 3")
    if args.compression is not None and args.output_format not in {"jpeg", "webp"}:
        raise RelayError("--compression is only valid with jpeg or webp")
    if args.compression is not None and not 0 <= args.compression <= 100:
        raise RelayError("--compression must be between 0 and 100")
    if args.model.startswith("gpt-image-2") and args.background == "transparent":
        raise RelayError("gpt-image-2 does not support transparent backgrounds")
    stream = bool(args.stream or args.partial_images)
    if stream and args.n != 1:
        raise RelayError("streaming currently requires --n 1")
    options: dict[str, object] = {
        "model": args.model,
        "n": args.n,
        "quality": args.quality,
        "size": validate_size(args.size),
        "output_format": args.output_format,
        "background": args.background,
        "moderation": args.moderation,
    }
    if args.compression is not None:
        options["output_compression"] = args.compression
    if stream:
        options["stream"] = True
        options["partial_images"] = args.partial_images
    return options


def request_id_digest(headers: Mapping[str, str]) -> str | None:
    value = headers.get("x-request-id") or headers.get("request-id")
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def sanitized_usage(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if key not in USAGE_FIELDS:
            continue
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            result[key] = item
        elif isinstance(item, dict):
            nested = sanitized_usage(item)
            if nested:
                result[key] = nested
    return result or None


def output_summary(
    operation: str,
    endpoint: str,
    args: argparse.Namespace,
    result: HttpResult,
    images: list[dict[str, object]],
    usage: object = None,
    contract_issues: Sequence[str] = (),
) -> None:
    strict_failure = bool(args.strict_output and contract_issues)
    summary: dict[str, object] = {
        "ok": not strict_failure,
        "operation": operation,
        "endpoint": endpoint,
        "model": args.model,
        "quality": args.quality,
        "requested_size": args.size,
        "requested_format": args.output_format,
        "output_contract_met": not contract_issues,
        "duration_ms": result.duration_ms,
        "images": images,
    }
    if contract_issues:
        summary["output_contract_issues"] = list(contract_issues)
    rid = request_id_digest(result.headers)
    if rid:
        summary["request_id_sha256"] = rid
    safe_usage = sanitized_usage(usage)
    if safe_usage:
        summary["usage"] = safe_usage
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if strict_failure:
        raise RelayError(
            "saved output does not match the requested format or dimensions",
            EXIT_RESPONSE,
        )


def output_contract_issues(
    requested_size: str,
    requested_format: str,
    images: Sequence[Mapping[str, object]],
) -> list[str]:
    issues: list[str] = []
    actual_formats = {
        str(item["format"])
        for item in images
        if isinstance(item.get("format"), str)
    }
    if actual_formats != {requested_format}:
        values = ", ".join(sorted(actual_formats)) or "unknown"
        issues.append(f"requested format {requested_format}; received {values}")
    if requested_size != "auto":
        match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", requested_size)
        assert match is not None
        expected = tuple(map(int, match.groups()))
        known_sizes = [
            (int(item["width"]), int(item["height"]))
            for item in images
            if isinstance(item.get("width"), int) and isinstance(item.get("height"), int)
        ]
        actual_sizes = set(known_sizes)
        if len(known_sizes) != len(images) or actual_sizes != {expected}:
            values = ", ".join(f"{width}x{height}" for width, height in sorted(actual_sizes)) or "unknown"
            issues.append(f"requested dimensions {requested_size}; received {values}")
    return issues


def finish_output(
    operation: str,
    endpoint: str,
    args: argparse.Namespace,
    result: HttpResult,
    all_images: list[dict[str, object]],
    final_images: Sequence[Mapping[str, object]],
    usage: object = None,
) -> None:
    issues = output_contract_issues(args.size, args.output_format, final_images)
    warn_dimension_mismatch(args.size, final_images)
    warn_format_mismatch(args.output_format, final_images)
    output_summary(operation, endpoint, args, result, all_images, usage, issues)


def warn_dimension_mismatch(requested_size: str, images: Sequence[Mapping[str, object]]) -> None:
    if requested_size == "auto":
        return
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", requested_size)
    if not match:
        return
    expected = tuple(map(int, match.groups()))
    actual = {
        (int(item["width"]), int(item["height"]))
        for item in images
        if isinstance(item.get("width"), int) and isinstance(item.get("height"), int)
    }
    if actual and actual != {expected}:
        values = ", ".join(f"{width}x{height}" for width, height in sorted(actual))
        eprint(
            f"warning: requested {requested_size}, but the relay returned {values}; "
            "use the reported file dimensions as authoritative"
        )


def warn_format_mismatch(requested_format: str, images: Sequence[Mapping[str, object]]) -> None:
    actual = {
        str(item["format"])
        for item in images
        if isinstance(item.get("format"), str)
    }
    if actual and actual != {requested_format}:
        values = ", ".join(sorted(actual))
        eprint(
            f"warning: requested {requested_format}, but the relay returned {values}; "
            "the saved extension follows the decoded file signature"
        )


def sse_events(response: http.client.HTTPResponse) -> Iterator[dict[str, object]]:
    data_lines: list[bytes] = []
    total = 0
    while True:
        line = response.readline(MAX_RESPONSE_BYTES + 1)
        if not line:
            if data_lines:
                joined = b"\n".join(data_lines)
                if joined != b"[DONE]":
                    yield parse_sse_json(joined)
            return
        total += len(line)
        if total > MAX_RESPONSE_BYTES:
            raise RelayError("stream exceeded the 256 MiB safety limit", EXIT_RESPONSE)
        if line in {b"\n", b"\r\n"}:
            if data_lines:
                joined = b"\n".join(data_lines)
                data_lines.clear()
                if joined != b"[DONE]":
                    yield parse_sse_json(joined)
            continue
        if line.startswith(b"data:"):
            data_lines.append(line[5:].strip())


def parse_sse_json(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RelayError("relay returned invalid SSE JSON", EXIT_RESPONSE) from exc
    if not isinstance(value, dict):
        raise RelayError("relay returned an unexpected SSE value", EXIT_RESPONSE)
    return value


def collect_image_stream(
    events: Iterable[dict[str, object]],
    max_partials: int,
) -> tuple[list[dict[str, object]], dict[str, object], object]:
    partials: list[dict[str, object]] = []
    final: dict[str, object] | None = None
    usage: object = None
    for event in events:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            continue
        if event_type.endswith(".partial_image"):
            if final is not None or len(partials) >= max_partials:
                raise RelayError("relay returned an unexpected number or order of partial images", EXIT_RESPONSE)
            encoded = event.get("b64_json")
            if not isinstance(encoded, str) or not encoded:
                raise RelayError("partial image event is missing Base64", EXIT_RESPONSE)
            partials.append({"encoded": encoded})
        elif event_type.endswith(".completed"):
            if final is not None:
                raise RelayError("relay returned more than one completed image event", EXIT_RESPONSE)
            encoded = event.get("b64_json")
            if not isinstance(encoded, str) or not encoded:
                raise RelayError("completed image event is missing Base64", EXIT_RESPONSE)
            final = {"encoded": encoded}
            usage = event.get("usage")
    if final is None:
        raise RelayError("image stream completed without a final image", EXIT_RESPONSE)
    return partials, final, usage


def stream_request(
    config: RelayConfig,
    suffix: str,
    body: bytes | "MultipartBody",
    content_type: str,
    timeout: float,
    max_partials: int,
) -> tuple[HttpResult, list[dict[str, object]], object]:
    conn = connection(config, timeout)
    path = endpoint_path(config, suffix)
    headers = {
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "User-Agent": "relay-images/1.0",
    }
    started = time.monotonic()
    try:
        conn.putrequest("POST", path)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        if isinstance(body, bytes):
            conn.send(body)
        else:
            for chunk in body:
                conn.send(chunk)
        response = conn.getresponse()
        response_headers = headers_dict(response)
        if not 200 <= response.status < 300:
            result = HttpResult(
                response.status,
                response_headers,
                read_limited(response),
                int((time.monotonic() - started) * 1000),
            )
            raise_for_status(result, f"/{suffix}")
        partials, final, usage = collect_image_stream(sse_events(response), max_partials)
        result = HttpResult(
            response.status,
            response_headers,
            b"",
            int((time.monotonic() - started) * 1000),
        )
        return result, partials + [final], usage
    except RelayError:
        raise
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise RelayError(
            "relay stream failed; completion status is unknown",
            EXIT_NETWORK,
        ) from exc
    finally:
        conn.close()


def run_generate(args: argparse.Namespace) -> None:
    prompt = read_prompt(args)
    options = image_options(args)
    payload = {**options, "prompt": prompt}
    output_plan = dry_run_output_plan(args, "generated")
    if args.dry_run:
        print(json.dumps({"operation": "generate", "endpoint": "/v1/images/generations", "payload": payload, "output": output_plan}, ensure_ascii=False, indent=2))
        return
    config = resolve_config(args)
    if payload.get("stream"):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        result, events, usage = stream_request(
            config,
            "images/generations",
            body,
            "application/json",
            args.timeout,
            args.partial_images,
        )
        partial = [item["encoded"] for item in events[:-1]]
        saved = save_images(partial, args.output, "generated", args.overwrite, "partial") if partial else []
        final_saved = save_images([events[-1]["encoded"]], args.output, "generated", args.overwrite)
        saved += final_saved
        finish_output(
            "generate", "/v1/images/generations", args, result, saved, final_saved, usage
        )
        return
    result = json_request(config, "images/generations", payload, args.timeout)
    response = parse_json_response(result, "/v1/images/generations")
    items = response.get("data")
    if not isinstance(items, list):
        raise RelayError("relay response is missing data[]", EXIT_RESPONSE)
    encoded = [item.get("b64_json") for item in items if isinstance(item, dict)]
    if len(encoded) != len(items) or not encoded:
        raise RelayError("relay response is missing one or more images", EXIT_RESPONSE)
    if len(encoded) != args.n:
        raise RelayError(
            f"relay returned {len(encoded)} images after {args.n} were requested",
            EXIT_RESPONSE,
        )
    saved = save_images(encoded, args.output, "generated", args.overwrite)
    finish_output(
        "generate",
        "/v1/images/generations",
        args,
        result,
        saved,
        saved,
        response.get("usage"),
    )


def detect_input(path: str, field: str = "image[]") -> InputFile:
    candidate = Path(path).expanduser()
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(candidate, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RelayError(f"input image is not a regular file: {candidate}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(MAX_INPUT_BYTES + 1)
    except RelayError:
        raise
    except OSError as exc:
        raise RelayError(f"cannot read input image {candidate}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not data:
        raise RelayError(f"input image is empty: {candidate}")
    if len(data) >= MAX_INPUT_BYTES:
        raise RelayError(f"input image must be smaller than 50 MB: {candidate}")
    fmt = sniff_image(data[:32])
    mime = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}[fmt]
    return InputFile(field, candidate.resolve(), mime, data)


def parse_png(item: InputFile) -> tuple[int, int, int, int, int, bytes]:
    data = item.data
    path = item.path
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RelayError(f"mask and its first input must be valid PNG files: {path}")
    offset = 8
    header: tuple[int, int, int, int, int] | None = None
    idat = bytearray()
    seen_iend = False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            break
        chunk_data = data[offset + 8 : offset + 8 + length]
        expected_crc = int.from_bytes(data[offset + 8 + length : chunk_end], "big")
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
            raise RelayError(f"PNG chunk checksum failed: {path}")
        if header is None and chunk_type != b"IHDR":
            raise RelayError(f"PNG does not start with IHDR: {path}")
        if chunk_type == b"IHDR":
            if header is not None or length != 13:
                raise RelayError(f"PNG has an invalid IHDR chunk: {path}")
            width = int.from_bytes(chunk_data[0:4], "big")
            height = int.from_bytes(chunk_data[4:8], "big")
            bit_depth = chunk_data[8]
            color_type = chunk_data[9]
            compression = chunk_data[10]
            filtering = chunk_data[11]
            interlace = chunk_data[12]
            if width <= 0 or height <= 0 or compression != 0 or filtering != 0:
                raise RelayError(f"PNG has unsupported header values: {path}")
            header = (width, height, bit_depth, color_type, interlace)
        elif chunk_type == b"IDAT":
            idat.extend(chunk_data)
        elif chunk_type == b"IEND":
            if length != 0:
                raise RelayError(f"PNG has an invalid IEND chunk: {path}")
            seen_iend = True
            offset = chunk_end
            break
        offset = chunk_end
    if header is None or not idat or not seen_iend or offset != len(data):
        raise RelayError(f"PNG is truncated or malformed: {path}")
    return (*header, bytes(idat))


def png_info(item: InputFile) -> tuple[int, int, bool]:
    width, height, _, color_type, _, _ = parse_png(item)
    return width, height, color_type in {4, 6}


def paeth_predictor(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def validate_mask_alpha(item: InputFile) -> None:
    path = item.path
    width, height, bit_depth, color_type, interlace, idat = parse_png(item)
    if color_type not in {4, 6}:
        raise RelayError("mask PNG must contain an alpha channel")
    if bit_depth not in {8, 16} or interlace != 0:
        raise RelayError("mask must be a non-interlaced 8-bit or 16-bit alpha PNG")
    sample_bytes = bit_depth // 8
    channels = 2 if color_type == 4 else 4
    bytes_per_pixel = channels * sample_bytes
    row_bytes = width * bytes_per_pixel
    expected_size = (row_bytes + 1) * height
    if expected_size > MAX_MASK_DECODE_BYTES:
        raise RelayError("decoded mask exceeds the 256 MiB safety limit")
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(idat, expected_size + 1)
    except zlib.error as exc:
        raise RelayError(f"mask PNG pixel data is corrupt: {path}") from exc
    if (
        len(raw) != expected_size
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise RelayError(f"mask PNG pixel data is malformed: {path}")

    previous = bytearray(row_bytes)
    position = 0
    has_editable = False
    has_protected = False
    alpha_offset = (channels - 1) * sample_bytes
    opaque_alpha = (1 << bit_depth) - 1
    for _ in range(height):
        filter_type = raw[position]
        position += 1
        if filter_type > 4:
            raise RelayError(f"mask PNG uses an invalid row filter: {path}")
        filtered = raw[position : position + row_bytes]
        position += row_bytes
        current = bytearray(row_bytes)
        for index, value in enumerate(filtered):
            left = current[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            above = previous[index]
            upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            if filter_type == 0:
                reconstructed = value
            elif filter_type == 1:
                reconstructed = value + left
            elif filter_type == 2:
                reconstructed = value + above
            elif filter_type == 3:
                reconstructed = value + ((left + above) // 2)
            else:
                reconstructed = value + paeth_predictor(left, above, upper_left)
            current[index] = reconstructed & 0xFF
        for index in range(alpha_offset, row_bytes, bytes_per_pixel):
            alpha = int.from_bytes(current[index : index + sample_bytes], "big")
            has_protected |= alpha == opaque_alpha
            has_editable |= alpha < opaque_alpha
        previous = current
    if not has_editable or not has_protected:
        raise RelayError("mask alpha must contain both editable and protected regions")


def validate_edit_files(images: Sequence[str], mask: str | None) -> list[InputFile]:
    if not 1 <= len(images) <= MAX_INPUT_IMAGES:
        raise RelayError(f"edit requires 1 to {MAX_INPUT_IMAGES} input images")
    files = [detect_input(value) for value in images]
    if mask:
        mask_file = detect_input(mask, "mask")
        first_size = png_info(files[0])
        mask_size = png_info(mask_file)
        if first_size[:2] != mask_size[:2]:
            raise RelayError("mask dimensions must match the first input image")
        if not mask_size[2]:
            raise RelayError("mask PNG must contain an alpha channel")
        validate_mask_alpha(mask_file)
        files.append(mask_file)
    return files


def safe_filename(path: Path) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]", "_", path.name)
    return value[:120] or "image.bin"


class MultipartBody:
    def __init__(self, fields: Mapping[str, object], files: Sequence[InputFile]):
        self.boundary = f"relay-images-{secrets.token_hex(16)}"
        self._parts: list[bytes | InputFile] = []
        for name, value in fields.items():
            prefix = (
                f"--{self.boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{str(value).lower() if isinstance(value, bool) else value}\r\n"
            ).encode("utf-8")
            self._parts.append(prefix)
        for item in files:
            prefix = (
                f"--{self.boundary}\r\n"
                f'Content-Disposition: form-data; name="{item.field}"; filename="{safe_filename(item.path)}"\r\n'
                f"Content-Type: {item.mime}\r\n\r\n"
            ).encode("ascii")
            self._parts.extend([prefix, item, b"\r\n"])
        self._parts.append(f"--{self.boundary}--\r\n".encode("ascii"))
        self.length = sum(part.size if isinstance(part, InputFile) else len(part) for part in self._parts)

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    def __len__(self) -> int:
        return self.length

    def __iter__(self) -> Iterator[bytes]:
        for part in self._parts:
            if not isinstance(part, InputFile):
                yield part
                continue
            for offset in range(0, part.size, CHUNK_SIZE):
                yield part.data[offset : offset + CHUNK_SIZE]


def validate_edit_body_size(body: MultipartBody) -> None:
    if len(body) > MAX_EDIT_REQUEST_BYTES:
        raise RelayError("edit multipart body exceeds the relay's 64 MiB safety limit")


def multipart_request(
    config: RelayConfig,
    suffix: str,
    body: MultipartBody,
    timeout: float,
) -> HttpResult:
    conn = connection(config, timeout)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": body.content_type,
        "Content-Length": str(len(body)),
        "User-Agent": "relay-images/1.0",
    }
    started = time.monotonic()
    try:
        conn.putrequest("POST", endpoint_path(config, suffix))
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        for chunk in body:
            conn.send(chunk)
        response = conn.getresponse()
        return HttpResult(
            response.status,
            headers_dict(response),
            read_limited(response),
            int((time.monotonic() - started) * 1000),
        )
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise RelayError(
            "relay upload failed; completion status is unknown",
            EXIT_NETWORK,
        ) from exc
    finally:
        conn.close()


def run_edit(args: argparse.Namespace) -> None:
    prompt = read_prompt(args)
    options = image_options(args)
    files = validate_edit_files(args.image, args.mask)
    fields = {**options, "prompt": prompt}
    body = MultipartBody(fields, files)
    validate_edit_body_size(body)
    output_plan = dry_run_output_plan(args, "edited")
    if args.dry_run:
        safe_files = [
            {"field": item.field, "path": str(item.path), "mime": item.mime, "bytes": item.size}
            for item in files
        ]
        print(json.dumps({"operation": "edit", "endpoint": "/v1/images/edits", "fields": fields, "files": safe_files, "multipart_bytes": len(body), "output": output_plan}, ensure_ascii=False, indent=2))
        return
    config = resolve_config(args)
    if fields.get("stream"):
        result, events, usage = stream_request(
            config,
            "images/edits",
            body,
            body.content_type,
            args.timeout,
            args.partial_images,
        )
        partial = [item["encoded"] for item in events[:-1]]
        saved = save_images(partial, args.output, "edited", args.overwrite, "partial") if partial else []
        final_saved = save_images([events[-1]["encoded"]], args.output, "edited", args.overwrite)
        saved += final_saved
        finish_output("edit", "/v1/images/edits", args, result, saved, final_saved, usage)
        return
    result = multipart_request(config, "images/edits", body, args.timeout)
    response = parse_json_response(result, "/v1/images/edits")
    items = response.get("data")
    if not isinstance(items, list):
        raise RelayError("relay response is missing data[]", EXIT_RESPONSE)
    encoded = [item.get("b64_json") for item in items if isinstance(item, dict)]
    if len(encoded) != len(items) or not encoded:
        raise RelayError("relay response is missing one or more edited images", EXIT_RESPONSE)
    if len(encoded) != args.n:
        raise RelayError(
            f"relay returned {len(encoded)} edited images after {args.n} were requested",
            EXIT_RESPONSE,
        )
    saved = save_images(encoded, args.output, "edited", args.overwrite)
    finish_output(
        "edit", "/v1/images/edits", args, result, saved, saved, response.get("usage")
    )


def run_check(args: argparse.Namespace) -> None:
    config = resolve_config(args)
    models = request(config, "GET", "models", None, None, args.timeout)
    generation = json_request(config, "images/generations", {}, args.timeout)
    edit_body = MultipartBody({"model": "gpt-image-2", "prompt": "route check"}, [])
    edit = multipart_request(config, "images/edits", edit_body, args.timeout)
    statuses = {
        "models": models.status,
        "images_generations": generation.status,
        "images_edits": edit.status,
    }
    ready = models.status == 200 and generation.status == 400 and edit.status == 400
    print(json.dumps({"ok": ready, "base_url": config.base_url, "status": statuses}, ensure_ascii=False, indent=2))
    if not ready:
        if any(value in {401, 403} for value in statuses.values()):
            raise RelayError("relay check failed authentication", EXIT_AUTH)
        raise RelayError("relay image routes are not ready", EXIT_ROUTE)


def atomic_config_write(path: Path, value: Mapping[str, object]) -> None:
    temp: Path | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = path.parent / f".{path.name}.part-{os.getpid()}-{secrets.token_hex(4)}"
        descriptor = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = None
        os.chmod(path, 0o600)
    except OSError as exc:
        raise RelayError(f"cannot write config {path}: {exc}", EXIT_FILESYSTEM) from exc
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def run_configure(args: argparse.Namespace) -> None:
    raw_key = os.environ.get("CODEX_RELAY_API_KEY")
    if raw_key is None:
        raw_key = sys.stdin.readline().rstrip("\r\n") if args.key_stdin else getpass.getpass("Relay API key: ")
    if not raw_key or "\n" in raw_key or "\r" in raw_key:
        raise RelayError("relay API key is empty or invalid")
    base_url, parsed = normalize_base_url(args.configure_base_url, args.allow_http)
    target = config_path(args.config)
    atomic_config_write(
        target,
        {
            "base_url": base_url,
            "api_key": raw_key,
            **(
                {"allow_http_base_url": base_url}
                if parsed.scheme == "http" and args.allow_http
                else {}
            ),
        },
    )
    print(json.dumps({"ok": True, "config": str(target), "base_url": base_url, "https": parsed.scheme == "https"}, ensure_ascii=False, indent=2))


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", choices=("gpt-image-2",), default="gpt-image-2")
    prompt = parser.add_mutually_exclusive_group(required=False)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file")
    parser.add_argument("--quality", choices=("low", "medium", "high", "auto"), default="low")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--format", dest="output_format", choices=("png", "jpeg", "webp"), default="png")
    parser.add_argument("--compression", type=int)
    parser.add_argument("--background", choices=("auto", "opaque", "transparent"), default="auto")
    parser.add_argument("--moderation", choices=("auto", "low"), default="auto")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--partial-images", type=int, default=0)
    parser.add_argument("--output", "--out")
    parser.add_argument("--overwrite", "--force", action="store_true")
    parser.add_argument(
        "--strict-output",
        action="store_true",
        help="exit nonzero after saving if returned format or dimensions differ",
    )
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and edit images through an OpenAI-compatible Codex relay."
    )
    parser.add_argument("--config", help=f"config path (default: {DEFAULT_CONFIG})")
    parser.add_argument("--base-url", help="override CODEX_RELAY_BASE_URL")
    parser.add_argument("--allow-http", action="store_true", help="allow an unencrypted remote relay")
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser("configure", help="save the relay URL and key with mode 0600")
    configure.add_argument("--base-url", dest="configure_base_url", required=True)
    configure.add_argument("--allow-http", action="store_true")
    configure.add_argument("--key-stdin", action="store_true", help="read one key line from stdin")
    configure.set_defaults(handler=run_configure)

    check = subparsers.add_parser("check", help="verify auth and image routes without generating")
    check.add_argument("--timeout", type=float, default=30.0)
    check.set_defaults(handler=run_check)

    generate = subparsers.add_parser("generate", help="generate images from text")
    add_common_options(generate)
    generate.set_defaults(handler=run_generate)

    edit = subparsers.add_parser("edit", help="edit or combine one or more local images")
    add_common_options(edit)
    edit.add_argument("--image", action="append", required=True, help="input image; repeat up to 16 times")
    edit.add_argument("--mask", help="optional alpha-channel PNG mask for the first input")
    edit.set_defaults(handler=run_edit)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if hasattr(args, "timeout") and args.timeout <= 0:
            raise RelayError("--timeout must be positive")
        args.handler(args)
        return 0
    except RelayError as exc:
        eprint(f"error: {exc}")
        return exc.exit_code
    except KeyboardInterrupt:
        eprint("error: interrupted; completion status may be unknown")
        return EXIT_NETWORK


if __name__ == "__main__":
    raise SystemExit(main())
