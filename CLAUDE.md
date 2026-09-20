# CLAUDE.md — LEVI

Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.

LEVI is a standalone bilingual workbench derived from LeRobot Dataset Visualizer.
Read README.md (English primary), README.zh-CN.md and docs/CONVERSION.md before changing data workflows.
Python dependencies are managed by uv; use `uv run levi check`, `uv run pytest` and `uv run levi build`.
Use only LEVI_WORKSPACE for runtime data (default checkout `.state/`). Never hardcode a developer path or require a sibling conversion repository.
Preserve source captures: conversion and annotation export write to new output directories.
Frontend text belongs in both locale catalogs; English is the default and Chinese is available from the language switch.

## Package manager

Always use **bun** (`bun install`, `bun dev`, `bun run build`, `bun test`). Never use npm or yarn.

## Post-process — run after every code change

After making any code changes, always run these commands in order and fix any errors before finishing:

```
bun run format        # auto-fix formatting (prettier)
bun run type-check    # TypeScript: app + test files
bun run lint          # ESLint
bun test              # unit tests
```

Or run them all at once (format first, then the full validate suite):

```
bun run format && bun run validate
```

`bun run validate` runs: type-check → lint → format:check → test

## Key scripts

```
bun dev              # Next.js dev server
bun test             # Run all unit tests (bun:test)
bun run type-check   # tsc --noEmit (app) + tsc -p tsconfig.test.json --noEmit (tests)
bun run lint         # eslint src
bun run validate     # type-check + lint + format:check + tests
```

## Architecture

### Dataset version support

Video-based v2.0, v2.1, v3.0 and v3.1 are supported. Version is detected from `meta/info.json` → `codebase_version`.

| Version  | Path pattern                                                      | Episode metadata                           | Video                                          |
| -------- | ----------------------------------------------------------------- | ------------------------------------------ | ---------------------------------------------- |
| **v2.0** | `data/{episode_chunk:03d}/episode_{episode_index:06d}.parquet`    | `meta/episodes.jsonl` when available         | Full file per episode                          |
| **v2.1** | Same as v2.0                                                      | `meta/episodes.jsonl`                       | Full file per episode                          |
| **v3.0/v3.1** | `data/chunk-{N:03d}/file-{N:03d}.parquet` (via `buildV3DataPath`) | `meta/episodes/chunk-{N}/file-{N}.parquet` | Segmented (timestamps per episode, per camera) |

### Routing to parsers

`src/app/[org]/[dataset]/[episode]/fetch-data.ts` → `getEpisodeData()` dispatches to:

- `getEpisodeDataV2()` for v2.0 and v2.1
- `getEpisodeDataV3()` for v3.0 and v3.1

### v3.x specifics

