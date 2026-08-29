# GitHub publication checklist

Run this checklist before making a release, tagging a version, or deploying a
public data snapshot.

## Required before the next public push

- [x] Select `AGPL-3.0-only`, add the official root `LICENSE`, and declare it in
  both package manifests.
- [ ] Record agreement from every existing copyright holder—at minimum, confirm
  Daniel Jensen's initial contribution—before representing all existing code as
  AGPL-licensed.
- [ ] For a binary, container, or hosted frontend release, generate and retain
  the third-party software notices for the exact resolved dependency set.
- [ ] Confirm that every tutorial MP4 and image was created by the contributors
  or is covered by a compatible licence.
- [ ] Run `just check` and retain the successful output.
- [ ] Review `git status --short` and `git diff --cached` before committing.
- [ ] Run `just release-history-check` after the history rewrite below.
- [ ] Compare current provider terms with `DATA_POLICY.md` and `NOTICE.md`.
- [ ] Confirm that every public deployment offers the corresponding source for
  its running version through the persistent Source link required by AGPL §13.

## Known public-history incident

The public repository's `main` history contains `data/Ark-IV.duckdb` and
`data/precomputed_cache.duckdb`. Removing them in a later commit does **not**
remove the downloadable objects from history. The files contain IUCN/EDGE-backed
records that should not be publicly redistributed.

History rewriting changes commit IDs and requires every collaborator to
re-clone. It should be done deliberately by a repository administrator, after
making a private backup and coordinating a short push freeze. A suitable
procedure is:

1. Temporarily make the GitHub repository private and stop collaborator pushes.
2. Create a private mirror backup outside the working repository.
3. Install `git-filter-repo` with Homebrew if needed.
4. In a disposable mirror clone, remove the two databases from every ref:

   ```bash
   git filter-repo --force --invert-paths \
     --path data/Ark-IV.duckdb \
     --path data/precomputed_cache.duckdb
   ```

5. Inspect the rewritten refs, run the history release check, then force-push
   the intended branches and tags with repository-administrator approval.
6. Ask every collaborator to delete old clones and re-clone. Old clones can
   reintroduce the removed objects.
7. Contact GitHub Support if cached views or pull-request refs still expose the
   files.

The old MOV tutorial files and obsolete diagram PNGs also occupy substantial
history. They may be removed in the same coordinated rewrite for repository
size, but they are not the licensing incident that makes the rewrite mandatory.

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

## Release evidence

Attach or archive:

- the Git commit and version tag;
- CI results and the local `just check` output;
- source/build manifests without local absolute paths or credentials;
- data validation and losslessness reports;
- the map performance trace described in `docs/map-performance-retrospective.md`;
- screenshots showing the basemap attribution control; and
- the reviewer and date for the code, data, and licence checks.
