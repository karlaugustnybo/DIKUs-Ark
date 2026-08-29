export type SpeciesSortKey =
  | 'species_name'
  | 'family'
  | 'redlist_category'
  | 'dna_level'
  | 'priority';

export type SpeciesTableRow = {
  gbif_accepted_id: string;
  iucn_sis_id?: string | null;
  iucn_assessment_id?: string | null;
  gbif_taxon_id?: string | null;
  goat_taxon_id?: string | null;
  species_name: string;
  family: string;
  redlist_category: string;
  dnaLabel: string;
  dnaStatus: 'data-deficient' | 'family' | 'genus' | 'species' | 'sampled';
  priority: number;
};

export function dnaStatus(value: string): SpeciesTableRow['dnaStatus'] {
  if (value.startsWith('GoaT Data Deficient')) return 'data-deficient';
  if (value.startsWith('Missing Family')) return 'family';
  if (value.startsWith('Missing Genus')) return 'genus';
  if (value.startsWith('Missing Species')) return 'species';
  return 'sampled';
}
