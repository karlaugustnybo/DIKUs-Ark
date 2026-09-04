# Map Performance Session Retrospective

## Overall result

The map has been substantially re-architected. The previously unresolved res7 rendered-coverage regression has been fixed and verified against the mounted global dataset. A later attempt to reduce minimum-zoom pan stutter with a separate res2 overview introduced unacceptable zoom lag and has been completely reverted. The map is back to the earlier behavior: one exact, extruded, selectable res3 layer at coarse zooms, with no geometry swap at zoom 1.25.

The retained improvements are the global dataset profile, the permanently cached res3 snapshot, the compact res7 format, removing tile loading from control of the camera, and the two-phase res7 coordinator. The coordinator publishes a complete visible generation first and then atomically expands it to a complete rendered two-tile guard. Exact 3D cells and both renderers use the earlier stable 2x Retina policy. The deterministic zoom-10 trace and renderer-isolation diagnostic remain; the experiment-only overview traces were removed with the rollback.

The recommended performance decision is to stop here and accept the slight minimum-zoom pan stutter rather than risk the much worse zoom lag. The earlier isolated zoom-10 trace recorded 17.6 ms P95 and 17.8 ms worst with no long frames, long tasks, coverage loss, or flashes. The original absolute 16.7 ms target remains below the measurable pacing floor of some valid sessions, including empty-renderer controls, so future regressions should be judged against a same-session empty control and by doubled intervals, long frames, gaps, flashes, and camera discontinuities.

The global rewrite initially shared a working tree with generated databases,
source extracts, prototype files, and obsolete media. The August 2026 release
cleanup removes the prototype runtime, keeps authorized data under ignored
paths, adds a release-content guard, and documents the remaining Git-history
rewrite as a mandatory administrator action.

## What was implemented

### Global dataset configuration

- `just start` uses the global dataset profile.
- The unsupported prototype launcher has been removed; compatibility input
  adapters remain only where they are covered by build tests.
- Global res7 source and aggregate directories are validated before startup.
- The database, PMTiles, metadata, exports, and res7 partitions are selected as one global profile.

This fixed the original situation where normal startup silently used the Denmark archive and `/api/tiles/res7` returned 404 outside that dataset.

### Permanently cached global res3

Loading the entire 69 MB PMTiles archive was investigated and rejected. Its decompressed MVT representation was much too expensive and repeated the same res3 cells at every web-tile zoom.

Instead, a dedicated global Arrow snapshot was built:

- 40,295 global res3 cells.
- Approximately 15 MB uncompressed.
- Columnar typed metric data and boundary memberships.
- Parsed once and shared through a module-level promise.
- Preloaded during homepage idle time.
- Also preloaded on map-link hover, focus, and pointer-down.
- Reused when navigating to the map.
- The entire world remains in memory, so rapid zoom-out does not request res3 tiles.

This is probably the cleanest and most successful architectural change from the session.

### Compact dynamic res7 delivery

The first res7 design dynamically generated GeoJSON polygons. It was fundamentally too heavy:

- Multi-megabyte JSON responses per tile.
- DuckDB querying and H3 polygon construction on every cache miss.
- Browser JSON parsing, object allocation, and polygon triangulation.
- Viewports involving tens of megabytes and tens of thousands of features.

That was replaced by compact positional arrays:

```text
[h3_index, metric_1, metric_2, ...]
```

The browser now reconstructs H3 geometry with `H3HexagonLayer`. This reduced representative tiles by roughly an order of magnitude and removed server-side polygon serialization.

The backend now also:

- Caches aggregate coverage based on directory modification time.
- Invalidates tile cache entries when partitions are replaced.
- Validates aggregate schemas once per immutable file version.
- Uses a bounded 64-tile response cache.
- Returns consistent `{cells: []}` responses.
- Runs tile rendering outside the async event loop.

### Res7 rendering and camera behavior

A dedicated coordinator was introduced to:

- Compute canonical web-tile coverage.
- Handle antimeridian wrapping.
- Decode and cache tiles.
- Give every camera request a generation number.
- Prevent stale asynchronous requests from overwriting newer camera state.
- Publish complete generations atomically.
- Publish the complete visible generation without waiting for overscan.
- Replace it with a complete rendered guard generation when that outer buffer is ready.
- Reject an obsolete guard if a newer camera generation has started.
- Keep stable data arrays and layer IDs per tile chunk.

Several major camera bugs were removed:

- The map no longer holds the camera at the res3/res7 boundary.
- Fine data never calls `jumpTo`.
- Zooming is no longer blocked while res7 loads.
- The “zooming sends me to Africa” bug was removed with the stored-camera replay.
- Zoom can continue to the configured maximum of 19.

The current configuration:

- Begins forecasting res7 at zoom 7.5.
- Forecasts the smaller zoom-11 viewport.
- Displays res7 above zoom 10.
- Uses web-tile zoom 10 for delivery.
- Keeps res7 web-tile partitioning stable during further map zooming.
- Renders two z10 guard tiles beyond the complete viewport.

The two-phase publication is important. Waiting for the entire guard delayed the handoff, while publishing only the visible tiles let consecutive drag gestures outrun the rendered data. Publishing visible tiles first and the complete guard second gives the handoff a small initial GPU upload without leaving the warmed buffer in CPU memory only.

### Rendering architecture

- CARTO is rendered by MapLibre rather than a custom Deck raster layer.
- A country layer sits underneath CARTO as a non-white fallback.
- Deck is a synchronized overlay used for priority cells.
- MapLibre owns world copies and raster-tile replacement.
- Res3 uses exact high-precision H3 polygons to avoid antimeridian distortion.
- Res7 uses stable per-tile instanced H3 layers.
- Picking is disabled during interaction.
- Unchanged tile chunks retain their GPU buffers.

