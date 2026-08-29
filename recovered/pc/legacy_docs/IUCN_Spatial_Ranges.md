# IUCN Spatial Range Data — GeoParquet

Derived from [IUCN Red List](https://www.iucnredlist.org/) ESRI Shapefiles, exported to GeoParquet with DNA gap analysis columns joined from `dna_gap_analysis.duckdb`.

**Total: 128,768 polygon rows** for **78,432 distinct species** across 3 kingdoms, 15 phyla, and 7 taxonomic group files.

Note: A single species can have multiple range polygons (e.g., separate resident, breeding, and seasonal ranges). The average is ~1.6 polygons per species.

---

## Files

| File | Rows | Distinct Species | Size |
|------|-----:|-----------------:|-----:|
| `class=freshwater_groups.parquet` | 52,565 | — | 9.9 GB |
| `class=fishes.parquet` | 20,547 | — | 15.2 GB |
| `class=plants.parquet` | 15,836 | — | 3.9 GB |
| `class=reptiles.parquet` | 14,052 | — | 1.7 GB |
| `class=mammals.parquet` | 13,238 | — | 1.5 GB |
| `class=amphibians.parquet` | 10,543 | — | 2.0 GB |
| `class=marine_groups.parquet` | 1,987 | — | 3.0 GB |
| **Total** | **128,768** | **78,432** | **~37.1 GB** |

Location: `<external-geodata-root>/iucn_ranges_v2/`

---

## Column Reference

37 columns organized into five groups:

### Taxonomy & Identifiers (columns 0–9)

| Column | Type | Description |
|--------|------|-------------|
| `id_no` | BIGINT | IUCN internal taxon identifier (join key with `merged_gbif.internalTaxonId`) |
| `sci_name` | VARCHAR | Scientific name (binomial) |
| `iucn_grouping` | VARCHAR[] | Hierarchical IUCN group labels (e.g., `["Amphibians", "Tailless Amphibians"]`) |
| `kingdom` | VARCHAR | Kingdom (ANIMALIA, PLANTAE, FUNGI) |
| `phylum` | VARCHAR | Phylum |
| `class` | VARCHAR | Class |
| `order_name` | VARCHAR | Order |
| `family` | VARCHAR | Family |
| `genus` | VARCHAR | Genus |
| `iucn_category` | VARCHAR | IUCN short code (CR, EN, VU, NT, DD, LC, EX, EW, LR/nt) |

### Range Metadata (columns 10–17)

| Column | Type | Description |
|--------|------|-------------|
| `presence` | BIGINT | Presence code (1=extant, 2=possibly extant, 3=possibly extinct, 4=extinct post-1500, 5=extinct pre-1500, 6=presence uncertain) |
| `origin` | BIGINT | Origin code (1=native, 2=reintroduced, 3=introduced, 4=vagrant, 5=origin uncertain, 6=assisted colonisation) |
| `seasonal` | BIGINT | Seasonality code (1=resident, 2=breeding season, 3=non-breeding season, 4=passage, 5=seasonal occurrence uncertain) |
| `marine` | VARCHAR | Marine system flag (`true`/`false`) |
| `terrestial` | VARCHAR | Terrestrial system flag (`true`/`false`; note: IUCN spelling) |
| `freshwater` | VARCHAR | Freshwater system flag (`true`/`false`) |
| `source` | VARCHAR | Source attribution for the range map |

### Geometry & H3 (columns 18–20)

| Column | Type | Description |
|--------|------|-------------|
| `geom_wkb` | BLOB | Polygon geometry in WKB format (WGS 84 / EPSG:4326) |
| `h3_res3` | UBIGINT | Pre-computed H3 cell index at resolution 3 |
| `h3_res7` | UBIGINT | Pre-computed H3 cell index at resolution 7 |

### Red List Assessment (columns 21–23)

| Column | Type | Description |
|--------|------|-------------|
| `redlistCategory` | VARCHAR | Full IUCN category name (e.g., `Critically Endangered`, `Data Deficient`) |
| `threat_score` | INTEGER | Numeric threat score (CR=3, EN=2, VU=1, else=0); NULL for 350 uncategorized rows |
| `sampling_priority` | INTEGER | Pre-computed priority = `threat_score × (4 - dna_coverage_score)` |

### GOAT DNA Coverage (columns 24–36)

| Column | Type | Description |
|--------|------|-------------|
| `assembly_level` | VARCHAR | Highest assembly level (Contig, Scaffold, Chromosome, Complete Genome) |
| `match_method` | VARCHAR | GBIF name match method (`gbif_matched`, `gbif_unmatched`, `no_gbif_match`) |
| `sequencing_status` | VARCHAR | Current sequencing status from GOAT |
| `sample_available` | VARCHAR | Project(s) with samples available |
| `sample_collected` | VARCHAR | Project(s) that have collected samples |
| `in_progress` | VARCHAR | Project(s) actively sequencing |
| `insdc_submitted` | VARCHAR | Project(s) that submitted to INSDC |
| `published` | VARCHAR | Project(s) with published genomes |
| `has_dna_species_level` | BOOLEAN | Whether species has any DNA data in GOAT |
| `genus_has_dna` | BOOLEAN | Whether the genus has any DNA data in GOAT |
| `family_has_dna` | BOOLEAN | Whether the family has any DNA data in GOAT |
| `dna_coverage_score` | INTEGER | DNA coverage score (0–4); NULL for 350 uncategorized rows |

---

## Distributions

### IUCN Red List Category

| Category | Count | % |
|----------|------:|--:|
| Least Concern | 76,628 | 59.5% |
| Data Deficient | 16,272 | 12.6% |
| Vulnerable | 10,852 | 8.4% |
| Endangered | 10,819 | 8.4% |
| Near Threatened | 7,523 | 5.8% |
| Critically Endangered | 6,319 | 4.9% |
| Extinct | 313 | 0.2% |
| Extinct in the Wild | 37 | <0.1% |
| Lower Risk/near threatened | 5 | <0.1% |
| None/uncategorized | 350 | 0.3% |

### Kingdom

| Kingdom | Count | % |
|---------|------:|--:|
| ANIMALIA | 109,392 | 84.9% |
| PLANTAE | 19,370 | 15.0% |
| FUNGI | 6 | <0.1% |

### Top 10 Phyla

| Phylum | Count |
|--------|------:|
| CHORDATA | 92,659 |
| TRACHEOPHYTA | 19,002 |
| ARTHROPODA | 10,778 |
| MOLLUSCA | 5,051 |
| CNIDARIA | 892 |
| BRYOPHYTA | 193 |
| RHODOPHYTA | 74 |
| MARCHANTIOPHYTA | 68 |
| CHAROPHYTA | 28 |
| ANNELIDA | 10 |

### Top 15 Classes

| Class | Count |
|-------|------:|
| ACTINOPTERYGII (ray-finned fish) | 39,217 |
| AMPHIBIA | 17,692 |
| REPTILIA | 14,819 |
| MAMMALIA | 13,613 |
| MAGNOLIOPSIDA (dicots) | 13,183 |
| INSECTA | 7,494 |
| LILIOPSIDA (monocots) | 5,108 |
| AVES (birds) | 4,334 |
| GASTROPODA | 4,100 |
| MALACOSTRACA | 3,212 |
| CHONDRICHTHYES (cartilaginous fish) | 2,716 |
| BIVALVIA | 951 |
| ANTHOZOA | 877 |
| POLYPODIOPSIDA | 531 |
| BRYOPSIDA | 187 |

### Top 15 IUCN Groupings

| Grouping | Count |
|----------|------:|
| Freshwater Groups | 52,565 |
| Fishes | 42,201 |
| Plants | 19,228 |
| Marine Fishes | 15,505 |
| Reptiles | 14,052 |
| Scaled Reptiles | 13,812 |
| Mammals | 13,238 |
| Other | 12,977 |
| Terrestrial Mammals | 12,703 |
| Amphibians | 10,543 |
| Tailless Amphibians | 9,322 |
| Odonata | 7,252 |
| Molluscs | 4,349 |
| Trees | 4,278 |
| Marine Groups | 1,987 |

### Presence Code

| Code | Meaning | Count |
|------|---------|------:|
| 1 | Extant | 111,560 |
| 2 | Possibly extant | 5,658 |
| 3 | Possibly extinct | 5,658 |
| 4 | Extinct (post-1500) | 1,772 |
| 5 | Extinct (pre-1500) | 1,568 |
| 6 | Presence uncertain | 2,552 |

### Origin Code

| Code | Meaning | Count |
|------|---------|------:|
| 1 | Native | 126,390 |
| 2 | Reintroduced | 253 |
| 3 | Introduced | 1,383 |
| 4 | Vagrant | 77 |
| 5 | Origin uncertain | 632 |
| 6 | Assisted colonisation | 33 |

### Seasonality Code

| Code | Meaning | Count |
|------|---------|------:|
| 1 | Resident | 124,880 |
| 2 | Breeding season | 825 |
| 3 | Non-breeding season | 913 |
| 4 | Passage | 520 |
| 5 | Seasonal occurrence uncertain | 1,630 |

### Ecological System

| System | Count |
|--------|------:|
| Terrestrial | 71,560 |
| Marine | 26,710 |
| Freshwater | 67,205 |

(Rows can have multiple system flags = true; does not sum to total)

### Threat Score

| Score | Category | Count |
|------:|----------|------:|
| 0 | LC, NT, DD | 100,428 |
| 1 | VU | 10,852 |
| 2 | EN | 10,819 |
| 3 | CR | 6,319 |
| NULL | Uncategorized | 350 |

### DNA Coverage Score

| Score | Meaning | Count |
|------:|---------|------:|
| 0 | No DNA data | 117,582 |
| 1 | Sequencing status only | 2,750 |
| 2 | INSDC open / published | 7 |
| 3 | Has assembly (Contig/Scaffold) | 4,675 |
| 4 | Chromosome / Complete Genome | 3,404 |
| NULL | Uncategorized | 350 |

### DNA Coverage Flags

| Flag | True | False | NULL |
|------|-----:|------:|-----:|
| `has_dna_species_level` | 10,836 | 117,582 | 350 |
| `genus_has_dna` | 113,498 | 14,920 | 350 |
| `family_has_dna` | 122,368 | 6,050 | 350 |

### Assembly Level

| Level | Count |
|-------|------:|
| Scaffold | 3,916 |
| Chromosome | 3,354 |
| Contig | 577 |
| scaffold (lowercase) | 182 |
| Complete Genome | 50 |

### Sequencing Status

| Status | Count |
|--------|------:|
| insdc_open | 7,582 |
| data_generation | 807 |
| in_progress | 792 |
| sample_acquired | 661 |
| published | 471 |
| open | 220 |
| sample_collected | 155 |
| in_assembly | 148 |

---

## Data Pipeline

1. **Source**: IUCN Red List ESRI Shapefiles (51 individual shapefile parts across 30 taxonomic groups)
2. **Ingest**: Loaded into `spatial_all.duckdb` via `ST_Read()` (47.7 GB)
3. **Join**: DNA gap analysis columns from `dna_gap_analysis.duckdb` merged on `id_no = internalTaxonId`
4. **Export**: Written to GeoParquet with Hive-style partitioning by `class`
5. **H3**: Pre-computed `h3_res3` and `h3_res7` indices per polygon

### Export Scripts

- `scripts/export_geoparquet.py` — DuckDB → GeoParquet with joined columns
- `scripts/precompute_h3.py` — H3 polyfill + per-cell aggregation
- `scripts/create_sample_data.py` — Small sample extraction for Denmark prototype

---

## Notes

- All geometries in **WGS 84 (EPSG:4326)**
- `terrestial` column has IUCN's original spelling (should be "terrestrial")
- 350 rows have NULL threat/dna scores — these are polygons from IUCN shapefiles that didn't match any species in `merged_gbif`
- `iucn_category` is the short code (CR, EN, VU, etc.); `redlistCategory` is the full name
- `iucn_grouping` is a VARCHAR[] array allowing hierarchical filtering (e.g., Mammals → Terrestrial Mammals)
- Data licensed under [IUCN Red List Terms and Conditions](https://www.iucnredlist.org/terms/terms-of-use)
