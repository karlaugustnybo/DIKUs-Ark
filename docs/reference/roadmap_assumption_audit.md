# Roadmap assumption audit

**Date: 3 September 2026. Scope: scientific and data-pipeline assumptions.**

The review compares the [roadmap](../roadmap.md), executable code/configuration,
focused tests, primary methodological sources, and registered metadata on the
external source disk. The detailed explanation is [methodology.md](../../methodology.md).
This is not a full global rebuild or an independent biology-domain sign-off.
UI feature completion unrelated to analytical interpretation was not re-audited.

“Verified behavior” below means the implementation was inspected and supported
by relevant checks. “Needs review” means the behavior exists but its scientific
justification or broader validity is not established. Passing a test does not
make a policy scientifically sound.

## Findings and acceptance criteria

| ID | Roadmap assumption | Assessment and evidence | What closes the issue |
| --- | --- | --- | --- |
| A01 | Resolution-7 cells describe observation counts or complete occupancy | **Incorrect interpretation.** A membership may come from a containing point cell or selected range/basin polygon contact. Point multiplicity and evidence type are removed; v3 can simplify large polygons before contact testing. | Publish the mixed-source membership definition; retain point/range/basin regression and calibration evidence. |
| A02 | Resolution-3 cells imply presence throughout their polygon | **Incorrect interpretation.** Distinct parent/species pairs are derived from fine membership. Coarse counts are not sums of children, and H3 hierarchy is not exact geometric nesting. | Treat the technical rule as resolved; keep coarse-location precision explicit. |
| A03 | Matching IUCN selection codes reproduces IUCN richness products | **Only the codes match.** Presence 1/4, origin 1/2/6, seasonality 1/2/3/5 were checked against IUCN's methodology. Grid, taxonomic and marine treatment differ. | Document comparable scope before a numerical comparison; do not claim product equivalence. |
| A04 | Valid, component-count-preserving simplification is sufficiently accurate | **Needs scientific calibration.** Equality of component counts is weaker than component correspondence. Holes may disappear. A tolerance-derived metre budget is not per-species cell-error validation. Earlier 50-row results predate the transform correction. | Same-v3-policy unsimplified reference, current code, per-geometry additions/omissions and local effects; explicitly accepted error limits. |
| A05 | Valid repair with less than 5% area change is biologically safe | **Unsupported as a general rule.** It checks net planar area, so additions/removals can cancel and small populations can disappear. | Report local/symmetric coverage effects, review ambiguous cases, and agree a repair policy separately from simplification. |
| A06 | All relevant records and fields are used losslessly | **Not established; false for unrestricted field preservation.** Pair/list reconciliation preserves the selected union, but seasons, evidence type, point coordinates, basin IDs, full attributes and detailed evidence do not survive aggregation. Point/basin sources are integrated with summarized rather than row-sized exclusion audits. | Release-specific column lineage and row reconciliation for each source; explicit acquired/integrated/served states and retained raw originals. |
| A07 | Accepted taxonomy links establish accurate trait transfer | **Partially guarded.** Unique accepted targets control known split/lump transfer, but do not prove match accuracy. Exact-match acceptance branches have unequal lineage safeguards. | Independent stratified precision audit; reconcile documented versus executable criteria and review conflicting accepted lineages. |
| A08 | `gbif_accepted_id` is a GBIF species identity | **Incorrect for global serving.** It is an IUCN SIS-valued compatibility key; actual GBIF IDs have a separate column. | Migrate the misleading alias with explicit compatibility handling; preserve IUCN identity throughout. |
| A09 | “Has DNA” means completed usable sequence exists | **Incorrect for the current predicate.** Acquired samples, work in progress, criteria text, and qualifying assemblies are combined. Per-value inference provenance is not retained by the downloader. | Separate evidence stages and direct/inferred status; choose which stage should reduce priority for each use case. |
| A10 | Extinct/EW species can count as represented without DNA evidence | **Unsupported biological shortcut, implemented and tested.** It seeds DNA and lineage representation, but this supplied assessment set has no EX/EW rows, so it is dormant here. | Review and quantify a separate extinction priority rule; do not encode extinction as genomic evidence. |
| A11 | Family representation searches every qualifying GoaT relative | **False.** It searches qualifying members of the IUCN crosswalk. Genus/family keys are name strings rather than lineage-qualified IDs. | Expand the evidence universe if intended, validate lineage identities and homonyms, and measure changed species/cell rankings. |
| A12 | `has_ebp_criteria_evidence` establishes compliance with EBP assembly standards | **False as a certification claim.** It checks a safe link plus `6.7`/`6.C` text; current standards contain additional requirements. | Version the target standard, preserve/check required measurements or rename the flag as limited source evidence. |
| A13 | Default priority weights and multiplication are scientifically calibrated | **Not established.** They are configurable heuristics; DD=VU and GoaT-missing=family-missing are deliberate weight choices. | Biological objective, independently reviewed evidence definitions, sensitivity/rank-stability study, and rationale for defaults. |
| A14 | System layers spatially isolate habitat | **Incorrect.** Systems classify species, and can overlap. They are not landcover/elevation masks or separate geographic range pieces. | Label appropriately; add explicit habitat refinement if that is the research question. |
| A15 | Boundary-filtered species are confirmed within the selected boundary | **Not guaranteed.** Species-range and boundary memberships may touch different parts of the same cell; the species table uses coarse cells. | Distinguish cell-based scope from direct range/boundary intersection and validate an exact inventory separately if needed. |
| A16 | Municipality coverage is only Denmark/Germany/Sweden | **Outdated.** Installed ADM2 support and a recorded 180-country, 49,308-area snapshot exist; the three-country data is a fallback. ADM2 is not uniformly a municipality. | Retain source coverage gaps/years and validate disputed/overlapping boundaries; do not call it all-country completeness. |
| A17 | Species-count normalization removes spatial comparison bias | **No.** It gives mean included-species priority. Cell area, source completeness, weights and display domains still differ. | State the denominator and colour domain; conduct sensitivity checks before cross-region comparisons. |
| A18 | Population trends, points, basins, birds and EDGE are already independent score inputs | **Partly false.** Points and basin relationships now expand spatial membership before the ordinary score aggregation, but do not add separate weights or evidence dimensions. Trends remain unused; birds remain source-dependent; optional EDGE contributes a group label only. | Validate point/basin coverage and add provenance-aware display if required; implement or clearly exclude every other proposed signal. |
| A19 | A passed historical benchmark predicts the current full build | **Not established.** Historical geometry counts, corrected code, v2-derived sampling, worker efficiency and unfinished tiles limit transferability. Prior ETA lookup checks source/profile compatibility, not full performance-environment equivalence. | Current v3-native sample, corrected-kernel calibration, complete end-to-end run and hardware/storage-specific measurements. |
| A20 | Update/reuse checks prove the complete scientific/public-release workflow | **Partially implemented.** Stage receipts and atomic generations provide useful engineering guarantees. They do not prove biological suitability, transactional consistency of a live API source, public permission, or a working ordinary-clone synthetic pack. | Validate the full authorized and synthetic workflows and review source permissions independently of stage success. |