### 3D cells and tilt

Both res3 and res7 are now always configured with `extruded: true`. The earlier optimization that removed res3 side-wall geometry while flat has been reverted because the latest requirement is to restore the earlier genuinely 3D cells. The initial camera pitch is again 25 degrees, so the elevation is visible immediately; the navigation compass still allows returning to a flat view.

The deterministic performance trace continues to reset to pitch 0. That preserves comparability with previous traces and deliberately measures the intensive zoom-10 res3/res7 boundary without adding tilted-camera footprint variance.

### Retina rendering

The low-resolution appearance had two independent causes:

- The Deck overlay was hard-capped to a 1x drawing buffer.
- The CARTO URL did not expose MapLibre's `{ratio}` placeholder, so a Retina canvas could still receive 256 px raster tiles.

Both renderers currently use a stable device-pixel ratio of 2, and CARTO uses an `@2x` tile URL. MapLibre's 220 ms tile fade keeps parent imagery visible while children arrive.

An adaptive implementation that switched both backing stores to 1x during movement and back to 2x after settling was attempted and then reverted. Repeatedly resizing the MapLibre and Deck WebGL canvases stalled the render pipeline: the camera could stop after a single drag step and the tab could hang. The stable 2x setting is therefore the current correctness baseline, not the final performance solution.

The following state is exposed on the map element:

- Configured MapLibre and Deck ratios through `data-basemap-pixel-ratio` and `data-cell-pixel-ratio`.
- Whether the basemap template supports Retina tile substitution.

The performance monitor also records the minimum and maximum ratio observed during each run. Under the current stable policy, both should remain 2.

### Testing and diagnostics

Frontend coordinator tests cover:

- Antimeridian canonicalization.
- Fixed delivery zoom.
- Pitched viewport calculation.
- Atomic generation publication.
- Visible-first guarded publication.
- Rejection of obsolete guard generations.
- Reusing warmed coverage.
- Invalidating coverage after large pans.

The latest checks, run with the external global-data drive mounted, reported:

- Twelve frontend tests passing: eight coordinator tests and four performance-monitor/aggregation tests.
- Zero Svelte errors or warnings.
- Successful production build.
- An existing large map-route warning at roughly 558 KB gzip. The opt-in monitor and deterministic trace are separate dynamic chunks (roughly 1.57 KB and 1.32 KB gzip) and are not loaded on normal map visits.
- Earlier backend runs reported 42 passing tests.

Real-browser checks against the global data reported:

- A one-tile rendered guard allowed one uncovered sample during three back-to-back pans.
- A two-tile rendered guard loaded 30–36 z10 tiles and eliminated uncovered samples in the same test.
- A harder five-pan run moved roughly 1.9 degrees with zero uncovered samples.
- A final 80-step rapid pan moved 2.24 degrees with zero uncovered samples across 40 diagnostics; all samples retained the complete 36-tile rendered buffer.
- The res7 handoff committed 5,297 cells across 36 tiles in the populated Europe test viewport.
- Zooming out to res3 and back to res7 retained the fine generation and did not flash an uncovered viewport.

An opt-in performance harness is now available with `?mapPerf=1`:

- Normal MapLibre gestures are measured automatically.
- Pressing `P` starts and stops an explicit multi-gesture session.
- Every animation frame records frame interval, fine-coverage readiness, loading state, resolution, tile count, cell count, camera travel, and zoom travel.
- The harness counts actual res7 `fetch` calls from the fine-tile source rather than relying on cross-origin Resource Timing visibility.
- Long tasks, P95 and worst frame time, over-budget frames, res3 flashes, and uncovered frames are exposed through `data-perf-*` attributes and a JSON result.

The canonical path is now automated with `?mapPerf=1&mapPerfTrace=1&mapPerfRuns=3`:

- It waits for MapLibre's initial tiles and the complete res7 guard before every measured run.
- It resets to longitude 10, latitude 45, zoom 10.00, pitch 0, and bearing 0.
- It executes five linear -330 px horizontal camera pans of 300 ms each, followed by a fixed 450 ms cooldown.
- The first measured path extends beyond the initially warmed two-tile guard and therefore exercises new coordinator requests. It is a first-path loading run, not a claim that browser and server caches are globally cold.
- Later runs reset and replay the identical path through the coordinator cache, providing a steady-state comparison.
- The report records the idle `requestAnimationFrame` cadence, visibility, and focus before measuring. It marks whether the browser can support a meaningful 60 FPS budget comparison rather than interpreting a throttled browser as map jank.
- The full specification, per-run results, first-path result, repeat aggregate, cadence, and errors are exposed through `data-perf-trace-*` attributes.
- `mapPerfRenderer=full|basemap-only|cells-only|empty` isolates renderer cost while keeping the same camera, coordinator, and request path. The default is `full`, and these modes are opt-in diagnostics only.

The canonical intensive trace runs at zoom 10.00, where the global high-precision res3 layer is still displayed while res7 forecasting and fetching are active. An 80-step session over 2.36 degrees recorded:

- 418 measured frames.
- 17.7 ms P95 and 65.7 ms worst frame time.
- Three frames over 50 ms.
- Two long tasks totaling 111 ms.
- 35 res7 fetch calls and 12 loading frames.
- Zero uncovered frames and zero resolution flashes.

