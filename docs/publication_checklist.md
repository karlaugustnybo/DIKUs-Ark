# GitHub publication checklist

Run this checklist before making a release, tagging a version, or deploying a
public data snapshot.

## Required before the next public push

- [x] Select `AGPL-3.0-only`, add the official root `LICENSE`, and declare it in
  both package manifests.
- [x] Record agreement from every existing copyright holder—at minimum, confirm
  Daniel Jensen's initial contribution—before representing all existing code as
  AGPL-licensed.
- [ ] For a binary, container, or hosted frontend release, generate and retain
  the third-party software notices for the exact resolved dependency set.
- [x] Confirm that every tutorial MP4 and image was created by the contributors
  or is covered by a compatible licence.
- [ ] Run `just check` and retain the successful output.
- [ ] Review `git status --short` and `git diff --cached` before committing.
- [ ] Run `just release-history-check` and investigate any affected ref before
  every public push.
- [ ] Compare current provider terms with `DATA_POLICY.md` and `NOTICE.md`.
- [ ] Confirm that every public deployment offers the corresponding source for
  its running version through the persistent Source link required by AGPL §13.

## Resolved public-history incident

On 4 September 2026, the repository history was rewritten to remove
`data/Ark-IV.duckdb` and `data/precomputed_cache.duckdb` from affected refs.
The databases contained IUCN/EDGE-backed records without an established public
redistribution grant. The cause, scope, remediation, residual risk, and
required re-clone procedure are preserved in
`docs/reference/restricted-data-history-incident.md`.

Do not merge or push branches from a clone made before the cleanup. Re-clone
instead, because an old ref can reintroduce the removed objects. If the history
audit fails, stop the release and identify the stale ref before pushing.

## Data snapshot review

Treat a hosted data snapshot as a separate release from this code repository.
For each source, record its exact version, access date, terms, citation, written
permissions, derivative status, and commercial-use limitations. Do not publish
until the responsible reviewers can answer “yes” to every item in the derived
data rule in `DATA_POLICY.md`.

The browser-facing surface is part of that review. Audit at least:

- resolution-3 Arrow snapshots and any PMTiles URL;
- dynamic resolution-7 tile responses;
- species search, suggestions, and identifiers;
- per-species H3 cell lists and per-cell species lists; and
- any endpoint or object-store URL that permits bulk or repeated retrieval.

The obsolete Parquet export catalogue and `/exports/{filename}` route were
removed before publication. Do not restore a bulk-data route without a
separately approved data release.

For IUCN-backed output, ask IUCN to confirm in writing whether this precise
aggregation and API surface is an acceptable derivative work, and how the
non-commercial condition and citation must be presented. Until then, keep the
data-bearing service private even when the code repository itself is public.
For basin-derived output, separately verify the then-current HydroBASINS product
license, required attribution, and redistribution conditions against the exact
basin-cell index, lists, metrics, and tiles being published.

## Release evidence

Attach or archive:

- the Git commit and version tag;
- CI results and the local `just check` output;
- source/build manifests without local absolute paths or credentials;
- data validation and losslessness reports;
- the map performance trace described in `docs/performance/map-performance-retrospective.md`;
- screenshots showing the basemap attribution control; and
- the reviewer and date for the code, data, and licence checks.
