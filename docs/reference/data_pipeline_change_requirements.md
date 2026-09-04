# Data pipeline change requirements

This document defines what must change before Ark-IV's global data pipeline is
treated as trustworthy, repeatable, and suitable for bring-your-own-data use.
It intentionally does not prescribe an implementation. Algorithm choice,
storage layout, orchestration, and optimization strategy belong to the person
implementing and benchmarking the pipeline.

The recovered PC v3 pipeline under `archive/pc/pipeline_v3/` is useful
historical evidence and a reasonable starting point, but it is not the current
production specification.

## Scale and current baseline

The pipeline must be designed and evaluated at the real global scale, not only
with synthetic or Denmark-sized inputs.

- The recovered spatial inventory describes 128,768 source polygon rows in
  seven GeoParquet files occupying about 37.1 GiB.
- The seven pair files currently stored on the T7 contain 22,414,776,593 rows.
- The current global serving documentation reports 95,984,189 resolution-7
  cells and 30,883,702,920 cell/species relationships in the later serving
  dataset. The difference from the pair-file count has not been reconciled and
  must not be treated as self-explanatory.
- The remembered v3 pair-generation run took roughly three hours on the PC.
  This is an approximate historical baseline, not a controlled benchmark.
- Pair generation was only one part of the complete update workflow. Total
  user time also includes source validation, taxonomy and DNA joins,
  aggregation, serving-data generation, validation, and publication checks.

Performance and efficiency are release requirements. Correctness work must not
turn the pipeline into a workflow that is impractical for an authorized user to
run on ordinary high-end workstation hardware.

## Required spatial semantics

The pipeline must define one explicit meaning for a species being present in an
H3 cell. The definition must cover both resolutions and must be visible in the
validation report and user-facing data documentation.

The current artifacts were produced from center-point containment semantics:
`h3.geo_to_cells()` includes cells whose center is inside the processed
polygon. That behavior is historical evidence, not the semantic definition for
the replacement pipeline.

For polygon range data, a species is present in a resolution-7 cell when the
closed source range polygon and the closed cell polygon intersect at any point.
A pure boundary-edge or corner-vertex touch counts even when the intersection
has zero area. Presence means that the cell touches or overlaps the species'
mapped range; it does not claim a point observation, suitable habitat, current
occupancy, or presence throughout the cell.

Polygon semantics must be preserved consistently:

- holes exclude coverage and multipolygon components contribute their union;
- very small, narrow, fragmented, and valid non-empty ranges must not disappear
  merely because they contain no H3 cell center;
- antimeridian-crossing ranges have the same intersection meaning as all other
  ranges; and
- numerical handling of boundary-only contact must be deterministic and
  covered by validation fixtures.

Invalid geometry may be repaired only by a deterministic, documented method.
The build must report the original validity problem, the repair applied, and
the resulting coverage change. A repair that causes a large or unexplained
change must fail validation rather than silently alter the range.

Topology-preserving simplification is permitted in routine builds. Routine
builds are not required to repeat a complete unsimplified global calculation
or prove cell-set equality for every source geometry. Instead, simplification
tolerances and routing rules must be approved through a versioned calibration
benchmark against unsimplified geometries. The benchmark must cover
representative and adversarial global geometries and report omitted cells,
added cells, boundary displacement, and geometries that lose all coverage.
Errors must be reported per geometry or species as well as globally so that
large ranges cannot hide failures on small ranges. No valid, non-empty source
geometry may silently lose all resolution-7 coverage.

Calibration must be repeated when the algorithm, tolerance,
geometry-processing dependencies, or source characteristics materially
change. Acceptable false-negative and false-positive limits must be fixed from
the first controlled calibration benchmark and recorded in the semantic
profile. The source's spatial precision, including IUCN's 2 x 2 km Area of
Occupancy convention where relevant, may inform that error policy but does not
by itself prove that an arbitrary simplification or buffer is safe.

Border expansion followed by center containment may be used as a conservative
candidate-generation method or as part of a documented approximate profile. It
must not be described as exact cell intersection: expansion can add cells that
do not intersect the original polygon and can change holes, narrow gaps, and
coastlines. Its measured omissions and additions must be included in the same
calibration evidence as simplification.

