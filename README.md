# OpenZLTravel

**English** | [简体中文](README.zh-CN.md)

![Python](https://img.shields.io/badge/Python-3.11--3.13-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)
![LangChain](https://img.shields.io/badge/LangChain-1.x-1C3C3C)
![LangGraph](https://img.shields.io/badge/LangGraph-1.x-FF6B35)
![Vue](https://img.shields.io/badge/Vue-3-42B883?logo=vuedotjs&logoColor=white)

> [!IMPORTANT]
>
> OpenZLTravel is a development-stage AI travel-planning workbench created to
> study the engineering boundary between LangChain and LangGraph. It is not a
> booking platform. It does not provide payments, purchases, flight search,
> multi-city itineraries, or production accounts, and its results are not
> inventory or price commitments.

Users describe their origin, destination, dates, party size, budget, and
preferences in natural language. The Assistant retrieves and validates
attractions, trains, hotels, and weather. After the user confirms those facts,
the server issues a short-lived travel order and TravelGraph builds an itinerary,
routes, and a budget for review. Confirmed trips are saved only in the current
runtime's history.

![OpenZLTravel workspace](docs/images/openzltravel-assistant.png)

## User Flow

1. Complete the travel requirements through conversation.
2. Select a city, attractions, outbound and return train options, and a hotel
   from server-provided cards. Seat-class preferences can be stated in the
   conversation; transport or lodging can instead be marked as self-arranged.
3. The Assistant refreshes time-sensitive facts and issues a signed
   `TravelOrder Token`.
4. TravelGraph builds the day-by-day itinerary, routes between consecutive
   stops, and budget.
5. The user confirms the plan or requests one of the supported revisions.
6. The final itinerary appears in the current runtime's history drawer, where it
   can be viewed or deleted.

## Architecture and Trust Boundaries

- The LangChain Assistant owns natural-language conversation and read-only fact
  tools. TravelGraph never reads the conversation and does not call an LLM.
- Cities, POIs, trains, and hotels use server-validated fact IDs. The frontend
  cannot create facts or prices.
- The Graph accepts only `{"order_token":"..."}` as its initial input and then
  follows a fixed planning path.
- Missing fares, room prices, weather, or route costs remain unknown and produce
  explicit notices.
- `route_preview` is the only interrupt. It accepts only two revision forms:
  move an attraction to day N, or make day N contain one fewer attraction while
  keeping all selected attractions in the itinerary.

```mermaid
flowchart LR
    U[User] --> UI[Vue workspace]
    UI --> A[LangChain Assistant]
    A <--> P[Catalog / AMap / 12306 / RollingGo / Weather]
    A -->|Signed TravelOrder Token| V[validate_order]
    V --> I[build_itinerary]
    I --> R[build_routes]
    R --> B[calculate_budget]
    B --> C[validate_plan]
    C --> Q{route_preview}
    Q -->|Supported revision| I
    Q -->|Confirm| S[save_trip]
```

| Module | Responsibility |
| --- | --- |
| `backend/src/assistant/` | Conversation, fact tools, selection validation, session snapshots, and order handoff |
| `backend/src/travel_graph/` | Order validation, deterministic planning, routes, budget, interrupt, and saving |
| `backend/src/domain/` | Domain models, planning algorithms, and fact-boundary validation |
| `backend/src/infrastructure/` | PostGIS, AMap, 12306, RollingGo, and weather providers |
| `backend/src/runtime/` | Configuration, dependency wiring, anonymous identity, and signed tokens |
| `frontend/src/features/` | Conversation, fact cards, plan review, results, and history UI |

See [ARCHITECTURE.md](ARCHITECTURE.md) for the detailed design and
[docs/CODE_FLOW.md](docs/CODE_FLOW.md) for the complete call sequence.

## Quick Start

The shortest path uses the built-in `fake` providers. Their facts are fixed
Hangzhou demo data, so no catalog download or AMap, RollingGo, or 12306
credentials are required. The Assistant still requires a configured and
reachable OpenAI-compatible chat model whose endpoint supports tool calls and
`response_format=json_object`.

The Docker path requires Git, Windows PowerShell, Docker Desktop, and network
access. Python and Node.js are included in the images and are not host
prerequisites for this path. If the source is already downloaded, start from the
repository root and skip the `git clone` command.

### 1. Create Local Configuration

```powershell
git clone https://github.com/Kkkirito-123/OpenZLTravel.git
cd OpenZLTravel
Copy-Item backend/.env.example backend/.env
Copy-Item backend/.env.catalog.example backend/.env.catalog.local
```

Set the following values in `backend/.env`. The current Compose file consumes
`LLM_*` names and maps them to the application-facing `OPENAI_*` names, so
filling only the original `OPENAI_*` template entries is not enough to start
Compose.

```dotenv
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://your-openai-compatible-endpoint/v1
LLM_MODEL=your-model
LLM_TIMEOUT_SECONDS=60

PROVIDER_MODE=fake
RAIL_PROVIDER=public
AUTH_SECRET=change-this-local-secret-to-at-least-32-characters
```

Set two distinct local passwords in `backend/.env.catalog.local`. Use long
alphanumeric values so they remain valid in both URI and libpq connection
strings; other special characters require the corresponding escaping. The
unified Compose stack starts PostGIS even in `fake` mode, so both password
entries must be nonempty. `CATALOG_DATABASE_URL` is additionally required by
the catalog management script used for `live` mode.

```dotenv
CATALOG_POSTGRES_PASSWORD=replace-with-owner-password
CATALOG_DATABASE_URL=postgresql://catalogowner:replace-with-owner-password@127.0.0.1:55432/openzltravelcatalog
TRAVELAPP_POSTGRES_PASSWORD=replace-with-reader-password
```

Both local files are excluded by `.gitignore`. Never commit real credentials.

### 2. Validate and Start

```powershell
docker compose --env-file backend/.env --env-file backend/.env.catalog.local config --quiet
.\start.ps1 up
.\start.ps1 ps
```

If the Windows execution policy blocks local scripts, invoke the same command
without changing the machine-wide policy:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 up
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). The first build downloads
the Python and Node.js base images and project dependencies, so its duration
depends on network conditions.

| Service | Address | Responsibility |
| --- | --- | --- |
| `frontend` | `127.0.0.1:5173` | Vue workspace and same-origin proxies |
| `assistant` | `127.0.0.1:2030` | LangChain conversation, tools, and order issuance |
| `agent` | `127.0.0.1:2024` | LangGraph threads, runs, and trip APIs |
| `catalogdb` | `127.0.0.1:55432` | PostGIS catalog; unused by `fake` mode |

Routine operations:

```powershell
.\start.ps1 logs
.\start.ps1 restart
.\start.ps1 down
```

`down` preserves the named `openzltravelcatalogdata` volume. Do not run
`docker compose down -v` unless you intend to delete the restored catalog.

## Live Data

When `PROVIDER_MODE=live`, the Assistant uses the real catalog and external
providers. The restore workflow calls `catalog.ps1`, so first install Python
3.11 through 3.13 and the backend package on the host:

```powershell
cd backend
python -m pip install -e .
cd ..
```

The current `catalog.ps1` evaluates the complete Compose file with only the
catalog env file. Until that script also passes `backend/.env`, replace the
restore guide's final two `catalog.ps1` commands with the isolated invocation
below. The placeholder model variables exist only inside the child process, the
execution-policy override is process-local, and only `catalogdb` is started.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command {
    $env:LLM_API_KEY = "catalog-only-placeholder"
    $env:LLM_BASE_URL = "http://catalog-only.invalid/v1"
    $env:LLM_MODEL = "catalog-only-placeholder"
    .\catalog.ps1 -ProvisionRuntime
    .\catalog.ps1 -Verify
}
```

Then prepare the PostGIS catalog:

1. Download the [2026-08-28 catalog backup](https://drive.google.com/file/d/1bfyU_XFjQcnFIaAehMtYXhZZdvu1RdXQ/view?usp=drive_link).
2. Follow the [restore guide](docs/data/RESTORE.md) to verify the SHA256, restore
   the `catalog` schema, and create the read-only `travelapp` role.
3. Set `PROVIDER_MODE=live` in `backend/.env`, add any AMap and RollingGo
   credentials you need, and restart the stack.

The dump is 346.67 MiB. Its restored schema is approximately 2.54 GiB and
contains 135,533 POIs. The [catalog manifest](docs/data/catalog-20260828.json)
records the counts, sources, hash, and licenses. The restore procedure replaces
an existing `catalog` schema in the target database, so confirm the target first.

| Source | Default behavior | Credential |
| --- | --- | --- |
| PostGIS Catalog | Primary place catalog in `live` mode | Required through `backend/.env.catalog.local` |
| AMap | Place fallback, transit or real-time driving, and weather fallback; degrades when unavailable | Optional `AMAP_API_KEY` |
| 12306 | Calls the public query endpoint by default | No key with `RAIL_PROVIDER=public` |
| RollingGo | Searches hotels; falls back to catalog hotels on failure | Optional `ROLLINGGO_API_KEY` |
| Open-Meteo | Provides weather forecasts | No key |

## Current Limitations

- Only single-destination trips from 1 to 7 days are supported.
- Assistant snapshots live in the current browser's `sessionStorage`; there is
  no server-side conversation database. Session tokens last 12 hours by default,
  and travel order tokens last 10 minutes.
- Checkpoints use `InMemorySaver`, so a process restart loses in-progress Graph
  checkpoints. `start.ps1 restart` force-recreates the Agent container and also
  clears its threads, runs, and saved-trip Store because no runtime-data volume
  is mounted. The PostGIS named volume is independent of this limitation.
- Budgets are planning estimates. Meals and admission tickets use fixed rules,
  while unknown rail fares, room prices, and local transport costs are not
  invented, so totals can be low.
- 12306 normally covers only its sales window, and weather normally covers the
  next 16 days. Distant dates and upstream failures produce explicit unknown or
  degraded states.
- Standard `auto`, `walk`, and `driving` routes use local distance estimates.
  Only `transit` and `realtime_driving` attempt AMap, and they fall back to local
  estimates when it is unavailable.

## Development and Verification

Local development uses Python 3.11 through 3.13 and recommends Node.js 22.
Install the dependencies first:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

cd ..\frontend
npm.cmd ci

cd ..
```

Run static checks, tests, and the frontend build:

```powershell
cd backend
.\.venv\Scripts\python.exe -m ruff check src tests catalog_builder
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest -q

cd ..\frontend
npm.cmd test
npm.cmd run build

cd ..
docker compose --env-file backend/.env --env-file backend/.env.catalog.local config --quiet
```

The default suite uses fake providers. The real PostGIS integration test is
skipped unless `CATALOG_TEST_DATABASE_URL` points to a separate database named
`openzltravelcatalogtest`. The pyosmium source test is skipped unless the
optional catalog dependencies are installed with
`python -m pip install -e ".[dev,catalog]"`. Passing the commands above does not
claim that external providers have been tested.

## Repository Map

```text
OpenZLTravel/
|-- backend/
|   |-- src/assistant/          # LangChain Assistant service
|   |-- src/travel_graph/       # LangGraph workflow and APIs
|   |-- src/domain/             # Domain models and deterministic planning
|   |-- src/infrastructure/     # Catalog and external providers
|   |-- src/runtime/            # Config, identity, tokens, and dependency wiring
|   |-- catalog_builder/        # Catalog build, permissions, and validation
|   |-- benchmarks/             # Fixed functional and quality evaluations
|   `-- tests/
|-- frontend/src/features/      # Assistant, Planning, and Trips UI
|-- docs/                       # Call flow, catalog manifest, and restore guide
|-- README.md                   # English guide
|-- README.zh-CN.md             # Simplified Chinese translation
|-- compose.yml
|-- langgraph.json
|-- catalog.ps1
`-- start.ps1
```

## License and Attribution

This repository currently has no root-level `LICENSE` file, so no open-source
license has been declared for the project code. The catalog combines public data
from OpenStreetMap, GeoNames, Modood, AreaCity, and other sources. When using or
redistributing the backup, preserve the source and license information in the
[catalog manifest](docs/data/catalog-20260828.json).

## Inspiration

- [tutu-zzz/zhilv-yuntu](https://github.com/tutu-zzz/zhilv-yuntu)
- [Reyzowter/Hello-Agents](https://github.com/Reyzowter/Hello-Agents)
