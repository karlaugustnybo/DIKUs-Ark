// Bun supplies this module at test runtime; the frontend production tsconfig
// intentionally does not include Bun's ambient server-side types.
// @ts-expect-error -- available to `bun test`, excluded from the browser bundle
import { describe, expect, test } from 'bun:test';
import { COARSE_MAP_LAYER_ID, resolveMapPick } from '../src/lib/map/mapPicking';

describe('map picking', () => {
  test('uses the row index when a typed coarse layer has no picked object', () => {
    expect(resolveMapPick({
      index: 5093,
      layer: { id: COARSE_MAP_LAYER_ID },
      object: undefined
    }, 40_000)).toEqual({ kind: 'coarse', index: 5093 });
  });

  test('rejects invalid coarse row indexes', () => {
    expect(resolveMapPick({ index: -1, layer: { id: COARSE_MAP_LAYER_ID } }, 10)).toBeNull();
    expect(resolveMapPick({ index: 10, layer: { id: COARSE_MAP_LAYER_ID } }, 10)).toBeNull();
  });

  test('keeps ordinary compact and feature objects for resolution-7 layers', () => {
    const compactCell = ['871f1d489ffffff', 42];
    const feature = { properties: { h3_index: '871f1d489ffffff' } };

    expect(resolveMapPick({ layer: { id: 'priorities-res7-dynamic' }, object: compactCell }, 0))
      .toEqual({ kind: 'object', object: compactCell });
    expect(resolveMapPick({ layer: { id: 'priorities-res7' }, object: feature }, 0))
      .toEqual({ kind: 'object', object: feature });
  });

  test('ignores empty picks from non-coarse layers', () => {
    expect(resolveMapPick({ layer: { id: 'priorities-res7' }, object: undefined }, 0)).toBeNull();
  });
});