- Episode metadata row has named keys (`episode_index`, `data/chunk_index`, `data/file_index`, `dataset_from_index`, `dataset_to_index`, `videos/{key}/chunk_index`, etc.)
- Integer columns from parquet come out as **BigInt** — always use `bigIntToNumber()` from `src/utils/typeGuards.ts`
- Row-range selection: `dataset_from_index` / `dataset_to_index` allow reading only the episode's rows from a shared parquet file
- Fallback format uses numeric keys `"0"`.."9"` when column names are unavailable
- Episode metadata can span **multiple chunks** (when episode count exceeds `chunks_size`). Always walk via the `iterateEpisodeMetadataFilesV3(repoId, version)` async generator in `fetch-data.ts` — it advances chunk-000 → chunk-001 → … and stops on the first missing `file-000`. Never hardcode `chunk-000`.
- Multi-task episodes: episode-metadata rows carry a `tasks` field (`list[str]`) — prefer it over the legacy single `task_index` lookup. `EpisodeMetadataV3.tasks?: string[]` exposes it.
- `meta/tasks.parquet` lookup: rows are **not** ordered by `task_index`, and the task string lives in a named pandas index (`__index_level_0__`). Always filter by the `task_index` **column** (`row.task_index === taskIndexNum`), never by row position.

### v2.x path construction

```ts
formatStringWithVars(info.data_path, {
  episode_chunk: Math.floor(episodeId / chunkSize)
    .toString()
    .padStart(3, "0"),
  episode_index: episodeId.toString().padStart(6, "0"),
});
// → "data/000/episode_000042.parquet"
```

`formatStringWithVars` strips `:03d` format specifiers — padding must be done by the caller.

## Key files

| File                                              | Purpose                                                                                                                                  |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `src/app/[org]/[dataset]/[episode]/fetch-data.ts` | Main data-loading entry point; v2/v3 parsers; `computeColumnMinMax`                                                                      |
| `src/utils/versionUtils.ts`                       | `getDatasetInfo`, `getDatasetVersionAndInfo`, `buildVersionedUrl`                                                                        |
| `src/utils/stringFormatting.ts`                   | `buildV3DataPath`, `buildV3VideoPath`, `buildV3EpisodesMetadataPath`, padding helpers                                                    |
| `src/utils/parquetUtils.ts`                       | `fetchParquetFile`, `readParquetAsObjects`, `formatStringWithVars`                                                                       |
| `src/utils/dataProcessing.ts`                     | Chart grouping pipeline: `buildSuffixGroupsMap` → `computeGroupStats` → `groupByScale` → `flattenScaleGroups` → `processChartDataGroups` |
| `src/utils/typeGuards.ts`                         | `bigIntToNumber`, `isNumeric`, `isValidTaskIndex`, etc.                                                                                  |
| `src/utils/constants.ts`                          | `PADDING`, `EXCLUDED_COLUMNS`, `CHART_CONFIG`, `THRESHOLDS`                                                                              |
| `src/types/`                                      | TypeScript types: `DatasetVersion`, `EpisodeMetadataV3`, `VideoInfo`, `ChartDataGroup`, etc.                                             |

## Action Insights scope

`loadCrossEpisodeActionVariance(repoId, version, info, fps, options)` takes a
`CrossEpisodeRequest`: a `scope` (`all` | `range` | `task`) plus `maxEpisodes`,
where `null` means "every episode in scope". The scope filter runs **before**
sampling, so a range or task gets the whole budget.
`CROSS_EPISODE_DEFAULTS.sampleSize` is only the panel's starting sample size;
`CROSS_EPISODE_DEFAULTS.ceiling` is the runaway guard. Note the `process.env`
reads in this module are inert in the browser (no `NEXT_PUBLIC_` prefix and no
`env` block in `next.config.ts`) — the episode-viewer path always gets the
fallback values. Parquet reads are concurrency-limited (`mapWithConcurrency`);
`onProgress` only fires for client-side calls.

`loadDatasetTaskIndex(repoId, version)` builds the dataset-wide task ↔ episode
map (v2: `meta/tasks.jsonl` + the `tasks` field on `meta/episodes.jsonl` rows;
v3: the `tasks` list on episode-metadata rows) and caches it for 5 minutes. It
powers both the by-task insights scope and the sidebar task filter, and
de-duplicates task strings — a dataset may declare the same instruction under
two `task_index` values.

## Chart data pipeline

Series keys use `" | "` as delimiter (e.g. `observation.state | 0`).
`groupRowBySuffix` groups by **suffix**: if two different prefixes share suffix `"0"` (e.g. `observation.state | 0` and `action | 0`), they are merged under `result["0"] = { "observation.state": ..., "action": ... }`. A series with a unique suffix stays flat with its full original key.

## Testing

- Test files live in `**/__tests__/` directories alongside source
- Uses `bun:test` (built-in, no extra install)
- BigInt literals (`42n`) require `tsconfig.test.json` (target ES2020) — test files are excluded from `tsconfig.json`
- `@types/bun` is installed as a devDependency for `bun:test` type resolution
- Mocking fetch: `globalThis.fetch = mock(() => Promise.resolve(new Response(...))) as unknown as typeof fetch`
- Python tests are in `tests/`; use `uv run pytest`. Run one suite at a time: two concurrent runs sharing `--basetemp` leave residue that makes later runs fail in unrelated places (ffprobe on a half-written fixture, `rm_rf` on a non-empty directory). If a run fails oddly, `rm -rf .state/tmp/build/pytest` and try again.
- CI: `.github/workflows/test.yml` runs frontend validation, backend/conversion tests and production build on push/PR.

## URL structure

Remote dataset URLs (local datasets use the same-origin LEVI file service):

```
https://huggingface.co/datasets/{org}/{dataset}/resolve/main/{path}
```

Built by `buildVersionedUrl(repoId, version, path)`. The `version` param is accepted but currently unused in the URL (always `main` revision).

## Excluded columns (not shown in charts)

Reserved/bookkeeping columns from lerobot — see `EXCLUDED_COLUMNS` in `src/utils/constants.ts`:

- v2.x: `timestamp`, `frame_index`, `episode_index`, `index`, `task_index`, `next.reward`, `next.done`, `next.truncated`, `is_success`
- v3.0: `index`, `task_index`, `episode_index`, `frame_index`, `next.reward`, `next.done`, `next.truncated`, `subtask_index`

## 3D URDF viewer (`src/components/urdf-viewer.tsx`)

- URDFs and meshes are hosted in the HF bucket `lerobot/robot-urdfs` — base URL `https://huggingface.co/buckets/lerobot/robot-urdfs/resolve` (no `/main` segment; buckets are unbranched). Override with `NEXT_PUBLIC_URDF_BASE_URL` for local development.
- Asset layout under the bucket: `g1/`, `openarm/`, `so101/` (both SO-100 and SO-101 live here).
- **URDFLoader gotcha**: after our `loadMeshCb` returns, `URDFLoader.js` does `if (obj instanceof THREE.Mesh) obj.material = <urdf-material>`, overwriting any material we set. Workaround: wrap the loaded mesh in a `THREE.Group` so the `instanceof Mesh` check fails. DAE returns a Group already; STL must be wrapped explicitly.
- **STLLoader event ordering**: `manager.itemEnd(url)` fires _before_ the user `onLoad` callback, so `manager.onLoad` can fire before meshes are attached to the robot tree. Defer post-load work (auto-fit camera, shadow flags) with `setTimeout(..., 0)`. Don't try to rebuild materials in `manager.onLoad` — pick the archetype color directly inside `loadMeshCb`.
- **OpenArm DAE files ship 23 stray `PointLight`s** that drown out scene lighting. Strip non-`AmbientLight` lights from `collada.scene` before adding it to the robot.
- Scene setup: `<Canvas shadows>` with `ACESFilmicToneMapping` (exposure 0.9), 3-point directional + ambient lights, `<Environment preset="studio" background={false} />`, `<color attach="background" args={["#1a2433"]} />`. `<OrbitControls makeDefault />` is required so `useThree().controls` exposes the controls for auto-fit.

