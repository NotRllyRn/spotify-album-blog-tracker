# Metadata Monorepo Plan

This file is the source of truth for combining the tracker and the WordPress metadata CLI. Update it after every stage. Do not push commits.

## Goal

Keep three clear layers:

1. **Metadata library** — reusable schema, matching, validation, enrichment, and payload logic with no Discord or command-line concerns.
2. **Tracker interface** — adapts a tracked Spotify release and asynchronous WordPress client to the library.
3. **CLI interface** — keeps the straightforward `python post_to_album.py ...` workflow and adapts batch WordPress operations to the same library.

The tracker and CLI must call the same enrichment and payload code. Given the same post, selected Spotify release, provider responses, existing metadata, and write policy, both interfaces must produce the same managed WordPress body.

## Non-negotiable behavior

- Preserve the current CLI commands, arguments, JSON artifacts, dry-run-first workflow, diagnostics, exit codes, and safe replay behavior.
- Preserve Spotify and Last.fm matching thresholds, ambiguity handling, validation, retries, circuit breakers, genre fallbacks, taxonomy behavior, category preservation, and editor-owned highlight preservation.
- Use `scf-export-2026-07-24.json` as the active WordPress contract.
- Never write removed ACF fields: `lastfm_release_id`, `music_mood_tags`, `unreleased`, or `listen-count`.
- Manage only current provider-owned ACF fields. Protect `music_rating`, `music_favorite`, `music_notes`, and track `highlight` values.
- Populate `artist`, `genre`, and `release_type` taxonomies through both interfaces.
- Preserve unrelated categories and marker categories, including Relisten and Unreleased.
- The CLI remains usable without Docker. Tracker Docker behavior and persisted `data/` and `logs/` volumes remain unchanged.
- No automated test may contact or write to live WordPress, Spotify, or Last.fm.
- Make no unrelated behavior change without a focused test and an entry in the issue ledger below.

## Target structure

```text
.
├── main.py                         # tracker entry point
├── post_to_album.py                # minimal CLI entry point
├── src/
│   ├── album_metadata/             # shared library
│   │   ├── schema.py               # active SCF contract and ownership
│   │   ├── common.py               # text/date/JSON helpers
│   │   ├── providers.py            # provider errors, retries, circuits
│   │   ├── spotify.py              # Spotify client and release matching
│   │   ├── lastfm.py               # Last.fm client, validation, genres
│   │   ├── enrichment.py           # shared enrichment pipeline
│   │   └── plans.py                # write policy and plan validation/materialization
│   ├── metadata_cli/               # manual CLI interface
│   │   ├── cli.py                  # parser and commands
│   │   └── wordpress.py            # synchronous batch WordPress adapter
│   └── ...                         # existing tracker interface/application
├── tests/
│   ├── metadata/                   # imported CLI characterization tests
│   ├── tracker/                    # tracker tests
│   └── test_metadata_parity.py     # interface-equivalence contract
└── scf-export-2026-07-24.json      # active schema fixture
```

The exact module count may shrink where merging files is simpler. Avoid abstraction that has only one caller.

## Baselines

- Tracker source: `c4b896d`; 186 tests passing before migration.
- CLI source: `eb8c1f3`; 119 tests passing before migration.
- The CLI checkout has uncommitted README clarification and date-materialization test corrections. Import its current working-tree behavior without modifying that checkout.

## Stages

### 0. Plan and baseline

- [x] Compare both current implementations and active SCF schema.
- [x] Record behavior conflicts and shared invariants.
- [x] Commit this plan before implementation.
- [x] Capture baseline test commands and results in commit history.

**Gate:** Both original suites pass and the tracker worktree is clean after the plan commit.

### 1. Import a recoverable CLI snapshot

- [x] Copy the current CLI source, tests, active schema, and user-facing reference into a temporary `legacy/` snapshot.
- [x] Commit the snapshot before refactoring it.
- [x] Confirm the source CLI checkout remains untouched.

**Gate:** The imported snapshot independently runs all 119 original tests.

### 2. Establish the shared schema contract

- [x] Add `album_metadata.schema` with the exact July 24 managed fields, editor-owned fields, track keys, taxonomies, category mapping, and removed-field denylist.
- [x] Move release-type classification and common text/date helpers into the library.
- [x] Add a contract test that reads the active SCF export and rejects schema drift.
- [x] Point tracker release-type classification at the shared function.

**Gate:** Original suites pass; tracker and CLI classification tests exercise one function.

### 3. Modularize the CLI without changing behavior

Extract in small, test-backed commits:

- [x] Provider error/retry/circuit infrastructure.
- [x] Spotify client, search, scoring, ambiguity recovery, and evidence validation.
- [x] Last.fm client, search, validation, recovery, and genre discovery.
- [x] Shared enrichment and managed-write policy.
- [x] Plan/artifact validation and WordPress body materialization.
- [x] Synchronous WordPress batch adapter and CLI commands.
- [x] Replace the root script with a minimal entry point.
- [x] Move characterization tests under the monorepo test layout.

**Gate:** All 119 imported tests pass unchanged in intent; CLI help and credential matrix remain compatible; generated fixture artifacts are equivalent.

### 4. Add the tracker interface to the same engine

