#!/usr/bin/env python3
"""Build and verify the public relay-artifacts Agent Skill archive."""

from __future__ import annotations

import argparse
import ast
import hashlib
import ipaddress
import os
import re
import stat
import tempfile
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SKILL_DIR = ROOT / "skills" / "relay-artifacts"
DEFAULT_OUTPUT = ROOT / "dist" / "relay-artifacts.zip"
ARCHIVE_ROOT = "relay-artifacts"
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
REQUIRED_FILES = {
    Path("SKILL.md"),
    Path("scripts/relay_artifacts.py"),
    Path("references/api-contract.md"),
    Path("assets/config.example.json"),
}

IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
IGNORED_NAMES = {".DS_Store", "Thumbs.db"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".swp", ".tmp"}
FORBIDDEN_NAMES = {
    ".env",
    "config.json",
    "credentials.json",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
}
FORBIDDEN_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
PUBLIC_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
SECRET_NAME_PATTERN = (
    r"api[_-]?key|relay(?:[_-]?api)?[_-]?key|"
    r"(?:feishu[_-]?|lark[_-]?)?app[_-]?(?:id|secret)|client[_-]?secret|"
    r"(?:tenant[_-]?)?access[_-]?token|refresh[_-]?token|"
    r"(?:input[_-]?folder|folder|file)[_-]?token|password"
)
SECRET_NAME_RE = re.compile(
    rf"(?:^|[_-])(?:{SECRET_NAME_PATTERN})$", re.IGNORECASE
)
SECRET_KEY_PATTERN = rf"(?:[A-Za-z0-9]+[_-])*(?:{SECRET_NAME_PATTERN})"
SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?ix)(?<![A-Za-z0-9])({SECRET_KEY_PATTERN})(?![A-Za-z0-9])"
    r"\s*[\"']?\s*[:=]\s*[\"']?([^\s\"',#}\]]+)"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+([^\s\"']+)")
PRIVATE_KEY_MARKER = "-----BEGIN PRIVATE KEY-----"
OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class PackageError(ValueError):
    pass


def is_ignored(relative: Path) -> bool:
    return (
        any(part in IGNORED_PARTS for part in relative.parts)
        or relative.name in IGNORED_NAMES
        or relative.suffix.lower() in IGNORED_SUFFIXES
    )


def looks_like_placeholder(value: str) -> bool:
    raw = value.strip().strip("\"'")
    normalized = raw.lower()
    if not normalized or normalized in {
        "...",
        "key",
        "none",
        "null",
        "secret",
        "str",
        "string",
        "token",
        "value",
    }:
        return True
    if re.fullmatch(
        r"(?:EXAMPLE|FAKE|PLACEHOLDER|REMOTE|REPLACE|TEST|YOUR)_[A-Z0-9_]+",
        raw,
    ):
        return True
    if normalized.startswith(("$", "%", "<", "{", "args.", "self.", "config.", "os.")):
        return True
    placeholder_markers = (
        "example",
        "placeholder",
        "replace",
        "redacted",
        "changeme",
        "your_",
        "your-",
        "xxxxx",
        "*****",
    )
    if any(marker in normalized for marker in placeholder_markers):
        return True
    return False


def is_forbidden(relative: Path) -> bool:
    name = relative.name
    return (
        name in FORBIDDEN_NAMES
        or name.startswith(".env.")
        or relative.suffix.lower() in FORBIDDEN_SUFFIXES
    )


def logical_path_key(parts: tuple[str, ...], source: str) -> tuple[str, ...]:
    normalized = []
    for part in parts:
        without_windows_suffix = part.rstrip(" .")
        if without_windows_suffix != part or not without_windows_suffix:
            raise PackageError(f"non-portable path component in {source}: {part!r}")
        normalized.append(unicodedata.normalize("NFC", part).casefold())
    return tuple(normalized)


def register_logical_path(
    seen: dict[tuple[str, ...], str], parts: tuple[str, ...], source: str
) -> None:
    key = logical_path_key(parts, source)
    previous = seen.get(key)
    if previous is not None and previous != source:
        raise PackageError(f"portable path collision: {previous!r} and {source!r}")
    seen[key] = source


def scan_public_text(text: str, source: str, *, scan_assignments: bool = True) -> None:
    for candidate in PUBLIC_IPV4_RE.findall(text):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.is_global:
            raise PackageError(f"public IPv4 address found in {source}: {candidate}")

    if PRIVATE_KEY_MARKER in text:
        raise PackageError(f"private key material found in {source}")
    if OPENAI_KEY_RE.search(text):
        raise PackageError(f"OpenAI-style API key found in {source}")

    if scan_assignments:
        for match in SECRET_ASSIGNMENT_RE.finditer(text):
            value = match.group(2)
            if not looks_like_placeholder(value):
                raise PackageError(
                    f"literal credential for {match.group(1)!r} found in {source}"
                )

    for match in BEARER_RE.finditer(text):
        value = match.group(1)
        if not looks_like_placeholder(value):
            raise PackageError(f"literal bearer token found in {source}")


def parse_frontmatter(data: bytes, source: str) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackageError(f"{source} must be UTF-8") from exc

    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise PackageError(f"{source} must start with YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise PackageError(f"{source} has unterminated YAML frontmatter") from exc

    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        if ":" not in line:
            raise PackageError(f"unsupported frontmatter line in {source}: {line!r}")
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip("\"'")

    if set(metadata) != {"name", "description"}:
        raise PackageError(f"{source} frontmatter must contain only name and description")
    if not SKILL_NAME_RE.fullmatch(metadata["name"]):
        raise PackageError(f"invalid Skill name in {source}: {metadata['name']!r}")
    if not metadata["description"] or "todo" in metadata["description"].lower():
        raise PackageError(f"unfinished Skill description in {source}")
    if "[TODO" in text or "TODO:" in text:
        raise PackageError(f"unfinished template marker found in {source}")
    return metadata


def python_target_name(target: ast.expr) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Subscript):
        key = target.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return key.value
    return None


def reject_python_literal(name: str | None, value: ast.expr, source: str) -> None:
    if not name or not SECRET_NAME_RE.search(name):
        return
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        if not looks_like_placeholder(value.value):
            raise PackageError(f"literal credential for {name!r} found in {source}")


def scan_python_credentials(tree: ast.AST, source: str) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                reject_python_literal(python_target_name(target), node.value, source)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            reject_python_literal(python_target_name(node.target), node.value, source)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    reject_python_literal(key.value, value, source)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                reject_python_literal(keyword.arg, keyword.value, source)


def validate_file(relative: Path, data: bytes) -> None:
    source = relative.as_posix()
    text = data.decode("utf-8", errors="ignore")
    if text:
        scan_public_text(text, source, scan_assignments=relative.suffix != ".py")
    if relative.suffix == ".py":
        try:
            tree = ast.parse(data, filename=source)
        except SyntaxError as exc:
            raise PackageError(f"invalid Python syntax in {source}: {exc}") from exc
        scan_python_credentials(tree, source)


def collect_skill_files(skill_dir: Path) -> dict[Path, bytes]:
    skill_dir = skill_dir.resolve()
    if not skill_dir.is_dir():
        raise PackageError(f"Skill directory not found: {skill_dir}")
    if skill_dir.name != ARCHIVE_ROOT:
        raise PackageError(
            f"Skill directory must be named {ARCHIVE_ROOT!r}, got {skill_dir.name!r}"
        )

    files: dict[Path, bytes] = {}
    logical_paths: dict[tuple[str, ...], str] = {}
    for path in sorted(skill_dir.rglob("*")):
        relative = path.relative_to(skill_dir)
        if path.is_symlink():
            raise PackageError(f"symbolic links are not allowed: {relative.as_posix()}")
        if is_ignored(relative):
            continue
        register_logical_path(logical_paths, relative.parts, relative.as_posix())
        if path.is_dir():
            continue
        if is_forbidden(relative):
            raise PackageError(f"private configuration file is not allowed: {relative}")
        data = path.read_bytes()
        validate_file(relative, data)
        files[relative] = data

    missing = REQUIRED_FILES.difference(files)
    if missing:
        names = ", ".join(sorted(path.as_posix() for path in missing))
        raise PackageError(f"required Skill files are missing: {names}")
    skill_file = Path("SKILL.md")
    metadata = parse_frontmatter(files[skill_file], "SKILL.md")
    if metadata["name"] != skill_dir.name:
        raise PackageError(
            f"SKILL.md name {metadata['name']!r} must match directory {skill_dir.name!r}"
        )
    return files


def archive_entries(files: dict[Path, bytes]) -> list[tuple[str, bytes | None]]:
    directories = {ARCHIVE_ROOT + "/"}
    for relative in files:
        parts = relative.parts[:-1]
        for index in range(1, len(parts) + 1):
            directories.add(f"{ARCHIVE_ROOT}/{'/'.join(parts[:index])}/")
    entries = [(name, None) for name in directories]
    entries.extend(
        (f"{ARCHIVE_ROOT}/{relative.as_posix()}", data)
        for relative, data in files.items()
    )
    return sorted(entries, key=lambda item: item[0])


def zip_info(name: str, is_directory: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_TIMESTAMP)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = expected_mode(name, is_directory)
    info.external_attr = mode << 16
    if is_directory:
        info.external_attr |= 0x10
    return info


def expected_mode(name: str, is_directory: bool) -> int:
    if is_directory:
        return stat.S_IFDIR | 0o755
    executable = "/scripts/" in name and name.endswith((".py", ".sh"))
    return stat.S_IFREG | (0o755 if executable else 0o644)


def build_archive(skill_dir: Path, output: Path) -> str:
    files = collect_skill_files(skill_dir)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for name, data in archive_entries(files):
                archive.writestr(zip_info(name, data is None), b"" if data is None else data)
        verify_archive(temporary, skill_dir)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)

    return hashlib.sha256(output.read_bytes()).hexdigest()