Resolution 3 is derived from resolution-7 membership rather than calculated
independently from the source polygon. A displayed resolution-3 cell claims
only that at least one resolution-7 child assigned to it intersects the
species' mapped range. It does not claim that the species occupies the whole
resolution-3 cell or that every location in it is suitable.

An exact reference calculation and an approximate production profile may
coexist, but their outputs, manifests, and user-visible labels must never be
interchangeable. The reference calculation may be limited to the calibration
corpus; it is not a mandatory full-global stage of every release build.

## Required correction of legacy v3 behavior

The T7 processing summary records that the current pair files were produced
with the v3 `fast` kernel. That kernel intentionally accepts a cell-set delta
from simplification and routed geometry processing. The current files must
therefore be treated as legacy approximate artifacts until their coverage is
measured against the chosen authoritative semantics.

A replacement pipeline must not retain these legacy behaviors as implicit
defaults:

- geometry or H3 exceptions becoming empty cell sets without a failed build;
- a zero error count that excludes internally suppressed failures;
- accepting any readable existing Parquet file as a valid resumable result;
- reusing outputs without checking the source identity, parameters, code
  version, semantic profile, or schema version;
- treating small observed cell-set differences as "effectively lossless"
  without an agreed error policy;
- assigning invalid H3 coordinates to `(0, 0)` or invalid partition parents to
  an empty string; and
- silently choosing one metadata value when rows for the same species disagree.

## Complete and lossless data use

Every source must have a field-level lineage from input through intermediate
relations, aggregate outputs, serving data, API responses, and displayed
features. Each field must be classified as retained, transformed, aggregated,
excluded by policy, unavailable, or intentionally unused.

The audit must cover at least:

- every input row and the reason any row is excluded;
- `presence`, `origin`, `seasonal`, and habitat/system fields;
- source and assessment identifiers;
- taxonomy and scientific-name fields;
- Red List status and assessment metadata;
- DNA, GoaT, and lineage evidence;
- EDGE and other priority inputs;
- geometry records and every geometry-processing failure;
- unmatched, ambiguous, duplicated, or conflicting species identifiers; and
- records present in spatial data but absent from the species dimension, and
  vice versa.

Filters such as `presence = 1`, `origin IN (1, 2)`, or non-null geometry are
policy decisions, not neutral cleanup. Their excluded counts must be reported
by source and category.

Deduplication must remove only duplicates under a documented key. Input,
excluded, duplicate, unmatched, failed, and output totals must reconcile at
every stage. A completed build must fail when reconciliation cannot be proven.

## Identity and metadata consistency

The stable application species identity is the IUCN SIS/internal taxon ID used
by the source range rows and H3 pair relations. It must remain distinct from
the IUCN assessment ID, GBIF taxon ID, GoaT/NCBI taxon ID, and every other
source-specific identifier. These identifiers must travel in explicitly named
crosswalk fields and must not replace or collapse the application identity.

New pipeline schemas and artifacts must use `iucn_sis_id` or an explicitly
defined generic application-key field whose value is the IUCN SIS/internal
taxon ID. They must not use the obsolete `gbif_accepted_id` field name for the
application key: that legacy name is misleading and is no longer part of the
global pipeline contract. Name matching must not silently turn an ambiguous
match into an authoritative crosswalk.

If multiple source rows disagree on taxonomy, threat status, DNA evidence, or
another species-level field, the build must expose the conflict and apply a
documented rule. Nondeterministic selection is not acceptable.

Point occurrences, range polygons, and future bird records must keep their
source identity and evidence type. The interface must not imply that a point
observation, a modeled range, and a polygon distribution have the same
meaning.

## Performance and resource requirements

Performance evaluation must cover the complete update workflow and each major
stage on named reference hardware. Reports must include wall time, CPU use,
peak memory, temporary-disk peak, bytes read and written, output size, and the
amount of work reused after restart.

The provisional performance gates are:

- no stage may regress against the recovered v3 baseline on equivalent data
  and hardware without an explicit correctness justification;
