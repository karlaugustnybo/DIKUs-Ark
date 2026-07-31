# Global IUCN–GoaT identifier crosswalk

## Result

The global build reviewed all **171,604** species in the supplied IUCN export
against **2,004,790** species records in the supplied GoaT export.

| Outcome | Species | Share |
|---|---:|---:|
| Accepted IUCN SIS → GoaT/NCBI link | 126,285 | 73.59% |
| No defensible GoaT/NCBI candidate | 43,342 | 25.26% |
| Candidate exists but remains unresolved | 1,977 | 1.15% |

The 73.59% figure is **coverage, not estimated accuracy**. No representative
manually curated gold-standard crosswalk was supplied, so a numerical precision
claim would be unsupported. The build instead maximizes precision using
conservative acceptance rules and retains unresolved evidence separately.

Of the accepted rows, **125,883** are marked safe for automatic species-level
trait transfer. The remaining 402 accepted rows represent 196 NCBI TaxIDs that
each correspond to multiple IUCN species concepts. They are useful identifier
cross-references but must not be treated as equivalent taxonomic concepts.

## Reviewer

Ambiguous and near-name decisions are documented as:

> **AI-review (GPT-5.6 Sol)**

The reviewer label identifies the policy author and expert-review stage. It
does not imply that a model-generated guess was accepted without source
evidence. Every accepted AI-review row also requires authoritative lineage and
independent GBIF/Catalogue of Life concept evidence.

## Sources

- IUCN taxonomy and assessments:
  `/Volumes/KA T7/Karl August/Ark-IV_data/IUCN_Red_List/`
- GoaT global species export:
  `/Volumes/KA T7/Karl August/Ark-IV_data/TOL/tol_species.tsv`
- NCBI Taxonomy `names.dmp`, `nodes.dmp`, `merged.dmp`, and `delnodes.dmp`
  downloaded on 2026-07-23
- GoaT API taxonomy index `taxon--ncbi--goat--2026.07.22`
- GBIF v2 species matcher using the current Catalogue of Life extended index

Exact source paths and SHA-256 hashes are stored in
`data/exports/iucn_goat_global/match_summary.json`.

## Matching tiers

### Tier A — deterministic

Accepted only when there is a unique candidate and at least two compatible
lineage levels:

- exact current scientific name in GoaT;
- exact current scientific name in NCBI Taxonomy; or
- an exact authoritative NCBI synonym.

An exact spelling alone is insufficient. The lineage check rejected 12
cross-kingdom or deep-lineage homonyms, including:

- IUCN plant *Solenopsis bicolor* versus an ant GoaT TaxID;
- IUCN plant *Salacia pyriformis* versus a hydrozoan GoaT TaxID; and
- IUCN plant *Burttia prunoides* versus an orthopteran GoaT TaxID.

### Tier B — exact concept with expert resolution

Used when an exact name or synonym has more than one candidate, or when an
accepted GBIF concept provides the bridge. Acceptance requires:

- an exact GBIF species concept;
- the matching IUCN SIS ID in GBIF's IUCN source status where applicable; and
- compatible GoaT/NCBI lineage.

### Tier C — AI-reviewed near name

Near-name candidates are generated only within tightly blocked genus/epithet
groups and at edit distance no greater than two. They are accepted only when:

- GBIF resolves the IUCN name and candidate to the same exact accepted species
  concept;
- the IUCN source ID is confirmed or the exact GBIF species match is otherwise
  explicit;
- at least two lineage levels agree; and
- there is no more than one lineage conflict.

The initial near-name rules proposed 196 matches. Independent GBIF concept
validation rejected 136 of those superficially plausible candidates. After
supplemental GoaT lineage was added, the final accepted Tier C set contains 79
rows.

### Tier D — unresolved

The accepted TaxID remains null. The best candidate and its evidence are kept
in the `review_candidate_*` fields and in
`unresolved_candidates.parquet`.

No fuzzy or higher-rank result is forced into the accepted crosswalk.

## Why coverage cannot approach 100%

For **38,437** of the no-candidate rows, GBIF independently confirmed the exact
IUCN SIS species concept, but that accepted species name still had no
defensible record or synonym in the supplied GoaT species export. This is a
GoaT/NCBI coverage limitation, not an unresolved string-matching problem.

Inventing a TaxID for these rows would raise nominal coverage while lowering
accuracy. They remain `NO_GOAT_NCBI_CANDIDATE`.

Coverage differs substantially by kingdom:

| IUCN kingdom | Matched | Total | Coverage |
|---|---:|---:|---:|
| Animalia | 80,107 | 93,607 | 85.58% |
| Plantae | 45,259 | 76,677 | 59.03% |
| Fungi | 916 | 1,302 | 70.35% |
| Chromista | 3 | 18 | 16.67% |

## Output fields to use

- `matched_ncbi_species_taxid`: accepted GoaT/NCBI species TaxID; null when
  unresolved.
- `match_status`: `MATCHED`, `REVIEW_UNRESOLVED`, or
  `NO_GOAT_NCBI_CANDIDATE`.
- `confidence_tier`: A, B, or C for accepted matches.
- `match_method` and `reviewer`: how the accepted decision was made.
- `lineage_source`: `NCBI_TAXDUMP` or `GOAT_API`.
- `taxonomic_concept_relation`: distinguishes current-name, synonym,
  GBIF-corroborated, and IUCN-split/NCBI-lump relationships.
- `ncbi_taxid_iucn_taxon_count`: number of supplied IUCN species mapped to the
  accepted TaxID.
- `safe_for_automatic_species_trait_transfer`: recommended gate for automatic
  species-level GoaT metadata joins.
- `review_candidate_*`: evidence retained for unresolved rows.

Never deduplicate IUCN assessments by NCBI or GBIF ID. Keep threat category and
assessment scope keyed by `iucn_sis_id` and `iucn_assessment_id`.

## Rebuilding

The complete local build is:

```sh
python3 scripts/match_iucn_goat_global.py \
  --iucn-taxonomy "/Volumes/KA T7/Karl August/Ark-IV_data/IUCN_Red_List/taxonomy.csv" \
  --iucn-assessments "/Volumes/KA T7/Karl August/Ark-IV_data/IUCN_Red_List/assessments.csv" \
  --goat-species "/Volumes/KA T7/Karl August/Ark-IV_data/TOL/tol_species.tsv" \
  --ncbi-taxdump-dir data/reference/ncbi_taxdump \
  --gbif-validation-json data/exports/iucn_goat_global/gbif_validation.json \
  --gbif-bridge-json data/exports/iucn_goat_global/gbif_bridge_results.json \
  --goat-lineages-json data/exports/iucn_goat_global/goat_lineages_complete.json \
  --output-dir data/exports/iucn_goat_global
```

The API evidence files are generated by:

```sh
node scripts/gbif_validate_iucn_goat_candidates.mjs INPUT.json OUTPUT.json
node scripts/gbif_bridge_iucn_names.mjs INPUT.json OUTPUT.json
node scripts/fetch_goat_lineages.mjs TAXIDS.json OUTPUT.json
```

All API responses are cached as build inputs so the final crosswalk can be
reproduced without silently changing decisions during a later taxonomy update.
