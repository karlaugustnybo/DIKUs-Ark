import { createReadStream, createWriteStream, promises as fs } from "node:fs";
import { once } from "node:events";
import readline from "node:readline";

const API = "https://goat.genomehubs.org/api/v2/search";
const MAX_PAGE = 9000;
const EXPECTED_SPECIES_ROWS = 2_004_790;
const RANKS = ["species", "genus", "family", "order", "class", "phylum", "kingdom", "domain"];
const FIELDS = [
  "assembly_level",
  "bioproject",
  "busco_completeness",
  "ebp_standard_criteria",
  "in_progress",
  "insdc_submitted",
  "published",
  "resampling_required",
  "sample_acquired",
  "sample_available",
  "sample_collected",
  "sequencing_status",
  "sequencing_status_ebp",
  "other_priority",
  "family_representative",
];
const SETS = [
  {
    name: "sequencing_status",
    expected: 38_992,
    query: "tax_rank(species) AND sequencing_status != null",
  },
  {
    name: "priority_without_status",
    expected: 13_535,
    query: "tax_rank(species) AND other_priority != null AND sequencing_status = null",
  },
  {
    name: "family_without_status_or_priority",
    expected: 787,
    query:
      "tax_rank(species) AND family_representative != null AND sequencing_status = null AND other_priority = null",
  },
  {
    name: "busco_only",
    expected: 32,
    query:
      "tax_rank(species) AND busco_completeness != null AND sequencing_status = null AND other_priority = null AND family_representative = null",
  },
];

const destination = process.argv[2];
if (!destination) {
  throw new Error(
    "Usage: node scripts/enrich_goat_species_metadata.mjs /path/to/tol_species.tsv",
  );
}

