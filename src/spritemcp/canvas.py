"""Pixel canvas with named layers for the sprite toolkit."""

from __future__ import annotations

from copy import deepcopy
from typing import Iterable, Iterator, Sequence

from .palette import TRANSPARENT, Color

PixelGrid = list[list[Color]]


class Layer:
    """A named RGBA pixel buffer (transparent by default)."""

    def __init__(self, name: str, width: int, height: int) -> None:
        if width < 1 or height < 1:
            raise ValueError("Layer size must be at least 1x1")
        self.name = name
        self.width = width
        self.height = height
        self.visible = True
        self.pixels: PixelGrid = [
            [TRANSPARENT for _ in range(width)] for _ in range(height)
        ]

    def clear(self) -> None:
        for y in range(self.height):
            for x in range(self.width):
                self.pixels[y][x] = TRANSPARENT

    def get(self, x: int, y: int) -> Color:
        self._check_bounds(x, y)
        return self.pixels[y][x]

    def set(self, x: int, y: int, color: Color) -> None:
        self._check_bounds(x, y)
        self.pixels[y][x] = color

    def fill_rect(self, x: int, y: int, w: int, h: int, color: Color) -> None:
        for py in range(y, y + h):
            for px in range(x, x + w):
                if 0 <= px < self.width and 0 <= py < self.height:
                    self.pixels[py][px] = color

    def paint(self, points: Iterable[tuple[int, int]], color: Color) -> None:
        for x, y in points:
            if 0 <= x < self.width and 0 <= y < self.height:
                self.pixels[y][x] = color

    def copy_pixels(self) -> PixelGrid:
        return deepcopy(self.pixels)

    def _check_bounds(self, x: int, y: int) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(
                f"Pixel ({x}, {y}) out of bounds for {self.width}x{self.height}"
            )


def _blend_over(dst: Color, src: Color) -> Color:
    """Standard 'src over dst' alpha composite for one pixel."""
    sr, sg, sb, sa = src
    dr, dg, db, da = dst
    if sa == 255:
        return src
    if sa == 0:
        return dst
    src_a = sa / 255.0
    dst_a = da / 255.0
    out_a = src_a + dst_a * (1.0 - src_a)
    if out_a <= 0.0:
        return TRANSPARENT
    r = int((sr * src_a + dr * dst_a * (1.0 - src_a)) / out_a)
    g = int((sg * src_a + dg * dst_a * (1.0 - src_a)) / out_a)
    b = int((sb * src_a + db * dst_a * (1.0 - src_a)) / out_a)
    a = int(round(out_a * 255.0))
    return (r, g, b, a)


class SpriteCanvas:
    """Multi-layer pixel canvas. Layers composite bottom → top by list order."""

    def __init__(self, width: int, height: int, *, name: str = "sprite") -> None:
        if width < 1 or height < 1:
            raise ValueError("Canvas size must be at least 1x1")
        self.width = width
        self.height = height
        self.name = name
        self._layers: list[Layer] = []
        self._by_name: dict[str, Layer] = {}

    def add_layer(self, name: str, *, index: int | None = None) -> Layer:
        if name in self._by_name:
            raise ValueError(f"Layer already exists: {name!r}")
        layer = Layer(name, self.width, self.height)
        if index is None:
            self._layers.append(layer)
        else:
            self._layers.insert(index, layer)
        self._by_name[name] = layer
        return layer

    def get_layer(self, name: str) -> Layer:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise KeyError(f"Unknown layer {name!r}") from exc

    def layer_names(self) -> list[str]:
        return [layer.name for layer in self._layers]

    def layers(self) -> Sequence[Layer]:
        return tuple(self._layers)

    def paint(
        self,
        layer: str,
        x: int,
        y: int,
        color: Color,
    ) -> None:
        """Paint a single pixel on a named layer."""
        self.get_layer(layer).set(x, y, color)

    def paint_points(
        self,
        layer: str,
        points: Iterable[tuple[int, int]],
        color: Color,
    ) -> None:
        self.get_layer(layer).paint(points, color)

    def fill_rect(
        self,
        layer: str,
        x: int,
        y: int,
        w: int,
        h: int,
        color: Color,
    ) -> None:
        self.get_layer(layer).fill_rect(x, y, w, h, color)

    def composite(self) -> PixelGrid:
        """Flatten visible layers bottom → top into one RGBA grid."""
        out: PixelGrid = [
            [TRANSPARENT for _ in range(self.width)] for _ in range(self.height)
        ]
        for layer in self._layers:
            if not layer.visible:
                continue
            for y in range(self.height):
                for x in range(self.width):
                    out[y][x] = _blend_over(out[y][x], layer.pixels[y][x])
        return out

    def __iter__(self) -> Iterator[Layer]:
        return iter(self._layers)
