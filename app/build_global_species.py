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
from pathlib import Path
from typing import Any

import duckdb


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
        connection.execute(
            """
            CREATE TEMP TABLE covered_lineages AS
            SELECT DISTINCT
                upper(coalesce(nullif(c.ncbi_genus, ''), c.iucn_genus)) AS genus,
                upper(coalesce(nullif(c.ncbi_family, ''), c.iucn_family)) AS family
            FROM crosswalk c
            LEFT JOIN goat_traits g
              ON g.taxon_id = c.matched_ncbi_species_taxid
            WHERE c.iucn_redlist_category IN ('Extinct', 'Extinct in the Wild')
               OR (
                    coalesce(c.safe_for_automatic_species_trait_transfer, false)
                    AND coalesce(g.has_qualifying_dna_evidence, false)
               )
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
                coalesce(c.iucn_redlist_category, 'Not Assessed') AS redlist_category,
                CASE
                    WHEN c.iucn_redlist_category IN ('Extinct', 'Extinct in the Wild')
                        THEN true
                    WHEN coalesce(c.safe_for_automatic_species_trait_transfer, false)
                         AND coalesce(g.has_qualifying_dna_evidence, false)
                        THEN true
                    ELSE false
                END AS has_dna_species_level,
                EXISTS (
                    SELECT 1 FROM covered_lineages l
                    WHERE l.genus = upper(coalesce(nullif(c.ncbi_genus, ''), c.iucn_genus))
                ) AS genus_has_dna,
                EXISTS (
                    SELECT 1 FROM covered_lineages l
                    WHERE l.family = upper(coalesce(nullif(c.ncbi_family, ''), c.iucn_family))
                ) AS family_has_dna,
                c.matched_ncbi_species_taxid IS NULL OR g.taxon_id IS NULL
                    AS goat_data_deficient,
                edge.edge_group_name,
                coalesce(c.safe_for_automatic_species_trait_transfer, false)
                    AND (
                        coalesce(g.ebp_standard_criteria, '') LIKE '%6.7%'
                        OR coalesce(g.ebp_standard_criteria, '') LIKE '%6.C%'
                    ) AS meets_ebp
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
                NULL::VARCHAR AS edge_group_name,
                false AS meets_ebp
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
                (SELECT count(*) FROM species WHERE goat_data_deficient)
            """
        ).fetchone()
        missing_ids = [
            row[0]
            for row in connection.execute(
                "SELECT gbif_accepted_id FROM missing_h3_species ORDER BY try_cast(gbif_accepted_id AS BIGINT)"
            ).fetchall()
        ]
    finally:
        connection.close()

    report = {
        "version": 1,
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
        "inputs": {
            "crosswalk": str(crosswalk_path),
            "assessments": str(assessments_path),
            "goat_species": str(goat_species_path),
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
        gbif_backbone_path=args.gbif_backbone,
        edge_species_path=args.edge_species,
        h3_paths=args.h3,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
