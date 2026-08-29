export const COARSE_MAP_LAYER_ID = 'priorities-res3-global';

export type MapPickInfo = {
  index?: number;
  layer?: { id?: string } | null;
  object?: unknown;
};

export type ResolvedMapPick =
  | { kind: 'coarse'; index: number }
  | { kind: 'object'; object: unknown };

/**
 * Resolve a deck.gl picking result without assuming that it contains an object.
 * Typed-array layers render and pick normally, but deck.gl reports only their
 * row index; regular array and MVT layers continue to expose the picked object.
 */
export function resolveMapPick(info: MapPickInfo, coarseCellCount: number): ResolvedMapPick | null {
  if (info.layer?.id === COARSE_MAP_LAYER_ID) {
    const index = info.index;
    return Number.isInteger(index) && index !== undefined && index >= 0 && index < coarseCellCount
      ? { kind: 'coarse', index }
      : null;
  }
  return info.object === null || info.object === undefined
    ? null
    : { kind: 'object', object: info.object };
}