## Design system

`src/app/globals.css` retains upstream base styles. LEVI overrides live in
`src/app/levi.css`: graphite green surfaces, parchment text and lime accents.
`src/components/levi-locale.tsx` and `src/i18n/` supply the Chinese/English UI.
Preserve the LEVI theme, keyboard access and responsive layouts when editing inherited components.

## Built-in conversion and service

- `levi/conversion/`: modular conversion. `registry.py` lists input formats (`inputs/`: `robot_capture`, `image_sequence`, `lerobot`) and export targets (`outputs/`: `lerobot_v21`, `recap_value`); `pipeline.py` is the single-pass converter (parent plans from CSVs, spawn pool does one decode + one encode per camera, or a lossless remux in `retime` mode), `report.py` the inspection/compatibility models the Workbench renders. `raw.py`/`media.py`/`dataset.py` are the tested primitives. Timestamps are always `frame_index / fps`. See docs/CONVERSION.md and docs/RECAP.md.
- `levi/annotations/sidecar.py`: object annotations. Parquet stores the mask flat (`rle_size`/`rle_counts`); `read_annotations`/`read_episode` return the `mask_rle` shape that `ObjectAnnotation` and the viewer both expect. Do not convert at a call site — that duplication is what once left the overlay with no mask.
- `levi/views.py`: browsing views of raw captures; `levi/annotations/carryover.py` moves annotations into conversions by `source_demo`; `levi/annotations/outcomes.py` stores human success/failure labels.
- Naming (`levi/naming.py`): per-dataset artifacts use the catalog name, per-run artifacts a timestamp — never a hash. `uv run levi migrate` upgrades older workspaces. See `.state.md`.
- `levi/sync.py`: background workspace sync (discover, refresh, rebuild raw views, drop removed datasets); `levi/revision.py` is the stat-only dataset revision shared with the annotation backend and viewer. Catalog writes go through `catalog.locked()` (cross-process).
- `levi/jobs.py`: allowlisted worker processes, immutable plans, timeouts and recovery.
- `levi/service.py`: local API, catalog, diagnostics and dataset file serving.
- `src/app/api/levi/` and `src/app/api/annotation/`: runtime proxies to the loopback backend.
- `levi/maintenance.py`: bounded cache cleanup and orphan reporting; preserve registered datasets, results and environments. It refuses while the service runs — `levi stop` first.
- `levi/agent/usage.py`: per-agent cost model (fixed + per-episode + per-frame, priced by reading mode, calibrated from recorded runs). Online runs are metered by LEVI itself; external agents self-report. Never present an estimate as a price.
- `levi/agent/housekeeping.py`: deletes only what `runs.prepare` can rebuild; committed revisions, provenance, inverse patches and open drafts stay.
- `levi/agent/detection.py`: model-free candidate regions for object annotation (colour quantisation plus connected components). It measures, it does not recognise.
- `src/utils/serviceToken.ts`: the UI token for the loopback API, read from the file the service owns so a Core restart does not strand the frontend. `authHeaders(url)` attaches it only to LEVI's own file service — never to a Hugging Face request.

See docs/AUDIT.md and docs/VALIDATION.md for guarantees and verified limits.
