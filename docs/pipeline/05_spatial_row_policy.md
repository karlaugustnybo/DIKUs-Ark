# IUCN spatial row policy

## Production choice

Ark's primary biodiversity layer uses the versioned
`iucn-richness-any-touch-v3` profile:

| Attribute | Included codes | Meaning |
|---|---|---|
| Presence | 1, 4 | extant; possibly extinct |
| Origin | 1, 2, 6 | native; reintroduced; assisted colonisation |
| Seasonality | 1, 2, 3, 5 | resident; breeding; non-breeding; seasonal occurrence uncertain |

These are the selection values published by IUCN for its 2021+ Species
Richness and Rarity-Weighted Richness products. Ark adopts them as its declared
potential-richness selection. This does not reproduce IUCN's products: Ark uses
a different grid, taxonomic coverage and marine treatment. Comparisons require
matching those scopes separately.

The profile must be described as **potential species richness**. Presence 4
does not mean confirmed current occurrence; it means possibly extinct. The
retained `iucn-any-touch-intersection-v2` profile narrows the selection to
Presence 1 and Origin 1 or 2 and applies no seasonality restriction. Even that
profile represents mapped ranges, not confirmed contemporary occupancy.

Introduced, vagrant, origin-uncertain, possibly-extant, presence-uncertain and
post-1500 extinct records are not silently converted into presence in the
primary layer. They remain visible in `row_audit.parquet` with their raw IUCN
codes and a deterministic exclusion reason. This preserves the evidence needed
to materialize a different research profile later without ambiguity.

## Why not include every polygon?

The attributes describe different ecological claims. Combining native,
introduced, vagrant, uncertain and extinct records into one unlabelled count
would answer no stable biological question and would make cells incomparable.
The correct research pattern is to choose a declared hypothesis before
aggregation, keep the source attributes, and publish the exact selection.

## Reproducibility

The TOML profile, its SHA-256 digest, the source release, code fingerprint,
dependency versions and every row decision are written into stage receipts or
the row audit. Changing any policy code produces a different derived-output
identity and cannot silently reuse an older build.

Run the fast attribute-only comparison with:

```bash
.venv/bin/python -m ark_pipeline.cli.spatial_audit \
  --data-root /path/to/authorized-data-root
```

The default comparison covers both the conservative v2 and richness v3
profiles and writes `data/spatial-test/benchmark-diagnostics/row-policy-audit.json`.
On the audited 130,108-row release, v2 admitted 110,868 rows by attributes and
v3 admitted 112,218. Of these, 110,362 were admitted by both profiles, 1,856
were newly admitted by v3, and 506 v2 rows were removed by v3's seasonality
selection. Geometry-null reconciliation remains part of the build audit; the
earlier complete geometry scan found no otherwise-eligible null geometries.

## Primary references

- [IUCN Species Richness and Rarity-Weighted Richness selection](https://nrl.iucnredlist.org/resources/sr-rwr-archive)
- [IUCN Mapping Standards and standard spatial attributes](https://nrl.iucnredlist.org/resources/mappingstandards)
