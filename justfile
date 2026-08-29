set dotenv-load

postgres_admin_url := env_var_or_default("POSTGRES_ADMIN_URL", "postgres")
postgres_app_user := env_var_or_default("POSTGRES_APP_USER", "ark")
postgres_app_password := env_var_or_default("POSTGRES_APP_PASSWORD", "ark")
postgres_app_database := env_var_or_default("POSTGRES_APP_DATABASE", "ark_iv")
global_data_root := env_var_or_default("GLOBAL_DATA_ROOT", "/Volumes/KA T7/Karl August/Ark-IV_data")
global_preview_root := env_var_or_default("GLOBAL_PREVIEW_ROOT", "/Volumes/KA T7/Karl August/Ark-IV_data/ark_iv_global_preview")
global_database_url := env_var_or_default("GLOBAL_DATABASE_URL", "postgresql://ark:ark@127.0.0.1:5432/ark_iv_global")

# Show available commands.
default:
    @just --list

# Install Python, lint, and frontend dependencies.
install:
    uv sync --group dev
    cd frontend && bun install --frozen-lockfile

# Create/configure the local Postgres role, database, and public schema.
# POSTGRES_ADMIN_URL must connect as a role allowed to create roles/databases.
db-configure:
    #!/usr/bin/env zsh
    set -euo pipefail
    psql '{{ postgres_admin_url }}' \
      -v ON_ERROR_STOP=1 \
      -v app_user='{{ postgres_app_user }}' \
      -v app_password='{{ postgres_app_password }}' \
      -v app_database='{{ postgres_app_database }}' <<'SQL'
    SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
    WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'app_user') \gexec
    SELECT format('CREATE DATABASE %I OWNER %I', :'app_database', :'app_user')
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'app_database') \gexec
    SELECT format('ALTER DATABASE %I OWNER TO %I', :'app_database', :'app_user') \gexec
    \connect :app_database
    SELECT format('ALTER SCHEMA public OWNER TO %I', :'app_user') \gexec
    SELECT format('GRANT ALL ON SCHEMA public TO %I', :'app_user') \gexec
    SQL

# Build Parquet exports and the PMTiles archive with DuckDB/Tippecanoe.
build-data:
    uv run python app/build_cache.py

# Load the generated Parquet serving tables into Postgres.
db-load:
    uv run python -m backend.load_postgres

# One-time complete setup. This intentionally rebuilds data.
setup: install db-configure build-data db-load

# Internal server launcher. Dataset profiles set all paths before invoking it.
serve:
    #!/usr/bin/env zsh
    set -euo pipefail

    test -f "${PMTILES_PATH:-data/tiles/priorities.pmtiles}" || {
      print -u2 "PMTiles is missing. Run 'just setup' once first."
      exit 1
    }
    test -f "${MAP_METADATA_PATH:-data/tiles/map-metadata.json}" || {
      print -u2 "Map metadata is missing. Run 'just build-data' once first."
      exit 1
    }

    api_host="${API_HOST:-127.0.0.1}"
    frontend_host="${FRONTEND_HOST:-127.0.0.1}"
    read -r api_port frontend_port < <(
      uv run python scripts/select_dev_ports.py \
        --api-host "$api_host" \
        --api-port "${API_PORT:-8000}" \
        --frontend-host "$frontend_host" \
        --frontend-port "${FRONTEND_PORT:-5173}"
    )

    url_host() {
      case "$1" in
        0.0.0.0|::|\[::\]) print '127.0.0.1' ;;
        *:*) print "[$1]" ;;
        *) print "$1" ;;
      esac
    }
    api_url="http://$(url_host "$api_host"):$api_port"
    frontend_url="http://$(url_host "$frontend_host"):$frontend_port"

    export API_PORT="$api_port"
    export FRONTEND_PORT="$frontend_port"
    export BACKEND_PROXY_TARGET="$api_url"
    export FRONTEND_ORIGIN="$frontend_url"

    print "Ark-IV development session"
    print "  Frontend: $frontend_url"
    print "  API:      $api_url"

    uv run granian \
      --interface asgi \
      --host "$api_host" \
      --port "$API_PORT" \
      --workers "${API_WORKERS:-1}" \
      backend.app:app &
    api_pid=$!

    cleanup() {
      trap - EXIT
      kill -TERM "$api_pid" 2>/dev/null || true
      wait "$api_pid" 2>/dev/null || true
    }
    trap cleanup EXIT
    trap 'exit 0' INT TERM

    cd frontend
    bun run dev -- \
      --host "$frontend_host" \
      --port "$FRONTEND_PORT" \
      --strictPort

# Start the global dataset.
start: global-start

dev: start

# Run pipeline unit tests.
test:
    uv run python -m unittest discover -s tests -v

# Lint Python source and tests.
lint:
    uv run ruff check .

# Type-check, test, and build the frontend.
frontend-check:
    cd frontend && bun run check
    cd frontend && bun test
    cd frontend && bun run build

# Check files that would be included in the next commit.
release-check:
    uv run python scripts/check_release.py

# Also check all local refs for known restricted database artifacts.
release-history-check:
    uv run python scripts/check_release.py --history

# Run the complete repository verification suite.
check: lint test frontend-check release-check

