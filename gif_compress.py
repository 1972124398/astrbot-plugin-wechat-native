"""Fit a GIF into the sticker channel's size, frame and duration limits.

WeChat only renders a sticker bubble for content sent through the sticker channel.
Native prepares one sticker from an image source, and the prepared output has hard
limits: 1 MiB, 200 frames, 10 s and a 1024 px longest edge. Most chat GIFs are far
above 1 MiB, so preparing one unchanged is refused and the send falls back to an
ordinary image, which WeChat shows as a still picture.

A GIF that already fits is handed back untouched; Native then keeps those exact
bytes, because its preparation preserves a GIF source when no edge limit or trim is
requested. Only a GIF outside the limits is re-encoded through a quality ladder, so
the ladder is a fallback rather than the normal path.
"""

import io
import math
import time

from PIL import Image, ImageSequence

MODES = ("none", "auto", "conservative")

MAX_STICKER_BYTES = 1024 * 1024
MAX_STICKER_FRAMES = 200
MAX_STICKER_DURATION_MS = 10_000
MAX_STICKER_EDGE = 1024
MAX_STICKER_PIXELS = 16_000_000
COMPRESSION_TIMEOUT = 30.0

# (longest edge, frame stride, palette colours), ordered from best to smallest.
# "auto" trades resolution and frame rate for a result that still animates; the
# "conservative" ladder never samples frames, so it only loses sharpness and colour.
LADDERS = {
    "auto": (
        (1024, 1, 256),
        (320, 1, 256),
        (320, 2, 256),
        (240, 2, 256),
        (240, 2, 128),
        (200, 2, 128),
        (160, 2, 64),
        (128, 2, 64),
        (96, 3, 32),
        (64, 4, 16),
    ),
    "conservative": (
        (1024, 1, 256),
        (768, 1, 256),
        (512, 1, 128),
        (384, 1, 128),
        (256, 1, 64),
        (192, 1, 32),
        (128, 1, 16),
    ),
}


def fit_sticker_gif(
    data: bytes, mode: str, *, timeout: float = COMPRESSION_TIMEOUT
) -> bytes | None:
    """Return GIF bytes the sticker channel can prepare, or None to leave the send alone.

    None means the caller keeps its previous behaviour: compression is off, the
    source is not a readable GIF, or no ladder step fit the budget in time.
    """
    ladder = LADDERS.get(mode)
    if ladder is None:
        return None
    try:
        size, durations = _scan(data)
    except Exception:
        return None
    if not durations or size[0] * size[1] > MAX_STICKER_PIXELS:
        return None
    if _fits(data, size, durations):
        return data
    needed = _needed_stride(durations, len(durations))
    if needed > 1 and mode == "conservative":
        # This mode promises to keep every frame, so a source that only fits by
        # dropping frames is left alone for the caller's ordinary image send.
        return None
    deadline = time.monotonic() + timeout
    for edge, stride, colours in ladder:
        if time.monotonic() > deadline:
            return None
        step = max(stride, needed)
        try:
            output = _encode(_frames(data, step), durations[::step], edge, colours)
        except Exception:
            return None
        if len(output) <= MAX_STICKER_BYTES:
            return output
    return None


def _fits(data: bytes, size: tuple[int, int], durations: list[int]) -> bool:
    return (
        len(data) <= MAX_STICKER_BYTES
        and len(durations) <= MAX_STICKER_FRAMES
        and sum(durations) <= MAX_STICKER_DURATION_MS
        and max(size) <= MAX_STICKER_EDGE
    )


def _needed_stride(durations: list[int], frames: int) -> int:
    """Return the smallest stride that keeps frames and duration under the limits."""
    duration = math.ceil(sum(durations) / MAX_STICKER_DURATION_MS)
    count = math.ceil(frames / MAX_STICKER_FRAMES)
    return max(1, duration, count)


def _scan(data: bytes) -> tuple[tuple[int, int], list[int]]:
    """Read the canvas size and per-frame delays without keeping pixel data."""
    durations = []
    with Image.open(io.BytesIO(data)) as image:
        size = image.size
        for frame in ImageSequence.Iterator(image):
            durations.append(int(frame.info.get("duration") or 0))
    return size, durations


def _frames(data: bytes, stride: int) -> list[Image.Image]:
    frames = []
    with Image.open(io.BytesIO(data)) as image:
        for index, frame in enumerate(ImageSequence.Iterator(image)):
            if index % stride:
                continue
            frames.append(_flatten(frame))
    return frames


def _flatten(frame: Image.Image) -> Image.Image:
    """Composite one frame onto white; a re-quantized GIF keeps no alpha channel."""
    layer = frame.convert("RGBA")
    background = Image.new("RGB", layer.size, (255, 255, 255))
    background.paste(layer, mask=layer.getchannel("A"))
    return background


def _encode(
    frames: list[Image.Image], durations: list[int], edge: int, colours: int
) -> bytes:
    width, height = frames[0].size
    scale = min(1.0, edge / max(width, height))
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    images = []
    for frame in frames:
        image = frame if frame.size == size else frame.resize(size, Image.Resampling.BILINEAR)
        # The source palette may carry a transparency index whose tuple form breaks
        # Pillow's local header writer, so each frame starts with no metadata.
        image.info = {}
        images.append(image.quantize(colors=colours, method=Image.FASTOCTREE))
    buffer = io.BytesIO()
    images[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    return buffer.getvalue()