This is the first real frame-level baseline. It narrowly misses the 16.7 ms P95 target and shows that long-task work remains during the pre-handoff state.

A controlled flat-res3 extrusion experiment then compared five fixed 330 px drag paths. Full extrusion produced P95 values of 18.3 and 17.6 ms; omitting invisible side walls while flat produced 17.3 and 17.5 ms. The gain was modest and input delivery still introduced some camera-distance variance. That optimization has now been reverted to restore true 3D cells; stable spatial chunking is the next interaction-time experiment.

The first mounted-global-data run of the automated trace correctly separated first-path loading from repeats:

- The browser reported a visible, focused 33.3 ms idle cadence, or approximately 30 Hz, so `supports60FpsBudget` was false.
- The first path moved 1.13 degrees, issued 18 new res7 requests, observed three loading frames, retained 36 committed tiles, and recorded zero uncovered frames, resolution flashes, long frames, or long tasks.
- The two same-path repeats issued zero res7 requests and observed zero loading frames. They also retained complete coverage with no flashes or long tasks.
- First-path P95 was 34.2 ms; repeat median P95 was also 34.2 ms and the worst observed interval was 34.3 ms. Those values track the browser's 33.3 ms baseline and must not be compared with the 16.7 ms target.
- The matching loading and repeat cadence provides no current evidence that request-time res7 work or the rendered guard is adding extra missed frames in this 30 Hz environment. It does not prove the same result at 60 Hz.

The cadence check was added because the first automated run initially looked like a severe regression: every frame exceeded 16.7 ms. The idle baseline showed that this was browser pacing, not a renderer conclusion. Future automated results are invalid for the 60 FPS acceptance budget whenever `supports60FpsBudget` is false.

A later browser session did provide a cadence-valid 60 Hz environment with a 16.7 ms baseline. It also verified that settled MapLibre and Deck canvases could both reach physical 2x backing buffers: 2048 by 1600 for a 1024 by 800 CSS viewport. That experiment still held Deck at 2x during the whole gesture, while only MapLibre dropped to 1x. Its result was decisive:

- First path: 2,067 ms, 112 frames, 33.3 ms P95, 51.8 ms worst frame, one long frame, and no long task.
- The first path issued 18 res7 requests and observed eight loading frames.
- Two cached repeats issued no res7 requests and observed no loading frames, but had a 65.9 ms median P95, an 83.2 ms worst P95, a 100 ms worst frame, 28 total long frames, and six long tasks totaling 407 ms.
- Every run retained complete coverage with zero uncovered frames and zero resolution flashes.
- The recorded ratios were MapLibre 1x to 2x and Deck 2x throughout.

The cached repeats were worse despite doing no request-time res7 work. That makes the always-Retina high-precision cell pass, rather than res7 loading or rendered-guard coverage, the leading explanation for this regression. It also shows why “Retina everywhere all the time” is not an acceptable solution: 2x width and height means roughly four times as many fragments.

An adaptive 1x-to-2x implementation was subsequently tried but was not safe to retain: repeated WebGL backing-store changes stalled interaction and could hang the tab. The code was returned to one stable 2x backing-store resolution. The next permitted browser session therefore needed to re-establish the stable-2x baseline before trying a different optimization.

Browser diagnostics were also added as data attributes for resolution, zoom, pitch, loading state, coverage, committed cells, and tile count.

## Mistakes and regressions

### Trying to solve visible loading cosmetically

Early attempts relied on near-zero opacity, Deck tile refinement, and viewport callbacks. Those mechanisms are not a viewport-wide atomic guarantee. A tile at `opacity: 0.001` is also technically still visible.

**Lesson:** zero visible loading must be designed around complete committed viewport generations, not opacity tricks.

### Letting loading control the camera

The initial res7 handoff held the camera at a zoom boundary and replayed a stored camera after loading. This caused:

- Zoom stopping.
- Sudden camera movement.
- The jump toward Africa.
- A feeling that the map was fighting the user.

**Lesson:** loading must always follow the camera. It should never command it.

### Loading far too much res7 data

At different stages, the first fine generation contained approximately:

- 67,000 cells.
- In earlier versions, more than 100,000 cells.
- Sometimes much more after a stale preload completed.

The causes included:

- Coarse z8 delivery partitions.
- Large guard rings.
- An overestimated pitched footprint.
- A duplicate scheduling path that first loaded a small forecast and then replaced it with the much larger current coarse footprint.

This was eventually reduced to roughly 590 visible cells in the tested viewport by using z10 delivery and forecasting zoom 11.

**Lesson:** GPU upload size matters just as much as network payload size.

### Taking the visible-only correction too far — resolved

To remove the res7 handoff spike, the coordinator was changed so only visible tiles were published to Deck, while the guard ring stayed in the CPU cache.

That improved the handoff substantially, but it created the current blank-region problem:

```text
guard tiles downloaded != guard tiles rendered
```

When the camera moves beyond the last committed visible set, cached cells may exist but Deck is not drawing them yet.

This regression is now resolved. The coordinator publishes the complete visible set first, then publishes the complete rendered guard only if that generation is still current. The configured two-tile guard survived the prescribed rapid-pan tests without an uncovered diagnostic sample.

### Bundling unrelated experiments together

The most recent failed experiment changed three things simultaneously:

- Published the res7 guard ring.
- Capped CARTO's Retina pixel ratio.
- Added aggressive 24 px `clip-path` rounding.

The user reported that this completely broke the map, so all three were rolled back together. Because they were bundled, it was not possible to isolate which change caused the breakage.

**Lesson:** every future performance experiment should be one bounded change, browser-verified, and independently revertible.

