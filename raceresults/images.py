"""Logo uploads: fix orientation, shrink to fit, keep transparency, save as PNG."""

import hashlib
import io
from pathlib import Path

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_SIZE = (600, 200)  # shown at up to 300 x 100 CSS pixels, so this stays sharp on phones
ALLOWED_FORMATS = {"PNG", "JPEG", "GIF", "WEBP", "BMP", "TIFF"}


class ImageError(ValueError):
    pass


def process_logo(data):
    """Return PNG bytes of the logo resized to fit MAX_SIZE."""
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 30_000_000  # keep huge images from exhausting a Pi's memory
    if not data:
        raise ImageError("The file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageError("That image is over 10 MB; please use a smaller file")
    try:
        image = Image.open(io.BytesIO(data))
        if image.format not in ALLOWED_FORMATS:
            raise ImageError("Use a PNG, JPG, GIF or WebP image")
        image.draft("RGB", (MAX_SIZE[0] * 2, MAX_SIZE[1] * 2))  # JPEG: decode at reduced size
        image.load()
    except ImageError:
        raise
    except Exception:
        raise ImageError("That file isn't an image this app can read (use PNG, JPG, GIF or WebP)") from None
    image = ImageOps.exif_transpose(image)
    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    image = image.convert("RGBA" if has_alpha else "RGB")
    image.thumbnail(MAX_SIZE, Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


def save_logo(data_dir, prefix, data):
    """Process and store a logo in data/logos. Returns the new file name."""
    png = process_logo(data)
    folder = Path(data_dir) / "logos"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}-{hashlib.sha256(png).hexdigest()[:12]}.png"
    (folder / name).write_bytes(png)
    return name


def remove_logo(data_dir, name):
    if name and "/" not in name and "\\" not in name:
        path = Path(data_dir) / "logos" / name
        if path.is_file():
            path.unlink()
