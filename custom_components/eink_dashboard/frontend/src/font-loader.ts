// Copyright 2026 Andreas Schneider
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Loads the fonts chosen under "Override fonts for specific sizes" into
// the browser.  The card shows widget SVGs inline, so the browser draws
// their text itself and needs the same font files the server renders with.
// Fonts are added to `document.fonts` because a `@font-face` rule inside
// the card's shadow root would not apply.

import type { FontFileInfo, HomeAssistant } from "./types/ha.js";

/** The part of a `FontFace` the loader uses. */
export interface FontFaceLike {
  /** Decode the font data; rejects when it is not a usable font. */
  load(): Promise<unknown>;
}

/** Browser operations the loader needs, injectable for tests. */
export interface FontLoaderDeps {
  /**
   * Download one font file of the display from the integration.
   *
   * @param file - File name from `FontFileInfo.file`.
   * @returns The raw font file.
   */
  fetchFont(file: string): Promise<ArrayBuffer>;
  /**
   * Create a font face for the given data.
   *
   * @param family - CSS family name the SVG refers to.
   * @param data - Raw font file.
   * @param weight - CSS font weight of this file (400, 500 or 700).
   * @returns A face that is not yet registered.
   */
  createFace(family: string, data: ArrayBuffer, weight: number): FontFaceLike;
  /**
   * Register a loaded face so all text in the page can use it.
   *
   * @param face - Face returned by `createFace` after `load()`.
   */
  addFace(face: FontFaceLike): void;
}

/**
 * Identify one font file of one display for the loaded-fonts set.
 *
 * @param entryId - Config entry ID of the dashboard.
 * @param font - Font file description.
 * @returns A key that is unique per entry, family, weight and file.
 */
export function fontKey(entryId: string, font: FontFileInfo): string {
  return `${entryId}|${font.family}|${font.weight}|${font.file}`;
}

/**
 * Load the font files that are not loaded yet.
 *
 * A file that cannot be fetched or decoded is logged and skipped, so
 * the preview keeps working and falls back to the next family in the
 * SVG's `font-family` list.  It is tried again on the next call.
 *
 * @param entryId - Config entry ID of the dashboard.
 * @param fonts - Files listed by the layout response, if any.
 * @param loaded - Keys of the files already loaded; updated in place.
 * @param deps - Browser operations.
 */
export async function loadFonts(
  entryId: string,
  fonts: FontFileInfo[] | undefined,
  loaded: Set<string>,
  deps: FontLoaderDeps,
): Promise<void> {
  const pending = (fonts ?? []).filter((f) => !loaded.has(fontKey(entryId, f)));
  await Promise.all(
    pending.map(async (font) => {
      try {
        const data = await deps.fetchFont(font.file);
        const face = deps.createFace(font.family, data, font.weight);
        await face.load();
        deps.addFace(face);
        loaded.add(fontKey(entryId, font));
      } catch (err) {
        console.warn(
          `eink-dashboard: could not load font ${font.file}: ${String(err)}`,
        );
      }
    }),
  );
}

/**
 * Build the loader operations for the real browser.
 *
 * @param hass - Home Assistant object, used to download with the
 *   logged-in user's credentials.
 * @param entryId - Config entry ID of the dashboard.
 * @returns The operations, or `null` when the browser has no FontFace API.
 */
export function browserFontDeps(
  hass: HomeAssistant,
  entryId: string,
): FontLoaderDeps | null {
  if (typeof FontFace === "undefined" || typeof document === "undefined") {
    return null;
  }
  return {
    async fetchFont(file) {
      const resp = await hass.fetchWithAuth(
        `/api/eink_dashboard/${entryId}/fonts/${encodeURIComponent(file)}`,
      );
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      return resp.arrayBuffer();
    },
    createFace: (family, data, weight) =>
      new FontFace(family, data, { weight: String(weight) }),
    addFace: (face) => document.fonts.add(face as FontFace),
  };
}
