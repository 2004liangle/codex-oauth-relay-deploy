#!/usr/bin/env python3
"""Isolated CPU background-removal helper for transparent artifact outputs."""

from __future__ import annotations

import argparse
import gc
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
MASK_FOREGROUND_THRESHOLD = 15
MATTING_FOREGROUND_THRESHOLD = 240
MATTING_BACKGROUND_THRESHOLD = 10
MATTING_ERODE_SIZE = 5
MATTING_MIN_CONFIDENT_PIXELS = MATTING_ERODE_SIZE**2
MATTING_MAX_PIXELS = 350_000
MATTING_MIN_PADDING = 12
MATTING_PADDING_RATIO = 0.04


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


def foreground_crop_box(mask) -> tuple[int, int, int, int]:
    foreground = mask.point(
        lambda value: 255 if value > MASK_FOREGROUND_THRESHOLD else 0
    )
    bounds = foreground.getbbox()
    if bounds is None:
        raise ValueError("background remover produced an empty mask")
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    padding = max(
        MATTING_MIN_PADDING,
        round(max(width, height) * MATTING_PADDING_RATIO),
    )
    return (
        max(0, bounds[0] - padding),
        max(0, bounds[1] - padding),
        min(mask.width, bounds[2] + padding),
        min(mask.height, bounds[3] + padding),
    )


def adaptive_cutout(source, mask, matting_function=None):
    from PIL import Image

    if source.size != mask.size:
        raise ValueError("background remover returned a mismatched mask")
    box = foreground_crop_box(mask)
    source_crop = source.crop(box)
    mask_crop = mask.crop(box)
    crop_pixels = source_crop.width * source_crop.height
    histogram = mask_crop.histogram()
    has_confident_foreground = (
        sum(histogram[MATTING_FOREGROUND_THRESHOLD + 1 :])
        >= MATTING_MIN_CONFIDENT_PIXELS
    )
    has_confident_background = (
        sum(histogram[:MATTING_BACKGROUND_THRESHOLD])
        >= MATTING_MIN_CONFIDENT_PIXELS
    )

    if (
        crop_pixels <= MATTING_MAX_PIXELS
        and has_confident_foreground
        and has_confident_background
    ):
        if matting_function is None:
            from rembg.bg import alpha_matting_cutout

            matting_function = alpha_matting_cutout
        try:
            cutout = matting_function(
                source_crop,
                mask_crop,
                MATTING_FOREGROUND_THRESHOLD,
                MATTING_BACKGROUND_THRESHOLD,
                MATTING_ERODE_SIZE,
            ).convert("RGBA")
        except ValueError:
            cutout = source_crop.convert("RGBA")
            cutout.putalpha(mask_crop)
    else:
        cutout = source_crop.convert("RGBA")
        cutout.putalpha(mask_crop)

    canvas = Image.new("RGBA", source.size, 0)
    canvas.alpha_composite(cutout, (box[0], box[1]))
    return canvas


def remove_background(source_path: Path, output_path: Path, model: str) -> None:
    from PIL import Image, ImageChops, ImageOps

    if not source_path.is_file() or source_path.is_symlink():
        raise ValueError("input is not a regular file")
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with Image.open(source_path) as source:
        validate_dimensions(source)
        source = ImageOps.exif_transpose(source)
        validate_dimensions(source)
        source.load()
        source = source.convert("RGB")
    session = create_session(model)
    masks = session.predict(source)
    if not isinstance(masks, list) or not masks:
        raise ValueError("background remover returned no mask")
    mask = masks[0].convert("L")
    for extra in masks[1:]:
        mask = ImageChops.lighter(mask, extra.convert("L"))
    del masks, session
    gc.collect()
    result = adaptive_cutout(source, mask)
    validate_dimensions(result)
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
            remove_background(args.input, args.output, args.model)
    except Exception as exc:
        print(f"background removal failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