## Corrections implemented after the snapshot audit

The audit measurements below describe the saved 2 September build. The code now
requires at least two lineage agreements and at most one conflict in every
automatic taxonomy-acceptance branch; removes EX/EW as a proxy for DNA evidence;
builds candidate lineage representation from all qualifying GoaT species using
kingdom/family/genus-qualified keys validated against unique family- and
genus-rank identities in the registered NCBI taxdump; maps the two unambiguous
legacy Lower Risk labels while reporting
conservation-dependent rows as unscored; renames the EBP
flag as criteria evidence; and emits the nine missing GoaT fields plus a generic
per-field provenance JSON column. A fresh registered build is needed before
reporting new biological counts.

### Fresh validation on 4 September 2026

A complete crosswalk and metadata rebuild reused the finished spatial/list
generation from benchmark `2026-09-02T22-30-48Z-2ae48975`. The stricter lineage
guard reduced automatic matches from 122,392 to 106,159, including 105,872 safe
one-to-one transfers. Of 587 wider-GoaT family candidates covering 11,434
species, current NCBI names/nodes validated 585 families covering 11,427 species
as unique family TaxIDs. Gonostomatidae and Cepheidae were rejected because each
resolved to multiple family TaxIDs. The fresh join contains 35 `DTOL`
resampling markers: 11 with qualifying assemblies and 24 with project/sample
evidence only. The saved machine-readable report is
`data/validation/dna-benchmark-2026-09-03/validation-report.json`.

