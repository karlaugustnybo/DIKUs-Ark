CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE OR REPLACE FUNCTION normalize_species_search(value TEXT)
RETURNS TEXT
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
RETURN lower(public.unaccent('public.unaccent', COALESCE(value, '')));

CREATE OR REPLACE FUNCTION escape_species_like(value TEXT)
RETURNS TEXT
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
RETURN replace(replace(replace(value, E'\\', E'\\\\'), '%', E'\\%'), '_', E'\\_');

CREATE TABLE IF NOT EXISTS species (
    gbif_accepted_id TEXT PRIMARY KEY,
    iucn_sis_id TEXT,
    iucn_assessment_id TEXT,
    gbif_taxon_id TEXT,
    goat_taxon_id TEXT,
    species_name TEXT,
    family TEXT,
    redlist_category TEXT,
    has_dna_species_level BOOLEAN NOT NULL,
    genus_has_dna BOOLEAN NOT NULL,
    family_has_dna BOOLEAN NOT NULL,
    goat_data_deficient BOOLEAN NOT NULL DEFAULT false,
    edge_group_name TEXT,
    has_ebp_criteria_evidence BOOLEAN
);
ALTER TABLE species ALTER COLUMN species_name DROP NOT NULL;
ALTER TABLE species ADD COLUMN IF NOT EXISTS iucn_sis_id TEXT;
ALTER TABLE species ADD COLUMN IF NOT EXISTS iucn_assessment_id TEXT;
ALTER TABLE species ADD COLUMN IF NOT EXISTS gbif_taxon_id TEXT;
ALTER TABLE species ADD COLUMN IF NOT EXISTS goat_taxon_id TEXT;
ALTER TABLE species ADD COLUMN IF NOT EXISTS goat_data_deficient BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE species ADD COLUMN IF NOT EXISTS has_ebp_criteria_evidence BOOLEAN;

CREATE TABLE IF NOT EXISTS species_systems (
    gbif_accepted_id TEXT NOT NULL REFERENCES species(gbif_accepted_id) ON DELETE CASCADE,
    system TEXT NOT NULL,
    PRIMARY KEY (gbif_accepted_id, system)
);

CREATE TABLE IF NOT EXISTS cell_species (
    h3_index BIGINT NOT NULL,
    resolution SMALLINT NOT NULL CHECK (resolution IN (3, 7)),
    gbif_accepted_id TEXT NOT NULL REFERENCES species(gbif_accepted_id) ON DELETE CASCADE,
    PRIMARY KEY (resolution, h3_index, gbif_accepted_id)
);

-- Compact cell membership is the global serving format. One indexed row per
-- H3 cell avoids expanding tens of billions of cell/species relationships.
CREATE TABLE IF NOT EXISTS cell_species_lists (
    h3_index BIGINT NOT NULL,
    resolution SMALLINT NOT NULL CHECK (resolution IN (3, 7)),
    species_ids TEXT[] NOT NULL,
    PRIMARY KEY (resolution, h3_index)
);

-- Compact inverse coverage for interactive species highlighting. Global builds
-- intentionally materialize resolution 3 only; expanding resolution 7 would
-- duplicate more than 30 billion relationships.
CREATE TABLE IF NOT EXISTS species_cells (
    gbif_accepted_id TEXT NOT NULL REFERENCES species(gbif_accepted_id) ON DELETE CASCADE,
    resolution SMALLINT NOT NULL CHECK (resolution IN (3, 7)),
    h3_indexes BIGINT[] NOT NULL,
    PRIMARY KEY (gbif_accepted_id, resolution)
);

