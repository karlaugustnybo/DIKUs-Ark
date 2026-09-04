# Ark-IV roadmap

Scientific and pipeline assumptions were reviewed on 3 September 2026; other
feature statuses retain their earlier implementation history. Priorities remain
provisional as the data model and conservation use cases are validated.

The [methodology](../methodology.md) describes the actual scientific and
implementation decisions across the pipeline. The [assumption audit](reference/roadmap_assumption_audit.md)
records 20 findings with evidence and acceptance criteria. Implemented behavior
is not automatically a scientifically validated assumption.

## Priority and status guide

- **P0 — Foundation:** Required for trustworthy results or for other features to work correctly.
- **P1 — High value:** Important improvements to the main conservation-analysis workflow.
- **P2 — Enhancement:** Useful additions that can follow the core work.
- **P3 — Nice to have:** Polish, outreach, or longer-term ideas.
- **Investigation:** The requirements or underlying assumptions still need to be verified.
- **In progress:** Implementation or validation is partly complete, with remaining work identified.
- **Proposed:** The feature is understood at a high level but has not been implemented.
- **External:** Progress depends on feedback, funding, or another outside contribution.
- **Completed:** The feature is already present in the current application.
- **Retired:** The item belonged to the prototype and is no longer part of the supported architecture.

## 1. Data correctness and prioritisation

### Validate H3 aggregation assumptions

**Priority:** P0 · **Status:** Implemented; taxonomy validation pending

The implementation is now checked: resolution 7 uses polygon any-touch
membership, including edge/vertex contact; resolution 3 uses distinct parents
of included fine cells. Neither is a point-observation or whole-cell occupancy
claim. Large v3 ranges may be simplified before intersection; 2.5-degree
computational tiles are separate from that approximation. The antimeridian
hole-placement bug is fixed.

Remaining P0 work is scientific calibration after that fix: measure per-species
omitted/added cells and local effects against an unsimplified reference with the
same v3 row policy, agree acceptance limits, and rebuild the timing fixture to
include v3-only eligible rows. Review planar repair-area thresholds and the
interpretation of coarse boundary filtering. See audit A01–A05 and A15.

### Verify complete and lossless data use

**Priority:** P0 · **Status:** Investigation

Selected pair/list relationships are reconciled, but complete source-field
preservation is not established. The methodology now records the principal
field transformations and omissions. Seasonal and spatial-evidence distinctions
disappear in species unions; several assessment/genomic fields remain unused.
Point and HydroBASINS memberships now enter the common pair pipeline, with
source-level receipts and decision summaries, but their coordinates/basin IDs
do not survive into serving lists. Finish a release-specific column/row lineage
audit and distinguish acquired, integrated, and served fields. See A06 and A18.

### Refine missing-DNA priority by family coverage

**Priority:** P0 · **Status:** Snapshot-validated; evidence-stage scoring pending

The rule searches all qualifying GoaT species, validates family/genus names
against unique rank identities in the registered NCBI taxdump, qualifies lineage
names by family and normalized kingdom, and never treats extinction as genomic
evidence. The fresh rebuild accepted 585 of 587 new family names, covering
11,427 species; Gonostomatidae and Cepheidae were rejected as multi-TaxID names.
Separate sample, project, assembly, quality and resampling score states remain an
open biological/product choice.
See A09–A11.

### Validate taxonomy transfer, DNA evidence, and priority weights

**Priority:** P0 · **Status:** Investigation

All automatic crosswalk branches now share the minimum-agreement and
maximum-conflict guard, and the downloader preserves generic field provenance.
Measure match precision independently of coverage. Review the broad
sample/project/assembly evidence predicate and the `has_ebp_criteria_evidence`
criteria-text check against a versioned assembly
standard. Keep IUCN Data Deficient separate from missing GoaT information.
Validate score objectives and ranking sensitivity before treating configurable
weights as conservation recommendations. Migrate the SIS-valued
`gbif_accepted_id` compatibility alias. See A07–A13.

