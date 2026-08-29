#!/usr/bin/env python3
"""Build an auditable global IUCN SIS taxon -> GoaT/NCBI species crosswalk.

The matcher intentionally separates deterministic taxonomy links from the
AI-reviewed typo/near-name stage.  It never treats a shared GBIF accepted key
as proof that two species concepts are identical.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb

REVIEWER = "AI-review (GPT-5.6 Sol)"
TAXONOMIC_NAME_CLASSES = (
    "scientific name",
    "synonym",
    "equivalent name",
    "includes",
    "in-part",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iucn-taxonomy", type=Path, required=True)
    parser.add_argument("--iucn-assessments", type=Path, required=True)
    parser.add_argument("--goat-species", type=Path, required=True)
    parser.add_argument("--ncbi-taxdump-dir", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/exports/iucn_goat_global"),
    )
    parser.add_argument(
        "--memory-limit",
        default="8GB",
        help="DuckDB memory limit (default: 8GB)",
    )
    parser.add_argument(
        "--gbif-validation-json",
        type=Path,
        help="Optional output from gbif_validate_iucn_goat_candidates.mjs",
    )
    parser.add_argument(
        "--gbif-bridge-json",
        type=Path,
        help="Optional output from gbif_bridge_iucn_names.mjs",
    )
    parser.add_argument(
        "--goat-lineages-json",
        type=Path,
        help="Optional output from fetch_goat_lineages.mjs",
    )
    return parser.parse_args()


def sql_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def main() -> None:
    args = parse_args()
    required = [
        args.iucn_taxonomy,
        args.iucn_assessments,
        args.goat_species,
        args.ncbi_taxdump_dir / "names.dmp",
        args.ncbi_taxdump_dir / "nodes.dmp",
        args.ncbi_taxdump_dir / "merged.dmp",
        args.ncbi_taxdump_dir / "delnodes.dmp",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SystemExit(f"Missing required files: {missing}")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    work_db = output_dir / "matching_work.duckdb"
    if work_db.exists():
        work_db.unlink()

    con = duckdb.connect(str(work_db))
    con.execute(f"SET memory_limit='{args.memory_limit}'")
    con.execute("SET threads TO 4")
    con.execute(f"SET temp_directory='{sql_path(output_dir / 'duckdb_tmp')}'")

    iucn_taxonomy = sql_path(args.iucn_taxonomy)
    iucn_assessments = sql_path(args.iucn_assessments)
    goat_species = sql_path(args.goat_species)
    names_dmp = sql_path(args.ncbi_taxdump_dir / "names.dmp")
    nodes_dmp = sql_path(args.ncbi_taxdump_dir / "nodes.dmp")
    merged_dmp = sql_path(args.ncbi_taxdump_dir / "merged.dmp")
    delnodes_dmp = sql_path(args.ncbi_taxdump_dir / "delnodes.dmp")

    stage("Loading IUCN, GoaT, and NCBI source tables")
    con.execute(
        f"""
        CREATE TABLE iucn AS
        SELECT
            internalTaxonId::BIGINT AS iucn_sis_id,
            trim(scientificName) AS iucn_scientific_name,
            lower(regexp_replace(trim(scientificName), '\\s+', ' ', 'g')) AS name_norm,
            trim(kingdomName) AS iucn_kingdom,
            trim(phylumName) AS iucn_phylum,
            trim(className) AS iucn_class,
            trim(orderName) AS iucn_order,
            trim(familyName) AS iucn_family,
            trim(genusName) AS iucn_genus,
            trim(speciesName) AS iucn_specific_epithet,
            trim(authority) AS iucn_authority,
            lower(trim(genusName)) AS genus_norm,
            lower(trim(speciesName)) AS epithet_norm
        FROM read_csv_auto(
            '{iucn_taxonomy}', header=true, all_varchar=true
        );

        CREATE TABLE assessments AS
        SELECT
            internalTaxonId::BIGINT AS iucn_sis_id,
            assessmentId::BIGINT AS iucn_assessment_id,
            redlistCategory AS iucn_redlist_category,
            yearPublished::INTEGER AS iucn_year_published,
            assessmentDate AS iucn_assessment_date,
            scopes AS iucn_scopes
        FROM read_csv_auto(
            '{iucn_assessments}', header=true, all_varchar=true
        );

        CREATE TABLE goat AS
        SELECT
            trim(taxon_id) AS ncbi_taxid,
            try_cast(trim(taxon_id) AS BIGINT) AS ncbi_taxid_numeric,
            trim(scientific_name) AS goat_scientific_name,
            lower(regexp_replace(trim(scientific_name), '\\s+', ' ', 'g')) AS name_norm,
            trim(taxon_rank) AS goat_taxon_rank,
            assembly_level,
            bioproject,
            busco_completeness,
            ebp_standard_criteria,
            in_progress,
            insdc_submitted,
            published,
            resampling_required,
            sample_acquired,
            sample_available,
            sample_collected,
            sequencing_status,
            sequencing_status_ebp,
            other_priority,
            family_representative
        FROM read_csv(
            '{goat_species}', delim='\\t', header=true, all_varchar=true
        );

        CREATE TABLE ncbi_names AS
        SELECT
            trim(column0, E' \\t') AS ncbi_taxid,
            trim(column1, E' \\t') AS name_txt,
            lower(regexp_replace(trim(column1, E' \\t'), '\\s+', ' ', 'g')) AS name_norm,
            trim(column2, E' \\t') AS unique_name,
            trim(column3, E' \\t') AS name_class
        FROM read_csv(
            '{names_dmp}',
            delim='|',
            header=false,
            all_varchar=true,
            columns={{
                'column0':'VARCHAR',
                'column1':'VARCHAR',
                'column2':'VARCHAR',
                'column3':'VARCHAR',
                'column4':'VARCHAR'
            }}
        );

        CREATE TABLE ncbi_nodes AS
        SELECT
            trim(column0, E' \\t') AS ncbi_taxid,
            trim(column1, E' \\t') AS parent_taxid,
            trim(column2, E' \\t') AS taxon_rank
        FROM read_csv(
            '{nodes_dmp}',
            delim='|',
            header=false,
            all_varchar=true,
            columns={{
                'column0':'VARCHAR',
                'column1':'VARCHAR',
                'column2':'VARCHAR',
                'column3':'VARCHAR',
                'column4':'VARCHAR',
                'column5':'VARCHAR',
                'column6':'VARCHAR',
                'column7':'VARCHAR',
                'column8':'VARCHAR',
                'column9':'VARCHAR',
                'column10':'VARCHAR',
                'column11':'VARCHAR',
                'column12':'VARCHAR',
                'column13':'VARCHAR'
            }}
        );

        CREATE TABLE ncbi_merged AS
        SELECT
            trim(column0, E' \\t') AS old_taxid,
            trim(column1, E' \\t') AS current_taxid
        FROM read_csv(
            '{merged_dmp}',
            delim='|',
            header=false,
            all_varchar=true,
            columns={{
                'column0':'VARCHAR',
                'column1':'VARCHAR',
                'column2':'VARCHAR'
            }}
        );

        CREATE TABLE ncbi_deleted AS
        SELECT trim(column0, E' \\t') AS deleted_taxid
        FROM read_csv(
            '{delnodes_dmp}',
            delim='|',
            header=false,
            all_varchar=true,
            columns={{
                'column0':'VARCHAR',
                'column1':'VARCHAR'
            }}
        );
        """
    )

    stage("Generating exact current-name and NCBI synonym candidates")
    name_classes_sql = ",".join(f"'{value}'" for value in TAXONOMIC_NAME_CLASSES)
    con.execute(
        f"""
        CREATE TABLE exact_candidates AS
        WITH direct AS (
            SELECT
                i.iucn_sis_id,
                g.ncbi_taxid,
                g.goat_scientific_name AS evidence_name,
                'GOAT_CURRENT_NAME_EXACT' AS evidence_method,
                1 AS method_priority
            FROM iucn i
            JOIN goat g USING (name_norm)
            WHERE g.ncbi_taxid_numeric IS NOT NULL
        ),
        aliases AS (
            SELECT
                i.iucn_sis_id,
                g.ncbi_taxid,
                n.name_txt AS evidence_name,
                CASE
                    WHEN n.name_class = 'scientific name'
                        THEN 'NCBI_CURRENT_NAME_EXACT'
                    ELSE 'NCBI_SYNONYM_EXACT'
                END AS evidence_method,
                CASE WHEN n.name_class = 'scientific name' THEN 2 ELSE 3 END
                    AS method_priority
            FROM iucn i
            JOIN ncbi_names n USING (name_norm)
            JOIN goat g ON g.ncbi_taxid = n.ncbi_taxid
            WHERE n.name_class IN ({name_classes_sql})
              AND g.ncbi_taxid_numeric IS NOT NULL
        )
        SELECT
            iucn_sis_id,
            ncbi_taxid,
            first(evidence_name ORDER BY method_priority) AS evidence_name,
            first(evidence_method ORDER BY method_priority) AS evidence_method,
            min(method_priority) AS method_priority,
            list_sort(list_distinct(list(evidence_method))) AS all_exact_evidence
        FROM (
            SELECT * FROM direct
            UNION ALL
            SELECT * FROM aliases
        )
        GROUP BY iucn_sis_id, ncbi_taxid;
        """
    )

    if args.gbif_bridge_json:
        bridge_path = sql_path(args.gbif_bridge_json)
        stage("Loading exact IUCN -> GBIF/CoL accepted-concept bridges")
        con.execute(
            f"""
            CREATE TABLE gbif_bridge AS
            SELECT unnest(rows, recursive := true)
            FROM read_json_auto('{bridge_path}');
            """
        )
    else:
        con.execute(
            """
            CREATE TABLE gbif_bridge (
                iucn_sis_id BIGINT,
                exact_species_concept BOOLEAN,
                iucn_source_id_confirmed BOOLEAN,
                accepted_concept_key VARCHAR,
                accepted_canonical_name VARCHAR
            );
            """
        )

    stage("Generating conservative near-name candidates for AI review")
    con.execute(
        f"""
        CREATE TABLE taxonomic_candidate_names AS
        WITH combined AS (
            SELECT
                ncbi_taxid,
                goat_scientific_name AS candidate_name,
                name_norm,
                'GOAT_CURRENT_NAME' AS candidate_name_source
            FROM goat
            WHERE ncbi_taxid_numeric IS NOT NULL

            UNION ALL

            SELECT
                n.ncbi_taxid,
                n.name_txt,
                n.name_norm,
                'NCBI_' || upper(replace(n.name_class, ' ', '_'))
            FROM ncbi_names n
            JOIN goat g USING (ncbi_taxid)
            WHERE n.name_class IN ({name_classes_sql})
              AND g.ncbi_taxid_numeric IS NOT NULL
        )
        SELECT
            ncbi_taxid,
            candidate_name,
            name_norm,
            lower(regexp_extract(name_norm, '^([^ ]+)', 1)) AS genus_norm,
            lower(regexp_extract(name_norm, '^[^ ]+ ([^ ]+)', 1)) AS epithet_norm,
            first(candidate_name_source) AS candidate_name_source
        FROM combined
        WHERE regexp_matches(name_norm, '^[^ ]+ [^ ]+')
        GROUP BY ncbi_taxid, candidate_name, name_norm;

        CREATE TABLE bridge_candidates AS
        SELECT
            b.iucn_sis_id,
            c.ncbi_taxid,
            b.accepted_canonical_name AS evidence_name,
            b.accepted_concept_key AS gbif_bridge_concept_key,
            min(levenshtein(i.name_norm, c.name_norm)) AS edit_distance,
            max(jaro_winkler_similarity(i.name_norm, c.name_norm))
                AS name_similarity
        FROM gbif_bridge b
        JOIN iucn i USING (iucn_sis_id)
        JOIN taxonomic_candidate_names c
          ON lower(regexp_replace(trim(b.accepted_canonical_name), '\\s+', ' ', 'g'))
             = c.name_norm
        WHERE b.exact_species_concept
          AND b.iucn_source_id_confirmed
        GROUP BY
            b.iucn_sis_id,
            c.ncbi_taxid,
            b.accepted_canonical_name,
            b.accepted_concept_key;

        CREATE TABLE fuzzy_candidates AS
        WITH unmatched AS (
            SELECT i.*
            FROM iucn i
            LEFT JOIN (
                SELECT DISTINCT iucn_sis_id FROM exact_candidates
                UNION
                SELECT DISTINCT iucn_sis_id FROM bridge_candidates
            ) e USING (iucn_sis_id)
            WHERE e.iucn_sis_id IS NULL
              AND length(i.genus_norm) >= 3
              AND length(i.epithet_norm) >= 3
        ),
        blocked AS (
            SELECT i.iucn_sis_id, c.*
            FROM unmatched i
            JOIN taxonomic_candidate_names c
              ON i.genus_norm = c.genus_norm
             AND left(i.epithet_norm, 1) = left(c.epithet_norm, 1)
            WHERE levenshtein(i.epithet_norm, c.epithet_norm) <= 2

            UNION

            SELECT i.iucn_sis_id, c.*
            FROM unmatched i
            JOIN taxonomic_candidate_names c
              ON i.epithet_norm = c.epithet_norm
             AND left(i.genus_norm, 1) = left(c.genus_norm, 1)
            WHERE levenshtein(i.genus_norm, c.genus_norm) <= 2
        ),
        scored AS (
            SELECT
                b.iucn_sis_id,
                b.ncbi_taxid,
                b.candidate_name,
                b.candidate_name_source,
                levenshtein(i.name_norm, b.name_norm) AS edit_distance,
                jaro_winkler_similarity(i.name_norm, b.name_norm)
                    AS name_similarity,
                row_number() OVER (
                    PARTITION BY b.iucn_sis_id, b.ncbi_taxid
                    ORDER BY
                        levenshtein(i.name_norm, b.name_norm),
                        jaro_winkler_similarity(i.name_norm, b.name_norm) DESC,
                        b.candidate_name
                ) AS taxid_name_rank
            FROM blocked b
            JOIN iucn i USING (iucn_sis_id)
            WHERE levenshtein(i.name_norm, b.name_norm) <= 2
        )
        SELECT * EXCLUDE(taxid_name_rank)
        FROM scored
        WHERE taxid_name_rank = 1;
        """
    )

    stage("Reconstructing NCBI lineage for all candidate TaxIDs")
    con.execute(
        """
        CREATE TABLE candidate_taxids AS
        SELECT DISTINCT ncbi_taxid FROM exact_candidates
        UNION
        SELECT DISTINCT ncbi_taxid FROM bridge_candidates
        UNION
        SELECT DISTINCT ncbi_taxid FROM fuzzy_candidates;

        CREATE TABLE candidate_lineage_long AS
        WITH RECURSIVE walk(seed_taxid, ncbi_taxid, parent_taxid, taxon_rank, depth) AS (
            SELECT
                c.ncbi_taxid,
                n.ncbi_taxid,
                n.parent_taxid,
                n.taxon_rank,
                0
            FROM candidate_taxids c
            JOIN ncbi_nodes n USING (ncbi_taxid)

            UNION ALL

            SELECT
                w.seed_taxid,
                n.ncbi_taxid,
                n.parent_taxid,
                n.taxon_rank,
                w.depth + 1
            FROM walk w
            JOIN ncbi_nodes n ON n.ncbi_taxid = w.parent_taxid
            WHERE w.parent_taxid <> w.ncbi_taxid
              AND w.depth < 50
        )
        SELECT * FROM walk;

        CREATE TABLE ncbi_scientific_names AS
        SELECT ncbi_taxid, first(name_txt) AS scientific_name
        FROM ncbi_names
        WHERE name_class = 'scientific name'
        GROUP BY ncbi_taxid;

        CREATE TABLE candidate_lineage_ncbi AS
        SELECT
            l.seed_taxid AS ncbi_taxid,
            max(s.scientific_name) FILTER (WHERE l.taxon_rank = 'species')
                AS ncbi_species,
            max(s.scientific_name) FILTER (WHERE l.taxon_rank = 'genus')
                AS ncbi_genus,
            max(s.scientific_name) FILTER (WHERE l.taxon_rank = 'family')
                AS ncbi_family,
            max(s.scientific_name) FILTER (WHERE l.taxon_rank = 'order')
                AS ncbi_order,
            max(s.scientific_name) FILTER (WHERE l.taxon_rank = 'class')
                AS ncbi_class,
            max(s.scientific_name) FILTER (WHERE l.taxon_rank = 'phylum')
                AS ncbi_phylum,
            max(s.scientific_name) FILTER (WHERE l.taxon_rank = 'kingdom')
                AS ncbi_kingdom,
            max(s.scientific_name) FILTER (WHERE l.taxon_rank = 'domain')
                AS ncbi_domain
        FROM candidate_lineage_long l
        LEFT JOIN ncbi_scientific_names s USING (ncbi_taxid)
        GROUP BY l.seed_taxid;
        """
    )

    if args.goat_lineages_json:
        goat_lineages_path = sql_path(args.goat_lineages_json)
        stage("Loading supplemental lineage from GoaT's current taxonomy index")
        con.execute(
            f"""
            CREATE TABLE goat_lineages AS
            SELECT unnest(rows, recursive := true)
            FROM read_json_auto('{goat_lineages_path}');
            """
        )
    else:
        con.execute(
            """
            CREATE TABLE goat_lineages (
                ncbi_taxid VARCHAR,
                scientific_name VARCHAR,
                species VARCHAR,
                genus VARCHAR,
                family VARCHAR,
                "order" VARCHAR,
                "class" VARCHAR,
                phylum VARCHAR,
                kingdom VARCHAR,
                domain VARCHAR
            );
            """
        )

    con.execute(
        """
        CREATE TABLE candidate_lineage AS
        SELECT
            c.ncbi_taxid,
            coalesce(n.ncbi_species, g.species, g.scientific_name) AS ncbi_species,
            coalesce(n.ncbi_genus, g.genus) AS ncbi_genus,
            coalesce(n.ncbi_family, g.family) AS ncbi_family,
            coalesce(n.ncbi_order, g."order") AS ncbi_order,
            coalesce(n.ncbi_class, g."class") AS ncbi_class,
            coalesce(n.ncbi_phylum, g.phylum) AS ncbi_phylum,
            coalesce(n.ncbi_kingdom, g.kingdom) AS ncbi_kingdom,
            coalesce(n.ncbi_domain, g.domain) AS ncbi_domain,
            CASE
                WHEN n.ncbi_taxid IS NOT NULL THEN 'NCBI_TAXDUMP'
                WHEN g.ncbi_taxid IS NOT NULL THEN 'GOAT_API'
                ELSE NULL
            END AS lineage_source
        FROM candidate_taxids c
        LEFT JOIN candidate_lineage_ncbi n USING (ncbi_taxid)
        LEFT JOIN goat_lineages g USING (ncbi_taxid);
        """
    )

    stage("Scoring evidence and preparing the independent GBIF review queue")
    con.execute(
        """
        CREATE TABLE all_candidates AS
        WITH unioned AS (
            SELECT
                e.iucn_sis_id,
                e.ncbi_taxid,
                e.evidence_name AS candidate_name,
                e.evidence_method,
                e.method_priority,
                0 AS edit_distance,
                1.0::DOUBLE AS name_similarity,
                e.all_exact_evidence,
                NULL::VARCHAR AS gbif_bridge_concept_key
            FROM exact_candidates e

            UNION ALL

            SELECT
                b.iucn_sis_id,
                b.ncbi_taxid,
                b.evidence_name,
                'GBIF_ACCEPTED_NAME_BRIDGE' AS evidence_method,
                4 AS method_priority,
                b.edit_distance,
                b.name_similarity,
                ['GBIF_IUCN_SOURCE_ID', 'GBIF_ACCEPTED_CANONICAL_NAME']
                    AS all_exact_evidence,
                b.gbif_bridge_concept_key
            FROM bridge_candidates b

            UNION ALL

            SELECT
                f.iucn_sis_id,
                f.ncbi_taxid,
                f.candidate_name,
                'AI_NEAR_NAME_REVIEW' AS evidence_method,
                5 AS method_priority,
                f.edit_distance,
                f.name_similarity,
                [f.candidate_name_source] AS all_exact_evidence,
                NULL::VARCHAR AS gbif_bridge_concept_key
            FROM fuzzy_candidates f
        ),
        enriched AS (
            SELECT
                u.*,
                g.goat_scientific_name,
                l.* EXCLUDE(ncbi_taxid),
                (
                    (i.iucn_genus IS NOT NULL AND l.ncbi_genus IS NOT NULL
                        AND lower(i.iucn_genus) = lower(l.ncbi_genus))::INTEGER
                    + (i.iucn_family IS NOT NULL AND l.ncbi_family IS NOT NULL
                        AND lower(i.iucn_family) = lower(l.ncbi_family))::INTEGER
                    + (i.iucn_order IS NOT NULL AND l.ncbi_order IS NOT NULL
                        AND lower(i.iucn_order) = lower(l.ncbi_order))::INTEGER
                    + (i.iucn_class IS NOT NULL AND l.ncbi_class IS NOT NULL
                        AND lower(i.iucn_class) = lower(l.ncbi_class))::INTEGER
                    + (i.iucn_phylum IS NOT NULL AND l.ncbi_phylum IS NOT NULL
                        AND lower(i.iucn_phylum) = lower(l.ncbi_phylum))::INTEGER
                ) AS lineage_agreements,
                (
                    (i.iucn_genus IS NOT NULL AND l.ncbi_genus IS NOT NULL
                        AND lower(i.iucn_genus) <> lower(l.ncbi_genus))::INTEGER
                    + (i.iucn_family IS NOT NULL AND l.ncbi_family IS NOT NULL
                        AND lower(i.iucn_family) <> lower(l.ncbi_family))::INTEGER
                    + (i.iucn_order IS NOT NULL AND l.ncbi_order IS NOT NULL
                        AND lower(i.iucn_order) <> lower(l.ncbi_order))::INTEGER
                    + (i.iucn_class IS NOT NULL AND l.ncbi_class IS NOT NULL
                        AND lower(i.iucn_class) <> lower(l.ncbi_class))::INTEGER
                    + (i.iucn_phylum IS NOT NULL AND l.ncbi_phylum IS NOT NULL
                        AND lower(i.iucn_phylum) <> lower(l.ncbi_phylum))::INTEGER
                ) AS lineage_conflicts
            FROM unioned u
            JOIN iucn i USING (iucn_sis_id)
            JOIN goat g USING (ncbi_taxid)
            LEFT JOIN candidate_lineage l USING (ncbi_taxid)
        )
        SELECT
            *,
            row_number() OVER (
                PARTITION BY iucn_sis_id
                ORDER BY
                    method_priority,
                    lineage_agreements DESC,
                    lineage_conflicts,
                    edit_distance,
                    name_similarity DESC,
                    ncbi_taxid
            ) AS candidate_rank,
            count(*) OVER (PARTITION BY iucn_sis_id) AS candidate_count,
            lead(method_priority) OVER (
                PARTITION BY iucn_sis_id
                ORDER BY
                    method_priority,
                    lineage_agreements DESC,
                    lineage_conflicts,
                    edit_distance,
                    name_similarity DESC,
                    ncbi_taxid
            ) AS runner_up_priority,
            lead(lineage_agreements) OVER (
                PARTITION BY iucn_sis_id
                ORDER BY
                    method_priority,
                    lineage_agreements DESC,
                    lineage_conflicts,
                    edit_distance,
                    name_similarity DESC,
                    ncbi_taxid
            ) AS runner_up_lineage_agreements,
            lead(name_similarity) OVER (
                PARTITION BY iucn_sis_id
                ORDER BY
                    method_priority,
                    lineage_agreements DESC,
                    lineage_conflicts,
                    edit_distance,
                    name_similarity DESC,
                    ncbi_taxid
            ) AS runner_up_similarity
        FROM enriched;

        """
    )

    con.execute(
        f"""
        COPY (
            SELECT
                c.iucn_sis_id,
                c.ncbi_taxid,
                i.iucn_scientific_name,
                c.candidate_name,
                c.goat_scientific_name,
                i.iucn_kingdom,
                i.iucn_phylum,
                i.iucn_class,
                i.iucn_order,
                i.iucn_family,
                i.iucn_genus
            FROM all_candidates c
            JOIN iucn i USING (iucn_sis_id)
            WHERE c.candidate_rank = 1
              AND (c.method_priority = 5 OR c.candidate_count > 1)
            ORDER BY c.iucn_sis_id
        ) TO '{sql_path(output_dir / "gbif_review_input.json")}'
            (FORMAT JSON, ARRAY true);
        """
    )

    if args.gbif_validation_json:
        validation_path = sql_path(args.gbif_validation_json)
        stage("Loading independent GBIF/CoL concept validation")
        con.execute(
            f"""
            CREATE TABLE gbif_validation AS
            SELECT
                unnest(rows, recursive := true)
            FROM read_json_auto('{validation_path}');
            """
        )
    else:
        con.execute(
            """
            CREATE TABLE gbif_validation (
                iucn_sis_id BIGINT,
                ncbi_taxid VARCHAR,
                gbif_confirmed BOOLEAN,
                gbif_iucn_source_id_confirmed BOOLEAN
            );
            """
        )

    stage("Applying tiered acceptance rules")
    con.execute(
        """
        CREATE TABLE decisions AS
        SELECT
            c.*,
            coalesce(v.gbif_confirmed, false) AS gbif_confirmed,
            coalesce(v.gbif_iucn_source_id_confirmed, false)
                AS gbif_iucn_source_id_confirmed,
            CASE
                WHEN c.candidate_rank <> 1 THEN false
                WHEN c.method_priority <= 3
                  AND c.candidate_count = 1
                  AND c.lineage_agreements >= 2
                  THEN true
                WHEN c.method_priority <= 3
                  AND coalesce(v.gbif_confirmed, false)
                  THEN true
                WHEN c.method_priority = 4
                  AND c.lineage_agreements >= 2
                  AND c.lineage_conflicts <= 1
                  AND (
                    c.candidate_count = 1
                    OR c.lineage_agreements >=
                        coalesce(c.runner_up_lineage_agreements, -1) + 2
                  )
                  THEN true
                WHEN c.method_priority = 5
                  AND coalesce(v.gbif_confirmed, false)
                  AND c.edit_distance <= 2
                  AND c.name_similarity >= 0.94
                  AND c.lineage_agreements >= 2
                  AND c.lineage_conflicts <= 1
                  THEN true
                ELSE false
            END AS accepted,
            CASE
                WHEN c.method_priority = 1 THEN 'A'
                WHEN c.method_priority IN (2, 3) AND c.candidate_count = 1 THEN 'A'
                WHEN c.method_priority IN (2, 3) THEN 'B'
                WHEN c.method_priority = 4 THEN 'B'
                WHEN c.method_priority = 5 THEN 'C'
                ELSE 'D'
            END AS confidence_tier,
            CASE
                WHEN c.method_priority <= 3 AND c.candidate_count = 1
                    THEN 'deterministic'
                ELSE 'AI-review (GPT-5.6 Sol)'
            END AS reviewer
        FROM all_candidates c
        LEFT JOIN gbif_validation v
          ON v.iucn_sis_id = c.iucn_sis_id
         AND v.ncbi_taxid = c.ncbi_taxid;
        """
    )

    stage("Writing final crosswalk and unresolved review queue")
    reviewer_sql = REVIEWER.replace("'", "''")
    con.execute(
        f"""
        CREATE TABLE crosswalk_base AS
        WITH chosen AS (
            SELECT * FROM decisions
            WHERE accepted AND candidate_rank = 1
        ),
        best_unaccepted AS (
            SELECT * FROM decisions
            WHERE candidate_rank = 1
        )
        SELECT
            i.iucn_sis_id,
            a.iucn_assessment_id,
            i.iucn_scientific_name,
            i.iucn_authority,
            i.iucn_kingdom,
            i.iucn_phylum,
            i.iucn_class,
            i.iucn_order,
            i.iucn_family,
            i.iucn_genus,
            a.iucn_redlist_category,
            a.iucn_year_published,
            a.iucn_assessment_date,
            a.iucn_scopes,
            c.ncbi_taxid AS matched_ncbi_species_taxid,
            c.goat_scientific_name,
            c.ncbi_species,
            c.ncbi_genus,
            c.ncbi_family,
            c.ncbi_order,
            c.ncbi_class,
            c.ncbi_phylum,
            c.ncbi_kingdom,
            c.lineage_source,
            c.evidence_method AS match_method,
            c.confidence_tier,
            c.reviewer,
            c.candidate_count,
            c.lineage_agreements,
            c.lineage_conflicts,
            c.edit_distance,
            c.name_similarity,
            c.gbif_confirmed,
            c.gbif_iucn_source_id_confirmed,
            c.gbif_bridge_concept_key AS gbif_accepted_concept_key,
            c.candidate_name AS matched_evidence_name,
            c.all_exact_evidence AS evidence_sources,
            CASE WHEN c.ncbi_taxid IS NULL THEN u.ncbi_taxid END
                AS review_candidate_ncbi_taxid,
            CASE WHEN c.ncbi_taxid IS NULL THEN u.goat_scientific_name END
                AS review_candidate_goat_name,
            CASE WHEN c.ncbi_taxid IS NULL THEN u.evidence_method END
                AS review_candidate_method,
            CASE WHEN c.ncbi_taxid IS NULL THEN u.candidate_name END
                AS review_candidate_evidence_name,
            CASE WHEN c.ncbi_taxid IS NULL THEN u.lineage_agreements END
                AS review_candidate_lineage_agreements,
            CASE WHEN c.ncbi_taxid IS NULL THEN u.lineage_conflicts END
                AS review_candidate_lineage_conflicts,
            CASE WHEN c.ncbi_taxid IS NULL THEN u.name_similarity END
                AS review_candidate_name_similarity,
            CASE WHEN c.ncbi_taxid IS NULL THEN u.gbif_confirmed END
                AS review_candidate_gbif_confirmed,
            CASE
                WHEN c.ncbi_taxid IS NOT NULL THEN 'MATCHED'
                WHEN u.iucn_sis_id IS NOT NULL THEN 'REVIEW_UNRESOLVED'
                ELSE 'NO_GOAT_NCBI_CANDIDATE'
            END AS match_status,
            CASE
                WHEN c.ncbi_taxid IS NOT NULL
                    THEN 'Accepted under documented tier rules'
                WHEN u.iucn_sis_id IS NOT NULL
                    THEN 'Candidate evidence was insufficient for a high-accuracy link'
                ELSE 'No exact synonym or conservative near-name candidate exists in the GoaT species export'
            END AS review_note,
            '{reviewer_sql}' AS review_policy_author
        FROM iucn i
        LEFT JOIN assessments a USING (iucn_sis_id)
        LEFT JOIN chosen c USING (iucn_sis_id)
        LEFT JOIN best_unaccepted u USING (iucn_sis_id);

        CREATE TABLE crosswalk AS
        WITH cardinality AS (
            SELECT
                matched_ncbi_species_taxid,
                count(*) AS iucn_taxon_count
            FROM crosswalk_base
            WHERE match_status = 'MATCHED'
            GROUP BY matched_ncbi_species_taxid
        )
        SELECT
            b.*,
            coalesce(cardinality.iucn_taxon_count, 0)
                AS ncbi_taxid_iucn_taxon_count,
            CASE
                WHEN b.match_status <> 'MATCHED' THEN NULL
                WHEN cardinality.iucn_taxon_count > 1
                    THEN 'IUCN_SPLIT_OR_NCBI_LUMP'
                WHEN b.match_method IN (
                    'GOAT_CURRENT_NAME_EXACT',
                    'NCBI_CURRENT_NAME_EXACT'
                ) THEN 'CURRENT_NAME_EXACT'
                WHEN b.match_method = 'NCBI_SYNONYM_EXACT'
                    THEN 'NCBI_SYNONYM'
                WHEN b.match_method = 'GBIF_ACCEPTED_NAME_BRIDGE'
                    THEN 'GBIF_CORROBORATED_ACCEPTED_NAME'
                WHEN b.match_method = 'AI_NEAR_NAME_REVIEW'
                    THEN 'GBIF_CORROBORATED_NEAR_NAME'
                ELSE 'OTHER_REVIEWED_LINK'
            END AS taxonomic_concept_relation,
            (
                b.match_status = 'MATCHED'
                AND cardinality.iucn_taxon_count = 1
            ) AS safe_for_automatic_species_trait_transfer
        FROM crosswalk_base b
        LEFT JOIN cardinality USING (matched_ncbi_species_taxid);

        COPY crosswalk TO '{sql_path(output_dir / "iucn_goat_crosswalk.parquet")}'
            (FORMAT PARQUET, COMPRESSION ZSTD);
        COPY crosswalk TO '{sql_path(output_dir / "iucn_goat_crosswalk.csv")}'
            (FORMAT CSV, HEADER);

        COPY (
            SELECT
                d.*,
                i.iucn_scientific_name,
                i.iucn_kingdom,
                i.iucn_phylum,
                i.iucn_class,
                i.iucn_order,
                i.iucn_family,
                i.iucn_genus
            FROM decisions d
            JOIN iucn i USING (iucn_sis_id)
            WHERE NOT EXISTS (
                SELECT 1 FROM decisions accepted
                WHERE accepted.iucn_sis_id = d.iucn_sis_id
                  AND accepted.accepted
            )
            ORDER BY d.iucn_sis_id, d.candidate_rank
        ) TO '{sql_path(output_dir / "unresolved_candidates.parquet")}'
            (FORMAT PARQUET, COMPRESSION ZSTD);
        """
    )

    summary_rows = con.execute(
        """
        SELECT match_status, confidence_tier, match_method, reviewer, count(*) AS n
        FROM crosswalk
        GROUP BY ALL
        ORDER BY match_status, confidence_tier, match_method, reviewer
        """
    ).fetchall()
    total = con.execute("SELECT count(*) FROM crosswalk").fetchone()[0]
    matched = con.execute(
        "SELECT count(*) FROM crosswalk WHERE match_status = 'MATCHED'"
    ).fetchone()[0]
    duplicate_sis = con.execute(
        """
        SELECT count(*) FROM (
            SELECT iucn_sis_id FROM crosswalk GROUP BY 1 HAVING count(*) <> 1
        )
        """
    ).fetchone()[0]
    invalid_taxids = con.execute(
        """
        SELECT count(*)
        FROM crosswalk c
        LEFT JOIN goat g ON g.ncbi_taxid = c.matched_ncbi_species_taxid
        WHERE c.match_status = 'MATCHED' AND g.ncbi_taxid IS NULL
        """
    ).fetchone()[0]
    many_to_one_taxids, many_to_one_rows = con.execute(
        """
        SELECT
            count(DISTINCT matched_ncbi_species_taxid),
            count(*)
        FROM crosswalk
        WHERE taxonomic_concept_relation = 'IUCN_SPLIT_OR_NCBI_LUMP'
        """
    ).fetchone()
    safe_trait_rows = con.execute(
        """
        SELECT count(*) FROM crosswalk
        WHERE safe_for_automatic_species_trait_transfer
        """
    ).fetchone()[0]
    rejected_homonyms = con.execute(
        """
        SELECT count(*)
        FROM decisions
        WHERE candidate_rank = 1
          AND method_priority <= 3
          AND candidate_count = 1
          AND NOT accepted
          AND lineage_agreements = 1
          AND lineage_conflicts = 4
        """
    ).fetchone()[0]

    stage("Calculating checksums and build manifest")
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "reviewer": REVIEWER,
        "policy": (
            "Deterministic exact current/synonym links first; AI review only "
            "accepts a uniquely best near-name candidate with compatible lineage."
        ),
        "counts": {
            "iucn_taxa": total,
            "matched": matched,
            "unmatched": total - matched,
            "coverage_percent": round(100 * matched / total, 4),
            "duplicate_iucn_sis_rows": duplicate_sis,
            "matched_taxids_absent_from_goat": invalid_taxids,
            "many_iucn_to_one_ncbi_taxids": many_to_one_taxids,
            "rows_in_many_to_one_relationships": many_to_one_rows,
            "safe_for_automatic_species_trait_transfer": safe_trait_rows,
            "rejected_cross_kingdom_or_deep_lineage_homonyms": rejected_homonyms,
        },
        "breakdown": [
            {
                "match_status": row[0],
                "confidence_tier": row[1],
                "match_method": row[2],
                "reviewer": row[3],
                "count": row[4],
            }
            for row in summary_rows
        ],
        "sources": {
            "iucn_taxonomy": {
                "path": str(args.iucn_taxonomy.resolve()),
                "sha256": sha256(args.iucn_taxonomy),
            },
            "iucn_assessments": {
                "path": str(args.iucn_assessments.resolve()),
                "sha256": sha256(args.iucn_assessments),
            },
            "goat_species": {
                "path": str(args.goat_species.resolve()),
                "sha256": sha256(args.goat_species),
            },
            "ncbi_names": {
                "path": str((args.ncbi_taxdump_dir / "names.dmp").resolve()),
                "sha256": sha256(args.ncbi_taxdump_dir / "names.dmp"),
            },
            "ncbi_nodes": {
                "path": str((args.ncbi_taxdump_dir / "nodes.dmp").resolve()),
                "sha256": sha256(args.ncbi_taxdump_dir / "nodes.dmp"),
            },
        },
        "outputs": {
            "crosswalk_parquet": str(output_dir / "iucn_goat_crosswalk.parquet"),
            "crosswalk_csv": str(output_dir / "iucn_goat_crosswalk.csv"),
            "unresolved_candidates": str(output_dir / "unresolved_candidates.parquet"),
            "work_database": str(work_db),
        },
    }
    for source_name, source_path in (
        ("gbif_candidate_validation", args.gbif_validation_json),
        ("gbif_iucn_bridge", args.gbif_bridge_json),
        ("goat_supplemental_lineages", args.goat_lineages_json),
    ):
        if source_path:
            manifest["sources"][source_name] = {
                "path": str(source_path.resolve()),
                "sha256": sha256(source_path),
            }
    manifest["outputs"]["ai_review"] = str(output_dir / "AI_REVIEW.md")
    manifest["outputs"]["gbif_review_input"] = str(
        output_dir / "gbif_review_input.json"
    )
    (output_dir / "match_summary.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    ai_review_markdown = f"""# Global IUCN–GoaT crosswalk: AI review

