import json

import duckdb


con = duckdb.connect(":memory:")
con.execute("PRAGMA threads=4")
con.execute("PRAGMA memory_limit='10GB'")

con.execute("""
    CREATE TEMP TABLE gbif_backbone AS
    SELECT
        taxonID::VARCHAR AS taxonID,
        acceptedNameUsageID::VARCHAR AS acceptedNameUsageID,
        canonicalName,
        taxonRank,
        taxonomicStatus,
        ROW_NUMBER() OVER (ORDER BY canonicalName, taxonID) AS _seq
    FROM read_csv_auto(
        'BioDatasets/backbone/backbone/Taxon.tsv',
        delim='\t',
        all_varchar=true,
        strict_mode=false
    )
    WHERE taxonRank='species'
      AND canonicalName IS NOT NULL
      AND canonicalName != ''
""")

con.execute("""
    CREATE TEMP TABLE gbif_name_map AS
    WITH ranked AS (
        SELECT
            canonicalName,
            LOWER(canonicalName) AS canonical_lower,
            COALESCE(acceptedNameUsageID, taxonID) AS gbif_id,
            taxonomicStatus,
            COUNT(*) OVER (
                PARTITION BY LOWER(canonicalName)
            ) AS candidate_rows,
            COUNT(DISTINCT COALESCE(acceptedNameUsageID, taxonID)) OVER (
                PARTITION BY LOWER(canonicalName)
            ) AS candidate_ids,
            ROW_NUMBER() OVER (
                PARTITION BY LOWER(canonicalName)
                ORDER BY
                    CASE taxonomicStatus WHEN 'accepted' THEN 0 ELSE 1 END,
                    taxonomicStatus,
                    _seq
            ) AS rn
        FROM gbif_backbone
        WHERE taxonomicStatus IN (
            'accepted',
            'synonym',
            'homotypic synonym',
            'heterotypic synonym',
            'proparte synonym'
        )
    )
    SELECT * FROM ranked WHERE rn=1
""")

# The deleted exploratory notebook used the same status preference but had no
# final tie-breaker. Reproduce it separately to quantify the effect.
con.execute("""
    CREATE TEMP TABLE gbif_name_map_legacy AS
    WITH ranked AS (
        SELECT
            LOWER(canonicalName) AS canonical_lower,
            COALESCE(acceptedNameUsageID, taxonID) AS gbif_id,
            ROW_NUMBER() OVER (
                PARTITION BY LOWER(canonicalName)
                ORDER BY
                    CASE taxonomicStatus WHEN 'accepted' THEN 0 ELSE 1 END,
                    taxonomicStatus
            ) AS rn
        FROM gbif_backbone
        WHERE taxonomicStatus IN (
            'accepted',
            'synonym',
            'homotypic synonym',
            'heterotypic synonym',
            'proparte synonym'
        )
    )
    SELECT canonical_lower, gbif_id FROM ranked WHERE rn=1
""")

con.execute("""
    CREATE TEMP TABLE iucn AS
    SELECT
        t.internalTaxonId,
        t.genusName,
        t.speciesName,
        LOWER(t.genusName || ' ' || t.speciesName) AS canonical_lower
    FROM read_csv_auto('BioDatasets/IUCN_Red_List/taxonomy.csv') t
    INNER JOIN read_csv_auto('BioDatasets/IUCN_Red_List/assessments.csv') a
        USING (internalTaxonId)
""")

con.execute("""
    CREATE TEMP TABLE goat_names AS
    SELECT
        ROW_NUMBER() OVER (ORDER BY taxon_id) AS _seq,
        species,
        LOWER(species) AS canonical_lower,
        taxon_id
    FROM read_csv_auto(
        'BioDatasets/GoaT/goat_dataset.tsv',
        delim='\t',
        all_varchar=true,
        strict_mode=false
    )
""")

con.execute("""
    CREATE TEMP TABLE iucn_mapped AS
    SELECT
        i.*,
        m.gbif_id,
        m.taxonomicStatus AS chosen_gbif_status,
        m.candidate_rows,
        m.candidate_ids
    FROM iucn i
    LEFT JOIN gbif_name_map m USING (canonical_lower)
""")

con.execute("""
    CREATE TEMP TABLE goat_mapped AS
    SELECT g.*, m.gbif_id
    FROM goat_names g
    LEFT JOIN gbif_name_map m USING (canonical_lower)
""")