The fresh crosswalk retains 35 safe qualifying rows with
`goat_resampling_required=DTOL`: 11 also have qualifying chromosome/BUSCO
assemblies and 24 have project/sample-stage evidence only. The marker is now
preserved in species metadata. A dedicated resampling score/filter remains to be
designed; treating it as absence of an existing genome would misclassify the 11
assembled species.

### Add population trends

**Priority:** P1 · **Status:** Proposed

Population trends are present in the registered assessment table but are not
currently used by the scoring model. Display increasing, stable, decreasing or
unknown with assessment date/scope, and review any score contribution before
adding it. See A18.

### Reintroduce EDGE enrichment as an optional source

**Priority:** P3 · **Status:** Proposed

Keep EDGE out of the required update pipeline while the core IUCN spatial and
assessment workflow is rebuilt. Reintroduce it later as optional enrichment for
the roughly 700 affected species, with a reviewed identifier join, source
version, and redistribution permission; its absence must never block a global
build.

### Add IUCN point data

**Priority:** P1 · **Status:** Integrated; evidence-type display and full-run validation pending

The IUCN point archives, HydroBASINS relationship tables, and HydroBASINS base
geometry are acquired, versioned, schema-checked, and included in the derived
H3 membership pipeline. Eligible points map vectorially to their containing
resolution-7 cell. Basin relationships are deduplicated out of core; each
referenced v1c basin is covered once and reused across species tables. Their
pairs are globally deduplicated with polygon pairs and therefore flow through
the existing metrics, API, tiles, map, status view, and run dashboard.

The integrated serving relation deliberately does not preserve evidence type.
A future provenance-aware map layer is still needed if users must distinguish
recorded point locations, basin distributions, and broader range polygons. Run
and review an authorized full global build before treating integration as a
validated release dataset.

### Add bird data

**Priority:** P2 · **Status:** External

Request access to the bird dataset linked from the IUCN website but distributed by a separate provider. Confirm its licensing and handling requirements before downloading it, then document how it complements the current sources and expose relevant bird-specific attributes in the map and table.

### Create a repeatable data-update pipeline

**Priority:** P2 · **Status:** Complete

The documented `just download` and `just update` coordinator incorporates new
source releases, skips current data, validates staged inputs, preserves
immutable manifests, and pauses cleanly for provider-authorized IUCN browser
downloads. Derived-data rebuilding and publication remain separate operations.

### Create a bring-your-own-data release profile

**Priority:** P0 · **Status:** Proposed

Keep the complete global map, species highlighting, cell details, and table
features in the public AGPL codebase without bundling restricted inputs or
generated serving data. A normal clone should run immediately with a small,
clearly labelled synthetic data pack, while authorized users can construct the
real global profile from files they obtained directly from the providers.

Build on the validated `just download`, `just update`, and `just start`
workflow. The remaining release profile must:

- identify every required input, version, and official download location
  without automating acceptance of provider terms;
- validate paths, schemas, identifiers, checksums, source versions, and licence
  acknowledgements before starting an expensive build;
- produce a versioned data-pack manifest containing source/build versions,
  checksums, attribution, schema compatibility, and validation results;
- keep all source files and generated Arrow, PMTiles, Parquet, database, and
  report artifacts ignored by Git;
- explain unavailable restricted features clearly and direct authorized users
  to the data-pack setup rather than silently degrading results; and
- support privately distributed prebuilt packs for authorized collaborators
  without using Git LFS or a public download URL.

Treat permission for a full public deployment as a parallel external track.
Send IUCN and ZSL a concrete disclosure of the non-commercial conservation use,
aggregated H3 fields, Arrow/PMTiles delivery, species/cell endpoints, absence of
bulk exports, attribution, and proposed downstream terms. Preserve the complete
feature set while awaiting their answer; introduce a reduced public API only
if the providers require it. The fallback public deployment should use the
synthetic pack, while the authorized local/private profile retains all features.

