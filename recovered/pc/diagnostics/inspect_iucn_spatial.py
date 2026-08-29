import os

import duckdb


con = duckdb.connect(":memory:")
con.execute("PRAGMA threads=4")
source_root = os.environ.get(
    "ARK_GEODATA_DIR", "external_data/iucn_ranges_v2"
).replace("\\", "/")
source = f"read_parquet('{source_root}/*.parquet', union_by_name=true)"

print("SCHEMA")
for row in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall():
    print(row[0], row[1], sep="\t")

print("\nSPATIAL COVERAGE")
for row in con.execute(f"""
    SELECT
        COALESCE(match_method, 'NULL') AS match_method,
        COUNT(DISTINCT id_no) AS species,
        COUNT(*) AS polygon_rows
    FROM {source}
    GROUP BY 1
    ORDER BY species DESC
""").fetchall():
    print(*row, sep="\t")

print("\nGRAIN CHECK")
print(con.execute(f"""
    SELECT
        COUNT(*) AS polygon_rows,
        COUNT(DISTINCT id_no) AS species,
        COUNT(*) FILTER (WHERE n_methods > 1) AS rows_for_inconsistent_species
    FROM (
        SELECT *, COUNT(DISTINCT match_method) OVER (PARTITION BY id_no) AS n_methods
        FROM {source}
    )
""").fetchone())