result = {}
result["source_counts"] = con.execute("""
    SELECT
        (SELECT COUNT(*) FROM iucn) AS iucn_rows,
        (SELECT COUNT(DISTINCT internalTaxonId) FROM iucn) AS iucn_taxa,
        (SELECT COUNT(*) FROM goat_names) AS goat_rows,
        (SELECT COUNT(*) FROM gbif_name_map) AS gbif_names
""").fetchone()

result["iucn_coverage"] = con.execute("""
    SELECT
        COUNT(*) AS total_iucn,
        COUNT(gbif_id) AS with_gbif_id,
        COUNT(*) FILTER (WHERE gbif_id IS NULL) AS no_gbif_id,
        COUNT(*) FILTER (
            WHERE gbif_id IN (
                SELECT gbif_id FROM goat_mapped WHERE gbif_id IS NOT NULL
            )
        ) AS with_goat_via_gbif,
        COUNT(*) FILTER (
            WHERE gbif_id IS NOT NULL
              AND gbif_id NOT IN (
                  SELECT gbif_id FROM goat_mapped WHERE gbif_id IS NOT NULL
              )
        ) AS gbif_but_no_goat,
        COUNT(DISTINCT gbif_id) FILTER (
            WHERE gbif_id IN (
                SELECT gbif_id FROM goat_mapped WHERE gbif_id IS NOT NULL
            )
        ) AS distinct_matched_gbif_ids
    FROM iucn_mapped
""").fetchone()

result["goat_mapping"] = con.execute("""
    SELECT
        COUNT(*) AS total_goat_rows,
        COUNT(gbif_id) AS mapped_goat_rows,
        COUNT(DISTINCT gbif_id) AS distinct_goat_gbif_ids,
        COUNT(*) - COUNT(gbif_id) AS unmapped_goat_rows
    FROM goat_mapped
""").fetchone()

result["status_breakdown"] = con.execute("""
    SELECT COALESCE(chosen_gbif_status, 'NO_MATCH'), COUNT(*)
    FROM iucn_mapped
    GROUP BY 1
    ORDER BY 2 DESC
""").fetchall()

result["ambiguity"] = con.execute("""
    SELECT
        COUNT(*) FILTER (WHERE candidate_rows > 1),
        COUNT(*) FILTER (WHERE candidate_ids > 1),
        MAX(candidate_rows),
        MAX(candidate_ids)
    FROM iucn_mapped
""").fetchone()

result["goat_id_duplication"] = con.execute("""
    SELECT
        COUNT(*) AS id_groups,
        SUM(n) AS rows_in_mapped_groups,
        SUM(n - 1) AS extra_rows,
        MAX(n) AS max_rows_per_id
    FROM (
        SELECT gbif_id, COUNT(*) AS n
        FROM goat_mapped
        WHERE gbif_id IS NOT NULL
        GROUP BY gbif_id
    )
""").fetchone()

result["legacy_notebook_coverage"] = con.execute("""
    WITH
    iucn_legacy AS (
        SELECT i.internalTaxonId, m.gbif_id
        FROM iucn i
        LEFT JOIN gbif_name_map_legacy m USING (canonical_lower)
    ),
    goat_legacy AS (
        SELECT g.taxon_id, m.gbif_id
        FROM goat_names g
        LEFT JOIN gbif_name_map_legacy m USING (canonical_lower)
    )
    SELECT
        COUNT(*) FILTER (
            WHERE gbif_id IN (
                SELECT gbif_id FROM goat_legacy WHERE gbif_id IS NOT NULL
            )
        ) AS with_goat_via_gbif,
        COUNT(*) FILTER (
            WHERE gbif_id IS NOT NULL
              AND gbif_id NOT IN (
                  SELECT gbif_id FROM goat_legacy WHERE gbif_id IS NOT NULL
              )
        ) AS gbif_but_no_goat,
        COUNT(*) FILTER (WHERE gbif_id IS NULL) AS no_gbif_id
    FROM iucn_legacy
""").fetchone()

result["legacy_vs_current_map"] = con.execute("""
    SELECT
        COUNT(*) FILTER (WHERE l.gbif_id != c.gbif_id) AS changed_name_mappings,
        COUNT(*) FILTER (
            WHERE i.canonical_lower IS NOT NULL AND l.gbif_id != c.gbif_id
        ) AS changed_iucn_rows
    FROM gbif_name_map_legacy l
    JOIN gbif_name_map c USING (canonical_lower)
    LEFT JOIN iucn i USING (canonical_lower)
""").fetchone()

print(json.dumps(result, indent=2, default=str))
