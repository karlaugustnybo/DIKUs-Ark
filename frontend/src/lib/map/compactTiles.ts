export const compactMetricNames = [
  'total', 'cr', 'en', 'vu', 'nt', 'dd', 'lc', 'ms', 'mg', 'mf', 'gdd',
  ...(['cr', 'en', 'vu', 'nt', 'dd', 'lc'] as const).flatMap((threat) =>
    (['gdd', 'fam', 'gen', 'sp', 'samp'] as const).map((dna) => `${threat}_${dna}`)
  )
] as const;

export type CompactMetricName = (typeof compactMetricNames)[number];
export type CompactCell = [h3Index: string, ...metrics: number[]];
export type CompactTile = { cells: CompactCell[]; byteLength?: number };

const compactMetricIndexes = new Map<string, number>(
  compactMetricNames.map((name, index) => [name, index + 1])
);

export function compactMetric(cell: CompactCell, name: CompactMetricName): number {
  const index = compactMetricIndexes.get(name);
  return index === undefined ? 0 : Number(cell[index] ?? 0);
}

export function compactMetricIndex(name: CompactMetricName): number {
  return compactMetricIndexes.get(name) ?? 0;
}
