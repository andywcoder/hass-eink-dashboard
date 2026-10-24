# Fonts and non-Latin scripts

The dashboard ships a single bundled font, Roboto, which only covers
Latin, Cyrillic, and Greek scripts. Text in other scripts — Hebrew,
Arabic, CJK, etc. — renders as empty boxes ("tofu") because Roboto has
no glyphs for those characters.

Two **Display Settings** options (**Configure → Display settings**)
add glyph coverage for those scripts. Both are additive: they add
fonts alongside Roboto, so `font-family` in widget templates never
needs to change — the renderer automatically falls back to whichever
loaded font actually has the glyph.

## Use system fonts

Under **Display settings**, the **Use system fonts** toggle
additionally loads whatever fonts are already installed on the Home
Assistant host, as a further fallback. It is convenient when the host
already has broad script coverage installed (for example, a
distro-packaged Noto font collection) and you don't want to source
and mount a font file yourself.

It is **off by default** because it trades away reproducibility: the
same dashboard can render with different glyph shapes — and slightly
different text widths, since layout is computed from measured text —
depending on what happens to be installed on that particular host.
HA OS and Docker images are usually minimal and often have no broad
script coverage installed, so a **Custom font directory** is more
likely to actually fix missing glyphs than this toggle.

## Custom font directory

Under **Display settings → Advanced**, set **Custom font directory**
to a path containing extra `.ttf`/`.otf` font files (for example,
[Noto Sans Hebrew](https://fonts.google.com/noto/specimen/Noto+Sans+Hebrew)
or a CJK font). This is the recommended option: it is reproducible —
the same fonts are loaded every render, regardless of host — and
works the same way on HA OS, Docker, and a plain Python install.

The directory must be reachable by Home Assistant:

- **HA OS / Docker**: mount a host directory into the container, e.g.
  `-v /host/path/to/fonts:/config/fonts` (Docker) or an equivalent
  Supervisor add-on mount, then enter the in-container path
  (`/config/fonts`) as the **Custom font directory** value.
- **Core / venv install**: any path readable by the Home Assistant
  process works directly.

Leave the field empty to use only the bundled Roboto font (the
default).

## Override fonts for specific sizes

Roboto is a scalable font, so small text is anti-aliased and can look
blurry on e-ink displays with few gray levels. Pixel-aligned bitmap
fonts such as [Tamzen](https://github.com/sunaku/tamzen-font) stay
crisp, but only at their native pixel size, and each size is a
separate font file. Because of that, the font is chosen per text
size, and every size needs a family name of its own (see
[Family names](#family-names)).

1. Copy the font files into the **Custom font directory** (see
   above) and set that option.
2. Under **Display settings → Advanced**, tick **Override fonts for
   specific sizes** (it is right below the custom font directory) and
   submit. The form is shown again with the Advanced section open and
   a text box for each text size from 8 to 20 px under the checkbox,
   plus a box for larger text at the end.
3. Enter the font family name in the box of every size your font has,
   for example `Tamzen 7x14` in the box for 14 px, and leave the other
   boxes empty. Submit to save. Emptying a box removes its entry.
4. Optionally fill **Font for larger text**. It is used for text
   larger than the biggest size you entered a font for.

Untick the checkbox to go back to Roboto everywhere. The boxes are
hidden then, and the entries you made are kept for when you tick it
again.

### Family names

The text is matched to a font by its family name, so two sizes of one
bitmap font must not share a name. Many bitmap fonts do: every
Tamzen file, for example, is called just `Tamzen`, so entering
`Tamzen` could not select a size. Rename each size to a name of its
own first, for example `Tamzen 7x14` for the 14 px file, with a font
editor or with [fontTools](https://github.com/fonttools/fonttools):

```python
from fontTools.ttLib import TTFont

font = TTFont("Tamzen7x14r.ttf")
for record in font["name"].names:
    if record.nameID in (1, 4):  # family name and full name
        record.string = "Tamzen 7x14"
font.save("Tamzen7x14.ttf")
```

Check the license of a font before changing and copying it. Then copy
the renamed files into the custom font directory.

Notes:

- The family name is the name stored in the font file, which is not
  always the file name. It is matched ignoring case.
- A font that is not installed in the custom font directory is
  skipped and the text stays in Roboto, so a typo never breaks a
  dashboard.
- Characters the chosen font has no glyph for are not drawn in Roboto
  and not as an empty box, because the renderer would otherwise
  switch the whole text to another font as soon as one character is
  missing. Instead they are replaced or left out:
  - the `…` that marks cut text becomes a period;
  - curly quotes become `'` and `"`, dashes (`–`, `—`, `−`) become
    `-` (so does the middle dot `·`), and `•` becomes `*`;
  - unusual spaces become a normal space;
  - everything else, such as `€`, `™` or letters of another script,
    is left out.

  Accents written as a base letter plus a combining mark are first
  combined into the single accented letter, which most fonts do have.
  If every character of your calendar entries must show, choose an
  override font that has all the glyphs you use.
- Text is only drawn at sizes that have a font. Widgets compute their
  text sizes from their own size, which rarely matches the few sizes a
  bitmap font has, so every text up to the biggest size you entered a
  font for is drawn at the nearest of your sizes below it (or at the
  smallest, when it is smaller than all of them), in the font of that
  size. Larger text keeps its size and uses **Font for larger text**,
  or Roboto when that is empty; a scaled bitmap font is still readable
  there. So fill the box of every size your font has. Without any
  override font installed, nothing changes.
- The week calendar goes one step further, because its event text
  has to fit small boxes: it picks its sizes from groups, and your
  fonts decide which exist (a size exists when you entered a font for
  it; with no override fonts all sizes exist):
  - the **start time** uses the smallest size of 9, 10, 11 and 12 px;
  - an **event title** uses 14 px, or the largest of 14, 13, 12 and
    11 px that exists, and when the title does not fit it steps down
    to the next smaller size that exists. If no smaller size exists,
    it keeps its size and the text is cut.

  So for a font with 10, 11, 12 and 14 px, fill the boxes for those
  sizes: the start time is drawn at 10 px and titles at 14, then 12,
  then 11 px.
- The Lovelace card preview draws the text in the browser, so the
  card downloads the chosen font files from Home Assistant (only the
  files of families used in the overrides, and only for logged-in
  users) and registers them in the page. After changing a font file,
  reload the browser page to pick it up. If a file cannot be loaded
  the preview falls back to Roboto, while the image sent to the
  display still uses your font.
- Text is measured with the chosen font, so truncation and alignment
  match what is drawn. Bold and medium text uses the family's bold or
  medium file when it has one, otherwise its regular file.

## Which one should I use?

- Know exactly which font you need and want identical rendering
  everywhere → **Custom font directory**.
- Know your host already has the right fonts installed and just want
  the quick option → **Use system fonts**.
- Both can be enabled together; fonts from both sources are loaded
  and resvg picks whichever one covers a given character.