The real data pack must retain the current optimized serving formats and pass
the established map-performance trace: no uncovered viewport, no resolution
flash, no visible fine-tile loading gap, and no regression against same-session
idle cadence. Live per-user calls to the IUCN API are not a replacement for the
precomputed spatial pipeline.

### Complete global municipality coverage

**Priority:** P1 · **Status:** In progress

Global ADM2 acquisition/installation and country-scoped catalogues now support
the recorded 49,308-area snapshot across 180 available countries. The Denmark,
Germany and Sweden preview remains a code-only fallback. Remaining work is
coverage-gap, represented-year, overlap and disputed-boundary validation, with
consistent rebuilt memberships, filters and serving generations. ADM2 does not
mean municipality in every country. See A16.

## 2. Map analysis and interaction

### Preload resolution-3 map cells on initial site load

**Priority:** P1 · **Status:** Completed

The resolution-3 Arrow snapshot is loaded through a shared module promise. The
homepage schedules it only during browser idle time, while hover, focus, and
pointer-down on the map link warm it immediately. This avoids competing with
the first page render and reuses the same parsed snapshot on map navigation.

### Filter cells by jurisdiction

**Priority:** P1 · **Status:** Completed

Users can search and select up to 30 boundaries per framework. Every H3 cell whose polygon intersects a selected boundary is included; non-matching cells are removed from static and dynamic map layers without drawing a separate boundary overlay. The colour scale uses selected resolution-3 subsets, and the same spatial scope filters the species table. Municipalities, EEZs, and terrestrial ecoregions are currently enabled, while the Protected Planet adapter remains source-required.

### Investigate municipality normalization

**Priority:** P0 · **Status:** Completed

Score-domain selection is now a pure, tested function shared by the map. It
uses the local municipality domain, unions selections within a framework,
intersects frameworks with the tightest maximum, supports species-count
normalization, and rejects missing metadata instead of silently using a global
fallback.

### Incorporate ocean waters into the country filter and remove the EEZ filter

**Priority:** P1 · **Status:** Completed

The jurisdiction build unions Natural Earth Admin-0 land with Marine Regions
World EEZ v12 memberships, so selecting a country includes its ocean scope by
default. EEZ remains available internally for membership and selected-cell
context, but its duplicate filter tab is hidden by manifest configuration.

### Show selected species at resolution 3

**Priority:** P1 · **Status:** Completed

Selecting a species highlights its compact inverse resolution-3 coverage. Fine
cells map to their resolution-3 parent while the overview is visible, so the
distribution persists across the resolution handoff without inverting the
30-billion-row fine relationship dataset.

### Search for H3 cells on the map

**Priority:** P1 · **Status:** Proposed

Species search and map highlighting are complete. The remaining work is to let
users enter a specific H3 index, validate it, navigate to its centre, select the
cell, and show a clear error for invalid or unavailable indexes.

### Switch to resolution 7 at a lower zoom level

**Priority:** P1 · **Status:** Proposed

Evaluate switching from resolution-3 to resolution-7 cells sooner while zooming in, so users can see and compare more resolution-7 cells at once. Choose the transition threshold based on readability, rendering performance, and consistency with species highlighting and cell selection.

### Restore hover and selected-cell statistics

**Priority:** P0 · **Status:** Completed

Map hover and click picking now handles both typed resolution-3 snapshots and ordinary resolution-7/MVT objects. Tooltips and selected-cell statistics work with or without jurisdiction filtering and while species highlighting is active. The statistics panel always identifies the cell's native H3 resolution and shows fixed text rows for country, state/region/province, municipality, conservation framework, and any additional displayed intersections. Boundary name catalogues load independently of filters, and changing a spatial filter no longer clears the selected cell or its location context. A selected cell retains its exact statistics across zoom handoffs, with resolution-7 selections projected to their resolution-3 parent for the coarse-map highlight. The typed-array picking path has regression coverage, and superseded detail requests are cancelled so rapid selections cannot publish stale context.