- [x] Add a known-Spotify-release entry path that skips discovery but shares Spotify validation, Last.fm matching, genre discovery, field computation, and write policy.
- [x] Refetch canonical Spotify album/tracks at publish time or supply equivalent validated data from the tracker adapter.
- [x] Convert the tracker `Release` editor state into protected existing values/highlight IDs.
- [x] Add async custom-taxonomy term listing/creation to the existing WordPress client.
- [x] Materialize taxonomy IDs through the shared payload function.
- [x] Replace tracker-only Last.fm and SCF payload code with the shared engine.
- [x] Keep metadata failure visible and prevent a silent “successful” incomplete publication.
- [x] Preserve post body, artwork, built-in artist tags, duplicate detection, relisten flow, post cache refresh, and Discord editor/navigation behavior.

**Gate:** No tracker module contains a second SCF field map or Last.fm matching algorithm.

### 5. Prove interface parity

- [x] Add fixture-driven tests that run CLI and tracker adapters with identical inputs/provider fakes.
- [x] Assert identical managed ACF, category, and taxonomy intent.
- [x] Cover albums, EPs, singles, compilations, collaborations, relistens, partial dates, explicit tracks, missing MBIDs/tags, highlights, and editor-owned values.
- [x] Cover fill-only and overwrite-managed behavior.
- [x] Cover provider ambiguity/failure and no-partial-success behavior.
- [x] Run both complete suites together from one command.

**Gate:** The parity test compares final WordPress REST bodies, not merely intermediate models.

### 6. Restore and simplify user interfaces

- [x] Keep `python post_to_album.py stats|fuzzy|run|apply-plan` straightforward.
- [x] Accept the unified tracker `.env` names while retaining `WORDPRESS_BASE_URL` as a CLI compatibility alias.
- [x] Keep dry-run as the default and artifacts easy to review.
- [x] Update `.env.example` for both interfaces without duplicating credentials.
- [x] Update Docker paths/dependencies only as required; retain the tracker command and volumes.
- [x] Replace the README with concise setup, tracker usage, CLI usage, artifacts, and architecture sections.
- [x] Move only essential non-obvious explanations into code docstrings/comments.

**Gate:** CLI `--help`, tracker startup configuration tests, Docker image build, and Compose config validation pass.

### 7. Focused bug/refactor ledger

Only implement items after core parity is green. Each item requires a focused test and separate commit when behavior changes.

- [ ] Replace obsolete tracker `unreleased` ACF writes with the active Unreleased category marker while preserving the editor feature.
- [ ] Resolve the current metadata `listen_count` conflict: the canonical CLI contract writes `1`, while the old tracker calculated a duplicate-post ordinal. Preserve canonical CLI behavior unless a later explicit contract changes it.
- [ ] Improve only clearly duplicated or unsafe code discovered during extraction.
- [ ] Record additional findings here before changing them.

**Gate:** No ledger change is mixed into mechanical extraction commits.

### 8. Full review, then checkpoint commit

- [ ] Review architecture boundaries, data ownership, errors, security, I/O, and naming.
- [ ] Review every metadata write key against the active schema.
- [ ] Review all Discord publication/editor paths affected by metadata changes.
- [ ] Run full tests, static compilation, CLI smoke tests, and Docker checks.
- [ ] Commit all functional work before cleanup.

**Gate:** A known-good pre-cleanup commit exists and can restore the complete implementation.

### 9. Cleanup and final verification

- [ ] Remove the temporary legacy snapshot.
- [ ] Remove `PROGRAM_DOCUMENTATION.md`, `.old/`, stale plans, superseded reports, old schema exports, duplicate deployment docs, generated artifacts, and dead code only after confirming they are no longer referenced.
- [ ] Keep only the active SCF export and concise README unless a file remains operationally necessary.
- [ ] Remove obsolete dependencies and ignore rules.
- [ ] Re-run every gate after cleanup.
- [ ] Review the final diff from the pre-cleanup checkpoint.
- [ ] Commit cleanup separately.

**Gate:** Clean worktree; all tests and smoke checks pass; no live writes; no push.

## Required verification

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q main.py post_to_album.py src tests
python3 post_to_album.py --help
python3 post_to_album.py run --help
python3 post_to_album.py apply-plan --help
docker compose config
docker build -t spotify-album-blog-tracker:verify .
```

Run targeted suites after each extraction step and the complete list at stage gates.

## Commit sequence

Use short commits at safe points, approximately:

1. `plan metadata monorepo`
2. `import metadata cli`
3. `add metadata contract`
4. `split spotify metadata`
5. `split lastfm metadata`
6. `split metadata workflow`
7. `add tracker metadata adapter`
8. `prove metadata parity`
9. `simplify project docs`
10. `checkpoint monorepo migration`
11. `clean stale files`

Adjust boundaries when a smaller coherent commit is safer. Never push.

## Issue ledger

| Status | Finding | Decision |
| --- | --- | --- |
| Planned | Tracker writes four fields removed by the active SCF schema and misses four replacements. | Remove duplicate payload builder and use the shared contract. |
| Planned | Tracker does not write `artist`, `genre`, or `release_type` custom taxonomies. | Apply shared taxonomy intent through its async WordPress adapter. |
| Planned | Tracker Last.fm lookup lacks the CLI’s identity and track validation. | Delete it after the tracker uses the shared Last.fm pipeline. |
| Planned | Tracker and CLI currently materialize date-picker values differently. | Use shared REST materialization and test final request bodies. |
| Planned | Tracker’s `unreleased` editor targets a deleted ACF field. | Preserve the feature through category marker 200, with tests. |
| Planned | CLI `listen_count=1` and tracker’s ordinal calculation conflict. | Canonicalize to the current CLI contract for parity. |
