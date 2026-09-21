import io
import unittest

from PIL import Image

from emotorad_ai.storage.derivatives import image_w900_webp


def png(width, height):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


class ImageDerivativeTests(unittest.TestCase):
    def test_a_wide_image_is_shrunk_to_900(self):
        out = Image.open(io.BytesIO(image_w900_webp(png(1800, 600))))
        self.assertEqual(out.format, "WEBP")
        self.assertEqual(out.size, (900, 300))

    def test_a_small_image_is_never_enlarged(self):
        out = Image.open(io.BytesIO(image_w900_webp(png(480, 320))))
        self.assertEqual(out.size, (480, 320))


if __name__ == "__main__":
    unittest.main()