### Normalize colours by species count

**Priority:** P1 · **Status:** Completed

The colour menu includes an optional species-count normalization mode. It uses
precomputed normalized global and jurisdiction domains consistently across
coarse, static, and dynamic cell layers.

### Add an accessible colour mode

**Priority:** P1 · **Status:** Completed

The paint-bucket control opens a compact side menu with the original Turbo scale plus Viridis, Cividis, and Inferno colour-vision-accessible alternatives and the species-count normalization switch. The custom controls expose their state to assistive technology, have visible keyboard focus, close with Escape or an outside click, and apply every palette consistently to coarse, static, and dynamic map-cell layers.

### Add a high-resolution map image download

**Priority:** P2 · **Status:** Proposed

Add a download-image button to the map that exports the current map view as a retina-resolution image. The export must contain the complete rendered map, including the background map and active data overlays, but exclude all buttons, tooltips, panels, and other interface controls. Keep the button disabled while map tiles or layers are still loading, and only capture the image after the background map and overlays for the current view have finished rendering at the export resolution.

## 3. Interface and navigation

### Unify shadows, animations, menus, and component styling

**Priority:** P2 · **Status:** Proposed

Apply a consistent visual and interaction system across the application. Standardize elevation and shadow levels, animation timing and easing, menu opening and closing behaviour, hover and focus states, spacing, borders, and control styling. Menus should support keyboard navigation, close with Escape or an outside click, behave consistently across screen sizes, and respect reduced-motion preferences. Reuse shared design tokens and component patterns so new and existing interfaces feel cohesive without obscuring content or slowing interaction.

### Show the resolution-7 cell count on the homepage

**Priority:** P2 · **Status:** Completed

The homepage's “At a Glance” statistics include the total number of resolution-7 H3 cells available in the dataset, formatted consistently with the other headline counts.

### Explain unavailable detailed species lists when raw data is absent

**Priority:** P1 · **Status:** Proposed

Add a small error/explanation screen for features that require the generated resolution-7 cell/species partitions when those files are unavailable, as they will be after a normal Git clone because the approximately 18 GB dataset is intentionally kept outside the repository. Show the screen only when a user requests a dependency-backed feature, such as the full species-detail table for a selected resolution-7 cell. The global map, tooltips, aggregate conservation metrics, and any other features that do not use the raw partitions should continue to work normally and should not display the warning.

### Replace regular-expression search with user-friendly text search

**Priority:** P1 · **Status:** Completed

Species and family queries are ordinary text, normalized for case and accents,
and safely escaped. Indexed exact, prefix, substring, and trigram tiers rank
results without regular-expression execution. Tests and the production-data
benchmark cover special characters, malformed input, ranking, and latency.

### Improve the species table

**Priority:** P2 · **Status:** Proposed

Review the main species table and the cell-details table on the map page for layout, sorting, filtering, pagination, responsive behaviour, and clear presentation of threat, DNA, and priority values. Give both tables consistent styling and add the relevant shared features while retaining controls specific to each context.

### Add contextual links

**Priority:** P2 · **Status:** Completed

Species and selected-cell tables link only when the build has an exact,
source-specific IUCN, GBIF, or GoaT identifier. Missing and ambiguous matches do
not receive guessed URLs.

## 4. Testing and quality assurance

### Make local development ports collision-resistant

**Priority:** P2 · **Status:** Completed

The local launcher now keeps the configured frontend and API ports when they
are available and allocates distinct free replacements when they are occupied.
It exports one set of session-scoped assignments to the service URLs, frontend
proxy, API origin, and CORS configuration, prints both final URLs, and starts
Vite in strict-port mode so neither service can silently drift.

### Strengthen automated testing and CI

**Priority:** P0 · **Status:** Completed

The repository has synthetic Python fixtures for builds, scoring, taxonomy,
ports, API and fine-tile behavior; frontend tests cover search, source links,
map coordination, performance aggregation, and colour domains. GitHub Actions
runs Ruff, Python tests, Svelte checks, frontend tests, the production build,
and the prospective-release content guard for every push and pull request.

