"""Core domain models.

These dataclasses are the shared vocabulary between the domain, infrastructure
(PDF engine) and application layers. They are intentionally engine-agnostic: no
PyMuPDF objects appear here, only plain serializable data. This lets them cross
the worker-process boundary (D4) and be stored in commands (D5).

Coordinate convention: PDF *user space*, origin top-left, y grows downward,
matching PyMuPDF's `Rect`. Rotation/crop normalization happens in the engine
before values reach these models (see coordinates.py / extractor.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class Direction(Enum):
    """Writing direction of a text line."""

    HORIZONTAL = auto()
    VERTICAL = auto()
    UNKNOWN = auto()


class EditMode(Enum):
    """The two supported replacement modes (AGENTS.md §9)."""

    PRESERVE_LINE = auto()  # Mode A: words / single lines keep baseline
    REFLOW_BOX = auto()  # Mode B: paragraphs reflow inside a rectangle


class TextAlignment(Enum):
    LEFT = auto()
    CENTER = auto()
    RIGHT = auto()
    JUSTIFY = auto()


@dataclass(frozen=True)
class Rect:
    """Axis-aligned rectangle in PDF user space (top-left origin)."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def intersects(self, other: Rect) -> bool:
        return not (self.x1 <= other.x0 or other.x1 <= self.x0
                    or self.y1 <= other.y0 or other.y1 <= self.y0)

    def inflated(self, dx: float, dy: float) -> Rect:
        return Rect(self.x0 - dx, self.y0 - dy, self.x1 + dx, self.y1 + dy)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class Color:
    """sRGB color, 0..1 per channel."""

    r: float
    g: float
    b: float

    @staticmethod
    def from_int(value: int) -> Color:
        r = ((value >> 16) & 0xFF) / 255.0
        g = ((value >> 8) & 0xFF) / 255.0
        b = (value & 0xFF) / 255.0
        return Color(r, g, b)

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.r, self.g, self.b)


@dataclass
class FontReference:
    """Font identity + presentation for a run of text.

    `embedded_xref` is the PDF object number of the embedded font when present
    (enables reuse per D7). `embedded_name` is the in-document name (may carry a
    subset prefix like `ABCDEF+Arial`).
    """

    name: str  # base font name as referenced on the page
    family: str = ""  # cleaned family (subset prefix stripped)
    size: float = 12.0
    bold: bool = False
    italic: bool = False
    monospace: bool = False
    serif: bool = False
    embedded_xref: int = 0
    embedded_name: str = ""
    color: Color = field(default_factory=lambda: Color(0, 0, 0))

    @property
    def is_subset(self) -> bool:
        return "+" in self.embedded_name[:8] if self.embedded_name else False


@dataclass
class TextRun:
    """A maximal run of same-style characters within a line."""

    text: str
    font: FontReference
    bbox: Rect
    origin: Point  # baseline start
    direction: Direction = Direction.HORIZONTAL
    # per-character bboxes, parallel to `text` (None when unavailable)
    char_bboxes: list[Rect] | None = None


@dataclass
class TextLine:
    """A single line composed of one or more runs."""

    runs: list[TextRun]
    bbox: Rect
    baseline_y: float
    direction: Direction = Direction.HORIZONTAL

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


@dataclass
class TextRegion:
    """A selectable/editable unit: one line (Mode A) or a paragraph (Mode B).

    `region_id` is revision-scoped (see AGENTS.md §8): it embeds the source
    revision so a stale id can be detected after mutations. Never treat raw
    extraction block numbers as stable.
    """

    region_id: str
    lines: list[TextLine]
    bbox: Rect
    mode: EditMode
    revision: int
    paragraph_box: Rect | None = None  # explicit reflow rect for Mode B
    editable: bool = True  # engine capability, attached at extraction time
    unsupported_reason: str = ""  # human-readable when not editable

    @property
    def text(self) -> str:
        sep = "" if self.mode == EditMode.PRESERVE_LINE else "\n"
        return sep.join(ln.text for ln in self.lines)

    @property
    def dominant_font(self) -> FontReference:
        # font of the longest run — good enough for style display / default reuse
        best = max((r for ln in self.lines for r in ln.runs),
                   key=lambda r: len(r.text), default=None)
        return best.font if best else FontReference(name="Helvetica")


@dataclass
class PageGeometry:
    """Normalized geometry of one page."""

    index: int
    mediabox: Rect
    cropbox: Rect
    rotation: int  # 0/90/180/270, the /Rotate value
    width: float  # cropbox width after accounting for rotation
    height: float


@dataclass
class ReplacementEdit:
    """A requested replacement, independent of any widget.

    Carries everything needed to replay deterministically (D5): the target
    region id + source revision (to detect staleness), the new text, the mode,
    and optional style overrides. For REFLOW_BOX, `target_box` is the layout
    rectangle, which is *separate* from the source removal geometry (§9 Mode B).
    """

    region_id: str
    source_revision: int
    new_text: str
    mode: EditMode
    target_box: Rect | None = None  # reflow rectangle (Mode B)
    font_override: str | None = None  # family name to substitute
    size_override: float | None = None
    color_override: Color | None = None
    bold_override: bool | None = None  # None = keep the original's weight
    italic_override: bool | None = None
    alignment: TextAlignment = TextAlignment.LEFT
    auto_shrink: bool = False  # opt-in only (§9)
    page_index: int = 0


class OverflowChoice(Enum):
    """Explicit overflow resolutions offered to the user (§9)."""

    ENLARGE_BOX = auto()
    REDUCE_FONT = auto()
    SHORTEN_TEXT = auto()
    CANCEL = auto()


@dataclass
class ValidationResult:
    """Outcome of validating a candidate edit before commit."""

    ok: bool
    overflowed: bool = False
    overflow_px: float = 0.0
    required_size: float | None = None  # suggested size when auto-shrink
    substituted_font: str | None = None  # family actually used, if != original
    missing_glyphs: list[str] = field(default_factory=list)
    collateral_change: bool = False  # text outside the region changed
    background_preserved: bool = True
    issues: list[str] = field(default_factory=list)

    def blocker(self) -> str | None:
        return self.issues[0] if self.issues else None


@dataclass
class EditCapability:
    """Whether a region can be edited, and how."""

    editable: bool
    mode: EditMode | None = None
    reason: str = ""  # human-readable when not editable


@dataclass
class DocumentMeta:
    """Stable-ish document facts surfaced to the UI."""

    doc_id: str
    path: str | None
    page_count: int
    is_encrypted: bool
    needs_pass: bool
    is_signed: bool
    permissions: int
    revision: int
    fingerprint: str  # source fingerprint for recovery staleness (§11)
    title: str = ""
