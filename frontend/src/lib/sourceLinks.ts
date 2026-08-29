const IUCN_SEARCH = 'https://www.iucnredlist.org/search';
const IUCN_SPECIES = 'https://www.iucnredlist.org/species';
const GBIF_SPECIES = 'https://www.gbif.org/species';
const GOAT_RECORD = 'https://goat.genomehubs.org/record';

export function gbifSpeciesUrl(gbifTaxonId: string | null | undefined): string | null {
  return gbifTaxonId ? `${GBIF_SPECIES}/${encodeURIComponent(gbifTaxonId)}` : null;
}

export function iucnSpeciesUrl(
  speciesName: string,
  sisId: string | null | undefined,
  assessmentId: string | null | undefined
): string {
  if (sisId && assessmentId) {
    return `${IUCN_SPECIES}/${encodeURIComponent(sisId)}/${encodeURIComponent(assessmentId)}`;
  }
  const query = new URLSearchParams({ query: speciesName, searchType: 'species' });
  return `${IUCN_SEARCH}?${query}`;
}

export function goatTaxonUrl(goatTaxonId: string | null | undefined): string | null {
  if (!goatTaxonId) return null;
  const query = new URLSearchParams({
    recordId: goatTaxonId,
    result: 'taxon',
    taxonomy: 'ncbi'
  });
  return `${GOAT_RECORD}?${query}`;
}
