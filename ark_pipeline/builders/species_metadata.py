#!/usr/bin/env python3
"""Build the global species dimension consumed by the Ark-IV map pipeline.

The application keeps IUCN ``internalTaxonId`` as its stable species key. IUCN
provides names, threat categories, and ecosystem membership; the reviewed
IUCN-to-GoaT crosswalk provides NCBI lineage and the safe species-level join;
and the enriched GoaT export provides sequencing metadata.

Species IDs present in an H3 input but absent from the IUCN export are retained
as explicit placeholder rows. This keeps preview builds usable without hiding
the upstream taxonomy-version mismatch. The generated report records them for
the later lossless audit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from ark_pipeline.runtime.progress import monitor_query
from ark_pipeline.runtime.resources import configure_duckdb


def sql_path(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _require_file(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def build_global_species(
    *,
    crosswalk_path: Path,
    assessments_path: Path,
    goat_species_path: Path,
    ncbi_names_path: Path | None = None,
    ncbi_nodes_path: Path | None = None,
    gbif_backbone_path: Path | None = None,
    edge_species_path: Path | None = None,
    h3_paths: list[Path],
    output_dir: Path,
) -> dict[str, Any]:
    """Create ``species.parquet`` and ``species_systems.parquet``.

    The build is intentionally set based: even the H3-only placeholder lookup
    is performed inside DuckDB, so Python never materializes millions of IDs.
    """
    crosswalk_path = _require_file(crosswalk_path)
    assessments_path = _require_file(assessments_path)
    goat_species_path = _require_file(goat_species_path)
    ncbi_names_path = _require_file(ncbi_names_path) if ncbi_names_path else None
    ncbi_nodes_path = _require_file(ncbi_nodes_path) if ncbi_nodes_path else None
    if (ncbi_names_path is None) != (ncbi_nodes_path is None):
        raise ValueError("NCBI names and nodes must be supplied together")
    gbif_backbone_path = (
        _require_file(gbif_backbone_path) if gbif_backbone_path is not None else None
    )
    edge_species_path = (
        _require_file(edge_species_path) if edge_species_path is not None else None
    )
    h3_paths = [_require_file(path) for path in h3_paths]
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    connection = duckdb.connect()
    configure_duckdb(connection)
    stop_query_monitor = monitor_query(connection)
    try:
        connection.execute(
            "CREATE TEMP TABLE crosswalk AS SELECT * FROM read_parquet(?)",
            [str(crosswalk_path)],
        )
        crosswalk_columns = {
            row[0] for row in connection.execute("DESCRIBE crosswalk").fetchall()
        }
        if "gbif_accepted_concept_key" not in crosswalk_columns:
            connection.execute(
                "ALTER TABLE crosswalk ADD COLUMN gbif_accepted_concept_key VARCHAR"
            )
        connection.execute(
            """
            CREATE TEMP TABLE assessments AS
            SELECT *
            FROM read_csv_auto(
                ?, all_varchar = true, sample_size = 200000,
                ignore_errors = false
            )
            """,
            [str(assessments_path)],
        )
        connection.execute(
            """
            CREATE TEMP TABLE goat AS
            SELECT *
            FROM read_csv_auto(
                ?, delim = '\t', quote = '"', all_varchar = true,
                sample_size = -1, ignore_errors = false
            )
            """,
            [str(goat_species_path)],
        )
        goat_columns = {
            row[0] for row in connection.execute("DESCRIBE goat").fetchall()
        }
        # Older registered exports did not retain the full lineage. They remain
        # readable for direct species evidence, but cannot seed broader lineage
        # representation until refreshed with the current downloader.
        for column in (
            "scientific_name", "taxon_rank", "kingdom", "family", "genus"
        ):
            if column not in goat_columns:
                connection.execute(f'ALTER TABLE goat ADD COLUMN "{column}" VARCHAR')
        if gbif_backbone_path is not None:
            connection.execute(
                """
                CREATE TEMP TABLE gbif_name_matches AS
                WITH accepted_species AS (
                    SELECT
                        lower(trim(canonicalName)) AS canonical_name,
                        min(cast(taxonID AS VARCHAR)) AS taxon_id,
                        count(DISTINCT taxonID) AS candidate_count
                    FROM read_csv(
                        ?, delim = '\t', header = true, auto_detect = true,
                        ignore_errors = false
                    )
                    WHERE lower(trim(taxonRank)) = 'species'
                      AND lower(trim(taxonomicStatus)) = 'accepted'
                      AND nullif(trim(canonicalName), '') IS NOT NULL
                    GROUP BY canonical_name
                )
                SELECT canonical_name, taxon_id
                FROM accepted_species
                WHERE candidate_count = 1
                """,
                [str(gbif_backbone_path)],
            )
        else:
            connection.execute(
                "CREATE TEMP TABLE gbif_name_matches "
                "(canonical_name VARCHAR, taxon_id VARCHAR)"
            )
        if edge_species_path is not None:
            connection.execute(
                """
                CREATE TEMP TABLE edge_species AS
                SELECT
                    cast(try_cast(rl_id AS BIGINT) AS VARCHAR) AS iucn_sis_id,
                    nullif(trim(group_name), '') AS edge_group_name
                FROM read_csv(
                    ?, delim = '\t', header = true, all_varchar = true,
                    ignore_errors = false
                )
                WHERE try_cast(rl_id AS BIGINT) IS NOT NULL
                QUALIFY row_number() OVER (
                    PARTITION BY try_cast(rl_id AS BIGINT)
                    ORDER BY try_cast(edge_rank AS DOUBLE) NULLS LAST, group_name
                ) = 1
                """,
                [str(edge_species_path)],
            )
        else:
            connection.execute(
                "CREATE TEMP TABLE edge_species "
                "(iucn_sis_id VARCHAR, edge_group_name VARCHAR)"
            )

        connection.execute(
            """
            CREATE TEMP TABLE goat_traits AS
            SELECT
                taxon_id,
                lower(trim(taxon_rank)) AS taxon_rank,
                CASE upper(trim(kingdom))
                    WHEN 'ANIMALIA' THEN 'METAZOA'
                    WHEN 'PLANTAE' THEN 'VIRIDIPLANTAE'
                    ELSE upper(trim(kingdom))
                END AS kingdom,
                upper(trim(family)) AS family,
                upper(trim(genus)) AS genus,
                assembly_level,
                try_cast(busco_completeness AS DOUBLE) AS busco_completeness,
                ebp_standard_criteria,
                in_progress,
                resampling_required,
                sample_acquired,
                sequencing_status,
                (
                    nullif(sample_acquired, '') IS NOT NULL
                    OR nullif(in_progress, '') IS NOT NULL
                    OR nullif(ebp_standard_criteria, '') IS NOT NULL
                    OR (
                        lower(coalesce(assembly_level, ''))
                            IN ('chromosome', 'complete genome')
                        AND coalesce(
                            try_cast(busco_completeness AS DOUBLE), 0
                        ) >= 90
                    )
                ) AS has_qualifying_dna_evidence
            FROM goat
            """
        )
        if ncbi_names_path is not None:
            connection.execute(
                """
                CREATE TEMP TABLE lineage_rank_identities AS
                WITH names AS (
                    SELECT
                        trim(column0, E' \t') AS taxon_id,
                        trim(column1, E' \t') AS scientific_name,
                        trim(column3, E' \t') AS name_class
                    FROM read_csv(
                        ?, delim='|', header=false, all_varchar=true,
                        columns={
                            'column0':'VARCHAR', 'column1':'VARCHAR',
                            'column2':'VARCHAR', 'column3':'VARCHAR',
                            'column4':'VARCHAR'
                        }
                    )
                ), nodes AS (
                    SELECT
                        trim(column0, E' \t') AS taxon_id,
                        lower(trim(column2, E' \t')) AS taxon_rank
                    FROM read_csv(
                        ?, delim='|', header=false, all_varchar=true,
                        columns={
                            'column0':'VARCHAR', 'column1':'VARCHAR',
                            'column2':'VARCHAR', 'column3':'VARCHAR',
                            'column4':'VARCHAR', 'column5':'VARCHAR',
                            'column6':'VARCHAR', 'column7':'VARCHAR',
                            'column8':'VARCHAR', 'column9':'VARCHAR',
                            'column10':'VARCHAR', 'column11':'VARCHAR',
                            'column12':'VARCHAR', 'column13':'VARCHAR'
                        }
                    )
                )
                SELECT
                    nodes.taxon_rank,
                    upper(trim(names.scientific_name)) AS scientific_name
                FROM names
                JOIN nodes USING (taxon_id)
                WHERE names.name_class = 'scientific name'
                  AND nodes.taxon_rank IN ('family', 'genus')
                GROUP BY 1, 2
                HAVING count(DISTINCT taxon_id) = 1
                """,
                [str(ncbi_names_path), str(ncbi_nodes_path)],
            )
        else:
            connection.execute(
                """
                CREATE TEMP TABLE lineage_rank_identities AS
                SELECT
                    lower(trim(taxon_rank)) AS taxon_rank,
                    upper(trim(scientific_name)) AS scientific_name
                FROM goat
                WHERE lower(trim(taxon_rank)) IN ('family', 'genus')
                  AND nullif(trim(scientific_name), '') IS NOT NULL
                GROUP BY 1, 2
                HAVING count(DISTINCT taxon_id) = 1
                """
            )
        connection.execute(
            """
            CREATE TEMP TABLE verified_goat_families AS
            SELECT scientific_name AS family
            FROM lineage_rank_identities
            WHERE taxon_rank = 'family'
            """
        )
        connection.execute(
            """
            CREATE TEMP TABLE verified_goat_genera AS
            SELECT
                DISTINCT upper(trim(goat.family)) AS family,
                identity.scientific_name AS genus
            FROM goat
            JOIN lineage_rank_identities identity
              ON identity.taxon_rank = 'genus'
             AND identity.scientific_name = upper(trim(goat.scientific_name))
            WHERE lower(trim(goat.taxon_rank)) = 'genus'
              AND nullif(trim(goat.family), '') IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE TEMP TABLE covered_lineages AS
            SELECT DISTINCT
                g.kingdom,
                g.family,
                verified_genus.genus
            FROM goat_traits g
            JOIN verified_goat_families verified_family
              ON verified_family.family = g.family
            LEFT JOIN verified_goat_genera verified_genus
              ON verified_genus.family = g.family
             AND verified_genus.genus = g.genus
            WHERE g.taxon_rank = 'species'
              AND g.has_qualifying_dna_evidence
              AND nullif(g.kingdom, '') IS NOT NULL
              AND nullif(g.family, '') IS NOT NULL
            """
        )

        connection.execute(
            """
            CREATE TEMP TABLE species_base AS
            SELECT
                cast(c.iucn_sis_id AS VARCHAR) AS gbif_accepted_id,
                cast(c.iucn_sis_id AS VARCHAR) AS iucn_sis_id,
                cast(c.iucn_assessment_id AS VARCHAR) AS iucn_assessment_id,
                coalesce(
                    nullif(cast(c.gbif_accepted_concept_key AS VARCHAR), ''),
                    gbif.taxon_id
                ) AS gbif_taxon_id,
                nullif(cast(c.matched_ncbi_species_taxid AS VARCHAR), '') AS goat_taxon_id,
                c.iucn_scientific_name AS species_name,
                coalesce(nullif(c.ncbi_family, ''), c.iucn_family, '') AS family,
                CASE c.iucn_redlist_category
                    WHEN 'Lower Risk/near threatened' THEN 'Near Threatened'
                    WHEN 'Lower Risk/least concern' THEN 'Least Concern'
                    ELSE coalesce(c.iucn_redlist_category, 'Not Assessed')
                END AS redlist_category,
                CASE
                    WHEN coalesce(c.safe_for_automatic_species_trait_transfer, false)
                         AND coalesce(g.has_qualifying_dna_evidence, false)
                        THEN true
                    ELSE false
                END AS has_dna_species_level,
                EXISTS (
                    SELECT 1 FROM covered_lineages l
                    WHERE l.kingdom = CASE upper(coalesce(nullif(c.ncbi_kingdom, ''), c.iucn_kingdom))
                        WHEN 'ANIMALIA' THEN 'METAZOA'
                        WHEN 'PLANTAE' THEN 'VIRIDIPLANTAE'
                        ELSE upper(coalesce(nullif(c.ncbi_kingdom, ''), c.iucn_kingdom))
                      END
                      AND l.family = upper(coalesce(nullif(c.ncbi_family, ''), c.iucn_family))
                      AND l.genus = upper(coalesce(nullif(c.ncbi_genus, ''), c.iucn_genus))
                ) AS genus_has_dna,
                EXISTS (
                    SELECT 1 FROM covered_lineages l
                    WHERE l.kingdom = CASE upper(coalesce(nullif(c.ncbi_kingdom, ''), c.iucn_kingdom))
                        WHEN 'ANIMALIA' THEN 'METAZOA'
                        WHEN 'PLANTAE' THEN 'VIRIDIPLANTAE'
                        ELSE upper(coalesce(nullif(c.ncbi_kingdom, ''), c.iucn_kingdom))
                      END
                      AND l.family = upper(coalesce(nullif(c.ncbi_family, ''), c.iucn_family))
                ) AS family_has_dna,
                c.matched_ncbi_species_taxid IS NULL OR g.taxon_id IS NULL
                    AS goat_data_deficient,
                CASE
                    WHEN coalesce(c.safe_for_automatic_species_trait_transfer, false)
                        THEN nullif(g.resampling_required, '')
                END AS goat_resampling_required,
                edge.edge_group_name,
                coalesce(c.safe_for_automatic_species_trait_transfer, false)
                    AND (
                        coalesce(g.ebp_standard_criteria, '') LIKE '%6.7%'
                        OR coalesce(g.ebp_standard_criteria, '') LIKE '%6.C%'
                    ) AS has_ebp_criteria_evidence
            FROM crosswalk c
            LEFT JOIN goat_traits g
              ON g.taxon_id = c.matched_ncbi_species_taxid
            LEFT JOIN gbif_name_matches gbif
              ON gbif.canonical_name = lower(trim(c.iucn_scientific_name))
            LEFT JOIN edge_species edge
              ON edge.iucn_sis_id = cast(c.iucn_sis_id AS VARCHAR)
            """
        )

        connection.execute(
            "CREATE TEMP TABLE h3_species_ids (gbif_accepted_id VARCHAR PRIMARY KEY)"
        )
        for path in h3_paths:
            columns = {
                row[0]
                for row in connection.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
                ).fetchall()
            }
            if "species_ids" in columns:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO h3_species_ids
                    SELECT DISTINCT cast(ids.species_id AS VARCHAR)
                    FROM read_parquet(?), unnest(species_ids) AS ids(species_id)
                    WHERE ids.species_id IS NOT NULL
                    """,
                    [str(path)],
                )
            elif "gbif_accepted_ids" in columns:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO h3_species_ids
                    SELECT DISTINCT cast(ids.species_id AS VARCHAR)
                    FROM read_parquet(?), unnest(gbif_accepted_ids) AS ids(species_id)
                    WHERE ids.species_id IS NOT NULL
                    """,
                    [str(path)],
                )
            else:
                raise ValueError(f"Unsupported H3 species-list schema: {path}")

        connection.execute(
            """
            CREATE TEMP TABLE missing_h3_species AS
            SELECT ids.gbif_accepted_id
            FROM h3_species_ids ids
            LEFT JOIN species_base species USING (gbif_accepted_id)
            WHERE species.gbif_accepted_id IS NULL
            """
        )
        connection.execute(
            """
            CREATE TEMP TABLE species AS
            SELECT * FROM species_base
            UNION ALL
            SELECT
                gbif_accepted_id,
                gbif_accepted_id AS iucn_sis_id,
                NULL::VARCHAR AS iucn_assessment_id,
                NULL::VARCHAR AS gbif_taxon_id,
                NULL::VARCHAR AS goat_taxon_id,
                'IUCN taxon ' || gbif_accepted_id AS species_name,
                '' AS family,
                'Not Assessed' AS redlist_category,
                false AS has_dna_species_level,
                false AS genus_has_dna,
                false AS family_has_dna,
                true AS goat_data_deficient,
                NULL::VARCHAR AS goat_resampling_required,
                NULL::VARCHAR AS edge_group_name,
                false AS has_ebp_criteria_evidence
            FROM missing_h3_species
            """
        )
        connection.execute(
            """
            CREATE TEMP TABLE species_systems AS
            SELECT DISTINCT
                cast(c.iucn_sis_id AS VARCHAR) AS gbif_accepted_id,
                CASE
                    WHEN trim(parts.raw_system) LIKE 'Freshwater%' THEN 'Freshwater'
                    WHEN trim(parts.raw_system) = 'Terrestrial' THEN 'Terrestrial'
                    WHEN trim(parts.raw_system) = 'Marine' THEN 'Marine'
                END AS system
            FROM crosswalk c
            JOIN assessments a
              ON try_cast(a.assessmentId AS BIGINT) = c.iucn_assessment_id,
            unnest(string_split(coalesce(a.systems, ''), '|')) AS parts(raw_system)
            WHERE trim(parts.raw_system) LIKE 'Freshwater%'
               OR trim(parts.raw_system) IN ('Terrestrial', 'Marine')
            """
        )

        species_path = output_dir / "species.parquet"
        systems_path = output_dir / "species_systems.parquet"
        connection.execute(
            f"""
            COPY (
                SELECT * FROM species ORDER BY gbif_accepted_id
            ) TO {sql_path(species_path)} (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        connection.execute(
            f"""
            COPY (
                SELECT * FROM species_systems ORDER BY gbif_accepted_id, system
            ) TO {sql_path(systems_path)} (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )

        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM species),
                (SELECT count(*) FROM species_systems),
                (SELECT count(*) FROM missing_h3_species),
                (SELECT count(*) FROM species WHERE has_dna_species_level),
                (SELECT count(*) FROM species WHERE genus_has_dna),
                (SELECT count(*) FROM species WHERE family_has_dna),
                (SELECT count(*) FROM species WHERE gbif_taxon_id IS NOT NULL),
                (SELECT count(*) FROM species WHERE goat_taxon_id IS NOT NULL),
                (SELECT count(*) FROM species WHERE edge_group_name IS NOT NULL),
                (SELECT count(*) FROM species WHERE goat_data_deficient),
                (SELECT count(*) FROM species
                    WHERE goat_resampling_required IS NOT NULL),
                (SELECT count(*) FROM species
                    WHERE redlist_category = 'Lower Risk/conservation dependent')
            """
        ).fetchone()
        missing_ids = [
            row[0]
            for row in connection.execute(
                "SELECT gbif_accepted_id FROM missing_h3_species ORDER BY try_cast(gbif_accepted_id AS BIGINT)"
            ).fetchall()
        ]
    finally:
        stop_query_monitor()
        connection.close()

    report = {
        "version": 2,
        "species": int(counts[0]),
        "species_systems": int(counts[1]),
        "h3_species_missing_iucn_metadata": int(counts[2]),
        "h3_species_missing_iucn_ids": missing_ids,
        "species_with_species_dna": int(counts[3]),
        "species_with_genus_dna": int(counts[4]),
        "species_with_family_dna": int(counts[5]),
        "species_with_gbif_taxon_id": int(counts[6]),
        "species_with_goat_taxon_id": int(counts[7]),
        "edge_species": int(counts[8]),
        "goat_data_deficient_species": int(counts[9]),
        "species_with_goat_resampling_required": int(counts[10]),
        "legacy_conservation_dependent_unscored_species": int(counts[11]),
        "inputs": {
            "crosswalk": str(crosswalk_path),
            "assessments": str(assessments_path),
            "goat_species": str(goat_species_path),
            "ncbi_names": str(ncbi_names_path) if ncbi_names_path else None,
            "ncbi_nodes": str(ncbi_nodes_path) if ncbi_nodes_path else None,
            "gbif_backbone": str(gbif_backbone_path) if gbif_backbone_path else None,
            "edge_species": str(edge_species_path) if edge_species_path else None,
            "h3": [str(path) for path in h3_paths],
        },
    }
    report_path = output_dir / "build-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"Built {output_dir / 'species.parquet'}")
    print(f"Built {output_dir / 'species_systems.parquet'}")
    print(f"Wrote {report_path}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crosswalk", type=Path, required=True)
    parser.add_argument("--assessments", type=Path, required=True)
    parser.add_argument("--goat-species", type=Path, required=True)
    parser.add_argument("--ncbi-names", type=Path)
    parser.add_argument("--ncbi-nodes", type=Path)
    parser.add_argument("--gbif-backbone", type=Path)
    parser.add_argument("--edge-species", type=Path)
    parser.add_argument(
        "--h3",
        type=Path,
        action="append",
        default=[],
        help="H3 list file whose species IDs must exist in the dimension; repeatable.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    build_global_species(
        crosswalk_path=args.crosswalk,
        assessments_path=args.assessments,
        goat_species_path=args.goat_species,
        ncbi_names_path=args.ncbi_names,
        ncbi_nodes_path=args.ncbi_nodes,
        gbif_backbone_path=args.gbif_backbone,
        edge_species_path=args.edge_species,
        h3_paths=args.h3,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
