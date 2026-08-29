# AI declaration

## Purpose

Generative AI has been used during Ark-IV development to explain unfamiliar
code, review changes, propose and implement selected technical features, improve
interface copy and styling, write tests and documentation, investigate map
performance, and audit the repository for publication risks.

## Development history

The project began as a hand-written Flask and SQLAlchemy application based on a
tutorial. The team contacted data providers directly, designed the conservation
question, obtained the datasets, and developed the original relational model.
ChatGPT and Gemini were introduced later as development aids. Agentic Codex
sessions were subsequently used for the global SvelteKit/Litestar migration,
data-serving and map-performance work, automated verification, repository
cleanup, and licence/attribution review.

## Human responsibility

AI output is not treated as evidence of biological correctness, licence
permission, or software quality. Contributors remain responsible for reviewing
the generated code and text, running the tests, checking source-provider terms,
and validating the scientific assumptions and results. In particular, the IUCN
and EDGE redistribution decisions in this repository must be confirmed against
the providers' current terms and any written permissions held by the team.

## Project integrity

The conservation question, source access, core biological interpretation,
database decomposition, and learning objectives remain the team's work. AI has
helped implement and test parts of the system, but it does not replace the
required ability to explain the architecture, scoring model, data lineage, and
limitations. Material AI-assisted changes are reviewed through version control,
tests, browser verification, and the project's publication checklist.