- the global spatial stage must show a material improvement over the roughly
  three-hour historical run before it replaces v3;
- total end-to-end update time must be measured and published, rather than
  presenting only the fastest spatial substage;
- work already validated as current must not be repeated during resume or an
  unchanged rebuild;
- memory and temporary storage must remain bounded and documented at global
  scale;
- processing one source update must be proportional to the affected work when
  a full rebuild is not semantically required; and
- validation must be efficient enough that users do not need to disable it to
  obtain practical build times.

A final numeric wall-time target should be fixed after the first controlled
benchmark on the actual PC and Mac/T7 environments. Until then, the three-hour
v3 recollection is the comparison baseline, not the desired endpoint.

Fast preview builds are valuable, but speed claims must include correctness
delta, hardware, input version, and whether cached or pre-existing work was
reused.

## Repeatability and provenance

A new build must begin with a cheap diagnostic that identifies missing,
unexpected, incompatible, stale, or unauthorized inputs before expensive work
starts.

Each accepted data pack must include a versioned manifest recording:

- every input's provider, official acquisition route, source release/version,
  local filename, size, and checksum;
- licence and attribution requirements;
- build commit, dependency versions, operating system, and relevant hardware;
- semantic profile and every result-affecting parameter;
- input and output schemas;
- stage timings and resource peaks;
- row, species, geometry, cell, and relationship reconciliation totals;
- warnings, repairs, approximations, unmatched records, and failures;
- checksums and sizes for every published artifact; and
- overall validation status.

Resumed work must be accepted only when its recorded provenance matches the
current build. A readable file alone is not proof that it is complete or
compatible.

The workflow must finish with one clear success or failure result. Partial
success, skipped work, and degraded profiles must be explicit.

## Bring-your-own-data release profile

A normal public clone must run without restricted datasets and must make its
synthetic status obvious. Authorized users must be able to point the pipeline
at provider downloads they obtained themselves without editing source code or
accepting provider terms through automation.

The authorized profile must retain the complete map, species highlighting,
cell details, and table features. Missing restricted inputs must produce a
specific explanation rather than silently changing results.

Raw inputs and generated Parquet, Arrow, PMTiles, databases, reports containing
private paths, and other provider-derived artifacts must remain outside Git.
Private prebuilt packs must carry the same manifest and validation evidence as
locally built packs.

## IUCN point, HydroBASINS, and bird data readiness

The recovered PC code contained no implementation for IUCN point data,
HydroBASINS relationship tables, or the separately provided bird data. The
current pipeline now integrates the first two without redefining range-polygon
coverage: points use their containing fine cell, and basin relationships join
to a reusable v1c any-touch basin index. Source archives, receipts, normalized
basin relationships, and audits retain their route identity before the common
serving union. Bird integration remains unimplemented.

The ordinary serving lists intentionally collapse evidence type to a distinct
species/cell set. This is efficient for current richness and priority metrics,
but does not yet satisfy a provenance-aware interface that lets a user render
point, basin, and range evidence separately. That remains an explicit product
acceptance gap rather than an implicit claim that the evidence types are
equivalent.

Before either source is accepted, the build requirements must identify:

- provider permission and redistribution constraints;
- source version and schema;
- stable identity and crosswalk coverage;
- duplicate and conflicting records;
- spatial meaning and precision;
- interaction with existing polygon and aggregate products;
- fields retained or intentionally excluded; and
- incremental and full-build performance impact.

## Acceptance evidence

The pipeline changes are complete only when an independent reviewer can verify:

- the chosen H3 semantics with adversarial geometry fixtures;
- exact or bounded-delta results on representative real global geometries;
- complete row and relationship reconciliation;
- deterministic metadata and identity outcomes;
- a clean rebuild from authorized inputs;
- a safe restart after interruption;
- rejection of stale or mismatched intermediate outputs;
- an end-to-end benchmark at full scale;
- a functioning synthetic public profile;
- a functioning authorized private profile; and
- a manifest sufficient to reproduce and explain the result later.

Passing unit tests alone is not sufficient. Acceptance requires source-backed
validation reports and full-scale performance evidence.