## 5. Documentation and communication

### Improve project documentation

**Priority:** P1 · **Status:** Completed

The README now distinguishes code-only setup from authorized data builds and
links to the serving pipeline, source inventory, boundary semantics, schema,
performance method, data policy, credits, and publication checklist.

### Update the E/R diagram and table screenshots

**Priority:** P2 · **Status:** Completed

The obsolete prototype PNGs were removed and replaced with a version-controlled
Mermaid serving-schema diagram derived from `backend/schema.sql`.

### Add map images to the README

**Priority:** P2 · **Status:** Proposed

Include current screenshots that demonstrate the map, controls, cell details, and overall visual result without requiring readers to run the application first.

### Convert tutorial media to MP4

**Priority:** P3 · **Status:** Completed

Tutorial clips are MP4 and the Svelte tutorial page supplies browser-native
controls and fallback copy. Contributor ownership still has to be confirmed in
the publication checklist before the first public release.

### Update the marimo notebook

**Priority:** P2 · **Status:** Retired

The prototype notebook and saved UI layouts were removed. The tested Python
build modules, validation reports, schema, and pipeline documentation are now
the reproducible explanation of the global system.

## 6. Research, review, and sustainability

### Decide how to serve detailed resolution-7 species lists

**Priority:** P1 · **Status:** Investigation

The compact resolution-7 aggregate partitions are sufficient for rendering the global map, tooltips, and conservation metrics. The separate raw cell/species partitions are only required when a user selects a resolution-7 cell and requests its full species-detail table. The current metadata completeness check also compares the raw and aggregate partition sets, but that dependency could be replaced by a small build manifest.

The raw dataset currently contains 95,984,189 cells and 30,883,702,920 cell/species relationships across 121 Snappy-compressed Parquet files. It occupies 19,073,649,372 bytes (17.76 GiB), of which approximately 97.9% is species-list data. It is already compact at roughly 0.60 compressed bytes per relationship, so changing integer widths alone is unlikely to save much space.

Before production hosting, decide whether full per-cell species lists are essential. If they are optional, omit the raw partitions and retain the complete map and aggregate-statistics experience, saving 17.76 GiB of deployed storage. If they are required, benchmark representative low-, medium-, and high-diversity partitions before choosing among these exact-data serving approaches:

- Recompress the Parquet partitions with Zstandard for a relatively simple, compatible reduction.
- Store unique species lists once and map cells to list identifiers; samples show substantial repetition in some regions, although high-diversity regions contain many unique lists.
- Investigate spatial or hierarchical delta encoding, storing only species additions and removals between related cells.
- Keep the partitions in object storage and retrieve the relevant base-cell partition on demand, reducing local server storage without reducing total stored data.

Any replacement must preserve exact selected-cell results and be measured for lookup latency, compressed size, memory use, build time, and operational complexity. Keep these generated datasets outside Git rather than committing them to the application repository.

### Identify new data insights

**Priority:** P2 · **Status:** Investigation

Explore the combined datasets for additional conservation questions, useful derived metrics, and patterns that could become new views or features. Define each insight as a concrete research question before implementation.

### Request biology-domain review

**Priority:** P1 · **Status:** External

Ask a biology/conservation reviewer to assess the explicit decisions in the
[methodology](../methodology.md) and [assumption audit](reference/roadmap_assumption_audit.md):
range-versus-occupancy interpretation, geometric error limits, taxonomic transfer,
DNA evidence stages, extinction handling, family representation, missingness,
priority weights and ranking sensitivity. No external review was performed by
the code/test audit, and no reviewer has approved those choices merely because
the implementation passes tests.

### Secure server-hosting funding

**Priority:** P2 · **Status:** External

Estimate production hosting and data-storage costs, then identify funding or institutional infrastructure that can keep the public application reliably available.
