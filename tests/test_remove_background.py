import contextlib
import io
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "artifact-relay" / "remove_background.py"
SPEC = importlib.util.spec_from_file_location("remove_background_helper", MODULE_PATH)
remove_background = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = remove_background
SPEC.loader.exec_module(remove_background)


class AdaptiveCutoutTests(unittest.TestCase):
    def test_foreground_crop_uses_bounded_padding(self):
        mask = Image.new("L", (100, 100), 0)
        ImageDraw.Draw(mask).rectangle((40, 20, 59, 79), fill=255)

        self.assertEqual(remove_background.foreground_crop_box(mask), (28, 8, 72, 92))

    def test_empty_mask_is_rejected(self):
        with self.assertRaises(ValueError):
            remove_background.foreground_crop_box(Image.new("L", (20, 20), 0))

    def test_small_foreground_uses_alpha_matting_and_clears_hidden_rgb(self):
        source = Image.new("RGB", (100, 100), (250, 250, 250))
        mask = Image.new("L", source.size, 0)
        ImageDraw.Draw(mask).rectangle((40, 20, 59, 79), fill=255)
        calls = []

        def fake_matting(image, alpha, foreground, background, erode):
            calls.append((image.size, foreground, background, erode))
            result = Image.new("RGBA", image.size, (255, 255, 255, 255))
            result.putalpha(alpha)
            return result

        result = remove_background.adaptive_cutout(source, mask, fake_matting)

        self.assertEqual(
            calls,
            [
                (
                    (44, 84),
                    remove_background.MATTING_FOREGROUND_THRESHOLD,
                    remove_background.MATTING_BACKGROUND_THRESHOLD,
                    remove_background.MATTING_ERODE_SIZE,
                )
            ],
        )
        self.assertEqual(result.mode, "RGBA")
        self.assertEqual(result.getpixel((0, 0)), (0, 0, 0, 0))
        self.assertEqual(result.getpixel((50, 50)), (255, 255, 255, 255))

    def test_large_foreground_uses_bounded_standard_path(self):
        source = Image.new("RGB", (700, 700), (10, 20, 30))
        mask = Image.new("L", source.size, 0)
        ImageDraw.Draw(mask).rectangle((50, 50, 649, 649), fill=255)
        mask.putpixel((50, 350), 128)

        def unexpected_matting(*_args):
            self.fail("oversized foreground must not invoke Alpha Matting")

        result = remove_background.adaptive_cutout(source, mask, unexpected_matting)

        self.assertEqual(result.getpixel((0, 0)), (0, 0, 0, 0))
        self.assertEqual(result.getpixel((350, 350)), (10, 20, 30, 255))
        self.assertEqual(result.getpixel((50, 350)), (10, 20, 30, 128))

    def test_matting_failure_preserves_straight_alpha(self):
        source = Image.new("RGB", (100, 100), (100, 150, 200))
        mask = Image.new("L", source.size, 0)
        ImageDraw.Draw(mask).rectangle((40, 20, 59, 79), fill=255)
        mask.putpixel((40, 50), 128)

        def failed_matting(*_args):
            raise ValueError("trimap has no unknown region")

        result = remove_background.adaptive_cutout(source, mask, failed_matting)

        self.assertEqual(result.getpixel((40, 50)), (100, 150, 200, 128))

    def test_low_confidence_mask_skips_alpha_matting(self):
        source = Image.new("RGB", (100, 100), (100, 150, 200))
        mask = Image.new("L", source.size, 0)
        ImageDraw.Draw(mask).rectangle((40, 20, 59, 79), fill=128)

        def unexpected_matting(*_args):
            self.fail("low-confidence mask must not invoke Alpha Matting")

        result = remove_background.adaptive_cutout(source, mask, unexpected_matting)

        self.assertEqual(result.getpixel((50, 50)), (100, 150, 200, 128))

    def test_mask_size_must_match_source(self):
        with self.assertRaises(ValueError):
            remove_background.adaptive_cutout(
                Image.new("RGB", (20, 20)), Image.new("L", (19, 20))
            )


