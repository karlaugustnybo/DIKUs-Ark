"""Read-only audit of registered metadata assumptions; writes only --output.

This records current policy consequences, not biological validation. No source,
receipt, crosswalk generation or serving output is modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from ark_pipeline.runtime.provenance import sha256


def audit(root: Path, crosswalk: Path, output: Path) -> dict:
    root, crosswalk, output = root.resolve(), crosswalk.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "acquisition/current.json"
    manifest = json.loads(manifest_path.read_text())
    records = {}
    for source_id in ("iucn-red-list-tabular", "goat-species"):
        for record in manifest["sources"][source_id]["files"]:
            path = root / record["path"]
            actual = sha256(path)
            if actual != record["sha256"]:
                raise ValueError(
                    f"Registered checksum mismatch: {source_id}/{record['logical_name']}"
                )
            records[record["logical_name"]] = {"path": str(path), "sha256": actual}
    summary = json.loads((crosswalk.parent / "match_summary.json").read_text())
    crosswalk_digest = sha256(crosswalk)
    receipt_path = crosswalk.parent / "receipt.json"
    receipt_checked = False
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get("status") != "passed":
            raise ValueError("Crosswalk receipt is not passed")
        for filename, digest in (
            (crosswalk.name, crosswalk_digest),
            ("match_summary.json", sha256(crosswalk.parent / "match_summary.json")),
        ):
            if receipt["outputs"][filename]["sha256"] != digest:
                raise ValueError(f"Crosswalk receipt mismatch: {filename}")
        receipt_checked = True
    goat_record = next(value for key, value in records.items() if key.endswith(".tsv"))
    expected = {
        "iucn_taxonomy": records["taxonomy.csv"],
        "iucn_assessments": records["assessments.csv"],
        "goat_species": goat_record,
    }
    for name, record in expected.items():
        if summary["sources"][name]["sha256"] != record["sha256"]:
            raise ValueError(f"Crosswalk source mismatch: {name}")
    report = {
        "reviewed_at": datetime.now(UTC).isoformat(),
        "scope": "registered metadata and supplied crosswalk; no spatial rebuild",
        "acquisition_manifest_sha256": sha256(manifest_path),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "duckdb_version": duckdb.__version__,
        "sources": records,
        "crosswalk_sha256": crosswalk_digest,
        "crosswalk_and_summary_receipt_checked": receipt_checked,
        "species_builder_sha256": sha256(
            Path(__file__).resolve().parents[1] / "ark_pipeline/builders/species_metadata.py"
        ),
        "crosswalk_source_check": "IUCN taxonomy, assessments and GoaT hashes matched",
        "crosswalk_summary_counts": summary["counts"],
    }
    con = duckdb.connect()
    con.execute("SET memory_limit='1GB'")
    con.execute("SET threads=2")
    con.execute("SET temp_directory=?", [str(output / "scratch")])

    def rows(sql):
        result = con.execute(sql)
        names = [col[0] for col in result.description]
        return [dict(zip(names, row)) for row in result.fetchall()]

    try:
        for name, record in [
            ("taxonomy", records["taxonomy.csv"]),
            ("assessments", records["assessments.csv"]),
            ("goat_raw", goat_record),
        ]:
            # Paths remain bound parameters, never interpolated into SQL text.
            relation = con.read_csv(
                record["path"],
                header=True,
                all_varchar=True,
                delimiter="\t" if name == "goat_raw" else ",",
                sample_size=-1,
            )
            relation.create_view(name)
        report["source_columns"] = {
            name: [r["column_name"] for r in rows(f"DESCRIBE {name}")]
            for name in ("taxonomy", "assessments", "goat_raw")
        }
        report["taxonomy_counts"] = rows(
            "SELECT count(*) AS n_rows, count(DISTINCT internalTaxonId) species_ids FROM taxonomy"
        )[0]
        report["assessment_counts"] = rows("""SELECT count(*) AS n_rows,
            count(DISTINCT internalTaxonId) species_ids, count(DISTINCT assessmentId) assessment_ids,
            min(try_cast(yearPublished AS INTEGER)) oldest_year,
            max(try_cast(yearPublished AS INTEGER)) newest_year,
            count(*) FILTER (WHERE nullif(trim(populationTrend),'') IS NOT NULL) with_population_trend
            FROM assessments""")[0]
        report["assessment_scopes"] = rows(
            "SELECT scopes, count(*) AS n_rows FROM assessments GROUP BY scopes ORDER BY n_rows DESC"
        )
        report["population_trends"] = rows(
            "SELECT populationTrend, count(*) AS n_rows FROM assessments GROUP BY populationTrend ORDER BY n_rows DESC"
        )
        report["assessment_categories"] = rows(
            "SELECT redlistCategory, count(*) AS n_rows FROM assessments GROUP BY redlistCategory ORDER BY n_rows DESC"
        )
        con.execute("""CREATE TEMP TABLE goat AS SELECT taxon_id, taxon_rank,
            upper(trim(genus)) AS genus_name, upper(trim(family)) AS family_name, upper(trim(kingdom)) AS kingdom_name,
            assembly_level, try_cast(busco_completeness AS DOUBLE) busco,
            ebp_standard_criteria, in_progress, sample_acquired, sequencing_status,
            resampling_required,
            (nullif(sample_acquired,'') IS NOT NULL OR nullif(in_progress,'') IS NOT NULL
             OR nullif(ebp_standard_criteria,'') IS NOT NULL OR
             (lower(coalesce(assembly_level,'')) IN ('chromosome','complete genome')
              AND coalesce(try_cast(busco_completeness AS DOUBLE),0)>=90)) qualifying
            FROM goat_raw""")
        report["goat_ranks"] = rows(
            "SELECT taxon_rank, count(*) AS n_rows FROM goat GROUP BY taxon_rank ORDER BY n_rows DESC"
        )
        report["goat_ids"] = rows(
            "SELECT count(*) AS n_rows, count(DISTINCT taxon_id) unique_ids FROM goat"
        )[0]
        if report["goat_ids"]["n_rows"] != report["goat_ids"]["unique_ids"]:
            raise ValueError("GoaT IDs are null or duplicated; joins would distort counts")
        report["goat_species_evidence"] = rows("""SELECT count(*) species,
            count(*) FILTER (WHERE qualifying) qualifying,
            count(*) FILTER (WHERE nullif(sample_acquired,'') IS NOT NULL) sample_acquired,
            count(*) FILTER (WHERE nullif(in_progress,'') IS NOT NULL) in_progress,
            count(*) FILTER (WHERE nullif(ebp_standard_criteria,'') IS NOT NULL) criteria_present,
            count(*) FILTER (WHERE qualifying AND nullif(resampling_required,'') IS NOT NULL) qualifying_with_resampling_field,
            count(*) FILTER (WHERE lower(coalesce(sample_acquired,'')) IN ('false','no','0','unknown')
                 OR lower(coalesce(in_progress,'')) IN ('false','no','0','unknown')) false_like_status
            FROM goat WHERE lower(taxon_rank)='species' """)[0]
        report["goat_criteria_values"] = rows(
            "SELECT ebp_standard_criteria, count(*) AS n_rows FROM goat WHERE lower(taxon_rank)='species' AND ebp_standard_criteria IS NOT NULL GROUP BY ebp_standard_criteria ORDER BY n_rows DESC LIMIT 15"
        )
        con.read_parquet(str(crosswalk)).create_view("crosswalk")
        identity = rows("""SELECT count(*) AS n_rows, count(DISTINCT iucn_sis_id) AS unique_ids,
            count(*) FILTER(WHERE match_status='MATCHED') AS matched_count FROM crosswalk""")[0]
        if (
            identity["n_rows"] != identity["unique_ids"]
            or identity["n_rows"] != summary["counts"]["iucn_taxa"]
            or identity["matched_count"] != summary["counts"]["matched"]
        ):
            raise ValueError("Crosswalk identities/counts do not reconcile with the summary")
        id_delta = rows("""SELECT count(*) AS differences FROM (
            (SELECT cast(internalTaxonId AS BIGINT) id FROM taxonomy EXCEPT SELECT iucn_sis_id FROM crosswalk)
            UNION ALL
            (SELECT iucn_sis_id FROM crosswalk EXCEPT SELECT cast(internalTaxonId AS BIGINT) FROM taxonomy))""")[
            0
        ]
        if id_delta["differences"]:
            raise ValueError("Crosswalk species set differs from registered IUCN taxonomy")
        report["crosswalk_identity_check"] = identity
        con.execute("""CREATE TEMP TABLE joined AS SELECT c.*,
            upper(coalesce(nullif(c.ncbi_genus,''),c.iucn_genus)) genus_key,
            upper(coalesce(nullif(c.ncbi_family,''),c.iucn_family)) family_key,
            coalesce(c.safe_for_automatic_species_trait_transfer,false) AND coalesce(g.qualifying,false) evidence,
            c.iucn_redlist_category IN ('Extinct','Extinct in the Wild') extinct,
            c.matched_ncbi_species_taxid IS NULL OR g.taxon_id IS NULL gdd,
            g.ebp_standard_criteria, g.resampling_required
            FROM crosswalk c LEFT JOIN goat g ON g.taxon_id=c.matched_ncbi_species_taxid""")
        report["linked_dna"] = rows("""SELECT count(*) species,
            count(*) FILTER (WHERE evidence) with_safe_qualifying_evidence,
            count(*) FILTER (WHERE extinct) extinct_or_ew,
            count(*) FILTER (WHERE extinct AND NOT evidence) extinct_without_safe_qualifying_evidence,
            count(*) FILTER (WHERE gdd) goat_data_deficient,
            count(*) FILTER (WHERE safe_for_automatic_species_trait_transfer AND
                (coalesce(ebp_standard_criteria,'') LIKE '%6.7%' OR coalesce(ebp_standard_criteria,'') LIKE '%6.C%')) current_ebp_flag,
            count(*) FILTER (WHERE evidence AND nullif(resampling_required,'') IS NOT NULL) evidence_with_resampling_field
            FROM joined""")[0]
        report["linked_resampling_values"] = rows("""SELECT resampling_required, count(*) AS n_rows
            FROM joined WHERE evidence AND nullif(resampling_required,'') IS NOT NULL
            GROUP BY resampling_required ORDER BY n_rows DESC""")
        report["matching_by_kingdom"] = rows(
            "SELECT iucn_kingdom, count(*) species, count(*) FILTER(WHERE match_status='MATCHED') AS matched_count FROM joined GROUP BY iucn_kingdom ORDER BY species DESC"
        )
        report["accepted_lineage_conflicts"] = rows(
            "SELECT lineage_agreements, lineage_conflicts, count(*) AS n_rows FROM joined WHERE match_status='MATCHED' AND lineage_conflicts>0 GROUP BY ALL ORDER BY lineage_conflicts DESC, lineage_agreements DESC"
        )
        con.execute("""CREATE TEMP TABLE families AS SELECT family_key,
            bool_or(evidence OR extinct) current_covered, bool_or(evidence) evidence_covered
            FROM joined WHERE nullif(family_key,'') IS NOT NULL GROUP BY family_key""")
        report["extinction_only_families"] = rows("""SELECT count(*) families FROM families
            WHERE current_covered AND NOT evidence_covered""")[0]
        report["extant_relatives_of_extinction_only_families"] = rows("""SELECT count(*) species,
            count(*) FILTER(WHERE NOT gdd) with_goat_record FROM joined j JOIN families f USING(family_key)
            WHERE NOT j.extinct AND f.current_covered AND NOT f.evidence_covered""")[0]
        # Name-key comparison mirrors the existing approach; it does not certify
        # taxonomic equivalence of the suggested wider-universe representatives.
        report["wider_goat_family_candidates"] = rows("""SELECT count(*) species,
            count(DISTINCT j.family_key) families FROM joined j JOIN families f USING(family_key)
            WHERE NOT f.current_covered AND EXISTS (SELECT 1 FROM goat g
                WHERE lower(g.taxon_rank)='species' AND g.qualifying AND g.family_name=j.family_key)""")[
            0
        ]
        report["goat_genus_name_ambiguity"] = rows("""SELECT count(*) genus_names FROM (
            SELECT genus_name FROM goat WHERE lower(taxon_rank)='species' AND nullif(genus_name,'') IS NOT NULL
            GROUP BY genus_name HAVING count(DISTINCT family_name)>1 OR count(DISTINCT kingdom_name)>1)""")[
            0
        ]
        report["goat_family_name_ambiguity"] = rows("""SELECT count(*) family_names FROM (
            SELECT family_name FROM goat WHERE lower(taxon_rank)='species' AND nullif(family_name,'') IS NOT NULL
            GROUP BY family_name HAVING count(DISTINCT kingdom_name)>1)""")[0]
    finally:
        con.close()
    target = output / "methodology-audit.json"
    target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--crosswalk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.root, args.crosswalk, args.output)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("sources", "source_columns")}, indent=2
        )
    )


if __name__ == "__main__":
    main()
