from io import BytesIO

from PIL import Image, ImageOps


def quantize_image(source: bytes, colors: int = 128) -> bytes:
    if not 2 <= colors <= 256:
        raise ValueError("colors must be between 2 and 256")

    with Image.open(BytesIO(source)) as opened:
        opened.load()
        image = ImageOps.exif_transpose(opened)

        has_alpha = "A" in image.getbands() or "transparency" in image.info

        if has_alpha:
            image = image.convert("RGBA")
            method = Image.Quantize.FASTOCTREE
        else:
            image = image.convert("RGB")
            method = Image.Quantize.MEDIANCUT

        quantized = image.quantize(
            colors=colors,
            method=method,
            dither=Image.Dither.FLOYDSTEINBERG,
        )

        output = BytesIO()
        quantized.save(output, format="PNG", optimize=True)
        return output.getvalue()
