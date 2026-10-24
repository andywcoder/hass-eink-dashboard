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

"""Tests for the per-size font family overrides."""

from __future__ import annotations

import html
import math
import shutil
from pathlib import Path
from typing import Any

import pytest
from PIL import ImageFont

from custom_components.eink_dashboard import font_overrides
from custom_components.eink_dashboard.font_overrides import (
    FontSettings,
    apply_font_families,
    available_sizes,
    browser_fonts,
    drop_missing_glyphs,
    font_options,
    font_settings_from_config,
    is_valid_family,
    resolve_font_path,
    snap_size,
    use_font_settings,
)
from custom_components.eink_dashboard.render import (
    _FONTS_DIR,
    _load_font,
    render_dashboard,
)
from custom_components.eink_dashboard.svg_render import render_widget_svg
from custom_components.eink_dashboard.widgets.graph.geometry import (
    _truncate_to_width,
)
from tests.helpers import png_to_image

_SVG = (
    '<svg><text font-family="Roboto" font-size="14">A</text>'
    '<text font-family="Roboto" font-size="9.0">B</text>'
    '<text font-family="Roboto" font-size="20" font-weight="500">C</text>'
    '<text font-family="Other" font-size="14">D</text></svg>'
)


@pytest.fixture
def font_dir(tmp_path: Path) -> Path:
    """Return a font directory holding only a copy of Roboto Bold."""
    shutil.copy(_FONTS_DIR / "Roboto-Bold.ttf", tmp_path / "Copy.ttf")
    return tmp_path


class TestFontOptions:
    def test_overrides_ignored_while_checkbox_is_off(self) -> None:
        # Stored overrides have no effect until the user turns on
        # "Override fonts for specific sizes".
        config = font_options(
            {
                "font_dir": "/fonts",
                "font_family": "Foo",
                "font_size_overrides": [{"size": 14, "family": "Bar"}],
            }
        )
        assert config == {"font_dir": "/fonts"}

    def test_overrides_forwarded_with_integer_sizes(self) -> None:
        # With the checkbox on, the stored list becomes a size to
        # family mapping next to the fallback family.
        config = font_options(
            {
                "override_font_sizes": True,
                "font_family": "Foo",
                "font_size_overrides": [{"size": 14, "family": "Bar"}],
            }
        )
        assert config["font_family"] == "Foo"
        assert config["font_size_overrides"] == {14: "Bar"}

    @pytest.mark.parametrize(
        ("name", "valid"),
        [
            ("Tamzen 7x14", True),
            ("Noto Sans", True),
            ("", False),
            ("   ", False),
            ('Foo"><script', False),
            ("It's", False),
            ("A, B", False),
        ],
    )
    def test_is_valid_family(self, name: str, valid: bool) -> None:
        # Names that could break out of an SVG attribute or the font
        # list are rejected.
        assert is_valid_family(name) is valid

    def test_invalid_family_in_config_is_dropped(self) -> None:
        # A hand-edited config with a bad family cannot reach the SVG.
        settings = font_settings_from_config(
            {"font_family": 'a"b', "font_size_overrides": {14: "x'y"}}
        )
        assert settings == FontSettings()


class TestApplyFontFamilies:
    def test_unchanged_without_settings(self) -> None:
        # No fallback and no overrides leave the SVG as it is.
        assert apply_font_families(_SVG, FontSettings()) == _SVG

    def test_overrides_only_matching_size(self) -> None:
        # Text of the overridden size gets the family first and Roboto
        # as the fallback; other sizes and other families are kept.
        svg = apply_font_families(
            _SVG, FontSettings(size_overrides={14: "Tamzen 7x14"})
        )
        assert (
            '<text font-family="\'Tamzen 7x14\', Roboto" font-size="14">A'
            in svg
        )
        assert '<text font-family="Roboto" font-size="9.0">B' in svg
        assert '<text font-family="Other" font-size="14">D' in svg

    def test_fallback_family_applies_to_other_sizes(self) -> None:
        # The fallback family is used for every size without an
        # override, and a fractional size matches its rounded value.
        svg = apply_font_families(
            _SVG, FontSettings(family="Inter", size_overrides={14: "Tamzen"})
        )
        assert '\'Tamzen\', Roboto" font-size="14"' in svg
        assert '\'Inter\', Roboto" font-size="9.0"' in svg
        assert '\'Inter\', Roboto" font-size="20"' in svg


