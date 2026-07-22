import { writable } from 'svelte/store';

export const defaultWeights = {
  cr: 4, en: 3, vu: 2, nt: 1, dd: 2, lc: 0.1,
  sp: 2, gen: 3, fam: 4, cov: 0, samp: 0
};

export type WeightKey = keyof typeof defaultWeights;
export const weights = writable({ ...defaultWeights });

export function resetWeights() {
  weights.set({ ...defaultWeights });
}
