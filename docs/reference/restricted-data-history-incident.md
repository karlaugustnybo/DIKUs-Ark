# Restricted data in Git history: 2026-09-04 incident record

## Summary

Ark-IV's public Git history included two generated DuckDB databases:

- `data/Ark-IV.duckdb`
- `data/precomputed_cache.duckdb`

They were removed from the checked-out tree on 29 August 2026, but an ordinary
deletion commit did not remove their underlying Git objects. Anyone who could
read an affected commit could therefore still download the databases.

On 4 September 2026 the affected branches and tags were rewritten to remove
both paths from every commit, the rewritten refs were force-pushed, and the
repository history audit was run against the result.

## Why publication was a problem

The databases were generated serving artifacts, not source code. They contained
records and derived values backed by IUCN Red List and EDGE of Existence inputs.
The project had not established permission to redistribute those source-backed
records in a public database, and Ark-IV's `AGPL-3.0-only` software licence
cannot grant rights in third-party data.

In particular, IUCN restricts redistribution of its raw, tabular, and spatial
data without permission, while EDGE content is copyrighted by the Zoological
Society of London and requires express permission for redistribution. A
database or other derivative can remain restricted when it exposes rows,
identifiers, rankings, geometry, or reconstructable spatial memberships from
those inputs. Git LFS would not have solved the issue because changing storage
does not change redistribution rights.

This was treated as a licensing and publication-control incident. It was not a
credential leak, and no claim is made here that every value in either database
was independently restricted. The absence of a documented redistribution grant
for the artifacts was enough to require removal.

## Scope and remediation

The history audit identified the two paths above across `main`, development
branches, remote-tracking refs, and local tool checkpoint refs. Remediation
consisted of:

1. retaining this non-data incident record and the repository data policy;
2. removing both paths from every affected commit with a history rewrite;
3. force-updating the affected public branches and tags;
4. running `just release-history-check` to confirm that no local ref retained
   either path; and
5. retaining prospective-commit checks that reject databases, generated data,
   restricted paths, and common data-export formats.

Git commit IDs changed as a consequence of the rewrite. Clones made before the
cleanup must be discarded and cloned again; merging or pushing an old branch
can restore the removed objects. Repository administrators should contact
GitHub Support if cached commit views or provider-managed pull-request refs
remain accessible after ref cleanup.

## Preventive controls

- The public repository is a code release, not a data release; see
  `DATA_POLICY.md`.
- Local inputs and generated outputs belong under ignored `data/` paths or an
  external data root.
- `just release-check` inspects the prospective commit.
- `just release-history-check` scans every local ref for the two known paths.
- Public data snapshots and browser-serving artifacts require their own source,
  terms, permission, derivative-disclosure, and delivery-channel review.

The history rewrite reduces continued exposure through ordinary Git refs. It
cannot revoke copies already fetched while the affected history was public.
