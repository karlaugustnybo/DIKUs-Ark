#!/usr/bin/env node

/**
 * Independently validate ambiguous/near-name IUCN-GoaT candidate pairs with
 * GBIF's v2 species matcher (Catalogue of Life XR by default).
 *
 * Input is the JSON array emitted by match_iucn_goat_global.py.
 */

import { promises as fs } from "node:fs";

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) {
  throw new Error(
    "Usage: node scripts/gbif_validate_iucn_goat_candidates.mjs input.json output.json",
  );
}

const API = "https://api.gbif.org/v2/species/match";
const CONCURRENCY = 12;
const rows = JSON.parse(await fs.readFile(inputPath, "utf8"));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function queryKey(name, row) {
  return JSON.stringify([
    name,
    row.iucn_kingdom,
    row.iucn_phylum,
    row.iucn_class,
    row.iucn_order,
    row.iucn_family,
    row.iucn_genus,
  ]);
}

function makeUrl(name, row) {
  const parameters = new URLSearchParams({ scientificName: name });
  const fields = [
    ["kingdom", row.iucn_kingdom],
    ["phylum", row.iucn_phylum],
    ["class", row.iucn_class],
    ["order", row.iucn_order],
    ["family", row.iucn_family],
    ["genus", row.iucn_genus],
  ];
  for (const [key, value] of fields) {
    if (value) parameters.set(key, value);
  }
  return `${API}?${parameters}`;
}

async function fetchMatch(name, row) {
  let lastError;
  for (let attempt = 1; attempt <= 6; attempt += 1) {
    try {
      const response = await fetch(makeUrl(name, row), {
        headers: {
          accept: "application/json",
          "user-agent": "Ark-IV-IUCN-GoaT-crosswalk/1.0",
        },
        signal: AbortSignal.timeout(60_000),
      });
      const text = await response.text();
      if (!response.ok) throw new Error(`HTTP ${response.status}: ${text.slice(0, 300)}`);
      return JSON.parse(text);
    } catch (error) {
      lastError = error;
      if (attempt < 6) await sleep(Math.min(20_000, 500 * 2 ** (attempt - 1)));
    }
  }
  return { error: String(lastError) };
}

function concept(result) {
  if (result?.error) return { error: result.error };
  const diagnostics = result?.diagnostics ?? {};
  const usage = result?.usage ?? {};
  const accepted = result?.acceptedUsage ?? usage;
  const exactSpecies =
    diagnostics.matchType === "EXACT" &&
    usage.rank === "SPECIES" &&
    accepted.rank === "SPECIES";
  return {
    concept_key: exactSpecies ? accepted.key ?? null : null,
    usage_key: usage.key ?? null,
    canonical_name: accepted.canonicalName ?? null,
    usage_canonical_name: usage.canonicalName ?? null,
    match_type: diagnostics.matchType ?? null,
    confidence: diagnostics.confidence ?? null,
    synonym: result?.synonym ?? null,
    iucn_source_ids: (result?.additionalStatus ?? [])
      .filter((status) => status.datasetAlias === "IUCN")
      .map((status) => String(status.sourceId)),
    error: null,
  };
}

const requests = new Map();
for (const row of rows) {
  for (const name of [
    row.iucn_scientific_name,
    row.candidate_name,
    row.goat_scientific_name,
  ]) {
    if (!name) continue;
    const key = queryKey(name, row);
    if (!requests.has(key)) requests.set(key, { key, name, row });
  }
}

const requestList = [...requests.values()];
const results = new Map();
let cursor = 0;
let completed = 0;

async function worker() {
  while (cursor < requestList.length) {
    const index = cursor;
    cursor += 1;
    const request = requestList[index];
    results.set(request.key, concept(await fetchMatch(request.name, request.row)));
    completed += 1;
    if (completed % 100 === 0 || completed === requestList.length) {
      process.stderr.write(`GBIF: ${completed}/${requestList.length} unique names checked\n`);
    }
  }
}

await Promise.all(Array.from({ length: CONCURRENCY }, () => worker()));

const validated = rows.map((row) => {
  const iucn = results.get(queryKey(row.iucn_scientific_name, row));
  const candidate = results.get(queryKey(row.candidate_name, row));
  const goat = results.get(queryKey(row.goat_scientific_name, row));
  const iucnKey = iucn?.concept_key ?? null;
  const corroboratingKeys = [candidate?.concept_key, goat?.concept_key].filter(Boolean);
  const sourceIdConfirmed = iucn?.iucn_source_ids?.includes(String(row.iucn_sis_id)) ?? false;
  return {
    iucn_sis_id: row.iucn_sis_id,
    ncbi_taxid: row.ncbi_taxid,
    gbif_confirmed:
      Boolean(iucnKey) &&
      corroboratingKeys.includes(iucnKey) &&
      (sourceIdConfirmed || iucn.match_type === "EXACT"),
    gbif_iucn_source_id_confirmed: sourceIdConfirmed,
    gbif_iucn: iucn,
    gbif_candidate: candidate,
    gbif_goat: goat,
  };
});

await fs.writeFile(
  outputPath,
  `${JSON.stringify(
    {
      generated_at: new Date().toISOString(),
      api: API,
      input_rows: rows.length,
      unique_requests: requestList.length,
      rows: validated,
    },
    null,
    2,
  )}\n`,
);

const confirmed = validated.filter((row) => row.gbif_confirmed).length;
process.stdout.write(
  `${JSON.stringify({
    input_rows: rows.length,
    unique_requests: requestList.length,
    confirmed,
    rejected_or_unresolved: rows.length - confirmed,
    output: outputPath,
  })}\n`,
);
