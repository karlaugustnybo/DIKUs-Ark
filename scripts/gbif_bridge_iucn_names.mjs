#!/usr/bin/env node

/**
 * Resolve unmatched IUCN names to GBIF/CoL accepted species concepts.
 *
 * The resulting accepted canonical name is only a bridge candidate. The
 * crosswalk builder still requires that it resolve to a GoaT/NCBI species and
 * that the IUCN and NCBI lineages are compatible.
 */

import { promises as fs } from "node:fs";

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) {
  throw new Error(
    "Usage: node scripts/gbif_bridge_iucn_names.mjs input.json output.json",
  );
}

const API = "https://api.gbif.org/v2/species/match";
const CONCURRENCY = 16;
const input = JSON.parse(await fs.readFile(inputPath, "utf8"));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function makeUrl(row) {
  const parameters = new URLSearchParams({
    scientificName: row.iucn_scientific_name,
  });
  for (const [key, value] of [
    ["kingdom", row.iucn_kingdom],
    ["phylum", row.iucn_phylum],
    ["class", row.iucn_class],
    ["order", row.iucn_order],
    ["family", row.iucn_family],
    ["genus", row.iucn_genus],
  ]) {
    if (value) parameters.set(key, value);
  }
  return `${API}?${parameters}`;
}

async function fetchMatch(row) {
  let lastError;
  for (let attempt = 1; attempt <= 6; attempt += 1) {
    try {
      const response = await fetch(makeUrl(row), {
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

function summarize(row, result) {
  if (result?.error) {
    return {
      iucn_sis_id: row.iucn_sis_id,
      error: result.error,
      exact_species_concept: false,
    };
  }
  const usage = result?.usage ?? {};
  const accepted = result?.acceptedUsage ?? usage;
  const diagnostics = result?.diagnostics ?? {};
  const sourceIds = (result?.additionalStatus ?? [])
    .filter((status) => status.datasetAlias === "IUCN")
    .map((status) => String(status.sourceId));
  return {
    iucn_sis_id: row.iucn_sis_id,
    input_name: row.iucn_scientific_name,
    exact_species_concept:
      diagnostics.matchType === "EXACT" &&
      usage.rank === "SPECIES" &&
      accepted.rank === "SPECIES",
    iucn_source_id_confirmed: sourceIds.includes(String(row.iucn_sis_id)),
    accepted_concept_key: accepted.key ?? null,
    accepted_canonical_name: accepted.canonicalName ?? null,
    usage_key: usage.key ?? null,
    usage_canonical_name: usage.canonicalName ?? null,
    usage_status: usage.status ?? null,
    synonym: result?.synonym ?? null,
    match_type: diagnostics.matchType ?? null,
    confidence: diagnostics.confidence ?? null,
    error: null,
  };
}

const output = new Array(input.length);
let cursor = 0;
let completed = 0;

async function worker() {
  while (cursor < input.length) {
    const index = cursor;
    cursor += 1;
    output[index] = summarize(input[index], await fetchMatch(input[index]));
    completed += 1;
    if (completed % 500 === 0 || completed === input.length) {
      process.stderr.write(`GBIF bridge: ${completed}/${input.length} names checked\n`);
    }
  }
}

await Promise.all(Array.from({ length: CONCURRENCY }, () => worker()));
await fs.writeFile(
  outputPath,
  `${JSON.stringify(
    {
      generated_at: new Date().toISOString(),
      api: API,
      input_rows: input.length,
      rows: output,
    },
    null,
    2,
  )}\n`,
);

const exact = output.filter((row) => row.exact_species_concept).length;
const sourceConfirmed = output.filter((row) => row.iucn_source_id_confirmed).length;
process.stdout.write(
  `${JSON.stringify({
    input_rows: input.length,
    exact_species_concepts: exact,
    iucn_source_id_confirmed: sourceConfirmed,
    output: outputPath,
  })}\n`,
);