### Cosmetic antimeridian fixes

One experiment expanded raster tile bounds geographically to cover cracks. Around plus or minus 180 degrees, that caused overlapping world-copy quads and likely contributed to the vertical white band.

The cleaner architecture—MapLibre owning basemap world copies and exact res3 H3 geometry—is now in place. The coordinator was exercised across the antimeridian without an uncovered coverage sample.

An apparent res7 rendering failure just east of the seam was investigated directly against the global tile API. At the tested latitude, canonical z10 tile `0/368` contained zero cells while tile `1018/368` west of the seam contained 132 cells centered between approximately 177.89 and 178.24 degrees longitude. The visual disappearance therefore followed the dataset boundary rather than a Deck world-copy failure. Exact-polygon and custom repeated-view experiments were reverted so the faster instanced res7 path remained in place.

**Lesson:** geometry and world-copy problems should not be solved with arbitrary coordinate padding.

**Lesson:** coverage readiness proves that the required source tiles are committed; it does not prove that those tiles contain data. Visual seam diagnoses must distinguish an empty dataset tile from a missing rendered tile before changing renderer geometry.

### Treating Retina resolution as a static setting

The first Retina experiment correctly improved settled sharpness but left the Deck cell canvas at 2x during movement. The cadence-valid cached repeats then performed worse than the loading run, even though they issued no res7 requests.

**Lesson:** high-DPI resolution multiplies fragment work. Treat it as a quality state that can change after interaction, and measure the physical backing buffer rather than trusting a requested renderer option.

### Declaring success too early

Several conclusions about fluidity were based on:

- DOM loading attributes.
- Discrete zoom-button traces.
- Screenshots after movement.
- No console errors.

Those tests did not prove continuous 60 FPS or zero exposed pixels during a fast gesture. Continuous drag automation was also not always reliable in the in-app browser.

**Lesson:** the hard requirement needs actual frame-time and visual-coverage instrumentation, not just settled-state checks.

## Current state

| Area | Current condition |
|---|---|
| Global data | Global profile is the default |
| Res3 | Entire world preloaded as Arrow and retained; one exact res3 layer is used throughout coarse navigation |
| Res7 format | Compact H3 arrays from global aggregate Parquet |
| Res7 handoff | Later, smaller, and no longer blocks zoom |
| Res7 panning | Visible-first commit followed by a complete rendered two-tile guard; no uncovered samples in the current prescribed tests |
| Basemap | CARTO through MapLibre with static country fallback and `{ratio}` Retina tiles |
| 3D | Res3 and res7 cells remain exact and extruded; the reverted overview no longer changes geometry during zoom |
| Retina | Both canvases and CARTO tiles use a stable 2x policy; dynamic backing-store resizing was reverted after it stalled interaction |
| Antimeridian | Canonical coverage exercised in the browser; empty source tiles can still produce legitimate blank areas |
| Corners | Current 1 rem outer radius exists in CSS, but remains visually unresolved |
| FPS | Isolated zoom-10 regression: 17.6 ms P95 and 17.8 ms worst, with no long frames, gaps, or flashes; slight minimum-zoom pan stutter is accepted to preserve responsive zooming |
| Worktree | Very large and needs cleanup and splitting |

The coordinator comment now describes the rendered two-tile guard and its visible-first publication behavior.

## What should be done next

### 1. Stop performance changes — recommended

The minimum-zoom overview experiment improved its scripted pan trace but made real zoom interaction substantially worse. It has been completely removed. Do not implement the remaining proposed optimizations speculatively; the user's interactive experience takes precedence over the synthetic trace.

Keep the diagnostic scenarios and the following test policy for future changes. This is what the earlier phrase "cadence-relative acceptance budget" meant in plain language: compare the map with an empty map in the same browser session, because a nominal 16.7 ms threshold can be slightly below the browser's own frame cadence.

Keep the 16.7 ms target as a useful nominal reference, but gate acceptance against an empty-renderer control captured in the same visible, focused browser session. The 2026-08-23 isolation runs show that an absolute `P95 < 16.7 ms` rule can fail even when no map renderer draws. Define and test a relative allowance, while continuing to reject any doubled frame interval, long frame, long task, coverage loss, stale commit, or camera discontinuity.

The opt-in harness records:

- `requestAnimationFrame` frame intervals.
- Long tasks over 50 ms.
- P95 and worst frame time.
- Committed tile coverage during every animation frame.
- The exact camera path and loaded/committed tile counts.
- Res7 fetch calls, long tasks, resolution flashes, and loading frames.

Pair the result with screenshots or video that confirm Retina sharpness, true extrusion at the default 25-degree pitch, and no visible coverage gap. The current programmatic camera path is deterministic and removes input-coalescing variance; a separate fixed-pointer trace can be added later if pointer-event overhead itself becomes the question.

Suggested acceptance criteria to formalize:

- Full-map P95 remains within a small documented allowance of the same-session empty control.
- No doubled refresh interval on the prescribed path.
- No frame where committed priority coverage is false during the prescribed gesture.
- No camera discontinuity.
- No stale generation commit.
- No res3 flash while res7 coverage should remain available.

DOM coverage remains a source-tile invariant rather than pixel inspection, so screenshots or video remain necessary for a true visual assertion.

Do not return to res3 network tiles: preloading and retaining the compact global snapshot remains one of the successful architectural choices. Do not retry backing-store resizing or res3 chunking without new profiling evidence.

### 2. Consider directional overscan only if measurements require it — deferred