def validate_member_name(name: str) -> None:
    if "\\" in name or name.startswith("/"):
        raise PackageError(f"unsafe archive member: {name!r}")
    path = PurePosixPath(name.rstrip("/"))
    if not path.parts or path.parts[0] != ARCHIVE_ROOT or ".." in path.parts:
        raise PackageError(f"archive member is outside {ARCHIVE_ROOT}/: {name!r}")


def verify_archive(archive_path: Path, skill_dir: Path | None = None) -> None:
    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise PackageError(f"archive not found: {archive_path}")
    expected = collect_skill_files(skill_dir) if skill_dir is not None else None

    try:
        with zipfile.ZipFile(archive_path) as archive:
            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise PackageError(f"corrupt archive member: {corrupt_member}")
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(names) != len(set(names)):
                raise PackageError("archive contains duplicate members")
            if names != sorted(names):
                raise PackageError("archive members are not deterministically sorted")
            if f"{ARCHIVE_ROOT}/" not in names:
                raise PackageError(f"archive is missing top-level {ARCHIVE_ROOT}/ directory")

            archived_files: dict[Path, bytes] = {}
            logical_paths: dict[tuple[str, ...], str] = {}
            for member in members:
                validate_member_name(member.filename)
                member_parts = PurePosixPath(member.filename.rstrip("/")).parts[1:]
                if member_parts:
                    register_logical_path(
                        logical_paths, member_parts, member.filename
                    )
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise PackageError(f"archive contains symbolic link: {member.filename}")
                if mode != expected_mode(member.filename, member.is_dir()):
                    raise PackageError(
                        f"archive member has non-reproducible mode: {member.filename}"
                    )
                if member.compress_type != zipfile.ZIP_DEFLATED:
                    raise PackageError(
                        f"archive member has non-reproducible compression: {member.filename}"
                    )
                if member.date_time != FIXED_TIMESTAMP:
                    raise PackageError(
                        f"archive member has non-reproducible timestamp: {member.filename}"
                    )
                if member.is_dir():
                    continue
                relative = Path(*PurePosixPath(member.filename).parts[1:])
                if is_ignored(relative) or is_forbidden(relative):
                    raise PackageError(f"private or transient file in archive: {relative}")
                data = archive.read(member)
                validate_file(relative, data)
                archived_files[relative] = data

            missing = REQUIRED_FILES.difference(archived_files)
            if missing:
                names = ", ".join(sorted(path.as_posix() for path in missing))
                raise PackageError(f"required Skill files are missing from archive: {names}")
            skill_file = Path("SKILL.md")
            metadata = parse_frontmatter(archived_files[skill_file], "relay-artifacts/SKILL.md")
            if metadata["name"] != ARCHIVE_ROOT:
                raise PackageError("archive root and SKILL.md name do not match")
            if expected is not None and archived_files != expected:
                raise PackageError("archive contents do not match the source Skill directory")
    except zipfile.BadZipFile as exc:
        raise PackageError(f"invalid ZIP archive: {archive_path}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a deterministic, credential-free relay-artifacts Skill ZIP."
    )
    parser.add_argument("--skill-dir", type=Path, default=DEFAULT_SKILL_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--verify",
        type=Path,
        metavar="ZIP",
        help="verify an existing archive instead of building one",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.verify is not None:
            verify_archive(args.verify, args.skill_dir)
            print(f"OK: {args.verify}")
        else:
            digest = build_archive(args.skill_dir, args.output)
            print(f"Created {args.output}")
            print(f"SHA256 {digest}")
    except PackageError as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