-- Every boundary touched by a cell. The legacy scalar columns remain during
-- migration; all current filtering uses the array membership columns.
CREATE TABLE IF NOT EXISTS cell_boundaries (
    h3_index BIGINT NOT NULL,
    resolution SMALLINT NOT NULL CHECK (resolution IN (3, 7)),
    admin0 TEXT NOT NULL DEFAULT '',
    admin1 TEXT NOT NULL DEFAULT '',
    municipality TEXT NOT NULL DEFAULT '',
    eez TEXT NOT NULL DEFAULT '',
    conservation_framework TEXT NOT NULL DEFAULT '',
    admin0_codes TEXT[] NOT NULL DEFAULT '{}',
    admin1_codes TEXT[] NOT NULL DEFAULT '{}',
    municipality_codes TEXT[] NOT NULL DEFAULT '{}',
    eez_codes TEXT[] NOT NULL DEFAULT '{}',
    conservation_framework_codes TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (resolution, h3_index)
);
ALTER TABLE cell_boundaries ADD COLUMN IF NOT EXISTS municipality TEXT NOT NULL DEFAULT '';
ALTER TABLE cell_boundaries ADD COLUMN IF NOT EXISTS eez TEXT NOT NULL DEFAULT '';
ALTER TABLE cell_boundaries ADD COLUMN IF NOT EXISTS conservation_framework TEXT NOT NULL DEFAULT '';
ALTER TABLE cell_boundaries ADD COLUMN IF NOT EXISTS admin0_codes TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE cell_boundaries ADD COLUMN IF NOT EXISTS admin1_codes TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE cell_boundaries ADD COLUMN IF NOT EXISTS municipality_codes TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE cell_boundaries ADD COLUMN IF NOT EXISTS eez_codes TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE cell_boundaries ADD COLUMN IF NOT EXISTS conservation_framework_codes TEXT[] NOT NULL DEFAULT '{}';

-- A one-row serving aggregate avoids rescanning cell_species on every homepage view.
CREATE TABLE IF NOT EXISTS app_stats (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    total INTEGER NOT NULL,
    critically_endangered INTEGER NOT NULL,
    edge_species INTEGER NOT NULL,
    needs_dna_sampling INTEGER NOT NULL,
    res3_cells INTEGER NOT NULL,
    res7_cells INTEGER NOT NULL
);

-- PostgreSQL B-tree indexes are the serving path for deliberate cell lookups.
CREATE INDEX IF NOT EXISTS cell_species_h3_index_btree ON cell_species USING btree (h3_index);
CREATE INDEX IF NOT EXISTS cell_species_resolution_h3_btree ON cell_species USING btree (resolution, h3_index);
CREATE INDEX IF NOT EXISTS cell_species_lists_h3_btree ON cell_species_lists USING btree (h3_index);
CREATE INDEX IF NOT EXISTS cell_boundaries_admin0_btree ON cell_boundaries (admin0, resolution);
CREATE INDEX IF NOT EXISTS cell_boundaries_admin1_btree ON cell_boundaries (admin1, resolution);
CREATE INDEX IF NOT EXISTS cell_boundaries_municipality_btree ON cell_boundaries (municipality, resolution);
CREATE INDEX IF NOT EXISTS cell_boundaries_conservation_btree ON cell_boundaries (conservation_framework, resolution);
CREATE INDEX IF NOT EXISTS cell_boundaries_admin0_codes_gin ON cell_boundaries USING gin (admin0_codes);
CREATE INDEX IF NOT EXISTS cell_boundaries_admin1_codes_gin ON cell_boundaries USING gin (admin1_codes);
CREATE INDEX IF NOT EXISTS cell_boundaries_municipality_codes_gin ON cell_boundaries USING gin (municipality_codes);
CREATE INDEX IF NOT EXISTS cell_boundaries_eez_codes_gin ON cell_boundaries USING gin (eez_codes);
CREATE INDEX IF NOT EXISTS cell_boundaries_conservation_codes_gin ON cell_boundaries USING gin (conservation_framework_codes);
CREATE INDEX IF NOT EXISTS species_name_search_btree ON species USING btree (normalize_species_search(species_name));
CREATE INDEX IF NOT EXISTS species_family_search_btree ON species USING btree (normalize_species_search(family));
CREATE INDEX IF NOT EXISTS species_name_prefix_btree ON species USING btree (normalize_species_search(species_name) text_pattern_ops);
CREATE INDEX IF NOT EXISTS species_family_prefix_btree ON species USING btree (normalize_species_search(family) text_pattern_ops);
CREATE INDEX IF NOT EXISTS species_name_search_trgm ON species USING gin (normalize_species_search(species_name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS species_family_search_trgm ON species USING gin (normalize_species_search(family) gin_trgm_ops);