## Evidence locations

- **A01–A05:** `ark_pipeline/spatial/coverage.py`, both `config/spatial_semantics_*.toml`, `tests/test_spatial_pipeline.py`, `tests/test_antimeridian_unwrap.py`, and [spatial experiments](../performance/spatial_hierarchy_and_simplification_benchmark.md).
- **A06/A08:** `ark_pipeline/aggregation/pairs.py`, `aggregation/species_lists.py`, `builders/species_metadata.py`, `backend/schema.sql`, and the [field lineage inventory](../../methodology.md#14-field-lineage-and-intentional-information-reduction).
- **A07:** `ark_pipeline/cli/crosswalk_match.py`, `cli/crosswalk_refresh.py`, `tests/test_refresh_crosswalk.py`, and the [historical crosswalk report](../pipeline/06_iucn_goat_global_crosswalk.md).
- **A09–A13:** `ark_pipeline/cli/sources_download_goat.mjs`, `builders/species_metadata.py`, `builders/coarse_cache.py`, `tests/test_global_build.py`, `tests/test_serving_metrics.py`, and `frontend/src/lib/stores/weights.ts`.
- **A14–A17:** `ark_pipeline/spatial/boundaries.py`, `builders/species_metadata.py`, `frontend/src/lib/map/mapDomains.ts`, `frontend/src/lib/components/Map.svelte`, and [boundary documentation](boundary_filtering.md).
- **A18–A20:** `config/data_sources.toml`, `ark_pipeline/cli/sources_acquire.py`, `cli/serving_prepare.py`, `cli/serving_tiles.py`, `runtime/forecasts.py`, and [benchmark methodology](../pipeline/08_pipeline_benchmark.md).

Paths continuing with `aggregation/`, `builders/`, `cli/`, or `runtime/` in this
list are relative to `ark_pipeline/`. Primary scientific/library references are
linked at the relevant claims in the methodology, rather than treated as
endorsement of Ark's full model.

## Registered metadata findings

The read-only audit verified SHA-256 hashes for the registered IUCN taxonomy,
assessments and GoaT TSV. It checked that the saved benchmark crosswalk named
the same inputs, verified its Parquet and summary against the passed receipt,
and reconciled the crosswalk's complete IUCN identity set and counts. No source
or derived generation was changed.

| Measurement | Observed value | Interpretation |
| --- | ---: | --- |
| IUCN taxonomy rows / distinct IDs | 171,604 / 171,604 | No duplicate taxonomy IDs in this snapshot |
| Assessment rows / IDs / distinct taxa | 171,604 / 171,604 / 171,604 | One supplied assessment per taxon |
| Assessment scope | All rows contain Global | Some also name regional scopes; not multiple separate assessment rows |
| Publication-year range | 1996–2025 | Acquisition's 2026-1 label does not establish contemporary evidence for every species |
| Population trends | 41,835 decreasing; 39,060 stable; 1,253 increasing; 83,082 unknown; 6,374 missing | Present in source, absent from the present priority formula |
| Older Lower Risk categories | 745 species | Outside the six modern category score predicates; spatial inclusion determines actual cell impact |
| EX/EW category rows | 0 | The unsupported extinction/DNA shortcut is dormant in this snapshot |
| GoaT records / unique IDs | 2,138,975 / 2,138,975 | Includes many ranks, not all species |
| GoaT species-rank records | 1,795,920 | Appropriate denominator for species-level evidence coverage |
| Species meeting current GoaT evidence predicate | 19,565 | Broad project/sample/assembly predicate, not completed-genome count |
| False-like status strings checked | 0 | No exact case-insensitive false/no/0/unknown values in the two tested project fields |
| Accepted crosswalk links | 122,392 / 171,604 (71.3223%) | Source-matched September automatic result; not the historical 73.59% enriched result |
| Safe-transfer flags | 122,004 | One-to-one within the accepted supplied crosswalk; not a precision estimate |
| Accepted rows with at least two lineage conflicts | 16,233 | Review targets, not demonstrated wrong matches; rank conventions can differ |
| Linked IUCN species with qualifying evidence | 5,855 | Safe transfer plus the implemented evidence predicate |
| Qualifying linked species with resampling entry | 39 | All entries are `DTOL`; current evidence predicate ignores this field |
| Current EBP flag | 2,398 species | Counts the substring predicate, not full standards compliance |
| Wider-GoaT family candidates | 580 family names covering 11,350 IUCN species | Name-based candidates beyond the current evidence universe; taxonomic confirmation required |
| Ambiguous genus-name groups in GoaT | 1,136 | Same genus name associated with multiple families or kingdoms among species records |
| Family names spanning kingdoms in GoaT | 0 | This limited check does not validate all lineage keys |

The accepted-link breakdown is 59,887/93,607 Animalia, 61,603/76,677 Plantae,
902/1,302 Fungi and 0/18 Chromista in this crosswalk. These coverage differences
must not be read as equivalent differences in genomic availability.

At audit time the registered GoaT file had 35 columns while the downloader
emitted 26. The downloader now carries the nine omitted fields:
`assembly_span`, `chromosome_number`, `haploid_number`, `genome_size`,
`contig_n50`, `scaffold_n50`, `chromosome_count`, `gene_count`, and
`sample_location`. It also writes non-value API field metadata into
`field_provenance_json` so estimated/source annotations are not discarded.

Snapshot identifiers for reproducing this audit:

| Artifact | SHA-256 |
| --- | --- |
| IUCN assessments | `ecc9081b74d722b7dc56e2b5f17b158e61bb65d5bc02b040303db0b514672afe` |
| IUCN taxonomy | `bd051e8e6f3efcd272660ba7b02aea77be32e340e7f30aa682c2c46cc2e7fdbd` |
| GoaT TSV, registered 30 August 2026 | `c7c27d07ba7d6deab56e112aeacf585c04d9d876f659b41cbe3011946c2bfa74` |
| Crosswalk from benchmark `2026-09-02T22-30-48Z-2ae48975` | `7a1f5f8a5c5b0187bd9ec7e0f1ba1560cfd2af6c991de20b1bb474e33689d661` |

The full local JSON includes source column names, category/status distributions,
audit-code identity and verification details. It is kept in ignored local data
at `data/validation/methodology/2026-09-03/methodology-audit.json`, rather than
committing provider-derived reports or personal source paths. The checked-in
script can regenerate it when the matching source disk is available.

## Verification performed

The focused Python suite exercised 63 cases across antimeridian, spatial
processing, pair aggregation, global build, metric equality, crosswalk refresh,
metadata, ADM2 and tile export. **All 63 passed with isolated scratch storage.**
An initial run inherited the local external scratch setting and failed one
aggregation command when the disk was absent. The test now explicitly supplies
its own temporary scratch directory. This was an environment-isolation failure,
not evidence that the selected relationship counts were wrong.

The frontend checks for map domains and boundary catalogues passed **11 tests**.
Three additional tiny-polygon probes at low, middle and high latitudes returned
the expected single resolution-7 cell using installed h3ronpy 0.22.0. These
probes are limited examples, not a global validation of all polar/dateline cases.

The prior antimeridian investigation recorded 997/997 valid, coordinate-identical
prepared inputs after the corrected transform and a successful full extreme-
polygon run. It did not establish unsimplified equivalence of that run's accepted
simplification. The full global build and completed tile export were not rerun
for this review.

To reproduce the metadata part of the audit against registered files:

```bash
uv run python -m scripts.audit_methodology \
  --root /path/to/authorized-data \
  --crosswalk /path/to/crosswalk-generation/iucn_goat_crosswalk.parquet \
  --output /path/to/local-audit-output
```

This verifies the registered IUCN/GoaT file hashes and their agreement with the
supplied crosswalk's source summary, reads the files, and writes a local JSON
report. It does not modify source files or pipeline generations. It records
actual column names, scope/trend counts, DNA-policy consequences, crosswalk
coverage/conflicts, and name-based wider-lineage candidates. Such candidates
require taxonomic review; they are not automatically accepted matches.

## Decisions that remain with biological review

The next review should decide what genomic evidence reduces sampling urgency,
whether and how extinction affects priority, which taxonomic universe represents
a family, how to handle unresolved matches, and which simplification/repair
errors are acceptable at the intended map scale. It should also assess the
priority model against a stated sampling or conservation objective.

These are concrete open decisions. This audit does not silently change them,
assign new error thresholds, or certify the current ranking as an optimal
conservation plan.
