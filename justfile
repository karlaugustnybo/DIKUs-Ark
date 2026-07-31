set dotenv-load

postgres_admin_url := env_var_or_default("POSTGRES_ADMIN_URL", "postgres")
postgres_app_user := env_var_or_default("POSTGRES_APP_USER", "ark")
postgres_app_password := env_var_or_default("POSTGRES_APP_PASSWORD", "ark")
postgres_app_database := env_var_or_default("POSTGRES_APP_DATABASE", "ark_iv")

# Show available commands.
default:
    @just --list

# Install Python and frontend dependencies.
install:
    uv sync
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

# Start Granian and SvelteKit only; never rebuild or reload data.
start:
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

    uv run granian \
      --interface asgi \
      --host "${API_HOST:-127.0.0.1}" \
      --port "${API_PORT:-8000}" \
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

    print "Ark-IV starting at http://${FRONTEND_HOST:-127.0.0.1}:${FRONTEND_PORT:-5173}"
    cd frontend
    bun run dev -- \
      --host "${FRONTEND_HOST:-127.0.0.1}" \
      --port "${FRONTEND_PORT:-5173}"

# Alias for the fast start command.
dev: start

# Run pipeline unit tests.
test:
    uv run python -m unittest discover -s tests -v

# Validate the configured H3 list files without requiring a crosswalk.
validate-h3:
    uv run python -m app.validate_h3_input \
      --res3 "${H3_RES3_PARQUET:-data/h3_res3_species.parquet}" \
      --res7 "${H3_RES7_PARQUET:-data/h3_res7_species.parquet}" \
      --output "${H3_VALIDATION_REPORT_PATH:-data/validation/h3-input.json}"
