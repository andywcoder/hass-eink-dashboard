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

import { describe, it, expect, vi } from "vitest";
import {
  fontKey,
  loadFonts,
  type FontFaceLike,
  type FontLoaderDeps,
} from "../src/font-loader.js";
import type { FontFileInfo } from "../src/types/ha.js";

const TAMZEN: FontFileInfo = {
  family: "Tamzen 7x14",
  weight: 400,
  file: "Tamzen7x14.ttf",
};

/** Build loader deps whose operations are recorded by spies. */
function makeDeps(overrides: Partial<FontLoaderDeps> = {}) {
  const added: FontFaceLike[] = [];
  const deps: FontLoaderDeps = {
    fetchFont: vi.fn(async () => new ArrayBuffer(4)),
    createFace: vi.fn(() => ({ load: async () => undefined })),
    addFace: (face) => void added.push(face),
    ...overrides,
  };
  return { deps, added };
}

describe("loadFonts", () => {
  it("does nothing when the layout lists no fonts", async () => {
    // Without overrides the layout has no fonts and nothing is fetched.
    const { deps } = makeDeps();
    await loadFonts("e1", undefined, new Set(), deps);
    expect(deps.fetchFont).not.toHaveBeenCalled();
  });

  it("fetches, decodes and registers each font once", async () => {
    // A listed font is downloaded under its family name and weight,
    // registered, and not downloaded again on the next layout fetch.
    const { deps, added } = makeDeps();
    const loaded = new Set<string>();
    await loadFonts("e1", [TAMZEN], loaded, deps);
    await loadFonts("e1", [TAMZEN], loaded, deps);
    expect(deps.fetchFont).toHaveBeenCalledTimes(1);
    expect(deps.fetchFont).toHaveBeenCalledWith("Tamzen7x14.ttf");
    expect(deps.createFace).toHaveBeenCalledWith(
      "Tamzen 7x14",
      expect.any(ArrayBuffer),
      400,
    );
    expect(added).toHaveLength(1);
    expect(loaded.has(fontKey("e1", TAMZEN))).toBe(true);
  });

  it("keeps going and retries later when a font fails to load", async () => {
    // A failing download must not break the preview or the other
    // fonts, and the failed file is attempted again next time.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const other: FontFileInfo = { ...TAMZEN, family: "Other", file: "o.ttf" };
    const fetchFont = vi.fn(async (file: string) => {
      if (file === TAMZEN.file) throw new Error("HTTP 404");
      return new ArrayBuffer(4);
    });
    const { deps, added } = makeDeps({ fetchFont });
    const loaded = new Set<string>();
    await loadFonts("e1", [TAMZEN, other], loaded, deps);
    expect(added).toHaveLength(1);
    expect(loaded.has(fontKey("e1", TAMZEN))).toBe(false);
    expect(warn).toHaveBeenCalledOnce();
    warn.mockRestore();
  });

  it("does not register a face that fails to decode", async () => {
    // A file that is not a usable font is skipped, not registered.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { deps, added } = makeDeps({
      createFace: () => ({
        load: async () => {
          throw new Error("bad font");
        },
      }),
    });
    await loadFonts("e1", [TAMZEN], new Set(), deps);
    expect(added).toHaveLength(0);
    warn.mockRestore();
  });
});