The two-tile symmetric guard currently eliminates observed coverage gaps, but it raises the fine generation from roughly four visible tiles to as many as 36 rendered tiles. The cadence-limited automated run showed no loading-versus-repeat difference, so there is not yet evidence to change it. If a cadence-valid trace shows that buffer upload or steady-state rendering is expensive, compare it with a motion-predicted buffer that is larger in the direction of travel and smaller behind the camera.

Do not make this change without a repeatable gesture trace and before/after frame-time measurements.

Coverage stayed complete in the failing high-DPI trace, and the worst cached repeats performed no loading. Directional overscan is therefore lower priority than reducing steady-state res3 rendering.

### 3. Move res7 away from request-time DuckDB — deferred backend work

Compact JSON helped, but dynamic serving still:

- Opens DuckDB per tile.
- Queries Parquet.
- Computes H3 tile membership.
- Applies boundary filtering.
- Serializes JSON.

For the final architecture, build immutable binary res7 artifacts from the current 121 aggregate partitions. The existing optional `global-res7-tiles` pipeline is the starting point, but its schema and metadata output must be validated before use.

Boundary membership should be baked into those aggregates so filters do not require request-time geometric work.

### 4. Fix metadata and HTTP caching — deferred backend work

The 2 MB metadata file is still read, parsed, modified, and serialized on each request. It also scans res7 source coverage.

It should be:

- Loaded and validated once.
- Regenerated only when the aggregate directory version changes.
- Served with an ETag.
- Revalidated cheaply.

PMTiles range responses and res7 responses also need stronger immutable or versioned caching and ETags.

### 5. Fix corners as a separate CSS task — unrelated

Use a dedicated outer `.map-frame` wrapper with:

- Border radius.
- `overflow: hidden`.
- No clip-path on MapLibre or Deck canvases.
- Browser verification that drag, controls, and fullscreen still work.

The previous aggressive clip-path affected too many interactive renderer elements at once.

### 6. Profile steady-state rendering — only after new evidence

The likely remaining steady-state candidates are:

- The global 40,295-cell high-precision extruded res3 layer.
- Picking passes.
- CARTO raster compositing.
- Multiple Deck and WebGL canvases.
- Layer reconstruction after state changes.

These should be measured before changing them. The first 30-degree res3 chunk experiment did not improve P95, so do not revisit chunking without a profile showing a res3-specific cost or a materially different culling design. Do not return to res3 network tiles.

### 7. Add antimeridian visual tests — useful regression hardening

Test both sides of plus or minus 180 degrees with:

- Basemap only.
- Res3 only.
- Res7 only.
- Repeated world copies.
- Several zoom levels.

That will identify whether any remaining seam belongs to MapLibre raster rendering, exact res3 polygons, or approximate res7 instances.

### 8. Clean and split the worktree — release cleanup completed

The August 2026 cleanup:

- Separate global dataset configuration.
- Separate res3 Arrow snapshot and preloading.
- Separate compact res7 backend.
- Separate coordinator and rendering changes.
- Separate boundary, species, and table changes.
- Removed tracked databases and kept local copies under ignored `data/private/`.
- Removed the Flask/templates/notebook runtime and obsolete diagram assets.
- Replaced MOV tutorial files with smaller MP4 files.
- Excluded generated global artifacts and rebuildable boundary partitions.
- Added CI, a release-content guard, data policy, credits, and a publication
  checklist.

The feature rewrite is still a broad change and should be reviewed as a global
release rather than represented as a small map-only patch. Restricted database
objects remain in public Git history until an administrator performs the
coordinated rewrite described in `docs/publication_checklist.md`.

## Revisit protocol

Treat this document as the map-performance decision log, not a one-time postmortem. Revisit it at these checkpoints:

- Before choosing the next optimization, to confirm that the problem is still current.
- After every browser-verified experiment, including reverted experiments.
- Whenever a measured result changes the priority order above.
- Before merging or splitting the map work, so the current-state table matches the code being reviewed.

For each material experiment, record:

1. The hypothesis and the one isolated change.
2. The dataset profile, camera path, and browser conditions.
3. Before-and-after frame, coverage, tile, cell, and network measurements that are available.
4. The visual result, including legitimate empty-data areas that could resemble rendering gaps.
5. The decision to keep, revise, or revert the change.

## Continuation log

### 2026-08-23 — stable Retina baseline resumed

Before reopening the browser experiment, the current frontend state was revalidated:

- All 12 coordinator and performance-summary tests passed.
- `svelte-check` reported zero errors and zero warnings.
- The production build completed successfully.
- The existing map-route size warning remains: the largest client route chunk is approximately 558.48 KB gzip.

Inspection before the browser run found that the adaptive backing-store policy described by an earlier version of this log was no longer present in the implementation. It had been reverted after resizing the two WebGL canvases during interaction stalled the camera and could hang the tab. The implementation under test uses a stable 2x ratio for MapLibre, Deck, and CARTO Retina tiles.

The canonical three-run trace then completed in a visible, focused, cadence-valid browser session:

- Idle baseline: 16.7 ms, approximately 59.88 Hz; `supports60FpsBudget` was true.
- First path: 120 frames, 17.6 ms P95, 32.9 ms worst frame, 18 res7 requests, and 19 loading frames.
- Two cached repeats: 17.6 ms median P95, 17.7 ms worst P95, 17.8 ms worst frame, zero res7 requests, and zero loading frames.
- All three runs: zero long frames over 50 ms, zero long tasks, zero uncovered frames, and zero resolution flashes.
- Every run kept all 36 committed guard tiles and moved the same 1.13 degrees.
- Both renderer ratios remained 2 throughout every run, as expected for the current stable policy.

