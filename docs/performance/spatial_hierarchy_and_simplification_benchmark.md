# Spatial hierarchy and simplification benchmark

Historical harnesses and raw reports below are retained in the Git-ignored
`archive/` only. They are not required for the supported build or test suite.
Use `just data-benchmark` for current end-to-end measurements.

The [methodology review](../../methodology.md#7-h3-coverage-and-simplification)
qualifies the scientific interpretation of the historical results below.
Tolerance-derived metre budgets and component counts do not by themselves prove
biological accuracy; post-antimeridian-fix calibration and explicit per-species
acceptance limits remain open.

## Scope

This investigation uses the corrected any-touch rule: a resolution-7 H3 cell
is included when its closed polygon intersects the closed source polygon at any
point, including a shared edge or vertex.

The benchmark compares:

- direct tiled H3 overlap at resolution 7;
- direct untiled H3 overlap at resolution 7;
- full adaptive descent from each start resolution 0 through 6;
- single-hop refinement from each start resolution 0 through 6 directly to
  resolution 7;
- unsimplified geometry against `0.01°` and conservative near-2 km
  (`0.017906068061°`) decision simplification.

## Reusable 1,000-polygon fixture

`data/spatial-test/benchmark-samples/iucn-polygons-stratified-1000.parquet`
is a deterministic, 265 MiB benchmark fixture built from all 30 IUCN spatial
archives. It contains 1,000 valid source polygons selected across fixed
logarithmic bbox-area bands.

The fixture spans:

- bbox area: `3.2278e-10` to `60,471.55 deg²`;
- coordinate count: 4 to 1,317,916;
- 22 populated size bands;
- 26 source archives represented.

## Population-weighted global estimate

The stratified fixture is not itself population representative. The original
bounds-only census assigned all 110,868 conservative-v2 eligible source
polygons to the same size bands. It is stored in
`data/spatial-test/benchmark-samples/iucn-polygon-size-distribution.json` and
took 853.1 seconds across all 30 compressed archives. After adopting IUCN's
published richness policy, the census was rerun and reconciled all 112,218
attribute-eligible polygons. That v3 distribution is stored in
`data/spatial-test/benchmark-samples/iucn-polygon-size-distribution-richness-v3.json`
and took 842.1 seconds.

The diagnostics analyzer chooses the artifact with the largest unmixed sample
for each strategy and size band, uses the within-band arithmetic mean, weights
it by the population census, and bootstraps the sampled polygons within every
band. Ten deterministic hash-sampled fixture polygons were timed in each of
the five largest bands. The forced global maximum is retained as a stress case
but is not allowed to stand in for the complete largest band.

| Route | Estimated CPU | Four-worker wall at 80% efficiency | Bootstrap 90% wall interval |
|---|---:|---:|---:|
| Historical 10° exact direct any-touch | 114.94 h | 35.92 h | 24.97--48.16 h |
| Exact below 0.03 deg²; `0.01°` simplified above | 38.14 h | 11.92 h | 8.22--16.09 h |
| Previous route plus 2.5° partitions for bands 19--23 | 15.88 h | 4.96 h | 3.71--6.30 h |

These estimates cover 100% of the eligible population by size band but exclude
fixed archive I/O, pair writing, and finalization. The interval measures
within-band polygon sampling uncertainty, not worker-scaling or disk-contention
uncertainty. An end-to-end multi-worker calibration remains necessary.

### Current production estimate

The unsimplified 50-polygon partition audit allows the actual v3 production
router to be estimated without borrowing simplified timings. The router uses
10° direct fill below 100 deg² and exact 2.5° fill at or above 100 deg².

| Route | Estimated CPU | 10-core wall at 80% | Bootstrap 90% wall interval |
|---|---:|---:|---:|
| Historical 10° exact route, v3 weights | 113.72 h | 14.22 h | 9.88--19.06 h |
| Production exact dynamic tiles | 33.98 h | 4.25 h | 3.14--5.43 h |
| Production 1.12 km-bounded tail | 25.90 h | 3.24 h | 2.33--4.18 h |
| Experimental `0.01°` simplified + 2.5° tail | 15.72 h | 1.97 h | 1.47--2.49 h |

The 1.12 km-bounded production estimate is the current planning number. It
excludes fixed source reads, pair writes and finalization, so 3.24 hours is not
an end-to-end guarantee. The faster route is approximate by design:
decision simplification changes some boundary-cell memberships. It is now
enabled only in the five largest bands with a 1,116.94 m conservative
displacement bound, inside the required 2 km accuracy budget. Rows that cannot
be simplified within the structural guards fall back to their original
geometry.

The 1,000-polygon fixture was selected under v2. The v3 and v2 policies overlap
on 110,362 rows; 1,856 rows are v3-only and 506 are v2-only. Reweighting uses
the complete v3 size distribution, but the 1.65% v3-only population is not yet
directly represented in the timing fixture. Rebuilding the fixture under v3
and running an end-to-end ten-worker archive calibration are the two remaining
steps before treating the interval as a release forecast.

### Two-kilometre production audit

The final production simplifier uses `0.01°`, whose maximum WGS84 local-scale
bound is 1,116.94 m, and only activates in bands 19--23 (bbox area at least
100 deg²). The fast path must remain valid and preserve all disconnected
components. If a remote component would disappear, topology-preserving
simplification is tried; if that also fails, the original geometry is used.
Small tolerance-scale holes may collapse because filling them remains within
the same set-displacement budget.

Ten unbiased polygons in each of the five bands were compared with their
unsimplified 2.5° reference:

- 50/50 were within the configured 1,116.94 m bound;
- 47/50 simplified and 45/50 were at least 5% faster;
- median total speedup was 1.30x;
- across 57,410,051 reference cells, 113,377 were omitted (0.197%) and 38,446
  were added (0.067%); and
- every component was retained; three rows safely fell back unchanged.

The reusable artifact is
`data/spatial-test/simplification-crossover/tail-bins-19-23-2km-topology-safe.parquet`.

The exact estimate is dominated by the five largest bands. Band 23 alone
contains 4,361 polygons and contributes an estimated 72.25 CPU-hours; its ten
unbiased timings range from well under a second to about three minutes. This
spread proves that bbox area is only a stratification variable. Coordinate
count, component count, spatial dispersion, and output-cell cardinality are
needed for per-polygon routing.

On the new unsimplified production profile, a 245,034-coordinate /
3.27-million-cell case took 15.22 seconds total and matched the old exact
3,265,747-cell result. The prior 10° exact timing for that polygon was 62.78
seconds, so dynamic 2.5° tiling was 4.12x faster. Native
`geometry_to_cells` consumed 13.65 of 14.62 profiled kernel seconds (93.4%).
Clipping consumed 0.74 seconds, sorting 0.11 seconds, and candidate
amplification was 1.019x.
The kernel used approximately one CPU core. This route is already close to the
current native implementation's practical single-core floor; Python-side
micro-optimization cannot close the remaining gap. The credible levers are
multi-process scaling, a faster native fill, or avoiding full resolution-7
cell materialization for global ranges.

The reusable commands are:

```bash
.venv/bin/python -m archive.research.spatial.build_spatial_size_distribution \
  --data-root /path/to/authorized-data-root
.venv/bin/python -m archive.research.spatial.diagnose_spatial_benchmark profile \
  --sample-id 984 --strategy direct-tiled-r7 \
  --simplification-degrees 0.01 --cprofile
.venv/bin/python -m archive.research.spatial.diagnose_spatial_benchmark analyze \
  --workers 4 --worker-efficiency 0.80
```

The analyzer writes a machine-readable JSON report, a Markdown summary, a
strategy-by-size Parquet table, optional per-run Parquet/JSON phase metrics,
and standard `.prof` files for call-level inspection.

## Large-range spatial partitioning

The direct kernel was already partitioning source geometry into 10° tiles, but
processing those tiles sequentially. Smaller exact partitions were tested
because native any-touch fill cost is superlinear for complex tile geometry.

On a 245,034-coordinate range producing 3.25 million unique cells:

| Tile size / execution | Fill time | Split + merge | Exact |
|---|---:|---:|---:|
| 10°, one thread | 19.22 s | 0.20 s | yes |
| 5°, one thread | 9.88 s | 0.22 s | yes |
| 2.5°, one thread | 6.28 s | 0.30 s | yes |
| 1°, one thread | 6.03 s | 0.89 s | yes |
| 2.5°, four threads | 2.66 s | 0.30 s | yes |
| 2.5°, eight threads | 2.12 s | 0.31 s | yes |

The 2.5° size is the best tested sequential balance: 1° no longer lowers
native fill enough to repay its additional clipping and task overhead. The
full test then processed ten deterministic hash-sampled polygons in each of
bands 19--23. All 50 partitioned cell sets matched the existing 10° results
bit for bit. Seam duplication was approximately 2% in representative cases.

The population-weighted experimental route that remains exact below 0.03 deg², applies
`0.01°` simplification above it, and uses sequential 2.5° partitions for
bands 19--23 is estimated at 15.88 CPU-hours. At ten cores and 80% efficiency,
that is 1.98 compute-hours with an approximate bootstrap interval of
1.48--2.52 hours, before fixed I/O, writing, and finalization.

Per-polygon tile threads reduce the latency of a single straggler by 4--12x in
the tested tail cases, but consume more aggregate CPU than sequential 2.5°
tiles. A global ten-core build should therefore schedule independent tile tasks
across the shared worker pool instead of giving every polygon ten nested
threads. Spare workers may help finish the last large polygon's remaining tiles
near the end of a build. Native `geometry_to_cells` itself exposes no internal
thread-count control in the current binding; exact concurrency is obtained by
independently filling clipped tiles and taking their union.

The reusable partition harness is
`archive/research/spatial/benchmark_spatial_partitioning.py`. Its 50-row exact result is
`data/spatial-test/hierarchy-benchmark/tail-random10-partition-2_5.parquet`.

The 2.5° partition is now selected by the production exact profile whenever a
polygon bbox is at least 100 deg². Smaller polygons retain 10° tiles. The
production setting is deliberately sequential inside a polygon worker so a
multi-process global run cannot accidentally multiply its requested core
count through nested tile threads.

An additional unsimplified production audit timed ten deterministic
hash-sampled polygons from each of bands 19--23. All 50 2.5° results matched
the old 10° exact result digests bit for bit. This artifact is
`data/spatial-test/hierarchy-benchmark/tail-random10-partition-2_5-exact.parquet`
and is the tail input to the `production-exact-dynamic-tiles` global estimate.

Each row retains source archive hash, layer, feature row, IUCN SIS ID, WKB,
bounds, planar area, coordinate and component counts, validity, selection band,
and deterministic selection priority. The accompanying JSON receipt records
the complete archive scan and selection method.

## Any-touch hierarchy results

The one-per-populated-band matrix produced 337 completed strategy runs over 22
real polygons. Twenty-one polygons completed the full 16-strategy matrix; the
largest band's direct tiled result was retained before the dominated variants
were stopped. All full-adaptive variants matched the direct reference.

The original single-hop implementation omitted cells on one polygon for starts
at resolutions 4, 5, and 6. Investigation showed this was not an H3 logical
parent failure: GEOS collapsed a valid descendant footprint when buffered by
`1e-12°`. The affected omitted groups were complete descendant sets (343, 49,
or 7 cells) of coarse parents. Changing the clipping-only buffer to `1e-10°`
(about 0.01 mm at the equator) retained valid footprints. The three failed
variants were rerun and matched all 929,932 direct-reference cells exactly.

### Performance findings

- Direct any-touch resolution 7 was fastest for 20 of the 21 complete size-band
  representatives. A sub-millisecond adaptive win on a one-cell polygon is
  timing noise and has no production significance.
- Full adaptive descent was consistently slower. On a 2,612 deg² / 71k-vertex
  polygon, direct took 5.47 s while adaptive starts took 15.4--17.2 s.
- Single-hop starts at resolutions 0--2 have large fixed descendant-expansion
  costs. On tiny polygons, r0→r7 took about 7.3 s and r2→r7 about 0.18 s while
  direct took less than 1 ms.
- Resolution 3 is the strongest single-hop start. On the same 2,612 deg²
  polygon, r3→r7 took 7.29 s; r4→r7 took 7.48 s, and later starts became slower.
- A real r3 crossover appeared at 5,604 deg² / 83k coordinates / 13 components:
  direct took 10.36 s and r3→r7 took 9.85 s (4.9% faster).
- A neighboring 7,747 deg² / 168k-coordinate / 11-component polygon favored
  r3→r7 by 23% (32.35 s versus 41.99 s).
- Size alone is not a sufficient router. Polygons at 1,123--4,312 deg² favored
  direct by 1.3x--19x, and a 28,809 deg² / 544k-coordinate polygon took 142.0 s
  direct versus 147.7 s with r3→r7.
- Fragmentation matters. The r3 winners had few large, highly detailed
  components. Polygons with hundreds or thousands of disjoint components made
  local parent refinement substantially slower even at similar bbox area.

The evidence supports retaining r3→r7 as a possible specialist route, but not
selecting it from bbox area alone. A production router needs at least area,
coordinate count, and component count, followed by held-out validation.

## What degree tolerance corresponds to 2 km?

There is no globally exact conversion because WGS84 metres per degree varies by
axis and latitude.

- At the equator, 2 km east--west is `0.0179663°` and north--south is
  `0.0180874°`.
- At Copenhagen's latitude, 2 km east--west is about `0.03179°` and
  north--south about `0.01796°`.
- WGS84's maximum local scale is approximately 111,693.98 metres per degree
  near the poles. Dividing 2,000 m by that maximum gives
  `0.017906068061°`.

Therefore `0.017906068061°` is a conservative scalar upper bound for a 2 km
Douglas--Peucker displacement in lon/lat coordinates. It is not an exact
2 km tolerance in every direction.

## Simplification crossover

The crossover harness measures total simplified cost (simplification plus H3
kernel), alternates execution order, and uses median kernel timings. It also
compares every simplified cell set against unsimplified any-touch coverage.

### Exhaustive lower tail

All 268 fixture polygons through size band 8 were tested three times per
tolerance.

- The first genuinely changed and >5% faster `0.01°` polygon had bbox area
  `0.000215 deg²`. It fell from 478 coordinates to 4 and omitted one cell.
- The first genuinely changed and >5% faster `0.017906068061°` polygon had bbox
  area `0.000958 deg²`. It fell from 739 coordinates to 4 and omitted one cell.
- No geometry below band 7 was changed by either simplifier.

Band 7 spans `0.0001--0.0003 deg²`. Therefore a literal "enable from the
first band containing a faster polygon" rule would keep only polygons below
`0.0001 deg²` unsimplified. That is a measured crossover observation, not the
recommended production cutoff: only 1 of the 49 band-7 polygons both changed
and became faster at `0.01°`, and it lost one of its output cells.

These literal first wins are unsuitable thresholds: they save fractions of a
millisecond by collapsing tiny ranges, for which losing one cell can mean a
large percentage of the complete range.

### Expanded boundary bands

Every fixture polygon in bands 11 and 12 (49 per band) was tested with three
alternating timings.

| Bbox band | Tolerance | Median speedup | Rows >5% faster | Weighted omissions | Median time saved |
|---|---:|---:|---:|---:|---:|
| `<0.03 deg²` | `0.01°` | 1.34x | 36/49 | 6.75% | 0.113 ms |
| `<0.03 deg²` | `0.017906°` | 1.47x | 42/49 | 11.45% | 0.143 ms |
| `0.03--0.1 deg²` | `0.01°` | 1.42x | 44/49 | 4.68% | 0.298 ms |
| `0.03--0.1 deg²` | `0.017906°` | 1.49x | 43/49 | 7.63% | 0.290 ms |

Across bands 12--18, `0.01°` produced a 1.56x median speedup and 0.73%
cell-weighted omissions. The near-2 km tolerance produced only a modestly higher
1.68x median speedup while omissions rose to 1.29% and additions from 0.25% to
0.52%.

### Recommendation

Use `0.01°`, not the full `0.017906068061°`, as the initial production
simplification tolerance. It stays within roughly 1.12 km everywhere, already
captures most of the speed benefit, loses substantially fewer boundary cells,
and avoids more non-topological simplification fallbacks.

Keep polygons with bbox area below `0.03 deg²` unsimplified. This is deliberately
more conservative than the literal first speed win: the smaller-band savings
are negligible in absolute time, while a one-cell omission can materially alter
a tiny range. Validate the `0.03 deg²` cutoff on the full production mix before
freezing it into the semantic profile.

## Extreme global range

The current pair builders share one spatial CPU-slot semaphore across polygon
processes and native tile helper threads. Each polygon holds one slot; extra
threads borrow only free slots, returning them after every tile. A polygon with
at least twice as many grid tiles as configured workers can use helpers. When
all polygon processes are occupied, filling stays serial within each polygon;
when processes become idle, the remaining large polygon can use their slots.
The pending queue holds at most `workers - 1` helper tile results per polygon,
plus its caller's current tile. Threads share the immutable source polygon;
they do not copy its full geometry into more processes. Final sorted
deduplication makes completion order irrelevant to the cell set.

This does not change the simplification policy. The fast `0.01°` candidate must
be nonempty, valid, and have the same disconnected-component count. Rejection
triggers a topology-preserving attempt subject to the same checks; rejecting
that attempt retains the entire original geometry. There is no minimum vertex
reduction or speedup requirement. Small holes may collapse within the tolerance,
but even a tiny disconnected component may not disappear. Requiring an equal
component count is a coarse guard, not a proof of individual component identity.
Trying simplification separately per component and keeping only rejected
components unchanged is a potential improvement, but changes the resulting
coverage and requires calibration before adoption.

Benchmark polygon timings and production row audits now record the selected
simplification method, coordinate counts, and reasons for each rejected attempt.
This makes a fallback distinguishable from successful simplification without
repeating a long calculation.

On 2026-09-03 the exact extreme fixture from benchmark
`2026-09-02T22-30-48Z-2ae48975` was rerun with eight shared tile slots. Total
polygon time fell from the saved run's **740.76 s to 369.32 s (2.01×)**.
All **69,370,937** output cells matched the saved pair file, in sorted order;
the uint64 array SHA-256 was
`7b633dcc463f56530969e1ea2b97da00e0e6338555fbcd61142e51d718e755f2`.
The profile and sample hashes were verified before the comparison. This is a
single extreme-case comparison on the local Mac, not a repeated controlled
full-pipeline benchmark; concurrent workloads and cache state can differ.

That parallelism-only rerun still used its original 1,317,916 coordinates: the fast simplifier
changed the component count from 36 to 233, and the topology-preserving attempt
was invalid (`Hole lies outside shell`). Its 9,862 grid tiles used up to eight
slots. Validation/unwrapping, simplification attempts, and final deduplication
remain serial. The speedup therefore preserves the prior coverage policy and
does not depend on accepting a previously rejected simplified geometry.

The follow-up diagnostic found and corrected a separate transform problem: both the
source and prepared geometry are valid, but the previous longitude unwrapping produced
`Hole lies outside shell[-187.508349141 61.1116015210001]` in component 18
(zero-based). The shell's vertex-average longitude was used to shift holes by
360 degrees. In this wide global shell, 306 eastern holes were shifted outside
the shell even though none of its rings crossed the date line. This was a
coordinate-transform bug, not invalid source data.

Already-continuous polygons now retain their exact original coordinates. For
genuine date-line crossings, holes are aligned by containment in the unwrapped
shell. Ambiguous placement and invalid transformed geometry fail explicitly;
the transform does not delete holes or invoke a repair to hide its own errors.
All 997 eligible geometries in the original benchmark sample remained valid
and byte-identical to their prepared inputs after this transform. The extreme
fixture also remained byte-identical to the raw source, including all 36
components and 23,096 holes. Regression tests cover uneven vertex density,
date-line holes, ring starting points and winding, unplaceable holes, and H3
coverage equivalence to an explicitly aligned reference.

The exact-cell comparison above validates parallelism against the previous
output; it does not establish correctness of that previous transform. The
simplification acceptance rules remain unchanged. A corrected transform can
allow a previously rejected simplification to succeed, so old output counts
are not the acceptance oracle for the corrected calculation.

With the corrected transform and the same eight-slot worker budget, the full
extreme-polygon calculation completed in **187.56 s (3.13 min)** and emitted
**69,313,101** unique cells. Fast simplification was rejected for changing 36
components to 225; topology-preserving simplification succeeded with **261,419
coordinates**. This single-run result includes both the transform fix and the
earlier parallelism improvement compared with the original 740.76 s run. It is
not a fresh simplification-error calibration against unsimplified cell coverage.

In the earlier hierarchy experiment, the largest fixture polygon had
1,317,916 coordinates, 36 components, and
38,145 deg² planar area. `0.01°` simplification reduced it to 198,340
coordinates in 30.84 s after 25.59 s of validation/unwrapping. Its simplified
r3→r7 run still took 965.77 s, produced 69,361,466 cells, and peaked at 3.22 GB
RSS.

This case shows that hierarchy and simplification alone do not guarantee the
three-hour whole-pipeline target. Output cardinality itself is material, and the
large-range production policy still needs an end-to-end throughput test.

## Reproducible artifacts

- Sample builder: `ark_pipeline/cli/benchmark_sample.py`
- Hierarchy harness: `archive/research/spatial/benchmark_spatial_hierarchies.py`
- Simplification harness: `archive/research/spatial/benchmark_spatial_simplification_crossover.py`
- One-per-band hierarchy matrix:
  `data/spatial-test/hierarchy-benchmark/matrix-one-per-bin.parquet`
- Large-threshold comparison:
  `data/spatial-test/hierarchy-benchmark/threshold-large-v1.parquet`
- Corrected parent/child validation:
  `data/spatial-test/hierarchy-benchmark/parent-child-fix.parquet`
- Repeated crossover bands:
  `data/spatial-test/simplification-crossover/bins-07-12-repeats5.parquet`
- Full boundary-band crossover:
  `data/spatial-test/simplification-crossover/bins-11-12-full-repeats3.parquet`
- Exhaustive lower tail:
  `data/spatial-test/simplification-crossover/lower-tail-full-repeats3.parquet`
