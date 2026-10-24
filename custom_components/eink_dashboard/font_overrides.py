# Copyright 2026 Andreas Schneider
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""User-selected font families, optionally chosen per font size.

Widgets draw all text in the bundled Roboto font.  When the user turns
on font overrides, the font family of text can be replaced, either for
every size (the fallback family) or for individual pixel sizes.  This
is meant for bitmap fonts such as Tamzen, which are only crisp at
their native size and are installed as one family per size.

The fonts themselves come from the custom font directory
(``config["font_dir"]``), which resvg already loads.  The Lovelace
card shows the SVG in the browser, which draws the text itself, so
:func:`browser_fonts` lists the files the card has to load too.

A family that is not installed in the font directory is skipped by
resvg and the text falls back to Roboto, so a missing font never
breaks a dashboard.

Bitmap fonts only exist in a few sizes, but widgets compute their text
sizes from their own size.  While overrides are active, text up to the
largest size that has an installed override font is therefore drawn at
the nearest of those sizes (see :func:`snap_size`); larger text keeps
its size and uses the fallback family, where a scaled bitmap font is
still readable.

Two things have to agree on the font of a piece of text: resvg, which
draws it, and PIL, which measures it for truncation and alignment.
:func:`apply_font_families` rewrites the ``font-family`` of the drawn
SVG, and :func:`resolve_font_path` makes ``render._load_font`` measure
with the same font file.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import html
import math
import re
import struct
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

from PIL import ImageFont

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

# Family names are written into SVG attributes and CSS-like font
# lists, so these characters are never allowed in them.
_FORBIDDEN_FAMILY_CHARS = frozenset("\"'<>&\\,;")

_FONT_SUFFIXES = (".ttf", ".otf")

# Plain stand-ins for typographic characters that bitmap fonts rarely
# have.  Used only when the chosen font lacks the original; characters
# without an entry here, such as the euro sign, are removed instead.
_LOOKALIKES = {
    "\u2018": "'",  # left single quotation mark
    "\u2019": "'",  # right single quotation mark (apostrophe)
    "\u201a": "'",  # single low-9 quotation mark
    "\u2032": "'",  # prime
    "\u201c": '"',  # left double quotation mark
    "\u201d": '"',  # right double quotation mark
    "\u201e": '"',  # double low-9 quotation mark
    "\u2033": '"',  # double prime
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2212": "-",  # minus sign
    "\u2022": "*",  # bullet
    "\u00b7": "-",  # middle dot (a separator, like a dash)
}
_TEXT_ELEMENT = re.compile(r"<text\b[^>]*>.*?</text>", re.DOTALL)
_MARKUP_PART = re.compile(r"(<[^>]*>)")
_FAMILY_ATTR = 'font-family="Roboto"'
_ANY_FAMILY_ATTR = re.compile(r'font-family="[^"]*"')
_SIZE_ATTR = re.compile(r'font-size="([0-9]+(?:\.[0-9]+)?)(?:px)?"')
_BASELINE_ATTR = re.compile(r'\s*dominant-baseline="(middle|central)"')
# Font of the text inside a tag while rewriting: size, family and weight.
Spec = tuple[float | None, str, int]
_WEIGHT_ATTR = re.compile(r'font-weight="(\w+)"')
_ANCHOR_ATTR = re.compile(r'\s*text-anchor="(middle|end)"')
_X_ATTR = re.compile(r'(\sx=")(-?[0-9.]+)(")')
_Y_ATTR = re.compile(r'(\sy=")(-?[0-9.]+)(")')


@dataclass(frozen=True)
class FontSettings:
    """Font family choices for one render.

    An empty instance changes nothing: all text stays in Roboto.
    """

    font_dir: str = ""
    """Custom font directory the override families are read from."""
    family: str = ""
    """Family used for every size without an entry in
    ``size_overrides``; empty keeps Roboto."""
    size_overrides: Mapping[int, str] = field(default_factory=dict)
    """Family per exact font size in pixels."""

    def family_for(self, size: int) -> str:
        """Return the override family for ``size``, or ``""`` for none."""
        return self.size_overrides.get(size) or self.family


# Holds the settings of the render running in this thread or task; the
# default is ``None`` because a ContextVar default must be immutable.
_FONT_SETTINGS: contextvars.ContextVar[FontSettings | None] = (
    contextvars.ContextVar("eink_dashboard_font_settings", default=None)
)


