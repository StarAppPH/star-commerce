"""colorFinder.py — the main colors of a photo the admin attached, as hex
codes (hand-written, no database).

readColors() takes one photo (a JPEG or PNG) and, optionally, a focus: a
short phrase naming the part to read ("green square logo").
1. With a focus, SAM (productFinder.biggestMatch) finds it and only its box
   is read; no match reads the whole photo (focusFound false).
2. The part read is shrunk to COLOR_SAMPLE_SIDE pixels on its long side,
   nearest pixel (no blend of two colors is made up); transparent pixels
   (a PNG logo's background) are left out.
3. Pillow's median cut sorts the pixels into COLOR_BUCKETS buckets. Each
   bucket's color is its most common exact color when that is at least
   COLOR_MODE_SHARE of the bucket (a flat area keeps its exact color, even
   in a bucket it shares), else the bucket's average (a photo's noise).
4. Colors closer than COLOR_MERGE_DISTANCE are one (the blended pixels on
   an edge join the color they blend from), each keeping the color of its
   biggest member; those under COLOR_MIN_SHARE of the pixels are dropped,
   and the PHOTO_COLORS_MAX biggest are returned, biggest first.

No model reads the colors: the chat model cannot see exact pixel values,
and Pillow can. The photo and the focus are never logged.
"""

import math
from collections import Counter
from dataclasses import dataclass

import productFinder
from objects import PHOTO_COLORS_MAX

COLOR_SAMPLE_SIDE = 200
COLOR_BUCKETS = 16
COLOR_MODE_SHARE = 0.25
# Euclidean distance in RGB (0 to 441) under which two colors are one.
COLOR_MERGE_DISTANCE = 40
COLOR_MIN_SHARE = 0.03
# A pixel at least this opaque (alpha, 0 to 255) is read.
COLOR_MIN_ALPHA = 128
# A color whose channels differ by at most this is white, black or grey.
NEUTRAL_SPREAD = 24


@dataclass(frozen=True)
class PhotoColor:
    hex: str
    share: float
    neutral: bool


@dataclass(frozen=True)
class ColorReading:
    colors: list[PhotoColor]
    focusFound: bool | None  # None without a focus


def hexOf(rgb: tuple[int, int, int]) -> str:
    return '#{:02X}{:02X}{:02X}'.format(*rgb)


def isNeutral(rgb: tuple[int, int, int]) -> bool:
    return max(rgb) - min(rgb) <= NEUTRAL_SPREAD


def _opaquePixels(image) -> list[tuple[int, int, int]]:
    """The RGB of each opaque pixel of `image` (RGBA), shrunk first."""
    from PIL import Image
    copy = image.copy()
    copy.thumbnail((COLOR_SAMPLE_SIDE, COLOR_SAMPLE_SIDE),
                   resample=Image.Resampling.NEAREST, reducing_gap=None)
    return [(r, g, b) for r, g, b, a in copy.get_flattened_data()
            if a >= COLOR_MIN_ALPHA]


def mainColors(image) -> list[PhotoColor]:
    """The main colors of `image` (RGBA), biggest share first."""
    from PIL import Image
    pixels = _opaquePixels(image)
    if not pixels:
        return []
    strip = Image.new('RGB', (len(pixels), 1))
    strip.putdata(pixels)
    quantized = strip.quantize(colors=COLOR_BUCKETS,
                               method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette()
    members: dict[int, Counter] = {}
    for index, rgb in zip(quantized.get_flattened_data(), pixels):
        members.setdefault(index, Counter())[rgb] += 1
    buckets = []
    for index, colors in members.items():
        count = colors.total()
        rgb, modeCount = colors.most_common(1)[0]
        if modeCount < count * COLOR_MODE_SHARE:
            rgb = tuple(palette[3 * index:3 * index + 3])
        buckets.append((count, rgb))
    buckets.sort(reverse=True)
    merged: list[list] = []  # [count, rgb], the biggest member's rgb
    for count, rgb in buckets:
        near = next((kept for kept in merged
                     if math.dist(kept[1], rgb) < COLOR_MERGE_DISTANCE),
                    None)
        if near is None:
            merged.append([count, rgb])
        else:
            near[0] += count
    merged.sort(key=lambda kept: kept[0], reverse=True)
    total = len(pixels)
    return [PhotoColor(hexOf(rgb), round(count / total, 3), isNeutral(rgb))
            for count, rgb in merged
            if count / total >= COLOR_MIN_SHARE][:PHOTO_COLORS_MAX]


def _flattened(image):
    """`image` (RGBA) on white, in RGB: what SAM sees of a PNG logo."""
    from PIL import Image
    flat = Image.new('RGB', image.size, 'white')
    flat.paste(image, mask=image.getchannel('A'))
    return flat


def readColors(data: bytes, focus: str | None,
               deadline: float) -> ColorReading:
    """The main colors of the photo `data`, of its `focus` only when SAM
    finds it, done before `deadline` (time.monotonic()). A photo that
    cannot be read is a ValueError; a SAM failure is a 503."""
    image = productFinder.uprightImage(data, 'RGBA')
    if focus is None:
        return ColorReading(mainColors(image), None)
    box = productFinder.biggestMatch(
        productFinder.modelCopy(_flattened(image)), focus, deadline)
    if box is None:
        return ColorReading(mainColors(image), False)
    # No margin: the colors around the part are not its own.
    scaleX, scaleY = image.width / box.width, image.height / box.height
    left, top = round(box.left * scaleX), round(box.top * scaleY)
    right = max(left + 1, min(image.width, round(box.right * scaleX)))
    bottom = max(top + 1, min(image.height, round(box.bottom * scaleY)))
    return ColorReading(mainColors(image.crop((left, top, right, bottom))),
                        True)