# Validate the configured H3 list files without requiring a crosswalk.
validate-h3:
    uv run python -m app.validate_h3_input \
      --res3 "${H3_RES3_PARQUET:-data/h3_res3_species.parquet}" \
      --res7 "${H3_RES7_PARQUET:-data/h3_res7_species.parquet}" \
      --output "${H3_VALIDATION_REPORT_PATH:-data/validation/h3-input.json}"

# Build the global resolution-3 preview without running the expensive lossless audit.
global-build-preview:
    uv run python -m app.build_global_species \
      --crosswalk data/exports/iucn_goat_global/iucn_goat_crosswalk.parquet \
      --assessments "{{ global_data_root }}/IUCN_Red_List/assessments.csv" \
      --goat-species "{{ global_data_root }}/TOL/tol_species.tsv" \
      --gbif-backbone "{{ global_data_root }}/gbif_backbone_species.tsv" \
      --edge-species "{{ global_data_root }}/2024_EDGE_species_external_with_gbif.tsv" \
      --h3 "{{ global_data_root }}/h3_aggregated/h3_res3_species_global_merged.parquet" \
      --output-dir data/global/species
    uv run python app/build_db.py \
      --target data/global/source.duckdb --overwrite --resolutions 3 \
      --species-parquet data/global/species/species.parquet \
      --species-systems-parquet data/global/species/species_systems.parquet \
      --h3-res3 "{{ global_data_root }}/h3_aggregated/h3_res3_species_global_merged.parquet" \
      --crosswalk data/exports/iucn_goat_global/iucn_goat_crosswalk.parquet \
      --defer-lossless-validation
    SOURCE_DUCKDB_PATH=./data/global/source.duckdb \
      BUILD_DUCKDB_PATH=./data/global/build.duckdb \
      EXPORT_DIR=./data/global/exports TILE_DIR=./data/global/tiles \
      PMTILES_PATH=./data/global/tiles/priorities.pmtiles \
      MAP_METADATA_PATH=./data/global/tiles/map-metadata.json \
      VALIDATION_REPORT_PATH=./data/global/build-validation.json \
      uv run python app/build_cache.py --rebuild-aggregates --resolutions 3 \
      --skip-expanded-cell-species --defer-lossless-validation

# Create the dedicated global serving database.
global-db-configure:
    #!/usr/bin/env zsh
    set -euo pipefail
    psql '{{ postgres_admin_url }}' -v ON_ERROR_STOP=1 \
      -v app_user='{{ postgres_app_user }}' <<'SQL'
    SELECT format('CREATE DATABASE ark_iv_global OWNER %I', :'app_user')
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'ark_iv_global') \gexec
    SQL

# Load the compact global preview exports into the separate preview database.
global-db-load:
    DATABASE_URL='{{ global_database_url }}' EXPORT_DIR=./data/global/exports \
      uv run python -m backend.load_postgres

# Resume global resolution-7 aggregation; stale metric schemas rebuild automatically.
global-res7-aggregate:
    uv run python -m app.build_res7_preview aggregate \
      --parts-dir "{{ global_data_root }}/h3_aggregated/res7_merged_parts" \
      --species data/global/species/species.parquet \
      --species-systems data/global/species/species_systems.parquet \
      --output-dir "{{ global_preview_root }}/res7_aggregates" \
      --scratch-dir "{{ global_preview_root }}/scratch" \
      --workers "${RES7_WORKERS:-2}" \
      --threads "${DUCKDB_THREADS:-1}" \
      --memory-limit "${DUCKDB_MEMORY_LIMIT:-750MB}"

# Optional: build a large fully static snapshot instead of on-demand res7 tiles.
global-res7-tiles:
    uv run python -m app.build_res7_preview tiles \
      --parts-dir "{{ global_preview_root }}/res7_aggregates" \
      --source-parts-dir "{{ global_data_root }}/h3_aggregated/res7_merged_parts" \
      --build-duckdb data/global/build.duckdb \
      --output "{{ global_preview_root }}/priorities-res3-res7.pmtiles" \
      --scratch-dir "{{ global_preview_root }}/scratch/tippecanoe" \
      --metadata-template data/global/tiles/map-metadata.json \
      --metadata-output "{{ global_preview_root }}/map-metadata.json"

# Start the already-built global dataset.
global-start:
    #!/usr/bin/env zsh
    set -euo pipefail
    source_parts="{{ global_data_root }}/h3_aggregated/res7_merged_parts"
    aggregate_parts="{{ global_preview_root }}/res7_aggregates"
    test -d "$source_parts" || {
      print -u2 "Global resolution-7 source partitions are unavailable: $source_parts"
      exit 1
    }
    test -d "$aggregate_parts" || {
      print -u2 "Global resolution-7 aggregate partitions are unavailable: $aggregate_parts"
      exit 1
    }
    DATABASE_URL='{{ global_database_url }}' \
      SOURCE_DUCKDB_PATH=./data/global/source.duckdb \
      BUILD_DUCKDB_PATH=./data/global/build.duckdb \
      EXPORT_DIR=./data/global/exports TILE_DIR=./data/global/tiles \
      PMTILES_PATH=./data/global/tiles/priorities.pmtiles \
      MAP_METADATA_PATH=./data/global/tiles/map-metadata.json \
      RES7_PARTS_DIR="$source_parts" \
      RES7_AGGREGATE_PARTS_DIR="$aggregate_parts" \
      just serve