This result is materially better than the earlier pathological always-2x session, but it still misses the strict 16.7 ms P95 target by approximately 0.9–1.0 ms. Because cached repeats were essentially identical to the loading run, request-time res7 work is again not the leading explanation. The next isolated experiment is spatially chunking the rendered global res3 indices while retaining the complete Arrow snapshot in memory.

### 2026-08-23 — res3 spatial-chunk experiment reverted

Hypothesis: the flat zoom-10 boundary trace is paying to draw all 40,295 high-precision, extruded res3 cells even though only a small geographic subset intersects the viewport. Partitioning the in-memory indices into stable coarse geographic chunks and marking offscreen chunks invisible should reduce fragment and geometry work without reintroducing network tiles or changing the source snapshot.

This experiment changes only res3 layer partitioning and viewport visibility. The stable 2x Retina policy, two-phase res7 coordinator, camera path, tile guard, and acceptance criteria remain unchanged. It will be kept only if the same three-run trace improves P95 without coverage or interaction regressions.

Implementation and verification:

- The 40,295 in-memory indices were partitioned into 72 stable 30-degree chunks.
- The trace viewport selected one visible chunk, while all source cells remained in the Arrow snapshot.
- Three focused unit tests verified exact index retention, viewport selection, and antimeridian selection; all 15 frontend tests passed and `svelte-check` remained clean.
- The browser reported a visible, focused 16.6 ms baseline, approximately 60.24 Hz, with `supports60FpsBudget` true.
- First path: 17.7 ms P95 and 17.7 ms worst frame, with 18 res7 requests and three loading frames.
- Cached repeats: 17.6 ms median P95, 17.6 ms worst P95, and 17.7 ms worst frame, with zero requests or loading frames.
- All three runs retained 36 fine tiles and complete coverage with zero flashes, long frames, or long tasks.

The experiment removed the baseline run's isolated 32.9 ms worst frame, but did not improve P95 and therefore failed its stated keep criterion. Visual inspection showed the expected map and cell overlay, although a full-page browser capture duplicated a hardware-rendered map region and was not suitable as a pixel-perfect artifact. The chunking code and its experiment-only tests were reverted; this decision log retains the result.

### 2026-08-23 — renderer isolation completed

Four opt-in variants ran the identical three-run trace in visible, focused, cadence-valid sessions. Each retained the same camera path, res7 requests, guard readiness, and performance sampling; only renderer visibility changed.

| Variant | Baseline | First P95 / worst | Repeat P95 / worst | Long frames / tasks | Coverage failures |
|---|---:|---:|---:|---:|---:|
| Full | 16.7 ms | 17.7 / 17.7 ms | 17.6 / 17.8 ms | 0 / 0 | 0 |
| Basemap only | 16.7 ms | 17.6 / 17.7 ms | 17.6 / 17.7 ms | 0 / 0 | 0 |
| Cells only | 16.7 ms | 17.6 / 17.7 ms | 17.6 / 17.7 ms | 0 / 0 | 0 |
| Empty | 16.6 ms | 17.1 / 17.7 ms | 16.8 / 17.7 ms | 0 / 0 | 0 |

All first paths issued 18 res7 requests; all cached repeats issued zero. The full map adds approximately 0.6 ms to first-path P95 and 0.8 ms to repeat P95 relative to the empty control, but it does not create a doubled interval or a worse worst-frame band. Basemap-only and cells-only results are indistinguishable at the trace's current resolution, and the complete map is not slower than either in a way this harness can resolve.

Decision: keep the opt-in `mapPerfRenderer` diagnostic because it provides the missing control, retain the stable full renderer, and stop optimizing from the absolute 16.7 ms number alone. The empty control's 16.8 ms repeat P95 proves that `P95 < 16.7 ms` is not a reachable literal threshold in this browser session. Formalize a same-session, cadence-relative budget before selecting another production optimization.

After retaining the diagnostic, all 12 frontend tests passed again, `svelte-check` reported zero errors and warnings, and the production build completed. The existing large map-route warning remains at approximately 558.62 KB gzip; the isolation switch does not create another route or normal-use bundle.

### 2026-08-23 — world-overview stutter investigation started

The remaining user-visible issue is slight stutter while panning at the minimum zoom. Before changing rendering, add a second deterministic trace that resets to the fully zoomed-out, pitched world overview and pans across repeated world copies. Run the existing full, basemap-only, cells-only, and empty controls against that exact path.

This phase is diagnostic only. It must not change res3 geometry, Retina resolution, extrusion, antimeridian behavior, res7 coordination, or normal map behavior. A production optimization will be considered only if the controls identify a renderer-specific cost and the same path can demonstrate a clear before-and-after improvement.

The opt-in `mapPerfScenario=world-overview` path now resets to zoom 0, pitch 25 degrees, and performs five fixed horizontal world pans. It deliberately does not wait for res7 because the overview remains at res3. The existing zoom-10 scenario stays the default. Two scenario-selection tests bring the frontend total to 14 passing tests, and `svelte-check` remains clean before the browser comparison.

The four cadence-valid controls isolate the regression to the global res3 cell pass:

| Variant | Baseline | First P95 / worst | Repeat P95 / worst | Long frames / tasks |
|---|---:|---:|---:|---:|
| Full | 16.6 ms | 33.4 / 34.0 ms | 33.4 / 33.9 ms | 0 / 0 |
| Basemap only | 16.7 ms | 17.7 / 17.7 ms | 17.6 / 17.8 ms | 0 / 0 |
| Cells only | 16.7 ms | 33.3 / 33.4 ms | 33.2 / 34.2 ms | 0 / 0 |
| Empty | 16.6 ms | 17.0 / 17.6 ms | 16.9 / 18.1 ms | 0 / 0 |

