#!/usr/bin/env python3
"""Isolated CPU background-removal helper for transparent artifact outputs."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path


MODELS = ("isnet-general-use", "isnet-anime")
ALPHA_COVERAGE_DIVISOR = 1000
ALPHA_NEAR_OPAQUE = 240
ALPHA_NEAR_TRANSPARENT = 15
MAX_CUTOUT_EDGE = 8192
MAX_CUTOUT_PIXELS = 16_777_216


def validate_dimensions(image) -> None:
    if (
        image.width <= 0
        or image.height <= 0
        or image.width > MAX_CUTOUT_EDGE
        or image.height > MAX_CUTOUT_EDGE
        or image.width * image.height > MAX_CUTOUT_PIXELS
    ):
        raise ValueError("image dimensions exceed the local cutout limit")


def create_session(model: str):
    import onnxruntime as ort
    from rembg import new_session

    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return new_session(
        model,
        providers=["CPUExecutionProvider"],
        sess_opts=options,
    )


def remove_background(source_path: Path, output_path: Path, model: str) -> None:
    from PIL import Image
    from rembg import remove

    if not source_path.is_file() or source_path.is_symlink():
        raise ValueError("input is not a regular file")
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    session = create_session(model)
    with Image.open(source_path) as source:
        validate_dimensions(source)
        source.load()
        result = remove(source.convert("RGB"), session=session)
    if not isinstance(result, Image.Image):
        raise ValueError("background remover returned an invalid image")
    validate_dimensions(result)
    result = result.convert("RGBA")
    alpha_histogram = result.getchannel("A").histogram()
    total = result.width * result.height
    required = max(1, (total + ALPHA_COVERAGE_DIVISOR - 1) // ALPHA_COVERAGE_DIVISOR)
    near_transparent = sum(alpha_histogram[: ALPHA_NEAR_TRANSPARENT + 1])
    near_opaque = sum(alpha_histogram[ALPHA_NEAR_OPAQUE:])
    if near_transparent < required or near_opaque < required:
        raise ValueError("background remover did not produce usable alpha")

    temporary = output_path.with_name(f".{output_path.name}.part-{uuid.uuid4().hex}")
    try:
        result.save(temporary, format="PNG", optimize=True)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local image background-removal helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    warmup = subparsers.add_parser("warmup")
    warmup.add_argument("--model", choices=MODELS, required=True)

    process = subparsers.add_parser("remove")
    process.add_argument("--model", choices=MODELS, required=True)
    process.add_argument("--input", type=Path, required=True)
    process.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    args = build_parser().parse_args()
    try:
        if args.command == "warmup":
            create_session(args.model)
        else:
            remove_background(args.input.resolve(), args.output.resolve(), args.model)
    except Exception as exc:
        print(f"background removal failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