class TestLookalikes:
    def _fit(
        self,
        font_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        text: str,
        covered: range = range(32, 127),
    ) -> str:
        """Apply the overrides to ``text`` for a font covering ``covered``."""
        monkeypatch.setattr(
            font_overrides, "_font_codepoints", lambda _p: frozenset(covered)
        )
        svg = (
            '<svg><text font-family="Roboto" font-size="14">'
            f"{text}</text></svg>"
        )
        settings = FontSettings(
            font_dir=str(font_dir), size_overrides={14: "Roboto"}
        )
        out = apply_font_families(svg, settings)
        return out.split(">", 2)[2].removesuffix("</text></svg>")

    def test_typographic_characters_become_plain_ones(
        self, font_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Curly quotes, dashes and bullets that the font lacks are
        # drawn as their plain look-alikes instead of disappearing.
        text = "Don\u2019t \u201cgo\u201d 10\u201311 \u2014 \u2022 a\u00b7b"
        out = self._fit(font_dir, monkeypatch, text)
        assert html.unescape(out) == 'Don\'t "go" 10-11 - * a-b'

    def test_unavailable_symbols_are_still_dropped(
        self, font_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Characters without a stand-in, such as the euro sign, are
        # removed, and so is a zero-width space.
        out = self._fit(font_dir, monkeypatch, "5\u20ac a\u200bb")
        assert html.unescape(out) == "5 ab"

    def test_present_characters_are_kept(
        self, font_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A font that has the typographic characters keeps them.
        text = "Don\u2019t \u2013 ok"
        out = self._fit(font_dir, monkeypatch, text, covered=range(32, 0x3000))
        assert html.unescape(out) == text

    def test_unusual_whitespace_becomes_a_space(
        self, font_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A thin space the font lacks would switch the whole text to
        # another font; it is replaced by a normal space.
        out = self._fit(font_dir, monkeypatch, "a\u2009b")
        assert html.unescape(out) == "a b"


class TestAvailableSizes:
    def test_all_candidates_without_overrides(self) -> None:
        # Scalable Roboto can draw every size, so nothing is filtered.
        assert available_sizes((9, 10, 11, 12)) == [9, 10, 11, 12]

    def test_only_sizes_with_an_installed_font(self, font_dir: Path) -> None:
        # With overrides, only the candidate sizes that have an installed
        # font of their own are available, in the given order.
        settings = FontSettings(
            font_dir=str(font_dir),
            size_overrides={9: "Roboto", 12: "Roboto", 20: "Roboto"},
        )
        with use_font_settings(settings):
            assert available_sizes((14, 13, 12, 9)) == [12, 9]

    def test_uninstalled_families_do_not_count(self, font_dir: Path) -> None:
        # An override whose font is not installed gives that size no
        # native font, so when nothing is installed all stay available.
        settings = FontSettings(
            font_dir=str(font_dir), size_overrides={9: "Nope", 12: "Nope"}
        )
        with use_font_settings(settings):
            assert available_sizes((9, 10, 11, 12)) == [9, 10, 11, 12]


class TestSnapSize:
    def _settings(self, font_dir: Path, *sizes: int, **kwargs: Any) -> Any:
        """Return settings with an installed override font per size."""
        return FontSettings(
            font_dir=str(font_dir),
            size_overrides=dict.fromkeys(sizes, "Roboto"),
            **kwargs,
        )

    def test_unchanged_without_installed_override_fonts(self) -> None:
        # Without override fonts, or when none of them is installed,
        # text keeps the size the widget asked for.
        assert snap_size(13) == 13
        with use_font_settings(FontSettings(size_overrides={12: "Nope"})):
            assert snap_size(13) == 13

    @pytest.mark.parametrize(
        ("asked", "drawn"),
        [
            (8, 12),  # smaller than every size: the smallest
            (11, 12),
            (12, 12),
            (13, 12),  # between two sizes: the one below
            (15, 14),
            (16, 16),
            (17, 17),  # larger than every size: unchanged
            (30, 30),
        ],
    )
    def test_text_snaps_to_the_installed_sizes(
        self, font_dir: Path, asked: int, drawn: int
    ) -> None:
        # Text up to the largest override size is drawn at the nearest
        # size below it (or the smallest size); larger text is left alone.
        with use_font_settings(self._settings(font_dir, 12, 14, 16)):
            assert snap_size(asked) == drawn

    def test_measuring_uses_the_snapped_size(self, font_dir: Path) -> None:
        # Text is measured at the size it is drawn at.
        with use_font_settings(self._settings(font_dir, 12, 14)):
            assert _load_font(13).size == 12
            assert _load_font(30).size == 30

    def test_svg_font_size_is_snapped(self, font_dir: Path) -> None:
        # The rendered SVG gets the snapped size and the override
        # family; text larger than every override size keeps its size
        # and stays in Roboto when there is no fallback family.
        svg = (
            '<svg><text font-family="Roboto" font-size="13">a</text>'
            '<text font-family="Roboto" font-size="30">b</text></svg>'
        )
        out = apply_font_families(svg, self._settings(font_dir, 12, 14))

        assert 'font-family="\'Roboto\', Roboto" font-size="12">a' in out
        assert '<text font-family="Roboto" font-size="30">b' in out

    def test_larger_text_uses_the_fallback_family(
        self, font_dir: Path
    ) -> None:
        # Text above the largest override size keeps its size and is
        # drawn in the fallback family, where scaling is acceptable.
        svg = '<svg><text font-family="Roboto" font-size="30">b</text></svg>'
        settings = self._settings(font_dir, 12, 14, family="Fallback")
        out = apply_font_families(svg, settings)

        assert 'font-family="\'Fallback\', Roboto" font-size="30">b' in out


class TestBaselineSnap:
    def _apply(self, font_dir: Path, tag: str) -> str:
        """Apply a 14 px override to a text element built from ``tag``."""
        svg = f"<svg><text {tag}>x</text></svg>"
        settings = FontSettings(
            font_dir=str(font_dir), size_overrides={14: "Roboto"}
        )
        return apply_font_families(svg, settings)

    @pytest.mark.parametrize("mode", ["middle", "central"])
    def test_centred_baseline_becomes_a_whole_pixel(
        self, font_dir: Path, mode: str
    ) -> None:
        # A centred baseline can land on half a pixel, which blurs
        # bitmap fonts; the baseline attribute is replaced by a whole
        # pixel offset of y, so the text keeps its place.
        out = self._apply(
            font_dir,
            'font-family="Roboto" font-size="14" x="4" y="30" '
            f'dominant-baseline="{mode}"',
        )

        assert "dominant-baseline" not in out
        y = float(out.split(' y="')[1].split('"')[0])
        assert y == int(y)
        assert 30 < y < 40

    def test_shift_matches_the_font_metrics(self, font_dir: Path) -> None:
        # "middle" moves the baseline down by half the x-height of the
        # chosen font, rounded half up.
        font = ImageFont.truetype(str(font_dir / "Copy.ttf"), 14)
        expected = math.floor(-font.getbbox("x", anchor="ls")[1] / 2 + 0.5)
        out = self._apply(
            font_dir,
            'font-family="Roboto" font-size="14" y="30" '
            'dominant-baseline="middle"',
        )

        assert f'y="{30 + expected}"' in out

    def test_other_baselines_and_sizes_are_untouched(
        self, font_dir: Path
    ) -> None:
        # Only centred text in an override font is changed: a hanging
        # baseline and text of a size without override keep theirs.
        hanging = (
            'font-family="Roboto" font-size="14" y="30" '
            'dominant-baseline="hanging"'
        )
        other = (
            'font-family="Roboto" font-size="20" y="30" '
            'dominant-baseline="middle"'
        )

        assert 'dominant-baseline="hanging"' in self._apply(font_dir, hanging)
        assert 'dominant-baseline="middle"' in self._apply(font_dir, other)


class TestMissingGlyphs:
    def _settings(self, font_dir: Path) -> FontSettings:
        """Return settings drawing 14 px text in the Roboto copy."""
        return FontSettings(
            font_dir=str(font_dir), size_overrides={14: "Roboto"}
        )

    def test_missing_characters_are_dropped(self, font_dir: Path) -> None:
        # A character the chosen font lacks is removed instead of
        # switching the whole text to another font.  U+E000 is a
        # private-use character no font has a glyph for.
        svg = (
            '<svg><text font-family="Roboto" font-size="14">abc</text></svg>'
        )
        out = apply_font_families(svg, self._settings(font_dir))
        assert ">abc</text>" in out

    def test_text_in_other_sizes_is_untouched(self, font_dir: Path) -> None:
        # Only text drawn in an overridden font is filtered.
        svg = (
            '<svg><text font-family="Roboto" font-size="20">abc</text></svg>'
        )
        assert apply_font_families(svg, self._settings(font_dir)) == svg

    def test_decomposed_accents_are_composed(self, font_dir: Path) -> None:
        # "u" + combining diaeresis becomes the single character "ü",
        # which bitmap fonts have while they lack the combining mark.
        svg = (
            '<svg><text font-family="Roboto" font-size="14">'
            "Müller &amp; Co</text></svg>"
        )
        out = apply_font_families(svg, self._settings(font_dir))
        assert ">Müller &amp; Co</text>" in out

    def test_spans_use_their_own_font(self, font_dir: Path) -> None:
        # A tspan with its own size is filtered by its own family, and
        # the text after it by the parent's.
        svg = (
            '<svg><text font-family="Roboto" font-size="14">'
            '<tspan font-family="Roboto" font-size="20">1</tspan>'
            "x</text></svg>"
        )
        settings = FontSettings(
            font_dir=str(font_dir), size_overrides={14: "Roboto"}
        )
        out = apply_font_families(svg, settings)
        # The 20 px span is larger than every override size, so it
        # keeps its character; the 14 px text around it loses its own.
        assert "1</tspan>x</text>" in out

    def test_drop_missing_glyphs_for_measured_text(
        self, font_dir: Path
    ) -> None:
        # Text that is measured but drawn without missing characters
        # is filtered with the same coverage; fonts without a file
        # (PIL's default) are left alone.
        font = ImageFont.truetype(str(font_dir / "Copy.ttf"), 14)
        assert drop_missing_glyphs("ab", font) == "ab"
        assert drop_missing_glyphs("ab", object()) == "ab"

    def test_truncation_uses_dot_when_font_lacks_ellipsis(
        self, font_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A font without "…" marks cut text with a period instead, and
        # the width is measured with it so the result still fits.
        monkeypatch.setattr(
            font_overrides,
            "_font_codepoints",
            lambda _p: frozenset(range(127)),
        )
        font = ImageFont.truetype(str(font_dir / "Copy.ttf"), 14)
        out = _truncate_to_width("x" * 60, font, 100)
        assert out.endswith(".")
        assert "…" not in out
        assert font.getlength(out) <= 100
        # A font that has "…" keeps it.
        monkeypatch.undo()
        assert _truncate_to_width("x" * 60, font, 100).endswith("…")


class TestMeasuringFont:
    def test_uses_roboto_without_settings(self) -> None:
        # Without overrides the measuring font is the bundled Roboto.
        assert Path(_load_font(14).path).name == "Roboto-Regular.ttf"

    def test_override_measures_with_installed_font(
        self, font_dir: Path
    ) -> None:
        # An override whose family is installed is measured with that
        # file, but only up to the overridden sizes; larger text stays
        # in Roboto, and the override is gone outside the settings.
        settings = FontSettings(
            font_dir=str(font_dir), size_overrides={14: "Roboto"}
        )
        with use_font_settings(settings):
            assert Path(_load_font(14).path).name == "Copy.ttf"
            assert Path(_load_font(20).path).name == "Roboto-Regular.ttf"
        assert Path(_load_font(14).path).name == "Roboto-Regular.ttf"

    def test_family_name_is_case_insensitive(self, font_dir: Path) -> None:
        # Family names match like resvg does, ignoring case.
        settings = FontSettings(font_dir=str(font_dir), family="rOBOTO")
        with use_font_settings(settings):
            assert resolve_font_path(14, False, False) is not None

    def test_missing_font_falls_back_to_roboto(self, font_dir: Path) -> None:
        # A family that is not in the font directory, or a missing
        # directory, leaves the text measured and drawn in Roboto.
        settings = FontSettings(
            font_dir=str(font_dir), size_overrides={14: "Nope"}
        )
        with use_font_settings(settings):
            assert resolve_font_path(14, False, False) is None
        gone = FontSettings(
            font_dir=str(font_dir / "gone"), size_overrides={14: "Roboto"}
        )
        with use_font_settings(gone):
            assert resolve_font_path(14, False, False) is None

    def test_single_weight_family_serves_every_weight(
        self, font_dir: Path
    ) -> None:
        # A family with one weight (typical for bitmap fonts) is used
        # for regular, medium and bold text alike.
        settings = FontSettings(font_dir=str(font_dir), family="Roboto")
        with use_font_settings(settings):
            for medium, bold in ((False, False), (True, False), (False, True)):
                path = resolve_font_path(14, medium, bold)
                assert path is not None
                assert Path(path).name == "Copy.ttf"


class TestBrowserFonts:
    def test_lists_files_of_chosen_families(self, font_dir: Path) -> None:
        # The browser gets the installed file of each chosen family,
        # named as the SVG refers to it.
        settings = FontSettings(
            font_dir=str(font_dir), size_overrides={14: "Roboto"}
        )
        assert browser_fonts(settings) == [
            {"family": "Roboto", "weight": 700, "file": "Copy.ttf"}
        ]

    def test_includes_fallback_family_once(self, font_dir: Path) -> None:
        # A family used for several sizes and as fallback is listed once.
        settings = FontSettings(
            font_dir=str(font_dir),
            family="Roboto",
            size_overrides={9: "Roboto", 14: "Roboto"},
        )
        assert len(browser_fonts(settings)) == 1

    def test_empty_when_nothing_is_installed(self, font_dir: Path) -> None:
        # Missing families, a missing directory or no directory give an
        # empty list, so the card loads nothing.
        missing = FontSettings(
            font_dir=str(font_dir), size_overrides={14: "Nope"}
        )
        assert browser_fonts(missing) == []
        gone = FontSettings(
            font_dir=str(font_dir / "gone"), size_overrides={14: "Roboto"}
        )
        assert browser_fonts(gone) == []
        assert browser_fonts(FontSettings(size_overrides={14: "Roboto"})) == []


class TestRendering:
    def _config(self, **overrides: Any) -> dict[str, Any]:
        """Return a minimal display config."""
        return {"width": 400, "height": 200, "states": {}, **overrides}

    def test_svg_uses_override_family_for_its_size(self) -> None:
        # A week calendar draws its start time at 9 px and its title
        # at 14 px; overriding 9 px changes only the time text.
        widget = {"type": "week_calendar", "entity": "calendar.t", "w": 400}
        config = self._config(
            font_size_overrides={9: "Tamzen 5x9"},
            states={
                "calendar.t": {
                    "state": "off",
                    "attributes": {
                        "week_events": [
                            {
                                "start": "2026-10-19T09:00:00",
                                "end": "2026-10-19T10:00:00",
                                "summary": "Solo",
                            }
                        ]
                    },
                }
            },
        )
        svg = render_widget_svg(widget, config)
        assert "font-family=\"'Tamzen 5x9', Roboto\"" in svg
        assert 'font-size="9"' in svg

    def test_render_without_installed_font_still_draws(self) -> None:
        # An override for a font nobody installed must not break the
        # render; the text falls back to Roboto.
        widget = {
            "type": "heading",
            "text": "Hello",
            "x": 0,
            "y": 0,
            "w": 400,
            "h": 80,
        }
        plain = png_to_image(render_dashboard([widget], self._config()))
        config = self._config(
            font_family="Not Installed",
            font_size_overrides={20: "Also Missing"},
        )
        fallback = png_to_image(render_dashboard([widget], config))
        assert fallback.tobytes() == plain.tobytes()
