# Data download and updates

For agents using browser or computer control, follow the short
[IUCN browser-download instructions](03_iucn_browser_download.md).

Ark-IV exposes two data-acquisition commands:

```console
just download
just update
```

`just download` bootstraps every missing required source. `just update` checks
the same complete source set and refreshes only releases that are due. Both
commands validate existing files before doing anything, so either command is
safe to rerun. Current files are not downloaded or copied again.

For the complete build through spatial processing, metadata, metrics and map
tiles, use `just data-build` or `just data-update`. These refresh the species
crosswalk after acquisition and before spatial pair processing. Preview the steps
with `just data-build --dry-run`. A missing-file or authorization action returns exit code 2,
so dependent stages stop. `just data-status` inspects readiness without updating
anything. See [the complete workflow](01_data_pipeline.md).

## Storage and setup

Set a writable large-data directory in the ignored `.env` file:

```dotenv
GLOBAL_DATA_ROOT=/path/to/Ark-IV_data
IUCN_DATA_AUTHORIZED=false
```

Without configuration, the portable data root is `./data/external`. A complete
raw acquisition currently needs about 39 GB, while derived map products need
additional space. The active manifest is
`$GLOBAL_DATA_ROOT/acquisition/current.json`; immutable source snapshots and
historical manifests sit below the same acquisition directory.

## First download

Run:

```console
just download
```

The coordinator downloads missing public sources, including HydroBASINS, GoaT,
NCBI Taxonomy, and the pinned GBIF Backbone. It then checks the restricted IUCN
sources.

IUCN does not provide a supported bulk spatial API. If authorized files are
missing, the command creates their exact destination directories and writes:

```text
$GLOBAL_DATA_ROOT/acquisition/action-required.json
```

That plan contains the configured release, official page, exact filenames,
download URLs where available, and destination for every outstanding IUCN
source. The account holder must log into IUCN, accept its terms, submit a
truthful intended-use statement, and download the files through the official
website route.

Keep browser downloads on the large-data disk. The spatial polygon archives
alone are about 33 GB. Do not commit raw IUCN files or credentials.

Once the files are present, record the account holder's authorization in
`.env`:

```dotenv
IUCN_DATA_AUTHORIZED=true
```

Then rerun the same command:

```console
just download
```

It detects the staged files, validates them, and registers them in place. No
separate target-listing, registration, or doctor command is needed.

## Updates

Run:

```console
just update
```

The coordinator:

1. Validates every registered required source.
2. Skips public sources that are still within their update interval.
3. Uses HTTP validators when the provider supplies reliable ones.
4. Compares versioned inventories, such as HydroBASINS v1c, with the registered
   release and downloads only when they differ.
5. Checks the configured IUCN release against the registered IUCN sources.
6. Automatically validates and registers a newly staged authorized IUCN
   release.

If `IUCN_REDLIST_KEY` is configured, `just update` also refreshes the optional
IUCN API assessment catalogue used to monitor Red List versions. Without that
token, the checked-in IUCN release remains the update boundary and the operator
must compare it with the release shown on IUCN's website before changing the
inventories.

## Idempotence and failure behavior

Every registered file has a byte size and SHA-256 checksum. Sources can also
declare column, archive-member, and exact-size requirements. Downloads are
written to a staging directory first; the active manifest changes only after
the complete source passes validation.

For IUCN, the configured inventories currently describe Red List 2026-1:

- 30 polygon archives;
- 17 point archives;
- 14 HydroBASINS relationship-table archives;
- the authorized assessment and taxonomy tables.

HydroSHEDS HydroBASINS geometry is a separate public source: standard revision
v1c, all nine regions, levels 1-12. Its server does not provide useful stable
HTTP validators, so its versioned inventory is authoritative. Once v1c is
registered, both commands report it as `current` without network transfer.

Interrupted or invalid downloads never replace a working manifest. Rerunning
the same command resumes the workflow from its recorded state and reports one
consolidated status for all required sources.
