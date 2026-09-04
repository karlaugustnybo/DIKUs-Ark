# Species search benchmark

Species search uses PostgreSQL `unaccent`, prefix, and trigram indexes. Table
results are ranked as exact, prefix, substring, then fuzzy matches, while all
Red List, DNA, ecosystem, and geographic filters remain in the same query.
Map autocomplete uses a compact suggestions endpoint: it fetches only the
identifier, scientific name, and family, skips table counts and scoring, and
only runs the fuzzy query when exact, prefix, and substring search find nothing.

Run the benchmark against the same database used by the API:

```bash
DATABASE_URL=postgresql://ark:ark@127.0.0.1:5432/ark_iv_global \
  uv run python -m scripts.benchmark_species_search
```

The output reports table and map-suggestion timings separately. The default
mix covers an exact scientific name, a prefix, a typo, a family, SQL/regex
metacharacters treated literally, and an absent species. Record the output
alongside the dataset build report when publishing a release; timings are
intentionally not checked into this document because they depend on the loaded
global snapshot and host.

For a useful comparison, warm the database first and run the benchmark before
and after a search change with the same process, dataset, and PostgreSQL
configuration. The unit tests in `tests/test_api.py` cover ordering, filter
composition, literal metacharacters, input limits, and the returned source
identifiers.
