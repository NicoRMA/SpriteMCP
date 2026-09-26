"""Frame sequence containers for animation clips.

``Frame`` / ``FrameSequence`` hold SpriteCanvas snapshots for future clip
pipelines. Finished on-disk frames use ``export.export_animation_sheet_and_gif``
/ ``character.export_animation_preview`` for contact sheets and preview GIFs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .canvas import SpriteCanvas


@dataclass
class Frame:
    """One animation frame. Canvas may be shared or cloned by callers later."""

    name: str
    canvas: SpriteCanvas
    duration_ms: int = 100


@dataclass
class FrameSequence:
    """Ordered frames for a clip (idle, walk, …)."""

    name: str
    frames: list[Frame] = field(default_factory=list)

    def add(self, frame: Frame) -> None:
        self.frames.append(frame)

    def __len__(self) -> int:
        return len(self.frames)

    def names(self) -> Sequence[str]:
        return [f.name for f in self.frames]