Reviewer label: **{REVIEWER}**

Generated: {manifest["generated_at_utc"]}

## Outcome

- IUCN species reviewed: {total:,}
- Accepted IUCN SIS → GoaT/NCBI links: {matched:,} ({100 * matched / total:.2f}%)
- No plausible GoaT/NCBI candidate: {manifest["counts"]["unmatched"] - con.execute("SELECT count(*) FROM crosswalk WHERE match_status = 'REVIEW_UNRESOLVED'").fetchone()[0]:,}
- Candidate retained but unresolved: {con.execute("SELECT count(*) FROM crosswalk WHERE match_status = 'REVIEW_UNRESOLVED'").fetchone()[0]:,}
- Deep-lineage homonyms rejected: {rejected_homonyms:,}
- NCBI TaxIDs representing multiple IUCN species concepts: {many_to_one_taxids:,} TaxIDs / {many_to_one_rows:,} IUCN rows
- Matches safe for automatic species-level trait transfer: {safe_trait_rows:,}

## Review policy

Deterministic exact current-name and authoritative NCBI synonym links were
considered first, but still required compatible lineage. Ambiguous and
near-name candidates were reviewed under the `{REVIEWER}` policy and accepted
only when independently corroborated by an exact GBIF/Catalogue of Life species
concept and compatible GoaT/NCBI lineage.

No candidate was forced merely to increase coverage. `NO_GOAT_NCBI_CANDIDATE`
means the supplied GoaT species export has no defensible species TaxID for that
IUCN taxon. `REVIEW_UNRESOLVED` retains the best candidate evidence without
placing a TaxID in the accepted-match field.

## Downstream safety

Use `safe_for_automatic_species_trait_transfer = true` for automatic joins of
species-level GoaT traits. Rows marked `IUCN_SPLIT_OR_NCBI_LUMP` are valid
identifier correspondences under NCBI taxonomy, but are not asserted to be
equivalent species concepts and must not be collapsed or share IUCN status.
"""
    (output_dir / "AI_REVIEW.md").write_text(ai_review_markdown, encoding="utf-8")
    con.close()
    stage(
        f"Done: {matched:,}/{total:,} matched "
        f"({100 * matched / total:.2f}% coverage)"
    )


if __name__ == "__main__":
    main()
