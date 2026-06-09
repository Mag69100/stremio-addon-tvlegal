# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Deployment context

- Local-hosted Stremio addon (Node 18+ / Express). Process listens on `PORT` (`.env` sets `PORT=8080`).
- Public URL: `https://tvlegal.loostick.ovh/configure`. DNS → home/VPS → Apache2 reverse proxy → Node process.
- `BASE_URL` defaults to `https://tvlegal.loostick.ovh` (used to build absolute URLs returned to Stremio clients).
- Apache must forward the **full path including the base64 config segment** (`/:config/manifest.json`, `/:config/catalog/...`, `/:config/stream/...`, `/drm/*`, `/decrypt/*`, `/mediaflow/*`). DRM endpoints stream binary segments — keep proxy buffering off and timeout high.
- README has a WARP/SOCKS5 section: TF1 blocks datacenter IPs, so on a VPS the `TF1_PROXY_HOST/PORT` env vars are mandatory for TF1 Direct.

## Common commands

```bash
npm install            # install deps (Node 14+, but Dockerfile uses node:18-alpine)
npm start              # node index.js — only script defined
docker compose up -d   # local docker-compose.yml: builds, mounts ./device.wvd:ro, exposes PORT=8080
```

There is **no test runner, linter, or build step**. The `test-*.js` / `test-*.py` files at the repo root are local DRM-research scratch files and are gitignored (along with `drm-proxy-server.py`, `start-mediaflow.sh`, `device.wvd`, `.widevine-keys-cache.json`).

The CI workflow (`.github/workflows/docker-publish.yml`) only builds & pushes a multi-arch image to `ghcr.io/loo-stick/stremio-addon-tvlegal` on push to `main` / tags `v*`.

## Architecture

The addon is a single Node process (`index.js`, ~2.4k lines) wrapping the `stremio-addon-sdk` inside Express so we can add custom routes (`/configure`, `/drm/*`, `/decrypt/*`, `/mediaflow/*`).

### Per-user configuration via base64 URL segment

- `configure.html` builds a JSON config (selected catalogs, TF1+ creds, RugbyPass creds, TMDB key, MediaFlow URL/password, genre filters) and base64-encodes it into the addon URL: `https://tvlegal.loostick.ovh/<base64>/manifest.json`.
- Express route order in `index.js` matters: the **default** `/manifest.json` and `getRouter(builder.getInterface())` mount must be registered **before** the `/:config/...` variants. The `/:config` middleware decodes the config, sets a module-level `currentConfig`, then passes the request to a second `getRouter` instance.
- `currentConfig` is a single shared mutable variable read inside catalog/meta/stream handlers. Concurrent requests with different configs can race — keep that in mind before adding any await between "read currentConfig" and "use the resulting client". Per-config clients (TF1, TMDB, RugbyPass) are cached in `Map`s keyed by credentials so we don't re-auth on every request.

### Source clients (`lib/*.js`)

Each source is a self-contained client with its own in-memory TTL cache:

| Module | Auth | Notes |
| --- | --- | --- |
| `francetv.js` | none | yatta/k7 mobile APIs + Akamai token |
| `arte.js` | none | EMAC v4 catalogue + Player v2 streams |
| `tf1.js` | Gigya login → TF1 token | SOCKS5 proxy via `TF1_PROXY_*` env (created at module load — restart needed to change). Has a **global** auth-failure cooldown (`globalAuthFailedUntil`) to avoid IP-locking the account |
| `rugbypass.js` | DCE/IMG Arena (`x-api-key` + `realm: dce.worldrugby`) | Algolia search for events |
| `tmdb.js` | `TMDB_API_KEY` | Optional; without it, Films/Séries genre filters are stripped from the manifest by `getManifest()` |

### DRM pipeline (TF1+ Replay only)

This is the most subtle part of the codebase. There are **two coexisting decryption paths**, only one of which is shipped:

1. **MediaFlow path (production, used by `index.js`'s `TF1_REPLAY` handler):**
   `tf1.ensureToken()` → fetch DASH MPD + license URL from `mediainfo.tf1.fr` → `lib/widevine.js` spawns `python3 -c '...'` running `pywidevine` (loads `device.wvd`, posts challenge to TF1 license server, returns `{kid, key}` pairs) → keys cached in `.widevine-keys-cache.json` (48h TTL, keyed by SHA256 of PSSH only — license URL ignored because it carries dynamic tokens) → URL handed to a **MediaFlow Proxy** instance (defaults to `http://localhost:8787`, configurable per-user via the `mediaflowUrl` config field) which converts encrypted DASH → ClearKey HLS for Stremio. `start-mediaflow.sh` shows how it's launched locally with `API_PASSWORD`.

2. **Self-decrypt path (`drm-proxy-server.py` + `lib/drm-proxy.js` `/decrypt/*` routes):** A Flask server on port 8888 that does the full key extraction + segment decryption itself. **`drm-proxy-server.py` is gitignored** — it's local research, not part of the deployed addon. The `/decrypt/*` Express routes proxy to it but won't work without the Python server running. Don't assume this path is live.

`lib/drm-proxy.js` also exposes `/drm/mpd` + `/drm/license`, an older approach that injects `dashif:Laurl` into the MPD so the client itself does the license exchange. Not currently wired into any stream response — kept around for the `/drm/test` diagnostic page.

`device.wvd` (the L3 CDM) is **not** in the repo. Users provide their own; the file's presence and `WVD` magic header are checked by `widevine.checkAvailability()` before any TF1 replay request.

### ID scheme

All addon IDs are namespaced under `tvlegal:<source>:<kind>:...` (see `ID_PREFIX` in `index.js`). The `idPrefixes` in the manifest also includes `tt`, so the addon responds to IMDB IDs from other catalogs (Cinemeta, etc.) — the IMDB handler in `index.js` looks up the title via TMDB then searches Arte / France.tv for a match.

## Conventions worth knowing

- All log output uses bracketed prefixes (`[TV Legal]`, `[TF1]`, `[Widevine]`, `[DRM Proxy]`, `[MediaFlow]`). Keep the convention when adding logs — it's how you'll grep production output.
- Secrets in URL: TF1+/RugbyPass credentials and the TMDB key are encoded into the addon URL the user installs. This is intentional (no server-side user store) but means **don't log `req.params.config`** raw.
- `.env` in this repo is the deployment env, not just an example — it contains real credentials and is gitignored. `.env.example` is the template.
- `.gitignore` excludes `*.wvd`, `.widevine-keys-cache.json`, `test-*`, `drm-proxy-server.py`, `start-mediaflow.sh`, and (notably) `CLAUDE.md` itself — do not commit it.