const outputPath = `${destination}.enriched.part`;
const backupPath = `${destination}.before-metadata-2026-07-23`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function fixedEncode(value) {
  return encodeURIComponent(value).replace(/[!'()*]/g, (char) =>
    `%${char.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

function apiUrl(query, { size = 0, format = "json" } = {}) {
  const params = [
    `query=${fixedEncode(query)}`,
    "result=taxon",
    "taxonomy=ncbi",
    `size=${size}`,
    "offset=0",
    "includeEstimates=true",
    "emptyColumns=true",
  ];
  if (format === "tsv") {
    params.push(`fields=${fixedEncode(`none,${FIELDS.join(",")}`)}`);
    params.push(`ranks=${fixedEncode(RANKS.join(","))}`);
  } else {
    params.push("fields=none");
  }
  return `${API}?${params.join("&")}`;
}

async function fetchWithRetry(url, accept, attempts = 8) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await fetch(url, {
        headers: { accept, "accept-encoding": "gzip, br" },
        signal: AbortSignal.timeout(180_000),
      });
      const body = await response.text();
      if (!response.ok) throw new Error(`HTTP ${response.status}: ${body.slice(0, 500)}`);
      if (body.trimStart().startsWith("{")) {
        const parsed = JSON.parse(body);
        if (parsed?.status?.success === false) {
          throw new Error(JSON.stringify(parsed.status));
        }
      }
      return body;
    } catch (error) {
      lastError = error;
      if (attempt < attempts) await sleep(Math.min(30_000, 1000 * 2 ** (attempt - 1)));
    }
  }
  throw lastError;
}

async function countQuery(query) {
  const body = await fetchWithRetry(apiUrl(query), "application/json");
  const data = JSON.parse(body);
  if (!data?.status?.success) throw new Error(`Count failed: ${JSON.stringify(data?.status)}`);
  return data.status.hits;
}

function rangeQuery(base, low, high) {
  return `${base} AND assembly_span >= ${low} AND assembly_span < ${high}`;
}

async function partitionQuery(base, expected) {
  const total = await countQuery(base);
  if (total !== expected) {
    throw new Error(`Expected ${expected} rows for "${base}", API reported ${total}`);
  }

  const partitions = [];
  async function split(low, high, knownCount, depth = 0) {
    if (knownCount === 0) return;
    if (knownCount <= MAX_PAGE) {
      partitions.push({ query: rangeQuery(base, low, high), count: knownCount });
      return;
    }
    if (depth > 40) {
      throw new Error(`Unable to split ${knownCount} records in assembly-span range [${low}, ${high})`);
    }
    const middle = Math.sqrt(low * high);
    if (!Number.isFinite(middle) || middle <= low || middle >= high) {
      throw new Error(`Invalid assembly-span split for [${low}, ${high})`);
    }
    const leftCount = await countQuery(rangeQuery(base, low, middle));
    if (leftCount === 0 || leftCount === knownCount) {
      const arithmeticMiddle = Math.floor((low + high) / 2);
      if (arithmeticMiddle <= low || arithmeticMiddle >= high) {
        throw new Error(
          `More than ${MAX_PAGE} records share an inseparable assembly-span range [${low}, ${high})`,
        );
      }
      const arithmeticLeft = await countQuery(rangeQuery(base, low, arithmeticMiddle));
      await split(low, arithmeticMiddle, arithmeticLeft, depth + 1);
      await split(arithmeticMiddle, high, knownCount - arithmeticLeft, depth + 1);
      return;
    }
    await split(low, middle, leftCount, depth + 1);
    await split(middle, high, knownCount - leftCount, depth + 1);
  }

  const covered = await countQuery(rangeQuery(base, 1, 100_000_000_000_000));
  if (covered !== expected) {
    throw new Error(
      `Assembly-span partitions cover ${covered}/${expected} rows for "${base}"`,
    );
  }
  await split(1, 100_000_000_000_000, covered);
  return partitions;
}

function unquote(value) {
  if (value?.startsWith('"') && value.endsWith('"')) {
    return value.slice(1, -1).replaceAll('""', '"');
  }
  return value ?? "";
}

function parseMetadataTsv(text, metadata) {
  const lines = text.replace(/\n$/, "").split("\n");
  if (lines.length < 2) return 0;
  const headers = lines[0].split("\t").map(unquote);
  const idIndex = headers.indexOf("taxon_id");
  const fieldIndexes = Object.fromEntries(FIELDS.map((field) => [field, headers.indexOf(field)]));
  if (idIndex < 0 || Object.values(fieldIndexes).some((index) => index < 0)) {
    throw new Error(`Unexpected GoaT TSV header: ${lines[0]}`);
  }

  let added = 0;
  for (const line of lines.slice(1)) {
    if (!line) continue;
    const cells = line.split("\t");
    const taxonId = unquote(cells[idIndex]);
    const record = {};
    for (const field of FIELDS) record[field] = cells[fieldIndexes[field]] ?? "";
    if (!metadata.has(taxonId)) added += 1;
    metadata.set(taxonId, record);
  }
  return added;
}

async function downloadSet(set, metadata) {
  const partitions = await partitionQuery(set.query, set.expected);
  process.stderr.write(`${set.name}: ${set.expected} rows in ${partitions.length} partitions\n`);
  let downloaded = 0;
  for (let index = 0; index < partitions.length; index += 1) {
    const partition = partitions[index];
    const text = await fetchWithRetry(
      apiUrl(partition.query, { size: partition.count, format: "tsv" }),
      "text/tab-separated-values",
    );
    const rows = text.replace(/\n$/, "").split("\n").length - 1;
    if (rows !== partition.count) {
      throw new Error(
        `${set.name} partition ${index + 1} expected ${partition.count} rows, received ${rows}`,
      );
    }
    parseMetadataTsv(text, metadata);
    downloaded += rows;
    process.stderr.write(
      `${set.name}: ${downloaded}/${set.expected} processed rows\n`,
    );
  }
}

async function writeChunk(stream, chunk) {
  if (!stream.write(chunk)) await once(stream, "drain");
}

async function mergeMetadata(metadata) {
  const input = readline.createInterface({
    input: createReadStream(destination, { encoding: "utf8" }),
    crlfDelay: Infinity,
  });
  const output = createWriteStream(outputPath, { encoding: "utf8" });
  let headers;
  let fieldIndexes;
  let assemblySpanIndex;
  let rows = 0;
  let matched = 0;

  for await (const line of input) {
    if (!headers) {
      headers = line.split("\t").map(unquote);
      fieldIndexes = Object.fromEntries(FIELDS.map((field) => [field, headers.indexOf(field)]));
      if (Object.values(fieldIndexes).some((index) => index < 0)) {
        throw new Error(`Destination is missing one or more metadata columns`);
      }
      assemblySpanIndex = headers.indexOf("assembly_span");
      const headerCells = line.split("\t");
      if (assemblySpanIndex >= 0) headerCells.splice(assemblySpanIndex, 1);
      await writeChunk(output, `${headerCells.join("\t")}\n`);
      continue;
    }

    rows += 1;
    const cells = line.split("\t");
    const taxonId = unquote(cells[0]);
    const record = metadata.get(taxonId);
    if (record) {
      for (const field of FIELDS) cells[fieldIndexes[field]] = record[field];
      matched += 1;
    }
    if (assemblySpanIndex >= 0) cells.splice(assemblySpanIndex, 1);
    await writeChunk(output, `${cells.join("\t")}\n`);
  }
  output.end();
  await once(output, "close");

  if (rows !== EXPECTED_SPECIES_ROWS) {
    throw new Error(`Expected ${EXPECTED_SPECIES_ROWS} destination rows, found ${rows}`);
  }
  if (matched !== metadata.size) {
    throw new Error(`Matched ${matched}/${metadata.size} downloaded metadata records`);
  }
  return { rows, matched };
}

if (await fs.stat(backupPath).catch(() => null)) {
  throw new Error(`Backup already exists: ${backupPath}`);
}

const metadata = new Map();
for (const set of SETS) await downloadSet(set, metadata);
const expectedUnion = SETS.reduce((sum, set) => sum + set.expected, 0);
if (metadata.size !== expectedUnion) {
  throw new Error(`Expected union of ${expectedUnion} species, downloaded ${metadata.size}`);
}

const merged = await mergeMetadata(metadata);
await fs.rename(destination, backupPath);
await fs.rename(outputPath, destination);
const stat = await fs.stat(destination);

process.stdout.write(
  `${JSON.stringify({
    destination,
    backup: backupPath,
    rows: merged.rows,
    enrichedSpecies: merged.matched,
    bytes: stat.size,
  })}\n`,
);
