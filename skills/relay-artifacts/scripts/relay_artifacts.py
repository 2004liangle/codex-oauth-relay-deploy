#!/usr/bin/env python3
"""通过飞书云盘传输异步中转任务的便携客户端。"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import mimetypes
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_SINGLE_UPLOAD = 20 * 1024 * 1024
MAX_MULTIPART_BLOCK = 20 * 1024 * 1024
READ_SIZE = 1024 * 1024
ACTIVE_STATUSES = {"queued", "downloading", "processing", "uploading"}
TERMINAL_STATUSES = {"ready_for_processing", "completed", "failed"}
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}\Z")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{6,200}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MIME_RE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")
LARK_RETRYABLE_CODES = {1061001, 1061006, 1061045, 1062012, 1064230}


class ToolError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Optional[Mapping[str, Any]] = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details or {})
        self.exit_code = exit_code


def translated_argparse_error(message: str) -> str:
    patterns = (
        (r"the following arguments are required: (.+)", "缺少必填参数: %s"),
        (r"one of the arguments (.+) is required", "必须提供以下参数之一: %s"),
        (r"unrecognized arguments: (.+)", "无法识别的参数: %s"),
        (
            r"argument (.+): invalid choice: (.+) \(choose from (.+)\)",
            "参数 %s 的选项无效: %s（可选值: %s）",
        ),
        (r"argument (.+): invalid choice: (.+)", "参数 %s 的选项无效: %s"),
        (r"argument (.+): expected one argument", "参数 %s 需要一个值"),
        (r"argument (.+): expected at most one argument", "参数 %s 最多只能有一个值"),
        (r"argument (.+): expected at least one argument", "参数 %s 至少需要一个值"),
        (r"argument (.+): expected (.+) argument", "参数 %s 需要 %s 个值"),
        (r"argument (.+): expected (.+) arguments", "参数 %s 需要 %s 个值"),
        (r"argument (.+): not allowed with argument (.+)", "参数 %s 不能与 %s 同时使用"),
        (r"argument (.+): ignored explicit argument (.+)", "参数 %s 不能直接附带值 %s"),
        (r"argument (.+): invalid .+ value: (.+)", "参数 %s 的值无效: %s"),
        (r"ambiguous option: (.+) could match (.+)", "选项 %s 不明确，可能匹配 %s"),
        (r"unexpected option string: (.+)", "出现了不应存在的选项: %s"),
        (r"argument (.+): unknown parser (.+) \(choices: (.+)\)", "参数 %s 指定了未知命令 %s（可选值: %s）"),
    )
    for pattern, template in patterns:
        match = re.fullmatch(pattern, message)
        if match:
            return template % match.groups()
    related = re.findall(
        r"(?<!\w)--?[A-Za-z0-9][A-Za-z0-9._-]*|'[^'\n]{1,80}'|\"[^\"\n]{1,80}\"|\b\d+\b",
        message,
    )
    if related:
        return "参数解析失败，相关内容: %s；请使用 --help 查看具体用法" % ", ".join(related)
    return "参数解析失败；请使用 --help 查看具体用法"


class ChineseHelpFormatter(argparse.HelpFormatter):
    def add_usage(
        self,
        usage: Optional[str],
        actions: Sequence[argparse.Action],
        groups: Sequence[argparse._MutuallyExclusiveGroup],
        prefix: Optional[str] = None,
    ) -> None:
        super().add_usage(usage, actions, groups, "用法: " if prefix is None else prefix)


class ChineseArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        add_help = bool(kwargs.pop("add_help", True))
        kwargs.setdefault("formatter_class", ChineseHelpFormatter)
        super().__init__(*args, add_help=False, **kwargs)
        self._positionals.title = "位置参数"
        self._optionals.title = "选项"
        if add_help:
            self.add_argument("-h", "--help", action="help", help="显示此帮助信息并退出")

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "%s: 错误: %s\n" % (self.prog, translated_argparse_error(message)))


@dataclass(frozen=True)
class Config:
    relay_base_url: str
    relay_api_key: str
    relay_timeout: float
    allow_insecure_http: bool
    lark_api_base_url: str
    lark_app_id: str
    lark_app_secret: str
    lark_input_folder_token: str
    lark_timeout: float


def emit(value: Mapping[str, Any], *, stream: Any = None) -> None:
    if stream is None:
        stream = sys.stdout
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def section(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        return {}
    item = value.get(name, {})
    if item is None:
        return {}
    if not isinstance(item, dict):
        raise ToolError("invalid_config", "配置节 %s 必须是对象" % name)
    return item


def positive_float(value: object, name: str, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ToolError("invalid_config", "%s 必须是数字" % name) from exc
    if result <= 0:
        raise ToolError("invalid_config", "%s 必须大于 0" % name)
    return result


def load_config(args: argparse.Namespace) -> Config:
    config_path = args.config or os.environ.get("RELAY_ARTIFACTS_CONFIG", "")
    if not config_path:
        candidate = Path(__file__).resolve().parents[1] / "assets" / "config.json"
        config_path = str(candidate) if candidate.exists() else ""
    raw: object = {}
    if config_path:
        path = Path(config_path).expanduser()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ToolError("config_unreadable", "无法读取私有配置文件") from exc
        except json.JSONDecodeError as exc:
            raise ToolError("invalid_config", "私有配置文件不是有效的 JSON") from exc
        if not isinstance(raw, dict):
            raise ToolError("invalid_config", "配置根节点必须是对象")

    relay = section(raw, "relay")
    lark = section(raw, "lark")
    base_url = first_env("RELAY_ARTIFACTS_BASE_URL", "CODEX_RELAY_BASE_URL") or str(
        relay.get("base_url", "")
    )
    api_key = first_env("RELAY_ARTIFACTS_API_KEY", "CODEX_RELAY_API_KEY") or str(
        relay.get("api_key", "")
    )
    lark_base = first_env("LARK_API_BASE_URL") or str(
        lark.get("api_base_url", "https://open.feishu.cn")
    )
    app_id = first_env("LARK_APP_ID") or str(lark.get("app_id", ""))
    app_secret = first_env("LARK_APP_SECRET") or str(lark.get("app_secret", ""))
    folder = first_env("LARK_INPUT_FOLDER_TOKEN") or str(lark.get("input_folder_token", ""))
    allow_http = bool(relay.get("allow_insecure_http", False)) or bool(args.allow_http)
    return Config(
        relay_base_url=base_url.strip().rstrip("/"),
        relay_api_key=api_key.strip(),
        relay_timeout=positive_float(relay.get("timeout_seconds"), "relay.timeout_seconds", 60.0),
        allow_insecure_http=allow_http,
        lark_api_base_url=lark_base.strip().rstrip("/"),
        lark_app_id=app_id.strip(),
        lark_app_secret=app_secret.strip(),
        lark_input_folder_token=folder.strip(),
        lark_timeout=positive_float(lark.get("timeout_seconds"), "lark.timeout_seconds", 120.0),
    )


def is_placeholder(value: str) -> bool:
    upper = value.upper()
    return not value or "REPLACE_WITH" in upper or value.startswith("<") or value.endswith(">")


def validate_url(value: str, label: str, *, allow_http: bool) -> str:
    if not value:
        raise ToolError("missing_config", "尚未配置 %s" % label)
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ToolError("invalid_config", "%s 必须是绝对 HTTP(S) URL" % label)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ToolError("invalid_config", "%s 不能包含凭据、查询参数或片段" % label)
    if parsed.scheme == "http" and not allow_http:
        raise ToolError(
            "insecure_http_disabled",
            "%s 使用明文 HTTP；仅可在用户明确接受传输风险后启用" % label,
        )
    return value.rstrip("/")


def require_relay(config: Config) -> Tuple[str, str]:
    base = validate_url(
        config.relay_base_url,
        "relay.base_url",
        allow_http=config.allow_insecure_http,
    )
    parsed = urllib.parse.urlsplit(base)
    path = parsed.path.rstrip("/")
    if path == "/v1":
        parsed = parsed._replace(path="")
        base = urllib.parse.urlunsplit(parsed).rstrip("/")
    elif path:
        raise ToolError(
            "invalid_config",
            "relay.base_url 必须是站点根地址，或以 /v1 结尾",
        )
    if is_placeholder(config.relay_api_key):
        raise ToolError("missing_config", "尚未配置 relay.api_key")
    if "\r" in config.relay_api_key or "\n" in config.relay_api_key:
        raise ToolError("invalid_config", "relay.api_key 包含无效字符")
    return base, config.relay_api_key


def require_lark(config: Config) -> Tuple[str, str, str]:
    base = validate_url(config.lark_api_base_url, "lark.api_base_url", allow_http=False)
    if is_placeholder(config.lark_app_id):
        raise ToolError("missing_config", "尚未配置 lark.app_id")
    if is_placeholder(config.lark_app_secret):
        raise ToolError("missing_config", "尚未配置 lark.app_secret")
    return base, config.lark_app_id, config.lark_app_secret


def retry_delay(attempt: int, retry_after: Optional[str] = None) -> float:
    if retry_after:
        try:
            return min(30.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(8.0, float(2 ** attempt))


def remote_error_details(data: bytes) -> Dict[str, Any]:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    error = parsed.get("error")
    if isinstance(error, dict):
        result: Dict[str, Any] = {}
        for key in ("code", "message", "retryable"):
            item = error.get(key)
            if isinstance(item, (str, bool, int)):
                result[key] = item
        return result
    result = {}
    if isinstance(parsed.get("code"), (str, int)):
        result["code"] = parsed["code"]
    if isinstance(parsed.get("msg"), str):
        result["message"] = parsed["msg"][:300]
    return result


def request_json(
    method: str,
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float,
    retries: int,
    service: str,
) -> Dict[str, Any]:
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=body, method=method)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise ToolError("response_too_large", "%s 返回的 JSON 响应过大" % service)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ToolError("invalid_response", "%s 返回了无效 JSON" % service) from exc
            if not isinstance(value, dict):
                raise ToolError("invalid_response", "%s 返回的 JSON 响应不是对象" % service)
            return value
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            if retryable and attempt < retries:
                time.sleep(retry_delay(attempt, exc.headers.get("Retry-After")))
                continue
            details = {"http_status": exc.code}
            details.update(remote_error_details(exc.read(65536)))
            raise ToolError(
                "%s_http_error" % service,
                "%s 请求失败，HTTP 状态码为 %d" % (service, exc.code),
                retryable=retryable,
                details=details,
                exit_code=3,
            ) from exc
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            if attempt < retries:
                time.sleep(retry_delay(attempt))
                continue
            raise ToolError(
                "%s_network_error" % service,
                "无法连接到 %s" % service,
                retryable=True,
                exit_code=3,
            ) from exc
    raise AssertionError("unreachable")


def json_body(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class RelayClient:
    def __init__(self, config: Config) -> None:
        self.base_url, self.api_key = require_relay(config)
        self.timeout = config.relay_timeout

    def _headers(self, *, json_content: bool = False) -> Dict[str, str]:
        headers = {"Authorization": "Bearer %s" % self.api_key, "Accept": "application/json"}
        if json_content:
            headers["Content-Type"] = "application/json"
        return headers

    def capabilities(self) -> Dict[str, Any]:
        return request_json(
            "GET",
            self.base_url + "/v1/artifact-capabilities",
            headers=self._headers(),
            timeout=self.timeout,
            retries=3,
            service="relay",
        )

    def submit(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        return request_json(
            "POST",
            self.base_url + "/v1/artifact-jobs",
            headers=self._headers(json_content=True),
            body=json_body(payload),
            timeout=self.timeout,
            retries=3,
            service="relay",
        )

    def job(self, request_id: str) -> Dict[str, Any]:
        validate_request_id(request_id)
        return request_json(
            "GET",
            self.base_url + "/v1/artifact-jobs/" + urllib.parse.quote(request_id, safe=""),
            headers=self._headers(),
            timeout=self.timeout,
            retries=3,
            service="relay",
        )


def multipart_body(
    fields: Mapping[str, object],
    file_bytes: bytes,
    *,
    mime_type: str = "application/octet-stream",
) -> Tuple[bytes, str]:
    boundary = "----relay-artifacts-%s" % uuid.uuid4().hex
    chunks: List[bytes] = []
    for name, value in fields.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ToolError("invalid_multipart", "multipart 字段名无效")
        chunks.extend(
            [
                ("--%s\r\n" % boundary).encode("ascii"),
                ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode("ascii"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            ("--%s\r\n" % boundary).encode("ascii"),
            b'Content-Disposition: form-data; name="file"; filename="payload.bin"\r\n',
            ("Content-Type: %s\r\n\r\n" % mime_type).encode("ascii"),
            file_bytes,
            b"\r\n",
            ("--%s--\r\n" % boundary).encode("ascii"),
        ]
    )
    return b"".join(chunks), "multipart/form-data; boundary=%s" % boundary


def safe_file_name(value: str, fallback: str = "artifact.bin") -> str:
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
    stem_bytes = stem.encode("utf-8")[: max(1, 180 - len(suffix_bytes))]
    while True:
        try:
            return stem_bytes.decode("utf-8") + suffix_bytes.decode("utf-8", "ignore")
        except UnicodeDecodeError:
            stem_bytes = stem_bytes[:-1]


def sniff_mime(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError as exc:
        raise ToolError("input_unreadable", "无法读取输入文件: %s" % path.name) from exc
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def lark_data(value: Mapping[str, Any], operation: str) -> Mapping[str, Any]:
    code = value.get("code")
    if code != 0:
        details: Dict[str, Any] = {}
        if isinstance(code, (int, str)):
            details["remote_code"] = code
        message = value.get("msg")
        if isinstance(message, str):
            details["remote_message"] = message[:300]
        raise ToolError(
            "lark_api_error",
            "Lark/飞书%s失败" % operation,
            retryable=isinstance(code, int) and code in LARK_RETRYABLE_CODES,
            details=details,
            exit_code=3,
        )
    data = value.get("data", {})
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ToolError("invalid_response", "Lark/飞书返回了无效响应数据")
    return data


class LarkClient:
    def __init__(self, config: Config) -> None:
        self.base_url, self.app_id, self.app_secret = require_lark(config)
        self.timeout = config.lark_timeout
        self._token = ""

    def token(self) -> str:
        if self._token:
            return self._token
        value = request_json(
            "POST",
            self.base_url + "/open-apis/auth/v3/tenant_access_token/internal",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            body=json_body({"app_id": self.app_id, "app_secret": self.app_secret}),
            timeout=self.timeout,
            retries=3,
            service="lark",
        )
        if value.get("code") != 0 or not isinstance(value.get("tenant_access_token"), str):
            raise ToolError("lark_auth_failed", "Lark/飞书应用身份验证失败", exit_code=3)
        self._token = str(value["tenant_access_token"])
        return self._token

    def _call(
        self,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        multipart: Optional[Tuple[bytes, str]] = None,
        retries: int = 0,
        operation: str,
    ) -> Mapping[str, Any]:
        if (payload is None) == (multipart is None):
            raise AssertionError("exactly one request body is required")
        if payload is not None:
            body = json_body(payload)
            content_type = "application/json"
        else:
            assert multipart is not None
            body, content_type = multipart
        for attempt in range(retries + 1):
            try:
                value = request_json(
                    "POST",
                    self.base_url + path,
                    headers={
                        "Authorization": "Bearer %s" % self.token(),
                        "Content-Type": content_type,
                        "Accept": "application/json",
                    },
                    body=body,
                    timeout=self.timeout,
                    retries=0,
                    service="lark",
                )
                return lark_data(value, operation)
            except ToolError as exc:
                if exc.retryable and attempt < retries:
                    time.sleep(retry_delay(attempt))
                    continue
                raise
        raise AssertionError("unreachable")

    def upload(self, path: Path, folder_token: str) -> Dict[str, Any]:
        if not path.is_file():
            raise ToolError("input_not_found", "输入文件不存在: %s" % path.name)
        try:
            declared_size = path.stat().st_size
        except OSError as exc:
            raise ToolError("input_unreadable", "无法检查输入文件: %s" % path.name) from exc
        if declared_size <= 0:
            raise ToolError("empty_input", "不能上传空文件: %s" % path.name)
        name = safe_file_name(path.name)
        mime_type = sniff_mime(path)
        if declared_size <= MAX_SINGLE_UPLOAD:
            return self._upload_all(path, name, mime_type, folder_token, declared_size)
        return self._upload_parts(path, name, mime_type, folder_token, declared_size)

    def _upload_all(
        self,
        path: Path,
        name: str,
        mime_type: str,
        folder_token: str,
        declared_size: int,
    ) -> Dict[str, Any]:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ToolError("input_unreadable", "无法读取输入文件: %s" % name) from exc
        if len(data) != declared_size:
            raise ToolError("input_changed", "读取期间输入文件发生变化: %s" % name)
        digest = hashlib.sha256(data).hexdigest()
        fields = {
            "file_name": name,
            "parent_type": "explorer",
            "parent_node": folder_token,
            "size": len(data),
            "checksum": str(zlib.adler32(data) & 0xFFFFFFFF),
        }
        result = self._call(
            "/open-apis/drive/v1/files/upload_all",
            multipart=multipart_body(fields, data, mime_type=mime_type),
            retries=0,
            operation="单文件上传",
        )
        token = result.get("file_token")
        if not isinstance(token, str) or not token:
            raise ToolError("invalid_response", "Lark/飞书上传未返回文件 token")
        return {
            "file_token": token,
            "name": name,
            "mime_type": mime_type,
            "size_bytes": len(data),
            "sha256": digest,
        }

    def _upload_parts(
        self,
        path: Path,
        name: str,
        mime_type: str,
        folder_token: str,
        declared_size: int,
    ) -> Dict[str, Any]:
        prepared = self._call(
            "/open-apis/drive/v1/files/upload_prepare",
            payload={
                "file_name": name,
                "parent_type": "explorer",
                "parent_node": folder_token,
                "size": declared_size,
            },
            retries=3,
            operation="分片上传准备",
        )
        upload_id = prepared.get("upload_id")
        block_size = prepared.get("block_size")
        block_num = prepared.get("block_num")
        if not isinstance(upload_id, str) or not upload_id:
            raise ToolError("invalid_response", "分片上传准备未返回 upload ID")
        if isinstance(block_size, bool) or not isinstance(block_size, int) or not 0 < block_size <= MAX_MULTIPART_BLOCK:
            raise ToolError("invalid_response", "分片上传准备返回了不支持的分块大小")
        expected_blocks = (declared_size + block_size - 1) // block_size
        if isinstance(block_num, bool) or not isinstance(block_num, int) or block_num != expected_blocks:
            raise ToolError("invalid_response", "分片上传准备返回的分块数量不一致")

        digest = hashlib.sha256()
        uploaded = 0
        try:
            with path.open("rb") as handle:
                for seq in range(block_num):
                    chunk = handle.read(block_size)
                    expected = min(block_size, declared_size - uploaded)
                    if len(chunk) != expected:
                        raise ToolError("input_changed", "上传期间输入文件发生变化: %s" % name)
                    digest.update(chunk)
                    uploaded += len(chunk)
                    fields = {
                        "upload_id": upload_id,
                        "seq": seq,
                        "size": len(chunk),
                        "checksum": str(zlib.adler32(chunk) & 0xFFFFFFFF),
                    }
                    self._call(
                        "/open-apis/drive/v1/files/upload_part",
                        multipart=multipart_body(fields, chunk, mime_type=mime_type),
                        retries=3,
                        operation="分片块上传",
                    )
                if handle.read(1):
                    raise ToolError("input_changed", "上传期间输入文件发生变化: %s" % name)
        except OSError as exc:
            raise ToolError("input_unreadable", "无法读取输入文件: %s" % name) from exc
        if uploaded != declared_size:
            raise ToolError("input_changed", "上传期间输入文件发生变化: %s" % name)
        finished = self._call(
            "/open-apis/drive/v1/files/upload_finish",
            payload={"upload_id": upload_id, "block_num": block_num},
            retries=3,
            operation="分片上传完成",
        )
        token = finished.get("file_token")
        if not isinstance(token, str) or not token:
            raise ToolError("invalid_response", "分片上传完成接口未返回文件 token")
        return {
            "file_token": token,
            "name": name,
            "mime_type": mime_type,
            "size_bytes": uploaded,
            "sha256": digest.hexdigest(),
        }

    def download(
        self,
        manifest: Mapping[str, Any],
        destination: Path,
        *,
        overwrite: bool,
        retries: int,
    ) -> Dict[str, Any]:
        token = manifest.get("file_token")
        size = manifest.get("size_bytes")
        expected_digest = manifest.get("sha256")
        if not isinstance(token, str) or not token:
            raise ToolError("invalid_manifest", "输出清单缺少文件 token")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ToolError("invalid_manifest", "输出清单的文件大小无效")
        if not isinstance(expected_digest, str) or not SHA256_RE.fullmatch(expected_digest):
            raise ToolError("invalid_manifest", "输出清单的 SHA-256 无效")
        if destination.exists() and not overwrite:
            raise ToolError("output_exists", "输出文件已存在: %s" % destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.parent / (".%s.part" % destination.name)
        url = self.base_url + "/open-apis/drive/v1/files/" + urllib.parse.quote(token, safe="") + "/download"

        for integrity_round in range(2):
            if part.exists() and part.stat().st_size > size:
                part.unlink()
            attempt = 0
            while not part.exists() or part.stat().st_size < size:
                offset = part.stat().st_size if part.exists() else 0
                headers = {"Authorization": "Bearer %s" % self.token()}
                if offset:
                    headers["Range"] = "bytes=%d-%d" % (offset, size - 1)
                request = urllib.request.Request(url, method="GET", headers=headers)
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        status = getattr(response, "status", response.getcode())
                        if offset and status == 200:
                            mode = "wb"
                            offset = 0
                        elif offset and status != 206:
                            raise ToolError(
                                "range_not_supported",
                                "Lark/飞书返回了无效的断点续传响应",
                                retryable=True,
                                exit_code=3,
                            )
                        elif not offset and status not in {200, 206}:
                            raise ToolError(
                                "download_http_error",
                                "Lark/飞书下载返回 HTTP 状态码 %d" % status,
                                retryable=status == 429 or status >= 500,
                                exit_code=3,
                            )
                        else:
                            mode = "ab" if offset else "wb"
                        with part.open(mode) as handle:
                            while True:
                                chunk = response.read(READ_SIZE)
                                if not chunk:
                                    break
                                handle.write(chunk)
                                if handle.tell() > size:
                                    raise ToolError("download_too_large", "下载内容超过清单声明的大小")
                    current = part.stat().st_size
                    if current < size:
                        raise ToolError(
                            "download_incomplete",
                            "下载在达到清单声明的大小前结束",
                            retryable=True,
                            exit_code=3,
                        )
                    attempt = 0
                except urllib.error.HTTPError as exc:
                    retryable = exc.code == 429 or 500 <= exc.code <= 599
                    error = ToolError(
                        "download_http_error",
                        "Lark/飞书下载失败，HTTP 状态码为 %d" % exc.code,
                        retryable=retryable,
                        details={"http_status": exc.code},
                        exit_code=3,
                    )
                    if retryable and attempt < retries:
                        time.sleep(retry_delay(attempt, exc.headers.get("Retry-After")))
                        attempt += 1
                        continue
                    raise error from exc
                except ToolError as exc:
                    if exc.retryable and attempt < retries:
                        time.sleep(retry_delay(attempt))
                        attempt += 1
                        continue
                    raise
                except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
                    if attempt < retries:
                        time.sleep(retry_delay(attempt))
                        attempt += 1
                        continue
                    raise ToolError(
                        "download_network_error",
                        "Lark/飞书下载中断；已保留部分文件以便续传",
                        retryable=True,
                        exit_code=3,
                    ) from exc

            actual_size, actual_digest = hash_file(part)
            if actual_size == size and actual_digest == expected_digest:
                os.replace(str(part), str(destination))
                return {
                    "path": str(destination),
                    "name": destination.name,
                    "size_bytes": actual_size,
                    "sha256": actual_digest,
                    "file_token": token,
                }
            if integrity_round == 0:
                part.unlink()
                continue
            raise ToolError(
                "download_integrity_mismatch",
                "下载文件的大小或 SHA-256 与清单不符",
                exit_code=4,
            )
        raise AssertionError("unreachable")


def hash_file(path: Path) -> Tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(READ_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise ToolError("file_unreadable", "无法读取文件: %s" % path.name) from exc
    return size, digest.hexdigest()


def json_argument(value: str, label: str) -> object:
    if value.startswith("@"):
        if len(value) == 1:
            raise ToolError("invalid_arguments", "%s 的 @path 为空" % label)
        try:
            raw = Path(value[1:]).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError("manifest_unreadable", "无法读取%s文件" % label) from exc
    else:
        raw = value
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError("invalid_manifest", "%s不是有效的 JSON" % label) from exc


def validate_input_manifest(
    value: object,
    *,
    allowed_roles: Sequence[str],
    default_role: str,
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError("invalid_manifest", "输入清单必须是 JSON 对象")
    allowed = {"file_token", "name", "mime_type", "size_bytes", "sha256", "role"}
    if set(value) - allowed:
        raise ToolError("invalid_manifest", "输入清单包含不支持的字段")
    token = value.get("file_token")
    name = value.get("name")
    mime_type = value.get("mime_type", "application/octet-stream")
    size = value.get("size_bytes")
    digest = value.get("sha256")
    role = value.get("role", default_role)
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise ToolError("invalid_manifest", "输入清单的 file_token 无效")
    if not isinstance(name, str) or not name or safe_file_name(name, "") != name:
        raise ToolError("invalid_manifest", "输入清单的 name 必须是安全的文件基本名")
    if not isinstance(mime_type, str) or not MIME_RE.fullmatch(mime_type):
        raise ToolError("invalid_manifest", "输入清单的 mime_type 无效")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ToolError("invalid_manifest", "输入清单的 size_bytes 无效")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ToolError("invalid_manifest", "输入清单的 sha256 无效")
    if role not in allowed_roles:
        raise ToolError("invalid_manifest", "输入清单的 role 不适用于当前操作")
    return {
        "file_token": token,
        "name": name,
        "mime_type": mime_type,
        "size_bytes": size,
        "sha256": digest,
        "role": role,
    }


def input_manifests(
    values: Sequence[str],
    *,
    allowed_roles: Sequence[str],
    default_role: str,
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for raw in values:
        parsed = json_argument(raw, "输入清单")
        if (
            isinstance(parsed, dict)
            and parsed.get("ok") is True
            and isinstance(parsed.get("manifest"), dict)
        ):
            parsed = parsed["manifest"]
        items = parsed if isinstance(parsed, list) else [parsed]
        for item in items:
            result.append(
                validate_input_manifest(
                    item,
                    allowed_roles=allowed_roles,
                    default_role=default_role,
                )
            )
    return result


def validate_request_id(value: str) -> str:
    if not REQUEST_ID_RE.fullmatch(value):
        raise ToolError("invalid_request_id", "request ID 必须包含 8-128 个安全 ASCII 字符")
    return value


def new_request_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "art-%s-%s" % (stamp, uuid.uuid4().hex[:16])


def read_text(value: Optional[str], path_value: Optional[str], label: str, *, required: bool) -> str:
    if value is not None:
        result = value
    elif path_value is not None:
        try:
            result = Path(path_value).read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError("text_unreadable", "无法读取%s文件" % label) from exc
    else:
        result = ""
    if required and not result.strip():
        raise ToolError("missing_text", "必须提供%s" % label)
    if len(result) > 100000:
        raise ToolError("text_too_long", "%s超过 100,000 个字符" % label)
    return result


def check_capability(capabilities: Mapping[str, Any], operation: str) -> None:
    operations = capabilities.get("operations")
    if not isinstance(operations, list) or operation not in operations:
        raise ToolError("unsupported_operation", "中转服务未声明支持 %s" % operation)


def check_transparent_output(
    capabilities: Mapping[str, Any], parameters: Mapping[str, Any]
) -> None:
    if parameters.get("background") != "transparent":
        return
    feature = capabilities.get("transparent_output")
    if (
        not isinstance(feature, dict)
        or feature.get("alpha_validation") is not True
        or feature.get("format") != "png"
    ):
        raise ToolError(
            "transparent_output_unsupported",
            "中转服务未启用真实透明 PNG 校验，任务尚未提交",
        )
    models = feature.get("models")
    if not isinstance(models, list) or not models or not all(
        isinstance(item, str) for item in models
    ):
        raise ToolError(
            "invalid_capabilities",
            "中转服务返回的透明抠图模型清单无效，任务尚未提交",
        )
    requested_model = parameters.get("background_removal_model")
    selected_model = (
        requested_model if requested_model is not None else feature.get("default_model")
    )
    if not isinstance(selected_model, str) or selected_model not in models:
        raise ToolError(
            "unsupported_cutout_model",
            "中转服务不支持所选透明抠图模型，任务尚未提交",
        )


def input_folder(config: Config, capabilities: Mapping[str, Any]) -> str:
    if config.lark_input_folder_token:
        return config.lark_input_folder_token
    target = capabilities.get("input_target")
    if not isinstance(target, dict):
        raise ToolError("invalid_capabilities", "中转服务未声明输入目标")
    target_type = target.get("type")
    token = target.get("token")
    if target_type != "folder":
        raise ToolError(
            "unsupported_input_target",
            "便携直传模式要求输入目标是 Lark/飞书云盘文件夹",
        )
    if not isinstance(token, str) or not token:
        raise ToolError("invalid_capabilities", "中转服务缺少输入文件夹 token")
    return token


def wait_for_job(
    relay: RelayClient,
    request_id: str,
    *,
    timeout: float,
    interval: float,
    require_completed: bool,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        job = relay.job(request_id)
        status = job.get("status")
        if status == "failed":
            error = job.get("error") if isinstance(job.get("error"), dict) else {}
            details: Dict[str, Any] = {"request_id": request_id}
            for key in ("code", "message", "retryable"):
                if key in error and isinstance(error[key], (str, bool, int)):
                    details[key] = error[key]
            raise ToolError(
                "artifact_job_failed",
                "附件任务失败",
                retryable=bool(error.get("retryable", False)),
                details=details,
                exit_code=4,
            )
        if status == "completed" or (status == "ready_for_processing" and not require_completed):
            return job
        if status not in ACTIVE_STATUSES and status != "ready_for_processing":
            raise ToolError("invalid_job_status", "中转服务返回了未知任务状态")
        if time.monotonic() >= deadline:
            raise ToolError(
                "wait_timeout",
                "任务仍在运行；请使用相同的 request ID 继续查询",
                retryable=True,
                details={"request_id": request_id, "status": status},
                exit_code=3,
            )
        time.sleep(interval)


def preflight_files(values: Sequence[str], minimum: int, maximum: int) -> List[Path]:
    if not minimum <= len(values) <= maximum:
        raise ToolError("invalid_input_count", "需要 %d-%d 个输入文件" % (minimum, maximum))
    paths = [Path(value).expanduser().resolve() for value in values]
    for path in paths:
        if not path.is_file():
            raise ToolError("input_not_found", "输入文件不存在: %s" % path.name)
        if path.stat().st_size <= 0:
            raise ToolError("empty_input", "不能上传空文件: %s" % path.name)
    return paths


def verify_total_size(paths: Sequence[Path], capabilities: Mapping[str, Any]) -> None:
    limit = capabilities.get("max_input_bytes")
    total = sum(path.stat().st_size for path in paths)
    if isinstance(limit, int) and not isinstance(limit, bool) and total > limit:
        raise ToolError("inputs_too_large", "本地输入超过中转服务声明的大小限制")


def verify_manifest_total(manifests: Sequence[Mapping[str, Any]], capabilities: Mapping[str, Any]) -> None:
    limit = capabilities.get("max_input_bytes")
    total = sum(int(item["size_bytes"]) for item in manifests)
    if isinstance(limit, int) and not isinstance(limit, bool) and total > limit:
        raise ToolError("inputs_too_large", "输入清单超过中转服务声明的大小限制")


def image_parameters(args: argparse.Namespace, prompt: str) -> Dict[str, Any]:
    if args.background == "transparent" and args.output_format != "png":
        raise ToolError("invalid_image_options", "透明背景必须使用 PNG 格式")
    if args.cutout_model is not None and args.background != "transparent":
        raise ToolError("invalid_image_options", "--cutout-model 只能与 --background transparent 一起使用")
    result: Dict[str, Any] = {
        "model": args.model,
        "prompt": prompt,
        "quality": args.quality,
        "size": args.size,
        "output_format": args.output_format,
        "n": args.n,
    }
    optional = {
        "output_compression": args.compression,
        "background": args.background,
        "background_removal_model": args.cutout_model,
        "moderation": args.moderation,
        "output_name": args.output_name,
    }
    result.update({key: value for key, value in optional.items() if value is not None})
    return result


def result_with_downloads(
    config: Config,
    job: Dict[str, Any],
    output_dir: Optional[str],
    overwrite: bool,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": True, "job": job}
    if output_dir is None:
        return result
    if job.get("status") != "completed":
        raise ToolError("job_not_completed", "任务尚无可下载的已完成输出")
    outputs = job.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise ToolError("no_outputs", "已完成任务没有输出清单")
    target = Path(output_dir).expanduser().resolve()
    destinations: List[Path] = []
    seen = set()
    for index, item in enumerate(outputs):
        if not isinstance(item, dict):
            raise ToolError("invalid_manifest", "任务输出清单无效")
        raw_name = item.get("name")
        name = safe_file_name(str(raw_name or "artifact-%d.bin" % (index + 1)))
        if name in seen:
            raise ToolError("duplicate_output_name", "任务包含重复的输出文件名")
        seen.add(name)
        destinations.append(target / name)
    for destination in destinations:
        if destination.exists() and not overwrite:
            raise ToolError("output_exists", "输出文件已存在: %s" % destination)
    lark = LarkClient(config)
    downloaded = []
    for manifest, destination in zip(outputs, destinations):
        downloaded.append(lark.download(manifest, destination, overwrite=overwrite, retries=5))
    result["downloads"] = downloaded
    return result


def command_capabilities(args: argparse.Namespace) -> Dict[str, Any]:
    relay = RelayClient(load_config(args))
    return {"ok": True, "capabilities": relay.capabilities()}


def command_manifest(args: argparse.Namespace) -> Dict[str, Any]:
    path = preflight_files([args.file], 1, 1)[0]
    if not TOKEN_RE.fullmatch(args.file_token):
        raise ToolError("invalid_manifest", "--file-token 无效")
    size, digest = hash_file(path)
    return {
        "ok": True,
        "manifest": {
            "file_token": args.file_token,
            "name": safe_file_name(path.name),
            "mime_type": sniff_mime(path),
            "size_bytes": size,
            "sha256": digest,
            "role": args.role,
        },
    }


def command_generate(args: argparse.Namespace) -> Dict[str, Any]:
    config = load_config(args)
    relay = RelayClient(config)
    capabilities = relay.capabilities()
    check_capability(capabilities, "image.generate")
    prompt = read_text(args.prompt, args.prompt_file, "prompt", required=True)
    parameters = image_parameters(args, prompt)
    check_transparent_output(capabilities, parameters)
    request_id = validate_request_id(args.request_id) if args.request_id else new_request_id()
    job = relay.submit(
        {
            "request_id": request_id,
            "operation": "image.generate",
            "parameters": parameters,
            "inputs": [],
        }
    )
    if args.wait:
        job = wait_for_job(
            relay,
            request_id,
            timeout=args.wait_timeout,
            interval=args.poll_interval,
            require_completed=True,
        )
    elif args.download_dir:
        raise ToolError("invalid_arguments", "使用 --download-dir 时必须同时使用 --wait")
    return result_with_downloads(config, job, args.download_dir, args.overwrite)


def command_submit_edit(args: argparse.Namespace) -> Dict[str, Any]:
    config = load_config(args)
    relay = RelayClient(config)
    capabilities = relay.capabilities()
    check_capability(capabilities, "image.edit")
    prompt = read_text(args.prompt, args.prompt_file, "prompt", required=True)
    parameters = image_parameters(args, prompt)
    check_transparent_output(capabilities, parameters)
    manifests = input_manifests(
        args.input_manifest,
        allowed_roles=("image", "mask"),
        default_role="image",
    )
    images = [item for item in manifests if item["role"] == "image"]
    masks = [item for item in manifests if item["role"] == "mask"]
    if not 1 <= len(images) <= 16 or len(masks) > 1:
        raise ToolError(
            "invalid_input_count",
            "submit-edit 需要 1-16 个图片清单，且最多包含一个蒙版",
        )
    verify_manifest_total(manifests, capabilities)
    request_id = validate_request_id(args.request_id) if args.request_id else new_request_id()
    job = relay.submit(
        {
            "request_id": request_id,
            "operation": "image.edit",
            "parameters": parameters,
            "inputs": manifests,
        }
    )
    if args.wait:
        job = wait_for_job(
            relay,
            request_id,
            timeout=args.wait_timeout,
            interval=args.poll_interval,
            require_completed=True,
        )
    elif args.download_dir:
        raise ToolError("invalid_arguments", "使用 --download-dir 时必须同时使用 --wait")
    return result_with_downloads(config, job, args.download_dir, args.overwrite)


def command_submit_handoff(args: argparse.Namespace) -> Dict[str, Any]:
    config = load_config(args)
    relay = RelayClient(config)
    capabilities = relay.capabilities()
    check_capability(capabilities, "artifact.handoff")
    manifests = input_manifests(
        args.input_manifest,
        allowed_roles=("attachment",),
        default_role="attachment",
    )
    if not 1 <= len(manifests) <= 32:
        raise ToolError("invalid_input_count", "submit-handoff 需要 1-32 个附件清单")
    verify_manifest_total(manifests, capabilities)
    instruction = read_text(
        args.instruction,
        args.instruction_file,
        "instruction",
        required=False,
    )
    parameters: Dict[str, Any] = {}
    if instruction:
        parameters["instruction"] = instruction
    request_id = validate_request_id(args.request_id) if args.request_id else new_request_id()
    job = relay.submit(
        {
            "request_id": request_id,
            "operation": "artifact.handoff",
            "parameters": parameters,
            "inputs": manifests,
        }
    )
    if args.wait:
        job = wait_for_job(
            relay,
            request_id,
            timeout=args.wait_timeout,
            interval=args.poll_interval,
            require_completed=False,
        )
    return {"ok": True, "job": job}


def command_submit_job(args: argparse.Namespace) -> Dict[str, Any]:
    payload = json_argument(args.job_manifest, "任务清单")
    if not isinstance(payload, dict):
        raise ToolError("invalid_manifest", "任务清单必须是 JSON 对象")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str):
        raise ToolError("invalid_manifest", "任务清单必须包含 request_id")
    validate_request_id(request_id)
    relay = RelayClient(load_config(args))
    parameters = payload.get("parameters")
    if isinstance(parameters, dict) and parameters.get("background") == "transparent":
        check_transparent_output(relay.capabilities(), parameters)
    job = relay.submit(payload)
    if args.wait or args.wait_completed:
        job = wait_for_job(
            relay,
            request_id,
            timeout=args.wait_timeout,
            interval=args.poll_interval,
            require_completed=args.wait_completed,
        )
    return {"ok": True, "job": job}


def command_edit(args: argparse.Namespace) -> Dict[str, Any]:
    config = load_config(args)
    relay = RelayClient(config)
    capabilities = relay.capabilities()
    check_capability(capabilities, "image.edit")
    prompt = read_text(args.prompt, args.prompt_file, "prompt", required=True)
    parameters = image_parameters(args, prompt)
    check_transparent_output(capabilities, parameters)
    images = preflight_files(args.image, 1, 16)
    mask = preflight_files([args.mask], 1, 1)[0] if args.mask else None
    all_paths = images + ([mask] if mask is not None else [])
    verify_total_size(all_paths, capabilities)
    folder = input_folder(config, capabilities)
    lark = LarkClient(config)
    manifests = []
    for path in images:
        manifest = lark.upload(path, folder)
        manifest["role"] = "image"
        manifests.append(manifest)
    if mask is not None:
        manifest = lark.upload(mask, folder)
        manifest["role"] = "mask"
        manifests.append(manifest)
    request_id = validate_request_id(args.request_id) if args.request_id else new_request_id()
    job = relay.submit(
        {
            "request_id": request_id,
            "operation": "image.edit",
            "parameters": parameters,
            "inputs": manifests,
        }
    )
    if args.wait:
        job = wait_for_job(
            relay,
            request_id,
            timeout=args.wait_timeout,
            interval=args.poll_interval,
            require_completed=True,
        )
    elif args.download_dir:
        raise ToolError("invalid_arguments", "使用 --download-dir 时必须同时使用 --wait")
    return result_with_downloads(config, job, args.download_dir, args.overwrite)


def command_handoff(args: argparse.Namespace) -> Dict[str, Any]:
    config = load_config(args)
    relay = RelayClient(config)
    capabilities = relay.capabilities()
    check_capability(capabilities, "artifact.handoff")
    paths = preflight_files(args.file, 1, 32)
    verify_total_size(paths, capabilities)
    folder = input_folder(config, capabilities)
    lark = LarkClient(config)
    manifests = []
    for path in paths:
        manifest = lark.upload(path, folder)
        manifest["role"] = "attachment"
        manifests.append(manifest)
    instruction = read_text(
        args.instruction,
        args.instruction_file,
        "instruction",
        required=False,
    )
    parameters: Dict[str, Any] = {}
    if instruction:
        parameters["instruction"] = instruction
    request_id = validate_request_id(args.request_id) if args.request_id else new_request_id()
    job = relay.submit(
        {
            "request_id": request_id,
            "operation": "artifact.handoff",
            "parameters": parameters,
            "inputs": manifests,
        }
    )
    if args.wait:
        job = wait_for_job(
            relay,
            request_id,
            timeout=args.wait_timeout,
            interval=args.poll_interval,
            require_completed=False,
        )
    return {"ok": True, "job": job}


def command_status(args: argparse.Namespace) -> Dict[str, Any]:
    relay = RelayClient(load_config(args))
    request_id = validate_request_id(args.request_id)
    if args.wait or args.wait_completed:
        job = wait_for_job(
            relay,
            request_id,
            timeout=args.wait_timeout,
            interval=args.poll_interval,
            require_completed=args.wait_completed,
        )
    else:
        job = relay.job(request_id)
    return {"ok": True, "job": job}


def command_download(args: argparse.Namespace) -> Dict[str, Any]:
    config = load_config(args)
    relay = RelayClient(config)
    request_id = validate_request_id(args.request_id)
    job = relay.job(request_id)
    if args.wait and job.get("status") != "completed":
        job = wait_for_job(
            relay,
            request_id,
            timeout=args.wait_timeout,
            interval=args.poll_interval,
            require_completed=True,
        )
    if job.get("status") != "completed":
        raise ToolError(
            "job_not_completed",
            "任务尚未完成；请使用 --wait 重试下载，或查询任务状态",
            retryable=True,
            details={"request_id": request_id, "status": job.get("status")},
            exit_code=3,
        )
    if args.output_dir is None:
        outputs = job.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            raise ToolError("no_outputs", "已完成任务没有输出清单")
        return {
            "ok": True,
            "request_id": request_id,
            "status": "completed",
            "outputs": outputs,
        }
    return result_with_downloads(config, job, args.output_dir, args.overwrite)


def command_self_test(args: argparse.Namespace) -> Dict[str, Any]:
    del args
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sample.bin"
        data = b"relay-artifacts-self-test\x00\xff"
        path.write_bytes(data)
        size, digest = hash_file(path)
        assert size == len(data)
        assert digest == hashlib.sha256(data).hexdigest()
        body, content_type = multipart_body({"size": size, "seq": 0}, data)
        assert data in body
        assert b'name="size"' in body
        assert content_type.startswith("multipart/form-data; boundary=")
        assert safe_file_name("../sample.bin") == "sample.bin"
        assert REQUEST_ID_RE.fullmatch(new_request_id())
        origin_config = Config(
            "https://relay.example.com",
            "private-test-key",
            60.0,
            False,
            "https://open.feishu.cn",
            "",
            "",
            "",
            120.0,
        )
        versioned_config = Config(
            "https://relay.example.com/v1",
            "private-test-key",
            60.0,
            False,
            "https://open.feishu.cn",
            "",
            "",
            "",
            120.0,
        )
        assert require_relay(origin_config)[0] == "https://relay.example.com"
        assert require_relay(versioned_config)[0] == "https://relay.example.com"
        manifest = validate_input_manifest(
            {
                "file_token": "REMOTE_FILE_TOKEN",
                "name": "sample.bin",
                "mime_type": "application/octet-stream",
                "size_bytes": size,
                "sha256": digest,
            },
            allowed_roles=("attachment",),
            default_role="attachment",
        )
        assert manifest["role"] == "attachment"
        enveloped = json.dumps({"ok": True, "manifest": manifest})
        assert input_manifests(
            [enveloped],
            allowed_roles=("attachment",),
            default_role="attachment",
        ) == [manifest]
    return {
        "ok": True,
        "tests": [
            "文件哈希",
            "multipart 编码",
            "安全文件名",
            "request ID",
            "中转 /v1 地址规范化",
            "客户端工具清单",
        ],
    }


def add_wait_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wait", action="store_true", help="持续查询，直到任务达到目标状态")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--wait-timeout", type=float, default=1800.0)


def add_image_options(parser: argparse.ArgumentParser) -> None:
    prompts = parser.add_mutually_exclusive_group(required=True)
    prompts.add_argument("--prompt")
    prompts.add_argument("--prompt-file")
    parser.add_argument("--model", default="gpt-image-2")
    parser.add_argument("--quality", choices=("auto", "low", "medium", "high"), default="auto")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--format", dest="output_format", choices=("png", "jpeg", "webp"), default="png")
    parser.add_argument("--compression", type=int)
    parser.add_argument("--background", choices=("auto", "opaque", "transparent"))
    parser.add_argument(
        "--cutout-model",
        choices=("isnet-general-use", "isnet-anime"),
        help="透明抠图模型：普通图片选 isnet-general-use，动漫人物选 isnet-anime",
    )
    parser.add_argument("--moderation", choices=("auto", "low"))
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--output-name")
    parser.add_argument("--request-id")
    parser.add_argument("--download-dir")
    parser.add_argument("--overwrite", action="store_true")
    add_wait_options(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = ChineseArgumentParser(
        description="通过飞书云盘中转图片和附件任务。"
    )
    parser.add_argument("--config", help="私密 JSON 配置文件路径")
    parser.add_argument(
        "--allow-http",
        action="store_true",
        help="用户明确接受风险后，允许使用未加密的 HTTP 中转地址",
    )
    public_commands = (
        "{capabilities,manifest,submit-generate,generate,submit-edit,edit,"
        "submit-handoff,handoff,status,submit-job,download}"
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        metavar=public_commands,
    )

    capabilities = commands.add_parser("capabilities", help="查看当前中转服务支持的能力")
    capabilities.set_defaults(handler=command_capabilities)

    manifest = commands.add_parser(
        "manifest",
        help="将客户端上传后返回的文件 token 与本地文件大小、SHA-256 合并成清单",
    )
    manifest.add_argument("--file", required=True, help="已由客户端上传的本地文件")
    manifest.add_argument("--file-token", required=True, help="客户端云盘工具返回的文件 token")
    manifest.add_argument("--role", required=True, choices=("image", "mask", "attachment"))
    manifest.set_defaults(handler=command_manifest)

    generate = commands.add_parser(
        "submit-generate",
        aliases=["generate"],
        help="提交文生图任务，不需要飞书应用凭据",
    )
    add_image_options(generate)
    generate.set_defaults(handler=command_generate)

    submit_edit = commands.add_parser(
        "submit-edit",
        help="提交客户端已上传的图片清单，不需要飞书应用凭据",
    )
    add_image_options(submit_edit)
    submit_edit.add_argument(
        "--input-manifest",
        action="append",
        required=True,
        help="直接传 JSON 或 @文件路径；可以重复使用，也可以传 JSON 数组",
    )
    submit_edit.set_defaults(handler=command_submit_edit)

    edit = commands.add_parser("edit", help="上传本地图片并创建异步编辑任务")
    add_image_options(edit)
    edit.add_argument("--image", action="append", required=True, help="按顺序输入图片，最多可重复 16 次")
    edit.add_argument("--mask", help="可选，作用于第一张图片的蒙版")
    edit.set_defaults(handler=command_edit)

    submit_handoff = commands.add_parser(
        "submit-handoff",
        help="提交客户端已上传的附件清单，不需要飞书应用凭据",
    )
    submit_handoff.add_argument(
        "--input-manifest",
        action="append",
        required=True,
        help="直接传 JSON 或 @文件路径；可以重复使用，也可以传 JSON 数组",
    )
    submit_instructions = submit_handoff.add_mutually_exclusive_group()
    submit_instructions.add_argument("--instruction")
    submit_instructions.add_argument("--instruction-file")
    submit_handoff.add_argument("--request-id")
    add_wait_options(submit_handoff)
    submit_handoff.set_defaults(handler=command_submit_handoff)

    handoff = commands.add_parser("handoff", help="上传本地附件，交给可信服务端处理")
    handoff.add_argument("--file", action="append", required=True, help="本地附件，最多可重复 32 次")
    instructions = handoff.add_mutually_exclusive_group()
    instructions.add_argument("--instruction")
    instructions.add_argument("--instruction-file")
    handoff.add_argument("--request-id")
    add_wait_options(handoff)
    handoff.set_defaults(handler=command_handoff)

    status = commands.add_parser("status", help="查看或等待已有任务")
    status.add_argument("request_id")
    status_wait = status.add_mutually_exclusive_group()
    status_wait.add_argument("--wait", action="store_true", help="到达 ready_for_processing 或 completed 时停止")
    status_wait.add_argument("--wait-completed", action="store_true", help="经过 ready_for_processing 后继续等待完成")
    status.add_argument("--poll-interval", type=float, default=2.0)
    status.add_argument("--wait-timeout", type=float, default=1800.0)
    status.set_defaults(handler=command_status)

    submit_job = commands.add_parser("submit-job", help="提交完整的中转任务 JSON 清单")
    submit_job.add_argument("--job-manifest", required=True, help="直接传 JSON 或 @文件路径")
    submit_job_wait = submit_job.add_mutually_exclusive_group()
    submit_job_wait.add_argument("--wait", action="store_true")
    submit_job_wait.add_argument("--wait-completed", action="store_true")
    submit_job.add_argument("--poll-interval", type=float, default=2.0)
    submit_job.add_argument("--wait-timeout", type=float, default=1800.0)
    submit_job.set_defaults(handler=command_submit_job)

    download = commands.add_parser("download", help="下载并校验已完成任务的输出文件")
    download.add_argument("request_id")
    download.add_argument(
        "--output-dir",
        help="脚本直传模式的下载目录；不填写则返回输出清单供客户端工具下载",
    )
    download.add_argument("--overwrite", action="store_true")
    add_wait_options(download)
    download.set_defaults(handler=command_download)

    self_test = commands.add_parser("self-test")
    self_test.set_defaults(handler=command_self_test)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    for name in ("poll_interval", "wait_timeout"):
        if hasattr(args, name) and getattr(args, name) <= 0:
            raise ToolError("invalid_arguments", "--%s 必须大于 0" % name.replace("_", "-"))
    if hasattr(args, "n") and not 1 <= args.n <= 10:
        raise ToolError("invalid_arguments", "--n 必须在 1 到 10 之间")
    if hasattr(args, "compression") and args.compression is not None and not 0 <= args.compression <= 100:
        raise ToolError("invalid_arguments", "--compression 必须在 0 到 100 之间")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        emit(args.handler(args))
        return 0
    except ToolError as exc:
        error: Dict[str, Any] = {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
        }
        if exc.details:
            error["details"] = exc.details
        emit({"ok": False, "error": error}, stream=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        emit(
            {
                "ok": False,
                "error": {
                    "code": "interrupted",
                    "message": "操作已中断；如果知道 request ID，请使用同一个 ID 重试",
                    "retryable": True,
                },
            },
            stream=sys.stderr,
        )
        return 130
    except Exception:
        emit(
            {
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": "本地客户端发生意外错误",
                    "retryable": False,
                },
            },
            stream=sys.stderr,
        )
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
