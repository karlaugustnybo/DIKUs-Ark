// Bun supplies this module at test runtime; the frontend production tsconfig
// intentionally does not include Bun's ambient server-side types.
// @ts-expect-error -- available to `bun test`, excluded from the browser bundle
import { describe, expect, test } from 'bun:test';
import {
  hasActiveSpatialFilters, scoreDomainForItems, selectScoreDomain
} from '../src/lib/map/mapDomains';

const base = {
  system: '',
  normalizeBySpecies: false,
  filters: {},
  scoreDomains: { all: { min: 0, max: 100 } },
  normalizedScoreDomains: { all: { min: 0, max: 5 } },
  boundaryScoreDomains: {
    municipality: { all: {
      'DNK-A': { min: 10, max: 40 },
      'DNK-B': { min: 20, max: 60 }
    } },
    admin0: { all: { DNK: { min: 5, max: 50 } } }
  },
  normalizedBoundaryScoreDomains: {
    municipality: { all: { 'DNK-A': { min: 0.5, max: 2.5 } } }
  }
};

describe('map score-domain selection', () => {
  test('uses the global domain without spatial filters', () => {
    expect(selectScoreDomain(base)).toEqual({ min: 0, max: 100 });
  });

  test('normalizes a municipality union against its local range', () => {
    expect(selectScoreDomain({
      ...base,
      filters: { municipality: ['DNK-A', 'DNK-B'] }
    })).toEqual({ min: 10, max: 60 });
  });

  test('uses the tightest ceiling for intersected frameworks', () => {
    expect(selectScoreDomain({
      ...base,
      filters: { municipality: ['DNK-A'], admin0: ['DNK'] }
    })).toEqual({ min: 5, max: 40 });
  });

  test('uses species-normalized boundary domains', () => {
    expect(selectScoreDomain({
      ...base,
      normalizeBySpecies: true,
      filters: { municipality: ['DNK-A'] }
    })).toEqual({ min: 0.5, max: 2.5 });
  });

  test('rejects stale metadata instead of silently using a global domain', () => {
    expect(() => selectScoreDomain({
      ...base,
      filters: { municipality: ['MISSING'] }
    })).toThrow('municipality: MISSING');
  });

  test('derives the complete range from the scores actually being rendered', () => {
    const cells = [
      { score: 25.8, included: true },
      { score: 30.9, included: true },
      { score: 47.9, included: true },
      { score: 118.7, included: false }
    ];
    expect(scoreDomainForItems(
      cells,
      (cell) => cell.score,
      (cell) => cell.included
    )).toEqual({ min: 25.8, max: 47.9 });
  });

  test('keeps a constant rendered range valid', () => {
    expect(scoreDomainForItems([30.9, 30.9], (score) => score))
      .toEqual({ min: 30.9, max: 30.9 });
  });

  test('only enables local rendered domains for active spatial filters', () => {
    expect(hasActiveSpatialFilters({})).toBe(false);
    expect(hasActiveSpatialFilters({ municipality: [] })).toBe(false);
    expect(hasActiveSpatialFilters({ municipality: ['DNK-GENTOFTE'] })).toBe(true);
  });
});
