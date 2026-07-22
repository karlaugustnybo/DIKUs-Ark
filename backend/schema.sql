CREATE TABLE IF NOT EXISTS species (
    gbif_accepted_id TEXT PRIMARY KEY,
    species_name TEXT,
    family TEXT,
    redlist_category TEXT,
    has_dna_species_level BOOLEAN NOT NULL,
    genus_has_dna BOOLEAN NOT NULL,
    family_has_dna BOOLEAN NOT NULL,
    edge_group_name TEXT,
    meets_ebp BOOLEAN
);
ALTER TABLE species ALTER COLUMN species_name DROP NOT NULL;

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
CREATE INDEX IF NOT EXISTS species_name_lower_btree ON species USING btree (lower(species_name));
CREATE INDEX IF NOT EXISTS species_family_lower_btree ON species USING btree (lower(family));