# Sizes that have an installed override font in the render running in
# this thread or task, ascending; set by ``use_font_settings``.
_NATIVE_SIZES: contextvars.ContextVar[tuple[int, ...]] = (
    contextvars.ContextVar("eink_dashboard_native_sizes", default=())
)


def is_valid_family(name: str) -> bool:
    """Return whether ``name`` can be used as a font family.

    Args:
        name: Family name typed by the user.

    Returns:
        ``True`` for a non-empty name without quote, bracket,
        ampersand, backslash, comma or semicolon characters.
    """
    return bool(name.strip()) and not _FORBIDDEN_FAMILY_CHARS & set(name)


def font_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Return the render config keys that describe the font choices.

    Args:
        options: Config entry options.

    Returns:
        ``font_dir`` always, plus ``font_family`` and
        ``font_size_overrides`` (size to family) when the user turned
        on **Override fonts for specific sizes**.
    """
    config: dict[str, Any] = {"font_dir": options.get("font_dir", "")}
    if options.get("override_font_sizes"):
        config["font_family"] = options.get("font_family", "")
        config["font_size_overrides"] = {
            int(o["size"]): o["family"]
            for o in options.get("font_size_overrides", [])
        }
    return config


def font_settings_from_config(config: Mapping[str, Any]) -> FontSettings:
    """Build :class:`FontSettings` from a render config.

    Args:
        config: Display config; reads ``font_dir``, ``font_family``
            and ``font_size_overrides``.

    Returns:
        The settings; entries with an invalid family name are dropped.
    """
    family = str(config.get("font_family") or "").strip()
    overrides = {
        int(size): name.strip()
        for size, name in (config.get("font_size_overrides") or {}).items()
        if is_valid_family(name)
    }
    return FontSettings(
        font_dir=str(config.get("font_dir") or ""),
        family=family if is_valid_family(family) else "",
        size_overrides=overrides,
    )


@contextlib.contextmanager
def use_font_settings(settings: FontSettings) -> Iterator[None]:
    """Make ``settings`` the active font choices inside a ``with`` block."""
    token = _FONT_SETTINGS.set(settings)
    native_token = _NATIVE_SIZES.set(_native_sizes(settings))
    try:
        yield
    finally:
        _NATIVE_SIZES.reset(native_token)
        _FONT_SETTINGS.reset(token)


def _weight_of(style: str) -> int:
    """Map a font style name such as ``"Bold"`` to 400, 500 or 700."""
    lowered = style.lower()
    if "bold" in lowered:
        return 700
    if "medium" in lowered:
        return 500
    return 400


@functools.lru_cache(maxsize=8)
def _scan_font_dir(font_dir: str, mtime_ns: int) -> dict[str, dict[int, str]]:
    """Index the fonts in a directory by family name and weight.

    Args:
        font_dir: Directory holding ``.ttf``/``.otf`` files.
        mtime_ns: Directory modification time, part of the cache key
            so newly copied fonts are noticed without a restart.

    Returns:
        Lower-cased family name to ``{weight: file path}``.
    """
    index: dict[str, dict[int, str]] = {}
    for path in sorted(Path(font_dir).iterdir()):
        if path.suffix.lower() not in _FONT_SUFFIXES:
            continue
        try:
            family, style = ImageFont.truetype(str(path), 10).getname()
        except (OSError, ValueError):
            continue
        if family:
            index.setdefault(family.lower(), {}).setdefault(
                _weight_of(style or ""), str(path)
            )
    return index


def _font_index(font_dir: str) -> dict[str, dict[int, str]]:
    """Return the :func:`_scan_font_dir` index, empty if unreadable."""
    try:
        mtime = Path(font_dir).stat().st_mtime_ns
        return _scan_font_dir(font_dir, mtime)
    except OSError:
        return {}


def browser_fonts(settings: FontSettings) -> list[dict[str, Any]]:
    """List the font files the browser needs to draw the same text.

    Does blocking file I/O, so call it from an executor.

    Args:
        settings: Active font choices.

    Returns:
        One ``{"family", "weight", "file"}`` entry per installed file
        of every chosen family.  ``family`` is the name as the user
        typed it, which is what the SVG refers to, and ``file`` is the
        file name inside the font directory.  Empty when nothing is
        overridden or the fonts are not installed.
    """
    if not settings.font_dir:
        return []
    families = {*settings.size_overrides.values(), settings.family}
    index = _font_index(settings.font_dir)
    return [
        {"family": family, "weight": weight, "file": Path(path).name}
        for family in sorted(f for f in families if f)
        for weight, path in sorted(index.get(family.lower(), {}).items())
    ]


def resolve_font_path(size: int, medium: bool, bold: bool) -> str | None:
    """Return the font file that text of ``size`` pixels is drawn in.

    Args:
        size: Font size in pixels.
        medium: Whether the text is Roboto Medium weight.
        bold: Whether the text is bold.

    Returns:
        Path of the override family's file in the font directory, or
        ``None`` when there is no override for ``size`` or the family
        is not installed, in which case Roboto is used.
    """
    settings = _FONT_SETTINGS.get() or FontSettings()
    family = settings.family_for(size)
    if not family or not settings.font_dir:
        return None
    weights = _font_index(settings.font_dir).get(family.lower())
    if not weights:
        return None
    wanted = 700 if bold else 500 if medium else 400
    # A family with a single weight (typical for bitmap fonts) serves
    # every request; otherwise prefer the exact weight, then regular.
    return (
        weights.get(wanted) or weights.get(400) or next(iter(weights.values()))
    )


def _cmap_offset(data: bytes) -> int | None:
    """Return the offset of the ``cmap`` table of a font file."""
    (num_tables,) = struct.unpack_from(">H", data, 4)
    for i in range(num_tables):
        tag, _checksum, offset, _length = struct.unpack_from(
            ">4sLLL", data, 12 + 16 * i
        )
        if tag == b"cmap":
            return offset
    return None


def _format4_codepoints(data: bytes, base: int) -> set[int]:
    """Read the covered code points of a cmap format 4 subtable."""
    (seg_x2,) = struct.unpack_from(">H", data, base + 6)
    segs = seg_x2 // 2
    ends = base + 14
    starts = ends + seg_x2 + 2
    deltas = starts + seg_x2
    range_offsets = deltas + seg_x2
    covered: set[int] = set()
    for i in range(segs):
        (end,) = struct.unpack_from(">H", data, ends + 2 * i)
        (start,) = struct.unpack_from(">H", data, starts + 2 * i)
        (delta,) = struct.unpack_from(">h", data, deltas + 2 * i)
        (range_offset,) = struct.unpack_from(">H", data, range_offsets + 2 * i)
        for code in range(start, min(end, 0xFFFE) + 1):
            if range_offset == 0:
                glyph = (code + delta) & 0xFFFF
            else:
                address = (
                    range_offsets + 2 * i + range_offset + 2 * (code - start)
                )
                (glyph,) = struct.unpack_from(">H", data, address)
                if glyph:
                    glyph = (glyph + delta) & 0xFFFF
            if glyph:
                covered.add(code)
    return covered


def _format12_codepoints(data: bytes, base: int) -> set[int]:
    """Read the covered code points of a cmap format 12 subtable."""
    (groups,) = struct.unpack_from(">L", data, base + 12)
    covered: set[int] = set()
    for i in range(groups):
        start, end, glyph = struct.unpack_from(
            ">LLL", data, base + 16 + 12 * i
        )
        if glyph:
            covered.update(range(start, end + 1))
    return covered


@functools.lru_cache(maxsize=32)
def _font_codepoints(path: str) -> frozenset[int] | None:
    """Return the code points a font file has glyphs for.

    Reads the font's ``cmap`` table directly, because PIL does not
    expose which characters a font covers.

    Args:
        path: Path of a ``.ttf``/``.otf`` file.

    Returns:
        The covered code points, or ``None`` when the table cannot be
        read, in which case the font is assumed to cover everything.
    """
    try:
        data = Path(path).read_bytes()
        table = _cmap_offset(data)
        if table is None:
            return None
        (count,) = struct.unpack_from(">H", data, table + 2)
        covered: set[int] = set()
        for i in range(count):
            platform, encoding, offset = struct.unpack_from(
                ">HHL", data, table + 4 + 8 * i
            )
            if platform not in (0, 3) or (platform == 3 and encoding == 0):
                continue
            (fmt,) = struct.unpack_from(">H", data, table + offset)
            if fmt == 4:
                covered |= _format4_codepoints(data, table + offset)
            elif fmt == 12:
                covered |= _format12_codepoints(data, table + offset)
    except (OSError, struct.error):
        return None
    return frozenset(covered) if covered else None


def _family_codepoints(family: str, font_dir: str) -> frozenset[int] | None:
    """Return the code points of the installed ``family``, if known."""
    if not font_dir:
        return None
    weights = _font_index(font_dir).get(family.lower())
    if not weights:
        return None
    return _font_codepoints(weights.get(400) or next(iter(weights.values())))


def _fit_char(char: str, covered: frozenset[int]) -> str:
    """Return what to draw for ``char`` in a font covering ``covered``.

    Args:
        char: One character of the text.
        covered: Code points the font has glyphs for.

    Returns:
        ``char`` itself when the font has it, otherwise its plain
        look-alike from ``_LOOKALIKES`` (a straight quote for a curly
        one, a hyphen for a dash, and so on) when the font has that,
        a normal space for other whitespace, or ``""``.
    """
    if ord(char) in covered:
        return char
    if char.isspace():
        return " " if 0x20 in covered else ""
    stand_in = _LOOKALIKES.get(char, "")
    if stand_in and all(ord(c) in covered for c in stand_in):
        return stand_in
    return ""


def _fit_to_font(text: str, covered: frozenset[int] | None) -> str:
    """Make ``text`` drawable with only the glyphs of the chosen font.

    resvg draws a whole text element in another font (it picked bold
    Roboto) as soon as one character is missing, and a browser would
    swap only that character.  So characters the font lacks are
    replaced by a plain look-alike where there is one, such as a
    straight apostrophe for a curly one, and removed otherwise.  This
    keeps the chosen font for the rest and gives the same text in the
    SVG preview and the server image, instead of a fallback font or an
    empty box.

    Args:
        text: Text content from the SVG, still XML-escaped.
        covered: Code points of the chosen font, or ``None`` if unknown,
            in which case the text is only normalized.

    Returns:
        XML-escaped text that only uses glyphs of the font.
    """
    # Compose accents ("u" + U+0308 becomes one character), because
    # bitmap fonts only have the composed forms.
    plain = unicodedata.normalize("NFC", html.unescape(text))
    if covered is not None:
        plain = "".join(_fit_char(c, covered) for c in plain)
    return escape(plain)


def drop_missing_glyphs(text: str, font: Any) -> str:
    """Remove the characters a PIL font has no glyph for.

    Used for text that is measured but drawn without these characters,
    such as the truncation ellipsis, so the measured width matches.

    Args:
        text: Text to filter.
        font: A PIL font.  Fonts without a file path are not filtered.

    Returns:
        ``text`` without the characters missing in ``font``.
    """
    path = getattr(font, "path", None)
    covered = _font_codepoints(str(path)) if path else None
    if covered is None:
        return text
    return "".join(c for c in text if c.isspace() or ord(c) in covered)


def _native_sizes(settings: FontSettings) -> tuple[int, ...]:
    """Return the sizes that have an installed override font, ascending."""
    if not settings.font_dir:
        return ()
    index = _font_index(settings.font_dir)
    return tuple(
        sorted(
            size
            for size, family in settings.size_overrides.items()
            if index.get(family.lower())
        )
    )


def _snap(size: int, native: Sequence[int]) -> int:
    """Return ``size`` snapped to a size in ``native`` (see ``snap_size``)."""
    if not native or size > native[-1]:
        return size
    return max((s for s in native if s <= size), default=native[0])


def snap_size(size: int) -> int:
    """Return the size text of ``size`` pixels is really drawn at.

    Widgets compute their text sizes from their own size, which rarely
    match the few sizes a bitmap font has.  With override fonts
    installed, text up to the largest of their sizes is drawn at the
    largest size not above it, or at the smallest when it is smaller
    than all of them.  Text above the largest size keeps its size and
    uses the fallback family.  Without installed override fonts the
    size is unchanged.

    Args:
        size: Font size in pixels the widget asked for.

    Returns:
        The size to measure and draw the text at.
    """
    return _snap(size, _NATIVE_SIZES.get())


def available_sizes(candidates: Sequence[int]) -> list[int]:
    """Return the candidate sizes the chosen fonts have natively.

    Bitmap fonts only exist in a few pixel sizes and the sizes differ
    between fonts, so a widget lists the sizes it could use for a piece
    of text and lets the user's fonts decide which of them it really
    does.  A size counts as available when an installed font is
    chosen for exactly that size.

    Args:
        candidates: Sizes in pixels the widget may use, in any order.

    Returns:
        The candidates with an installed override font, in the given
        order; all candidates when none has one, because Roboto and
        other scalable fonts can draw every size.
    """
    settings = _FONT_SETTINGS.get() or FontSettings()
    native = [
        size
        for size in candidates
        if size in settings.size_overrides
        and resolve_font_path(size, False, False) is not None
    ]
    return native or list(candidates)


@functools.lru_cache(maxsize=256)
def _baseline_shift(path: str, size: int, mode: str) -> int:
    """Return the whole-pixel distance from a text's ``y`` to its baseline.

    ``dominant-baseline="middle"`` and ``"central"`` put the text's
    centre at ``y``, which puts the baseline at a fractional pixel
    whenever half the x-height (``middle``) or half the difference of
    ascent and descent (``central``) is not a whole number.  A bitmap
    font drawn on a half pixel is blurred, so the baseline is placed
    on a whole pixel instead.

    Args:
        path: Font file.
        size: Font size in pixels.
        mode: ``"middle"`` or ``"central"``.

    Returns:
        The shift in pixels, rounded half up.
    """
    font = ImageFont.truetype(path, size)
    if mode == "middle":
        centre = -font.getbbox("x", anchor="ls")[1] / 2
    else:
        ascent, descent = font.getmetrics()
        centre = (ascent - descent) / 2
    return math.floor(centre + 0.5)


def _snap_baseline(tag: str, family: str, size: float, font_dir: str) -> str:
    """Put the baseline of text in an override font on a whole pixel.

    Args:
        tag: Opening ``<text>`` tag whose family was just replaced.
        family: The chosen family.
        size: Font size of the text in pixels.
        font_dir: Directory the family is installed in.

    Returns:
        The tag without ``dominant-baseline`` and with ``y`` moved to
        the baseline, or the tag unchanged when it has no centred
        baseline, no numeric ``y`` or the font is not readable.
    """
    baseline = _BASELINE_ATTR.search(tag)
    y_attr = _Y_ATTR.search(tag)
    if not baseline or not y_attr or not font_dir:
        return tag
    weights = _font_index(font_dir).get(family.lower())
    if not weights:
        return tag
    path = weights.get(400) or next(iter(weights.values()))
    try:
        shift = _baseline_shift(path, round(size), baseline.group(1))
    except OSError:
        return tag
    tag = _BASELINE_ATTR.sub("", tag, count=1)
    return _Y_ATTR.sub(
        lambda m: f"{m.group(1)}{float(m.group(2)) + shift:g}{m.group(3)}",
        tag,
        count=1,
    )


def _css_weight(value: str, default: int) -> int:
    """Return the numeric value of a ``font-weight`` attribute."""
    if value in ("bold", "bolder"):
        return 700
    if value == "normal":
        return 400
    return int(value) if value.isdigit() else default


@functools.lru_cache(maxsize=256)
def _open_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    """Open a font file at a pixel size (cached)."""
    return ImageFont.truetype(path, size)


def _text_width(
    text: str, family: str, size: float, weight: int, font_dir: str
) -> float | None:
    """Return the width in pixels of ``text`` drawn in an installed family.

    Uses the file the renderer picks for the weight, and measures at the
    drawn size, where a bitmap font has whole-pixel advances.

    Args:
        text: Plain (unescaped) text.
        family: The chosen family.
        size: Font size in pixels.
        weight: CSS font weight of the text.
        font_dir: Directory the family is installed in.

    Returns:
        The width, or ``None`` when the family cannot be read.
    """
    weights = _font_index(font_dir).get(family.lower()) if font_dir else None
    if not weights:
        return None
    wanted = 700 if weight >= 600 else 500 if weight == 500 else 400
    path = (
        weights.get(wanted) or weights.get(400) or next(iter(weights.values()))
    )
    try:
        return _open_font(path, round(size)).getlength(text)
    except (OSError, ValueError):
        return None


def _snap_anchor(tag: str, width: float) -> str:
    """Start centred or right-aligned text on a whole pixel.

    Text with ``text-anchor`` ``middle`` or ``end`` begins at ``x`` minus
    half its width or its width, which is a half pixel whenever the
    width is odd.  Bitmap fonts blur on half pixels, and proportional
    ones have a different width for every text, so only some of them
    would.  The anchor is replaced by the whole-pixel start position.

    Args:
        tag: Opening ``<text>`` tag.
        width: Width of the text in pixels.

    Returns:
        The tag with a start anchor and a rounded ``x``, or unchanged
        when it has no such anchor or no numeric ``x``.
    """
    anchor = _ANCHOR_ATTR.search(tag)
    x_attr = _X_ATTR.search(tag)
    if not anchor or not x_attr:
        return tag
    shift = width / 2 if anchor.group(1) == "middle" else width
    start = math.floor(float(x_attr.group(2)) - shift + 0.5)
    tag = _ANCHOR_ATTR.sub("", tag, count=1)
    return _X_ATTR.sub(
        lambda m: f"{m.group(1)}{start}{m.group(3)}", tag, count=1
    )


def apply_font_families(svg: str, settings: FontSettings) -> str:
    """Replace the Roboto font family of text by the chosen families.

    The chosen family is put first and Roboto stays last, so the text
    is drawn in Roboto when the font is not installed.  Characters an
    installed font has no glyph for are replaced by a plain look-alike
    or removed, see :func:`_fit_to_font`.  The font size is snapped to a
    size with an installed override font, see :func:`snap_size`.  Text
    with a centred baseline gets a whole-pixel baseline, and centred or
    right-aligned text a whole-pixel start, so bitmap fonts stay sharp,
    see :func:`_snap_baseline` and :func:`_snap_anchor`.

    Args:
        svg: Rendered widget SVG whose text uses ``font-family="Roboto"``.
        settings: Active font choices.

    Returns:
        The SVG with ``font-family`` rewritten on every text element
        whose ``font-size`` has an override or fallback family.
    """
    if not settings.family and not settings.size_overrides:
        return svg
    native = _native_sizes(settings)

    def rewrite_tag(tag: str, parent: Spec) -> tuple[str, Spec]:
        """Return the tag with its family replaced and its font spec.

        The spec is ``(size, family, weight)`` of the text inside the
        tag, where family is ``""`` when it is Roboto or unchanged.
        """
        size_match = _SIZE_ATTR.search(tag)
        size = float(size_match.group(1)) if size_match else parent[0]
        weight_match = _WEIGHT_ATTR.search(tag)
        weight = (
            _css_weight(weight_match.group(1), parent[2])
            if weight_match
            else parent[2]
        )
        if _FAMILY_ATTR in tag and size is not None:
            snapped = _snap(round(size), native)
            if snapped != round(size):
                tag = _SIZE_ATTR.sub(f'font-size="{snapped}"', tag, count=1)
                size = snapped
            family = settings.family_for(round(size))
            if family:
                tag = tag.replace(
                    _FAMILY_ATTR, f"font-family=\"'{family}', Roboto\""
                )
                tag = _snap_baseline(tag, family, size, settings.font_dir)
                return tag, (size, family, weight)
            return tag, (size, "", weight)
        if _ANY_FAMILY_ATTR.search(tag):
            return tag, (size, "", weight)
        return tag, (size, parent[1], weight)

    def rewrite_element(match: re.Match[str]) -> str:
        """Return one ``<text>`` element with fonts and fallbacks applied."""
        stack: list[Spec] = [(None, "", 400)]
        out: list[str] = []
        open_at: int | None = None
        width = 0.0
        # The start can only be snapped when every text is measurable.
        measurable = True
        for part in _MARKUP_PART.split(match.group(0)):
            if part.startswith("</"):
                out.append(part)
                if len(stack) > 1:
                    stack.pop()
            elif part.startswith("<"):
                tag, spec = rewrite_tag(part, stack[-1])
                if open_at is None:
                    open_at = len(out)
                out.append(tag)
                if not part.endswith("/>"):
                    stack.append(spec)
            elif part and stack[-1][1]:
                size, family, weight = stack[-1]
                fitted = _fit_to_font(
                    part, _family_codepoints(family, settings.font_dir)
                )
                out.append(fitted)
                run = _text_width(
                    html.unescape(fitted),
                    family,
                    size or 0,
                    weight,
                    settings.font_dir,
                )
                if run is None:
                    measurable = False
                else:
                    width += run
            else:
                out.append(part)
                if part.strip():
                    measurable = False
        if open_at is not None and measurable and width > 0:
            out[open_at] = _snap_anchor(out[open_at], width)
        return "".join(out)

    return _TEXT_ELEMENT.sub(rewrite_element, svg)