MapLibre constrains the effective minimum zoom to approximately 0.644 for the test viewport. Every run traversed the same 540 degrees across repeated world copies. Full and cells-only rendering consistently fall to approximately 30 FPS, while basemap-only and empty remain near 60 FPS. No variant recorded a long task, so the stutter is sustained rendering load from the res3 cell layer rather than request work or an intermittent JavaScript pause.

Before choosing a production change, compare diagnostic-only res3 variants on the same cells-only path: exact versus instanced H3 geometry, and extruded versus flat geometry. Normal visits must continue using the current exact, extruded layer until both performance and visual seam checks support a change.

The diagnostic variants identify extrusion as the expensive property:

- Approximate instanced geometry remained slow: 33.6 ms first-path P95 and 33.4 ms repeat P95. It also introduced one 66.7 ms frame. This rules out exact polygon construction as the primary sustained cost and provides no reason to accept the known global-distortion risk of low-precision res3 geometry.
- Exact but flat geometry returned to near-60 FPS pacing: 17.0 ms first-path P95 and 17.3 ms repeat P95, with 17.6 and 18.6 ms worst frames and no long frames or tasks.

Do not remove 3D cells permanently. The next isolated experiment is to keep exact res3 polygons, temporarily omit their side walls only during motion below a low zoom threshold, and restore true extrusion after a short settled delay. It must be tested as an opt-in diagnostic first, including the cost and visual continuity of the flat-to-extruded restoration.

That motion-only flattening experiment was rejected immediately. Panning returned close to the refresh cadence, but rebuilding the extruded global layer after each settled run produced 18.6–18.7 ms P95, 166.7–202 ms worst frames, three long frames, and three long tasks. The map did restore `data-cells-extruded="true"`, but the restoration pause is worse than the original sustained stutter.

Do not toggle the 40,295-cell layer between flat and extruded states. The next diagnostic is a separate, continuously extruded res2 overview derived from the resident res3 snapshot. Roughly one res2 parent exists for each seven res3 cells, so this can reduce the low-zoom prism count to approximately 5,800 without canvas resizing or post-pan geometry rebuilding. Keep it opt-in until its performance, color aggregation, zoom transition, selection behavior, and antimeridian appearance are verified.

The diagnostic overview contains 5,782 exact, extruded res2 parents and eliminates the sustained 30 FPS state:

- Cells only: 17.2 ms first-path P95 and 17.4 ms repeat P95, with 17.6–17.7 ms worst frames.
- Full map: 17.4 ms first-path P95 and 16.8 ms repeat P95. One cached run contained a 33.4 ms interval, but there were no long frames or tasks.
- Every run remained cadence-valid, traversed the same 540 degrees, and reported `data-cells-extruded="true"`.

The overview screenshot showed continuous repeated worlds, exact polar boundaries, and the expected global priority pattern without an obvious antimeridian band. Its res2 cells are visibly coarser, so it should be restricted to the minimum-zoom overview rather than replacing res3 generally. The next step is an opt-in zoom-threshold candidate that keeps both exact layers stable, shows res2 only below the threshold, and switches back to res3 before ordinary navigation and picking.

The first threshold-candidate trace produced an anomalous 66–83 ms P95 result after a long sequence of experiments in the same browser tab. It contradicted the static overview result and was not reproducible in a fresh tab: the fresh static overview recorded 17.6 ms repeat P95, and the threshold candidate recorded 16.8 ms repeat P95 with a 17.7 ms worst frame and no long frames or tasks. Treat the contaminated run as browser/GPU-state evidence, not as a production conclusion.

The threshold candidate now needs a dedicated rapid zoom-reversal trace. It must repeatedly cross the res2/res3 boundary so any buffer initialization, visibility swap, color discontinuity, or delayed interaction becomes part of the measured result rather than being hidden before the pan trace starts.

The five-reversal stress trace is expensive with or without the candidate. The existing res3 path recorded 82.3 ms first-path P95 and 116.3 ms repeat P95; the threshold candidate improved those to 65.7 and 100 ms respectively. It therefore does not introduce a new zoom-transition regression, but it does not solve rapid programmatic zoom reversal either. Both paths traversed the same 6.5 zoom levels and 13.15 degrees of camera adjustment.

Visual checks in both directions showed that zoom 0.70 uses the 5,782-cell overview and zoom 1.70 restores the exact extruded res3 layer. No blank settled state or antimeridian band was observed. The expected tradeoff is visible coarser res2 boundaries and no individual-cell picking at the minimum zoom; ordinary zoom, extrusion, and picking return to res3 above the 1.25 threshold.

One final exact-res3 diagnostic disabled material lighting while keeping all 40,295 prisms. Its first-path P95 remained 33.8 ms in a fresh tab and later runs became unstable, so lighting is not the leading cost. Reject that variant along with instancing and motion-time flattening.

The first final-verification run showed that the extruded res2 overview was not sufficiently robust: it began at 34.3 ms P95 and degraded to 66.2 and 216.5 ms P95 over three repeated pans. The matching empty control remained stable at 17.0–17.3 ms P95, proving that the result was overview geometry cost rather than browser cadence. At world scale, the res2 side walls are sub-pixel detail, so flattening only this continuously mounted overview is a much narrower change than toggling the 40,295-cell res3 layer after every gesture.

