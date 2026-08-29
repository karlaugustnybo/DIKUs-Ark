import type { ScoreDomain } from '$lib/map/mapBundle';

type BoundaryDomains = Record<string, Record<string, Record<string, ScoreDomain>>>;

export type SelectScoreDomainOptions = {
  system: string;
  normalizeBySpecies: boolean;
  filters: Record<string, string[]>;
  scoreDomains: Record<string, ScoreDomain>;
  normalizedScoreDomains: Record<string, ScoreDomain>;
  boundaryScoreDomains: BoundaryDomains;
  normalizedBoundaryScoreDomains: BoundaryDomains;
};

function validDomain(domain: ScoreDomain | undefined): domain is ScoreDomain {
  return Boolean(
    domain && Number.isFinite(domain.min) && Number.isFinite(domain.max) && domain.max >= domain.min
  );
}

export function scoreDomainForItems<T>(
  items: Iterable<T>,
  score: (item: T) => number,
  included: (item: T) => boolean = () => true
): ScoreDomain | undefined {
  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  for (const item of items) {
    if (!included(item)) continue;
    const value = score(item);
    if (!Number.isFinite(value)) continue;
    min = Math.min(min, value);
    max = Math.max(max, value);
  }
  return Number.isFinite(min) && Number.isFinite(max) ? { min, max } : undefined;
}

export function hasActiveSpatialFilters(filters: Record<string, string[]>): boolean {
  return Object.values(filters).some((codes) => codes.length > 0);
}

export function selectScoreDomain(options: SelectScoreDomainOptions): ScoreDomain {
  const key = options.system.toLowerCase() || 'all';
  const globalDomains = options.normalizeBySpecies
    ? options.normalizedScoreDomains
    : options.scoreDomains;
  const boundaryDomains = options.normalizeBySpecies
    ? options.normalizedBoundaryScoreDomains
    : options.boundaryScoreDomains;
  const activeFilters = Object.entries(options.filters).filter(([, codes]) => codes.length > 0);

  let domain = globalDomains[key];
  if (activeFilters.length) {
    const frameworkDomains = activeFilters.map(([framework, codes]) => {
      const domains = codes.map((code) => boundaryDomains[framework]?.[key]?.[code]);
      if (domains.some((candidate) => !validDomain(candidate))) {
        const missingCodes = codes.filter((_, index) => !validDomain(domains[index]));
        throw new Error(
          `Map metadata is missing ${key} score domains for ${framework}: ${missingCodes.join(', ')}`
        );
      }
      return domains as ScoreDomain[];
    });
    domain = {
      min: Math.min(...frameworkDomains.flat().map((candidate) => candidate.min)),
      // Codes within a framework form a union. Filters across frameworks form
      // an intersection, so the smallest union maximum is the safest ceiling.
      max: Math.min(
        ...frameworkDomains.map((domains) => Math.max(...domains.map((candidate) => candidate.max)))
      )
    };
  }

  if (!validDomain(domain)) {
    throw new Error(`Map metadata contains an invalid score domain for ${key}`);
  }
  return domain;
}