class RemoveBackgroundTests(unittest.TestCase):
    @staticmethod
    def usable_cutout(source, _mask):
        result = Image.new("RGBA", source.size, (0, 0, 0, 0))
        if source.width < 4 or source.height < 4:
            result.putpixel(
                (source.width - 1, source.height - 1), (10, 20, 30, 255)
            )
        else:
            ImageDraw.Draw(result).rectangle(
                (
                    source.width // 4,
                    source.height // 4,
                    source.width * 3 // 4,
                    source.height * 3 // 4,
                ),
                fill=(10, 20, 30, 255),
            )
        return result

    def test_prediction_masks_are_merged_and_output_is_replaced_atomically(self):
        first = Image.new("L", (20, 20), 0)
        second = Image.new("L", (20, 20), 0)
        first.putpixel((6, 6), 100)
        second.putpixel((6, 6), 200)
        second.putpixel((13, 13), 150)
        session = mock.Mock()
        session.predict.return_value = [first, second]

        def inspect_cutout(source, mask):
            self.assertEqual(source.mode, "RGB")
            self.assertEqual(mask.getpixel((6, 6)), 200)
            self.assertEqual(mask.getpixel((13, 13)), 150)
            return self.usable_cutout(source, mask)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.png"
            output_path = root / "result.png"
            Image.new("RGB", (20, 20), (255, 255, 255)).save(source_path)
            output_path.write_bytes(b"old output")

            with (
                mock.patch.object(remove_background, "create_session", return_value=session),
                mock.patch.object(
                    remove_background, "adaptive_cutout", side_effect=inspect_cutout
                ),
            ):
                remove_background.remove_background(
                    source_path, output_path, "isnet-general-use"
                )

            session.predict.assert_called_once()
            with Image.open(output_path) as result:
                self.assertEqual(result.mode, "RGBA")
                self.assertEqual(result.getpixel((10, 10)), (10, 20, 30, 255))
            self.assertEqual(list(root.glob(".result.png.part-*")), [])

    def test_missing_prediction_mask_is_rejected_without_output(self):
        session = mock.Mock()
        session.predict.return_value = []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.png"
            output_path = root / "result.png"
            Image.new("RGB", (20, 20), (255, 255, 255)).save(source_path)

            with mock.patch.object(
                remove_background, "create_session", return_value=session
            ):
                with self.assertRaisesRegex(ValueError, "returned no mask"):
                    remove_background.remove_background(
                        source_path, output_path, "isnet-general-use"
                    )

            self.assertFalse(output_path.exists())
            self.assertEqual(list(root.glob(".result.png.part-*")), [])

    def test_invalid_input_is_rejected_before_model_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "invalid.png"
            source_path.write_bytes(b"not an image")

            with mock.patch.object(remove_background, "create_session") as create:
                with self.assertRaises(Exception):
                    remove_background.remove_background(
                        source_path, root / "result.png", "isnet-general-use"
                    )

            create.assert_not_called()

    def test_cli_rejects_symlink_input_before_model_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.png"
            link_path = root / "source-link.png"
            output_path = root / "result.png"
            Image.new("RGB", (20, 20), (255, 255, 255)).save(source_path)
            link_path.symlink_to(source_path)

            arguments = [
                "remove_background.py",
                "remove",
                "--model",
                "isnet-general-use",
                "--input",
                str(link_path),
                "--output",
                str(output_path),
            ]
            with (
                mock.patch.object(sys, "argv", arguments),
                mock.patch.object(remove_background, "create_session") as create,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(remove_background.main(), 1)

            create.assert_not_called()
            self.assertFalse(output_path.exists())

    def test_exif_orientation_is_applied_before_prediction(self):
        session = mock.Mock()

        def predict(source):
            self.assertEqual(source.size, (2, 3))
            return [Image.new("L", source.size, 255)]

        session.predict.side_effect = predict

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "oriented.jpg"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (3, 2), (255, 255, 255)).save(source_path, exif=exif)

            with (
                mock.patch.object(remove_background, "create_session", return_value=session),
                mock.patch.object(
                    remove_background,
                    "adaptive_cutout",
                    side_effect=self.usable_cutout,
                ),
            ):
                remove_background.remove_background(
                    source_path, root / "result.png", "isnet-general-use"
                )

            session.predict.assert_called_once()


if __name__ == "__main__":
    unittest.main()