Decision: promote the exact, flat res2 overview only below zoom 1.25. Remove the rejected diagnostic rendering branches, leave the exact extruded res3 and res7 paths untouched, and retain the world-pan and zoom-reversal traces for regression testing.

### 2026-08-23 — minimum-zoom fix finalized and verified

The accepted production path derives 5,782 res2 parent cells from the already resident 40,295 res3 indices. It performs no extra request, preserves the existing score domain by averaging included child scores, honors ecosystem, boundary, normalization, and species-highlight state, and mounts exactly one coarse layer at a time. Below zoom 1.25 the overview is exact but flat and non-pickable; above the threshold, the original exact, extruded, pickable res3 layer returns.

The final visible, focused, cadence-valid three-run world-pan trace produced:

- 16.8, 17.3, and 17.4 ms P95.
- 33.3 ms first-path worst frame and 17.6 ms worst frames on both repeats.
- Zero long frames, long tasks, uncovered frames, and resolution flashes.
- The same 540-degree camera travel on every run, with no res7 request or loading work.
- `data-overview-active="true"`, 5,782 overview cells, and `data-overview-extruded="false"` throughout the minimum-zoom trace.

A visual check showed continuous repeated worlds, the expected priority colors, and no obvious antimeridian band. One zoom-control step reached zoom 1.64, changed `data-overview-active` to false, and restored the ordinary exact, extruded res3 map.

Back-to-back WebGL stress traces can contaminate later measurements in the shared browser GPU process. In the saturated process, a three-run zoom-10 batch degraded to 66–100 ms P95, but the supposedly empty matching control also later degraded to 115.8 ms P95. After closing every map tab and allowing a 45-second cooldown, an isolated unchanged zoom-10 run returned to 17.6 ms P95 and 17.8 ms worst with zero long frames, long tasks, gaps, or flashes. Future performance sessions should start from a clean WebGL context, pair full runs with empty controls, and avoid interpreting a late saturated run in isolation.

Final code validation passed 17 frontend tests across four files, including exact aggregation coverage and antimeridian parents. `svelte-check` reported zero errors and zero warnings. The production build completed successfully after the final overview change; the existing large map-route warning remains at 559.08 KB gzip.

### 2026-08-23 — minimum-zoom experiment fully reverted

Real interaction overruled the synthetic world-pan result: the overview threshold caused much worse lag while zooming. The entire experiment was rolled back rather than adjusted further.

The rollback removed:

- The derived res2 overview cells and aggregation module.
- The zoom-1.25 layer swap and all overview state from `Map.svelte`.
- Flat, non-pickable overview geometry and its diagnostic DOM attributes.
- The world-overview and rapid zoom-reversal performance scenarios.
- The aggregation and scenario-selection tests added for the experiment.

The map again mounts the original exact, extruded, pickable res3 layer throughout coarse navigation. The established res7 coordinator, stable 2x Retina policy, zoom-10 trace, and renderer-isolation controls were not part of the experiment and remain unchanged. After the rollback, all 12 pre-experiment frontend tests passed, `svelte-check` reported zero errors and zero warnings, and the production build completed successfully. The map route returned to approximately 558.63 KB gzip.

### 2026-08-26 — GitHub release-cleanup regression check

Before the release cleanup, an isolated cadence-valid run established the
comparison point: 59.88 Hz idle cadence, 17.6 ms first and repeat P95, zero long
frames/tasks, zero uncovered frames, and zero resolution flashes. The first
path requested 18 res7 tiles; cached repeats requested none.

After the cleanup, the shared in-app browser was paced at 30.03 Hz, so its
absolute frame values are not a valid 60 FPS comparison. Two full-map batches
nevertheless retained all 36 guard tiles, 5,297–5,345 fine cells, zero uncovered
frames, zero flashes, and zero browser warnings/errors. The stable batch
recorded 34.3 ms P95 and no long frames/tasks; its first path made 18 res7
requests and observed three background-loading frames, while both cached
repeats made zero requests and observed zero loading frames.

The same-session empty-renderer control recorded 35.2–35.3 ms P95—slightly
slower than the full map—with the same 33.3 ms idle cadence and no long
frames/tasks. This confirms that the post-cleanup 34 ms measurement is the
browser's 30 Hz scheduling band, not added map work. It does not replace the
cadence-valid 17.6 ms baseline, but it provides no evidence of a cleanup
regression and preserves the user-visible no-gap/no-flash guarantees.

The real UI check also confirmed that coarse data and the basemap become ready,
the duplicate EEZ filter is absent, Denmark changes the colour-domain maximum
from 4,106.3 to 177.4, and adding Copenhagen narrows it to 118.7. The release
validation finishes with 44 Python tests, 24 frontend tests, zero Svelte errors
or warnings, Ruff clean, a successful production build, and a map-route size of
approximately 559.99 KB gzip.

## Main conclusion

The core handoff behaves coherently in the tested global-data scenarios: visible res7 tiles commit atomically, a complete rendered two-tile guard follows, and obsolete work cannot replace the current camera generation. Stable Retina quality and exact, extruded, selectable cells remain throughout navigation. There is no longer an overview threshold or geometry swap during zoom.

Leave map performance here. The slight fully zoomed-out pan stutter is preferable to the overview experiment's much worse zoom lag. Backing-store resizing, res3 chunking, approximate global geometry, lighting removal, motion-time flattening, and the res2 overview were all rejected or reverted. The remaining backend and caching proposals may still be worthwhile as separate maintenance work, but they should not be bundled into this performance work.
