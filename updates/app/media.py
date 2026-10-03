"""Editable site imagery stored durably in PostgreSQL, with bounded decoding."""
import hashlib
import io
import re
from functools import lru_cache
from html import escape, unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from .models import SiteImage

SITE = Path(__file__).resolve().parent / "original_site"
Image.MAX_IMAGE_PIXELS = 24_000_000


def canonical_url(url):
    parsed = urlsplit(unescape(url))
    return parse_qs(parsed.query).get("url", ["/" + parsed.path.lstrip("/")])[0]


@lru_cache(maxsize=1)
def catalog():
    entries = {}
    for page in sorted(SITE.glob("*.html")):
        text = page.read_text()
        for tag in re.findall(r'<img\b[^>]*>', text):
            src = re.search(r'\bsrc="([^"]+)"', tag)
            if not src:
                continue
            url = unescape(src[1])
            path = SITE / urlsplit(url).path.lstrip("/")
            if not path.is_file():
                continue
            canonical = canonical_url(url)
            key = hashlib.sha256(canonical.encode()).hexdigest()[:24]
            alt = re.search(r'\balt="([^"]*)"', tag)
            entry = entries.setdefault(key, {"key": key, "canonical": canonical, "path": "/" + urlsplit(url).path.lstrip("/"), "alt": unescape(alt[1]) if alt else "", "pages": set()})
            entry["pages"].add(page.stem)
    # Social preview and logo are editable too, with the same verified raster upload.
    for path, alt in (("/images/og-ethos-tribo.jpg", "Prévia de compartilhamento"), ("/iconc399.svg", "Marca Ethos Tribo")):
        if (SITE / path.lstrip("/")).is_file():
            key = hashlib.sha256(path.encode()).hexdigest()[:24]
            entries[key] = {"key": key, "canonical": path, "path": path, "alt": alt, "pages": {"todas"}}
    return entries


def apply_media_html(db, text):
    replacements = {catalog()[x.key]["canonical"]: x for x in db.scalars(select(SiteImage)).all() if x.key in catalog()}
    if not replacements:
        return text
    def image_tag(match):
        tag = match[0]
        src = re.search(r'\bsrc="([^"]+)"', tag)
        image = replacements.get(canonical_url(src[1])) if src else None
        if image:
            url = f"/media/site/{image.key}?v={image.updated_at.isoformat()}"
            tag = re.sub(r'\bsrc="[^"]*"', f'src="{url}"', tag)
            tag = re.sub(r'\bsrc[Ss]et="[^"]*"', '', tag)
            tag = re.sub(r'\balt="[^"]*"', f'alt="{escape(image.alt, quote=True)}"', tag)
        return tag
    text = re.sub(r'<img\b[^>]*>', image_tag, text)
    # OG/Twitter metadata remains correct when the social preview image changes.
    for canonical, image in replacements.items():
        if canonical == "/images/og-ethos-tribo.jpg":
            text = text.replace(canonical, f"/media/site/{image.key}?v={image.updated_at.isoformat()}")
    return text


def decode_image(raw):
    if not raw or len(raw) > 5_000_000:
        raise ValueError("Envie uma foto JPG, PNG ou WebP de até 5 MB.")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in ("JPEG", "PNG", "WEBP"):
                raise ValueError("Formato não permitido. Use JPG, PNG ou WebP.")
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("Foto inválida ou grande demais.") from exc
    if image.width * image.height > 24_000_000:
        raise ValueError("A foto deve ter até 24 megapixels.")
    image.thumbnail((2400, 2400))
    return image


def encode(image):
    output = io.BytesIO()
    image.save(output, format="WEBP", quality=85, method=4)
    return output.getvalue()


def transform(original, rotation, ratio, x, y):
    image = decode_image(original)
    if rotation not in (0, 90, 180, 270) or ratio not in ("original", "16:9", "4:3", "1:1", "3:4") or not 0 <= x <= 100 or not 0 <= y <= 100:
        raise ValueError("Ajustes de foto inválidos.")
    if rotation:
        image = image.rotate(-rotation, expand=True)
    if ratio != "original":
        a, b = map(int, ratio.split(":"))
        if image.width / image.height > a / b:
            width, height = int(image.height * a / b), image.height
        else:
            width, height = image.width, int(image.width * b / a)
        left = int((image.width - width) * x / 100)
        top = int((image.height - height) * y / 100)
        image = image.crop((left, top, left + width, top + height))
    return encode(image)
