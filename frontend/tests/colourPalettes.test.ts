// Bun supplies this module at test runtime; the frontend production tsconfig
// intentionally does not include Bun's ambient server-side types.
// @ts-expect-error -- available to `bun test`, excluded from the browser bundle
import { describe, expect, test } from 'bun:test';
import {
  colourPaletteLuts,
  colourPalettes,
  sampleColourPalette
} from '../src/lib/map/colourPalettes';

describe('map colour palettes', () => {
  test('provides accessible alternatives to the original palette', () => {
    expect(colourPalettes.map(({ id }) => id)).toEqual(['turbo', 'viridis', 'cividis', 'inferno']);
    expect(colourPalettes.filter(({ accessible }) => accessible).map(({ id }) => id))
      .toEqual(['viridis', 'cividis', 'inferno']);
  });

  test('builds complete RGBA lookup tables for every palette', () => {
    for (const palette of colourPalettes) {
      expect(colourPaletteLuts[palette.id]).toHaveLength(256);
      expect(colourPaletteLuts[palette.id].every((colour) =>
        colour.length === 4 && colour.every((channel) => channel >= 0 && channel <= 255)
      )).toBe(true);
    }
  });

  test('clamps samples and preserves the map overlay opacity', () => {
    expect(sampleColourPalette('viridis', -1)).toEqual([68, 1, 84, 50]);
    expect(sampleColourPalette('viridis', 2)).toEqual([253, 231, 37, 50]);
    expect(sampleColourPalette('cividis', Number.NaN)).toEqual([0, 32, 76, 50]);
  });
});
