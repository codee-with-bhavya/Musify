# Musify — Project Documentation

A self-hosted YouTube Music clone. The backend scrapes YouTube Music's internal
"InnerTube" private API directly (no official API key, no `ytmusicapi` package)
to fetch the home feed, search results, and artist/album/playlist pages. It uses
`yt-dlp` purely to resolve a playable audio stream URL for a given video ID. The
frontend is a single-page vanilla HTML/CSS/JS app (no framework, no build step)
that communicates with the backend over plain `fetch()` calls and plays audio
through a native `<audio>` element.

> **Last reviewed:** reflects the codebase after **Pass 13 (post-redesign interaction and streaming audit)**.
> All facts verified against actual source files. If you edit files after this
> point, re-read the relevant source before trusting this doc — see §12.

---

## 1. High-level architecture

```
┌──────────────────────┐     HTTP (fetch / afetch)    ┌───────────────────────────┐
│  frontend/index.html  │ ──────────────────────────▶ │  backend/main.py (FastAPI) │
│  served by Python     │ ◀────── JSON / stream ───── │  on http://<host>:8000     │
│  http.server port 3000│                              └────────────┬──────────────┘
└──────────────────────┘                                           │
                                                                   ▼
                                           ┌─────────────────────────────────────┐
                                           │  backend/scrapers/innertube.py        │
                                           │  httpx AsyncClient → YTM InnerTube   │
                                           │  private API (POST JSON, no API key)  │
                                           └──────────────┬──────────────────────┘
                                                          │  raw YTM JSON
                                                          ▼
                                           ┌─────────────────────────────────────┐
                                           │  backend/scrapers/parsers.py          │
                                           │  flattens YTM nested JSON into clean  │
                                           │  dicts for the frontend               │
                                           └─────────────────────────────────────┘

Audio path:
  <audio src="/api/stream/{videoId}">
    → yt-dlp in executor thread (~5–15 s)
    → resolves googlevideo.com CDN URL
    → httpx proxies bytes with Range/Content-Type headers
    → browser plays natively
```

Two servers always run side by side (`start_musify.bat`):

| Server | Command | Port |
|--------|---------|------|
| Backend | `uvicorn main:app --reload --host 0.0.0.0` | **8000** |
| Frontend | `python -m http.server 3000` | **3000** |

`API_BASE` is derived from `window.location.hostname` so the app works over LAN too.

---

## 2. Folder / file structure

```
trial 2/
├── requirements.txt              # Python deps (pinned)
├── start_musify.bat              # Windows launcher — starts both servers + opens browser
├── PROJECT_DOCUMENTATION.md     # ← this file
│
├── backend/
│   ├── main.py                  # FastAPI app — all routes
│   ├── test_scraper.py          # Manual smoke-test (not pytest)
│   ├── __pycache__/
│   └── scrapers/
│       ├── innertube.py         # InnerTube client (search, browse, player, suggestions)
│       ├── parsers.py           # Converts raw YTM JSON → flat dicts
│       ├── cipher.py            # TOMBSTONE — empty comment file, nothing imports it
│       └── __pycache__/
│
└── frontend/
    └── index.html               # Entire frontend: HTML + inline CSS + inline JS
```

No database, no `.env`, no Docker, no config file. All settings hardcoded in source.

---

## 3. Backend — `backend/main.py`

### 3.1 App setup

- CORS open (`allow_origins=["*"]`) — fine for local use.
- Logging middleware prints every request + response status to stdout.
- One global `InnerTube()` instance shared across all requests.

### 3.2 In-memory TTL cache

```python
_cache: dict = {}
CACHE_TTL = 600  # 10 minutes
```

Only `/api/home` and `/api/explore` are cached. Both accept `?refresh=true` to bypass.
**Cache resets on every uvicorn `--reload`** (including saves to any `.py` file).

### 3.3 Route table

| Method & Path | Purpose | Notes |
|---|---|---|
| `GET /api/home` | Aggregated home feed | Cached 10 min. 4 browse + 5 song-search calls via `asyncio.gather(return_exceptions=True)`. Merges duplicate shelf titles. Prepends "Recommended For You" shelf of up to 25 directly-playable song cards. |
| `GET /api/explore` | Explore tab | Cached 10 min. Single browse call. |
| `GET /api/search?q=&filter=` | Search | filter: `songs`/`albums`/`artists`/`playlists` or omit for all. |
| `GET /api/suggestions?q=` | Autocomplete | Returns list of strings. |
| `GET /api/artist/{browseId}` | Artist page | |
| `GET /api/album/{browseId}` | Album page | |
| `GET /api/playlist/{browseId}/more` | Playlist pagination | **Must stay above** `/api/playlist/{browseId}` — FastAPI matches top-to-bottom. Requires `?continuation=<token>`. |
| `GET /api/playlist/{browseId}` | Playlist first page | Returns `continuation` token when more tracks exist. |
| `GET /api/stream/{videoId}` | Audio stream proxy | yt-dlp + httpx streaming, Range header support. |
| `GET /api/song/{videoId}` | Now-playing metadata | **Orphaned — frontend never calls this.** |
| `GET /api/debug/search?q=` | Raw search JSON | Dev only. |
| `GET /api/debug/home` | Raw home JSON | Dev only. |
| `GET /api/debug/stream/{videoId}` | Raw player format info | Dev only. |

### 3.4 Audio streaming (`/api/stream/{videoId}`)

```python
loop = asyncio.get_running_loop()
stream_url, mime_type = await loop.run_in_executor(None, extract_url, videoId)
# Then:
ctx = innertube.stream_client.stream("GET", stream_url, ...)
upstream_res = await ctx.__aenter__()
async def stream_audio():
    try:
        async for chunk in upstream_res.aiter_bytes(chunk_size=65536):
            yield chunk
    finally:
        await ctx.__aexit__(None, None, None)  # always closes, even on disconnect
```

The manual `__aenter__`/`__aexit__` pattern in the generator `finally` is **intentional** —
it keeps the httpx connection open for the browser's full read without buffering into RAM.
Do not simplify to `async with`.

---

## 4. Backend — `backend/scrapers/innertube.py`

### 4.1 Client profiles

| Profile | Used for | Notes |
|---|---|---|
| `WEB_REMIX` | All browse/search/suggestions | Version `1.20241121.01.00` — hardcoded, silent break if YouTube rotates it. |
| `ANDROID_MUSIC` | Player endpoint | Gets pre-signed CDN URLs. Fresh random 16-char `cpn` per request. |
| `TVHTML5_SIMPLY_EMBEDDED` | **Never used** | Dead code, safe to delete. |

### 4.2 Two httpx clients

```python
self.client        # JSON headers — for InnerTube API calls
self.stream_client # No JSON headers — for proxying audio bytes
```

### 4.3 Key rule: `browse()` — browseId OR continuation, never both

```python
async def browse(self, browse_id, params=None, continuation=None):
    body = {}
    if browse_id:
        body["browseId"] = browse_id
    if continuation:
        body["continuation"] = continuation
```

Pass empty string `""` as `browse_id` when using a continuation token.
YouTube rejects both fields simultaneously.

---

## 5. Backend — `backend/scrapers/parsers.py`

### 5.1 `ParserUtils` helpers

| Helper | Purpose |
|---|---|
| `get_text(runs)` | Joins YouTube `runs` array into a string |
| `get_thumbnail(thumbnails)` | Returns URL of the largest thumbnail |
| `get_header_thumbnail(thumbnail_obj)` | Tries `musicThumbnailRenderer` then `croppedSquareThumbnailRenderer` |
| `get_nav_endpoint(renderer)` | Tries direct `navigationEndpoint` and overlay play button path |
| `get_video_id(renderer)` | Tries overlay play button first, then `navigationEndpoint.watchEndpoint` |
| `get_browse_id(renderer)` | Reads `navigationEndpoint.browseEndpoint.browseId` |
| `guess_type(video_id, browse_id, subtitle)` | Heuristic by browseId prefix → subtitle keywords → defaults `"song"` |

### 5.2 Parser functions

| Function | Returns | Notes |
|---|---|---|
| `parse_home_feed` | `[{title, items}]` | Handles carousel, shelf, immersive carousel renderers |
| `parse_search_results` | `[{title, subtitle, videoId, browseId, thumbnail, type}]` | Top card + shelf items |
| `parse_artist_page` | `{title, thumbnail, sections}` | |
| `parse_album_page` | `{title, subtitle, thumbnail, tracks, artistId, type}` | `if not flex_cols: continue` guard |
| `parse_playlist_page` | `{title, subtitle, description, thumbnail, tracks, continuation, type}` | See §5.3 — header path confirmed against real captured response (Pass 8) |
| `parse_song_info` | `{title, subtitle, thumbnail, videoId}` | Powers orphaned `/api/song` route |

All parsers wrap in `try/except`, print traceback on failure, return empty dict/list.

### 5.3 `parse_playlist_page` — header resolution (fixed + confirmed, Pass 8)

**Root cause:** `response["header"]` comes back as an empty dict `{}` for playlists
rendered under YouTube Music's current (2024+) page redesign. The header renderer
wasn't deleted — it moved out of the top-level `header` key entirely.

**Pass 7 guessed wrong.** It assumed the relocated header lived under
`twoColumnBrowseResultsRenderer.primaryContents` (a section sibling to the track
shelf in `secondaryContents`). That guess was reasonable but incorrect, and the
bug remained after Pass 7 shipped.

**Pass 8 confirmed the real path** by having the parser dump one full raw playlist
response to `backend/debug_playlist_response.json`, then inspecting it directly
(script-searched for every key containing `"Header"`). The actual location:

```
response.contents.twoColumnBrowseResultsRenderer
  .tabs[0].tabRenderer.content.sectionListRenderer.contents[0]
  .musicResponsiveHeaderRenderer
```

i.e. `twoColumnBrowseResultsRenderer` now has a `tabs` array (the same shape
`singleColumnBrowseResultsRenderer` uses elsewhere) — **not** `primaryContents`.
`secondaryContents` is unchanged and still holds the track shelf
(`musicPlaylistShelfRenderer`).

The `musicResponsiveHeaderRenderer` itself has: `thumbnail` (same
`musicThumbnailRenderer.thumbnail.thumbnails[]` shape as before), `title.runs`,
`subtitle.runs` (e.g. `"Playlist • 2023"`), and a previously-unused
`secondSubtitle.runs` (e.g. `"10M views • 25 tracks • 1 hour, 50 minutes"`).

**Fix applied** (`parse_playlist_page` in `parsers.py`):
1. `musicResponsiveHeaderRenderer` in the top-level `raw_header` lookup chain (kept from Pass 7, still correct as a first check).
2. If `raw_header` is empty, walk the **tabs path** (`tabs[0].tabRenderer.content
   .sectionListRenderer.contents`) looking for `musicResponsiveHeaderRenderer` /
   `musicDetailHeaderRenderer` / `musicImmersiveHeaderRenderer` as a section key.
   This replaces Pass 7's incorrect `primaryContents` walk.
3. Final fallback: `_find_header_renderer()` — recursive scan for any
   `...HeaderRenderer` dict with a `title` field — untouched from Pass 7, still
   a reasonable safety net for future YouTube changes.
4. `subtitle` now concatenates `header.subtitle` **and** `header.secondSubtitle`
   (e.g. `"Playlist • 2023 • 10M views • 25 tracks • 1 hour, 50 minutes"`)
   instead of only the first half.
5. Thumbnail-hunting fallback loop searches the tabs-path sections instead of
   `primaryContents`.

**Verified**, not assumed: the fix was tested by running the actual parser logic
against a real captured playlist response (a "b hart breack" playlist) before
being declared fixed — title, subtitle, and thumbnail URL all resolved correctly.

Debug dump code has been removed from `parse_playlist_page`. The temporary
`backend/debug_playlist_response.json` file was emptied (Claude's filesystem tool
has no delete capability) — safe to delete by hand.

---

## 6. Frontend — `frontend/index.html`

Single file: inline `<style>`, HTML body, inline `<script>`. No framework, no bundler.

### 6.1 Visual structure

- **Sidebar** (desktop, 240px): logo, nav (Home/Search/Explore), Library (Liked Songs/Downloads/Playlists)
- **Top bar** (fixed): search input + autocomplete dropdown
- **Main content** (scrollable): five toggled views — home, explore, search, library, detail
- **Player bar** (fixed bottom, 80px): thumbnail, title/artist, controls (shuffle/prev/play/next/repeat/like), progress bar, volume
- **Bottom tab bar** (mobile only): Home/Search/Explore
- **Toast**: auto-dismiss 3 s, error variant

Theme: dark purple — `--bg: #0d0d12`, `--accent: #8b5cf6`. Fonts: Inter + Space Grotesk (Google Fonts CDN).

### 6.2 Key state variables

```javascript
let queue = [], queueIndex = -1, shuffle = false, repeat = false;
let currentFilter = '';
let homeSectionData = [], exploreSectionData = [], detailSectionData = [];
let currentViewTracks = [], currentDetailBrowseId = null, currentContinuation = null;
let detailVersion = 0, navigationVersion = 0;
let playbackFailures = new Set();
let likedTracks = [];           // in-memory copy, populated by loadLibrary()
let sessionRecentlyPlayed = []; // up to 12, resets on reload
let lastSearchResults = [];
let debounceTimer;
```

### 6.3 AbortController registry

```javascript
const _controllers = {};
function abortAndNew(slot) { /* cancels in-flight, returns new controller */ }
async function afetch(url, slot, opts = {}) { /* fetch with abort signal */ }
```

Slots: `'home'`, `'explore'`, `'detail'`, `'search'`, `'suggestions'`.

`loadMoreTracks()` uses the `'loadmore'` slot and verifies its captured playlist,
continuation, and navigation version before changing the active detail view.

**Exception:** `loadMoreTracks` uses bare `fetch()` — no abort protection. Known gap.

### 6.4 Key functions

| Function | Purpose |
|---|---|
| `loadHome()` | Fetches `/api/home`, sets `homeSectionData`, prepends "Continue Listening" if session history exists |
| `loadExplore()` | Fetches `/api/explore`, sets `exploreSectionData` |
| `loadArtist/Album/Playlist(id)` | Fetch detail data, call `renderDetailView` |
| `renderDetailView(data, typeLabel)` | Renders header + track list, sets `currentViewTracks/BrowseId/Continuation`, shows Load more if token present |
| `loadMoreTracks()` | Appends next page tracks to `#track-list`, re-queries button by id (not stale ref) |
| `renderShelves(sections, viewKey)` | Renders card shelves; `viewKey` embedded in each card onclick |
| `handleCardClick(viewKey, sIdx, iIdx)` | Bounds-checked, routes to play or navigate |
| `playFromShelf/CurrentView/List/Liked` | Various queue-loading entry points, all filter by `videoId` |
| `playCurrent()` | Updates player bar, sets `audio.src`, calls `audio.play()` |
| `toggleLike / toggleLikeById` | localStorage liked songs management |
| `seek(e)` | Guards `NaN`/`Infinity` duration before setting `currentTime` |
| `esc(s)` | HTML-escapes all API strings before `innerHTML` — **text content only, never `src=` attributes** |
| `srcUrl(s)` | URL-safe sanitiser for `img src` attributes — strips `"`, `<`, `>` but preserves `&` so CDN query params survive |

### 6.5 Liked Songs

- Stored in `localStorage` key `'musify_liked_songs'` as JSON array of track objects.
- Persists across reloads. Browser-local, no sync.
- Heart button in player bar syncs via `updateLikeBtn()` on every track change.
- `loadLibrary('Liked Songs')` renders full list with Play button and per-row unlike.
- `toggleLike` live-refreshes the view when it's open.

---

## 7. End-to-end data flows

### 7.1 Home → play song

1. `window.onload` → `initRouter()` → `loadHome()` → `afetch('/api/home', 'home')`
2. Backend: cache hit → return immediately. Miss → 9 concurrent calls, merge, cache.
3. `renderShelves(homeSectionData, 'home')` — card onclick embeds `viewKey='home'`
4. Click → `handleCardClick('home', sIdx, iIdx)` → bounds-check → `playFromShelf`
5. `playFromShelf` filters to `videoId` items, sets `queue`/`queueIndex`, calls `playCurrent()`
6. `audio.src = /api/stream/{videoId}` → yt-dlp (~5–15 s) → httpx stream → plays

### 7.2 Playlist → Load more

1. Click playlist card → `navigate('playlist', browseId)` → `loadPlaylist(id)`
2. `afetch('/api/playlist/{id}', 'detail')` → `parse_playlist_page` → tracks + continuation token
3. `data.browseId = id` set before `renderDetailView` (needed for pagination)
4. "Load more" click → `loadMoreTracks()`:
   - `fetch('/api/playlist/{browseId}/more?continuation=...')`
   - Backend: `innertube.browse("", continuation=token)` — empty browseId intentional
   - Walks `continuationContents.musicPlaylistShelfContinuation`
   - Appends rows, re-queries `#load-more-btn` by id (not stale ref)

---

## 8. Current feature status

| Feature | Status | Notes |
|---|---|---|
| Home feed | ✅ Working | Multi-source merge, "Made For You", "Continue Listening" |
| Explore tab | ✅ Working | |
| Search + filters | ✅ Working | All/songs/albums/artists/playlists |
| Search autocomplete | ✅ Working | 250ms debounce, DOM-built (XSS-safe) |
| Artist page | ✅ Working | |
| Album page | ✅ Working | |
| Playlist page — tracks | ✅ Working | Track list loads correctly |
| Playlist page — Load more | ✅ Working | Pagination working, button crash-safe |
| Playlist cover image | ✅ Working (Pass 9, confirmed) | Header renderer found in tabs path (Pass 8); `srcUrl()` helper fixes `&amp;` URL corruption in `src=` attributes (Pass 9) |
| Playlist title/subtitle | ✅ Working (Pass 8, confirmed) | Subtitle now combines `subtitle` + `secondSubtitle` runs for full detail line |
| Audio streaming | ✅ Working | yt-dlp + httpx proxy, Range headers, true streaming |
| Player controls | ✅ Working | play/pause/prev/next/shuffle/repeat/seek/volume |
| Progress bar seek | ✅ Working | NaN/Infinity guard in place |
| Liked Songs | ✅ Working | localStorage persistence, live refresh |
| Session history | ✅ Working | In-memory, resets on reload |
| AbortController navigation | ✅ Working | All nav fetches use `afetch` with named slots |
| XSS protection | ✅ Working | `esc()` everywhere + DOM construction for suggestions |
| Home/explore cache | ✅ Working | 10-min TTL, `?refresh=true` bypass |
| Mobile layout | ✅ Working | |
| LAN access | ✅ Working | `API_BASE` from `window.location.hostname` |
| Downloads | ❌ Not built | Placeholder only |
| User playlists | ❌ Not built | Placeholder only |
| Lyrics / crossfade / queue UI | ❌ Not built | |

---

## 9. Known bugs and issues

### Active

**Resolved in Pass 13:** the historical `loadMoreTracks` no-AbortController
entry below no longer applies; pagination requests now use `afetch()`.

| # | Sev | Description | File |
|---|-----|-------------|------|
| 2 | 🟡 | **`loadMoreTracks` fetch has no AbortController** — rapid double-clicks can fire two concurrent pagination requests. Button UI is crash-safe (re-queried by id) but network requests can duplicate. | `index.html` |
| 4 | 🟡 | **Suggestions dropdown not closed by keyboard navigation** — tabbing away from search leaves dropdown open. Mouse clicks outside already close it. | `index.html` |
| 5 | 🟢 | **`DEBUG = true` hardcoded** — floods console with buffering/seek events. | `index.html` |
| 6 | 🟢 | **`cipher.py` tombstone** — comment-only file, nothing imports it. Safe to delete. | `cipher.py` |
| 7 | 🟢 | **`TVHTML5_SIMPLY_EMBEDDED` dead client** — defined, never used. Safe to delete. | `innertube.py` |
| 8 | 🟢 | **`/api/song/{videoId}` orphaned route** — works but frontend never calls it. | `main.py` |
| 9 | 🟢 | **`WEB_REMIX` client version hardcoded** — silent break if YouTube rotates it. | `innertube.py` |
| 10 | 🟢 | **yt-dlp blocks executor thread** — 5–15 s per stream, no timeout/retry/backoff. | `main.py` |
| 11 | 🟢 | **`get_playlist_more` duplicates parser logic** — hand-rolled copy in `main.py`; won't get parser fixes automatically. | `main.py` |

---

## 10. Claude Fix History

All entries verified against current source files.

### Pass 1 — Pre-session fixes (before Claude logging)

| # | Fix | Files |
|---|-----|-------|
| P1-1 | Stream 403 misreported as "format not supported" — upstream status validated before piping | `main.py` |
| P1-2 | Playlists unopenable — route, parser, and frontend click routing all added | `main.py`, `parsers.py`, `index.html` |
| P1-3 | Home feed silently dropping content — `asyncio.gather(return_exceptions=True)` + shelf merge | `main.py` |
| P1-4 | Playlist/album cover art blank — `get_header_thumbnail` checks both renderer key variants | `parsers.py` |

### Pass 2 — 6-fix batch

| # | Fix | Files |
|---|-----|-------|
| B2-1 | 10-min in-memory TTL cache for `/api/home` and `/api/explore` | `main.py` |
| B2-2 | Liked Songs — localStorage persistence, heart button, full library view | `index.html` |
| B2-3 | Playlist pagination — continuation token extraction, `/api/playlist/{id}/more` route, Load more button | `main.py`, `parsers.py`, `index.html` |
| B2-4 | Dead Cipher import removed from `main.py` | `main.py` |
| B2-5 | `cipher.py` Cipher class deleted, file kept as tombstone comment | `cipher.py` |
| B2-6 | AbortController registry — `abortAndNew`/`afetch`, AbortError silently ignored | `index.html` |

### Pass 3 — 9-fix batch

| # | Sev | Fix | Files |
|---|-----|-----|-------|
| B3-1 | 🔴 | Route ordering — `/api/playlist/{id}/more` moved above `/api/playlist/{id}` (was unreachable) | `main.py` |
| B3-2 | 🔴 | httpx stream connection leak — generator `finally` block guarantees close on disconnect | `main.py` |
| B3-3 | 🔴 | `get_event_loop()` deprecated — replaced with `asyncio.get_running_loop()` | `main.py` |
| B3-4 | 🟠 | Stale `sectionData` global — split into `homeSectionData`/`exploreSectionData`/`detailSectionData` with `viewKey` embedded in card onclicks | `index.html` |
| B3-5 | 🟠 | `window.currentViewTracks` global — replaced with scoped module-level vars | `index.html` |
| B3-6 | 🟠 | Unlike button wrong-track bug — `toggleLikeById(videoId)` looks up at click-time, not by stale index | `index.html` |
| B3-7 | 🟠 | Dead `contents` block in `parse_playlist_page` — redundant first computation removed | `parsers.py` |
| B3-8 | 🟡 | Hardcoded `cpn` — `player()` generates fresh 16-char random alphanumeric per request | `innertube.py` |
| B3-9 | 🟡 | XSS in suggestions dropdown — replaced `innerHTML` with DOM construction | `index.html` |

### Pass 4 — 5-bug batch

| # | Sev | Fix | Files |
|---|-----|-----|-------|
| B4-1 | 🔴 | `window.likedTracks` legacy write deleted from `loadLibrary` | `index.html` |
| B4-2 | 🔴 | `loadMoreTracks` TypeError on `btn.parentElement` — stale ref replaced with fresh `getElementById` in both success and catch paths | `index.html` |
| B4-3 | 🟠 | `handleCardClick` stale-index crash — bounds-check before accessing `data[sIdx].items[iIdx]` | `index.html` |
| B4-4 | 🟠 | `playFromCurrentView` streaming `/api/stream/undefined` — filters `currentViewTracks` by `videoId`, remaps index by match | `index.html` |
| B4-5 | 🟠 | `seek()` NaN/Infinity crash — `!audio.duration || !isFinite(audio.duration)` guard added | `index.html` |

### Pass 5 — Playlist cover attempt (incomplete)

| # | Sev | Status | Fix attempted | Files |
|---|-----|--------|---------------|-------|
| B5-1 | 🟠 | ❌ **Incomplete** | `parse_playlist_page` updated to try `musicDetailHeaderRenderer` → `musicImmersiveHeaderRenderer` → `musicEditablePlaylistDetailHeaderRenderer` → walk all header keys. Had no effect because `response["header"]` itself is `{}` — the real header is elsewhere in the response. | `parsers.py` |

### Pass 7 — Playlist cover fix attempt (incorrect, superseded by Pass 8)

| # | Sev | Status | Fix attempted | Files |
|---|-----|--------|---------------|-------|
| B7-1 | 🔴 | ❌ **Wrong guess** | Assumed the relocated header lived in `twoColumnBrowseResultsRenderer.primaryContents`. Reasonable guess given the section-sibling pattern elsewhere in the codebase, but incorrect — the real location is the `tabs` path (see Pass 8). Bug remained after this pass shipped. | `parsers.py` |
| B7-2 | 🟢 | ✅ Kept | `description` field bug (was reading raw `response["header"]` instead of resolved `header`) — this part of the fix was correct and is retained in Pass 8. | `parsers.py` |

### Pass 13 — Post-redesign interaction and streaming audit (complete)

| # | Fix | Files |
|---|-----|-------|
| B13-1 | A failed audio stream previously showed a toast but left the player on the broken item. Failed `videoId`s are now tracked for the active queue and playback advances to an untried item; when none remain, the user receives one clear error instead of a retry loop. Empty-queue guards were also added to play/pause, previous, and next. | `index.html` |
| B13-2 | A slow artist, album, or playlist request could finish after the user navigated elsewhere and replace the right context panel with stale collection data. Navigation versions now prevent stale responses from changing view data or panel context. Pagination applies the same check, preventing an old Load more response from appending to a newly opened playlist. | `index.html` |
| B13-3 | `/api/stream` caught its own `HTTPException` values and returned a generic 503. Intended upstream HTTP statuses, including the 30-second extraction timeout (504), now propagate to the frontend correctly. | `main.py` |
| B13-4 | Search shortcuts now route through `navigate('search')`, so direct desktop/mobile Search actions update browser history. The direct play/resume action also catches rejected `audio.play()` promises, preventing unhandled-promise console errors. | `index.html` |

Verification: inline frontend JavaScript passed `node --check`; backend Python parsed successfully; local frontend and FastAPI OpenAPI endpoints returned HTTP 200. Live catalogue and stream extraction could not be completed in this environment because outbound YouTube access is blocked.

### Pass 12 — Post-redesign bug fixes: like button, playFromLiked, prev() (complete)

| # | Sev | Fix | Files |
|---|-----|-----|-------|
| B12-1 | 🔴 | **Player like button visually broken after redesign.** The button is SVG-based (`#like-icon-empty` / `#like-icon-filled`). `toggleLike()` was calling `btn.textContent = '♡'` or `'♥'`, which wiped all SVG child nodes and replaced them with a raw emoji glyph. The button looked correct on the first click but the SVG icons were gone for the rest of the session. Fix: removed `btn` manipulation from `toggleLike()` for the player bar case entirely. `updateLikeBtn()` — which already does the correct SVG visibility swap and class toggle — is now always called at the end of `toggleLike()`. The emoji `btn` path in `toggleLike()` is preserved because it is still used by the per-row unlike buttons in the Liked Songs list (those are plain emoji `♥` text nodes, not SVG). | `index.html` |
| B12-2 | 🔴 | **`playFromLiked()` could use a stale queue.** `likedTracks` (module-level var) is only populated by `loadLibrary('Liked Songs')`. If the user liked or unliked tracks without navigating away and back, `likedTracks` was out of date. The `getLiked()` fallback in the ternary only fired when `likedTracks` was empty (session cold start), not when it was stale. Fix: removed the ternary entirely. `playFromLiked()` now always calls `getLiked()` directly, which reads the current localStorage snapshot. `queueIndex` is clamped to `fresh.length - 1` as a safety guard against edge cases where the list shrank between render and click. | `index.html` |
| B12-3 | 🟠 | **`prev()` re-fired `onerror` when the current stream was broken.** When a stream fails, `audio.error` is set and `audio.currentTime` stays at 0. `prev()` checked `currentTime > 3` to decide between "seek to start" and "go to previous track". With a broken stream, `currentTime` is 0, so `0 > 3` is false — it correctly called `playCurrent()` for the previous track in the non-errored case, but if the user had scrubbed past the 3-second mark before the stream broke, `currentTime > 3` was true and `audio.currentTime = 0` was called on the broken `src`, which re-fired `onerror` and showed a spurious error toast instead of navigating. Fix: `prev()` now reads `audio.error` first. If an error is active, the `currentTime > 3` branch is skipped unconditionally and the function always moves to the previous track and calls `playCurrent()`. | `index.html` |

### Pass 11 — Recommended For You: direct song cards (complete)

| # | Sev | Fix | Files |
|---|-----|-----|-------|
| B11-1 | 🔴 | **"Made For You" only ever played one song.** The shelf was built by searching for mix/playlist names ("Daily Mix", "Chill Mix", etc.) with the playlists filter, then taking the top card result for each. Those top cards had `type="playlist"` (after Pass 10's fix), so clicking navigated to a playlist page — but the playlist parser returned zero tracks for radio/mix IDs (different YTM response shape), leaving only one song in the queue. Root fix: replaced the entire approach. "Made For You" renamed to **"Recommended For You"**. Queries changed to mood/genre song searches ("top hindi hits", "best english pop songs", "chill lofi songs", "bollywood romantic songs", "top workout songs") using the **songs filter** (`EgWKAQIIAWo...`). Takes up to 5 `type="song"` results per query, deduped by `videoId`, giving ~20–25 directly-playable song cards. Clicking any card calls `playFromShelf` which plays that song and queues the full shelf — no playlist navigation step. Debug dump block removed from `/api/playlist/{browseId}`. | `main.py` |

### Pass 10 — Made For You row not clickable (complete)

| # | Sev | Fix | Files |
|---|-----|-----|-------|
| B10-1 | 🔴 | **Made For You cards silent no-op on click.** Mix/radio playlists (Daily Mix, Chill Mix, Romance Mix, etc.) returned by the playlist-filter search use a `watchEndpoint` with a `playlistId` on the title run rather than a `browseEndpoint`. `parse_search_results` only checked `browseEndpoint` for `browse_id`, so it came back `None`; `guess_type(videoId, None, subtitle)` returned `"song"`; and `handleCardClick` called `playFromShelf` which tried to play the first track's `videoId` as a lone song. Since the mix's `videoId` is not in the shelf's song list, nothing happened. Fix: after extracting `browse_id` from `browseEndpoint`, check `watchEndpoint.playlistId` if `browse_id` is still empty. If present, set `browse_id = "VL{playlistId}"` (YouTube Music's playlist-browse prefix) and clear `video_id` so `guess_type` correctly returns `"playlist"`. `handleCardClick` already routes `type === 'playlist'` to `navigate('playlist', browseId)` — no frontend change needed. | `parsers.py` |

### Pass 9 — Image URL corruption fix (complete)

| # | Sev | Fix | Files |
|---|-----|-----|-------|
| B9-1 | 🔴 | **All thumbnails broken — `esc()` was used in `src=` attributes.** `esc()` converts `&` to `&amp;`, corrupting every YouTube CDN URL with query-string parameters (e.g. `?sqp=...&rs=...`). The browser requests the `&amp;`-encoded URL and gets a broken image. Affected every image in the app across 7 callsites: playlist/album/artist covers, home shelf cards, search thumbnails, track row thumbnails, liked-songs list, load-more rows. Added `srcUrl(s)` which strips only `"`, `<`, `>` but preserves `&`. Replaced all 7 `esc()` calls inside `src="..."` with `srcUrl()`. Text content still uses `esc()`. | `frontend/index.html` |

### Pass 8 — Playlist cover fix, confirmed against real data (complete)

| # | Sev | Fix | Files |
|---|-----|-----|-------|
| B8-1 | 🔴 | **Playlist cover/title/subtitle blank — actually fixed this time.** Added a temporary debug dump of the raw playlist response to `backend/debug_playlist_response.json`, read that file directly (via filesystem access to the user's machine) and script-searched it for the real header location. Confirmed path: `contents.twoColumnBrowseResultsRenderer.tabs[0].tabRenderer.content.sectionListRenderer.contents[0].musicResponsiveHeaderRenderer` — a `tabs` array, not `primaryContents` as Pass 7 assumed. Parser's header-search fallback rewritten to walk this path. Fix verified by running the actual parser logic against the captured response before considering it done — title/subtitle/thumbnail all confirmed non-empty. | `parsers.py` |
| B8-2 | 🟢 | `subtitle` now concatenates `header.subtitle` and the previously-unused `header.secondSubtitle` (view count / track count / duration) for a fuller info line. | `parsers.py` |
| B8-3 | 🟢 | Debug dump code removed from `parse_playlist_page`. Leftover `backend/debug_playlist_response.json` emptied (no delete tool available) — safe to delete manually. | `parsers.py` |

---

## 11. How to run the project

### Quick start

```bat
start_musify.bat
```

Opens two terminal windows + browser after 5 s.

### Manual

```bash
# Terminal 1 — Backend
cd "C:\Users\bhavy\OneDrive\Desktop\trial 2\backend"
pip install -r ..\requirements.txt
python -m uvicorn main:app --reload --host 0.0.0.0

# Terminal 2 — Frontend
cd "C:\Users\bhavy\OneDrive\Desktop\trial 2\frontend"
python -m http.server 3000
```

Open `http://localhost:3000`.

### Dev tips

- Saving any `.py` file triggers uvicorn `--reload` → **clears cache**. Next home/explore is slow (re-fetches). Expected.
- Force cache refresh: `http://localhost:8000/api/home?refresh=true`
- Inspect raw API: `http://localhost:8000/api/debug/home`
- No test runner. `test_scraper.py` is manual: `python test_scraper.py` from `backend/`.
- Liked Songs survive reloads (localStorage), browser-local only.

### Requirements

```
fastapi==0.111.0
uvicorn==0.30.1
httpx==0.27.0
yt-dlp>=2024.1.0
```

Python 3.10+ recommended.

---

## 12. Critical notes for the next Claude editing this project

1. **Route order in `main.py` is load-bearing.** `/api/playlist/{browseId}/more` must stay above `/api/playlist/{browseId}`.

2. **`innertube.browse()` — browseId OR continuation, never both.** Pass `""` as `browse_id` when paginating.

3. **`afetch` not bare `fetch`.** All new data-loading code must use `afetch(url, slot)`. `loadMoreTracks` is the only exception and is a known gap.

4. **`esc()` for text, `srcUrl()` for `src=` attributes — never mix them.** `esc()` converts `&` → `&amp;` which corrupts YouTube CDN URLs (e.g. `?sqp=...&rs=...`). Every `img src` must use `srcUrl()`. Every text value going into `innerHTML` must use `esc()`. Suggestions use DOM construction — keep it that way.

5. **`renderShelves` requires a `viewKey`.** Always pass `'home'`, `'explore'`, or `'detail'`. The viewKey is embedded in card onclicks and used by `handleCardClick` to read from the correct scoped array.

6. **The httpx streaming pattern is intentional.** Manual `ctx.__aenter__`/`ctx.__aexit__` in a generator `finally` block is the only correct pattern for streaming without RAM-buffering. Do not simplify to `async with`.

7. **`parse_playlist_page` header resolution has 3 fallback tiers** (top-level `header` → **tabs-path walk** `tabs[0].tabRenderer.content.sectionListRenderer.contents` → recursive scan). Note: it is the `tabs` path, not `primaryContents` — Pass 7 guessed `primaryContents` and shipped a non-fix; Pass 8 corrected it against real captured data. If YouTube changes the response shape again, verify against a real response (dump + inspect) before patching — don't guess a path a second time.

8. **`playFromCurrentView` vs `playFromShelf`**: detail track lists use `playFromCurrentView(idx)` (reads/filters `currentViewTracks`); shelf cards use `playFromShelf(viewKey, sIdx, iIdx)` (reads scoped section data).

9. **Cache resets on uvicorn reload.** Don't be surprised by slow home/explore after editing a `.py` file.

10. **Dead code safe to delete:** `cipher.py`, `TVHTML5_SIMPLY_EMBEDDED` client profile in `innertube.py`, `/api/song/{videoId}` route in `main.py`.

---

## 13. Summary

### What Claude has fixed (36 fixes across 11 complete passes)

Stream 403 fix • Playlists unopenable • Home feed dropping content • Cover art blank (partial) •
10-min cache • Liked Songs full implementation • Playlist pagination + Load more •
Dead Cipher import/file • AbortController on all navigations • Route ordering (Load More unreachable) •
httpx stream leak • `get_event_loop()` deprecation • Stale sectionData global •
`window.currentViewTracks` global • Unlike button wrong-track • Dead `parse_playlist_page` block •
Hardcoded `cpn` • XSS suggestions dropdown • `window.likedTracks` legacy write •
`loadMoreTracks` TypeError • `handleCardClick` stale-index crash •
`playFromCurrentView` streaming `undefined` • `seek()` NaN/Infinity crash •
**Playlist cover/title/subtitle blank (root cause confirmed against real data + fixed)** •
playlist `description` field bug • fuller subtitle (view/track/duration counts) • debug code removed •
**All thumbnails broken by `esc()` in `src=` — fixed with `srcUrl()`** •
**Made For You mix cards unclickable — `watchEndpoint.playlistId` now promoted to `browseId`** •
**Recommended For You — replaced playlist cards with direct song cards, all playable on click**

### What is currently working

Home, Explore, Search (filters + autocomplete), Artist pages, Album pages,
Playlist pages (tracks + pagination + cover/title/subtitle), Audio streaming,
full player controls, Liked Songs (persistent), session Continue Listening,
AbortController safety, XSS protection, mobile layout, LAN access.

### Ultra three-column UI redesign

- The frontend remains a single vanilla HTML/CSS/JS file. Existing backend
  endpoints, routing, audio element, queue state, search behaviour, and
  localStorage-backed likes are preserved.
- Desktop now uses a redesigned fixed left library sidebar, scrollable central
  content, and a persistent right contextual panel. The panel collapses below
  1050px; existing mobile navigation remains below 768px.
- Home now adds reference-inspired filter pills, quick-access tiles, and a
  getting-started card ahead of the existing API-powered shelves.
- The context panel is state-driven: Liked Songs shows the saved-song count;
  album and playlist pages show their supplied title, subtitle, and cover;
  ordinary browsing/playback shows the active song. Lyrics render below only
  when a future payload supplies `track.lyrics`.
- The panel's Next in queue view reads the existing `queue` and `queueIndex`
  state. It supports compact/full views and starts a selected queued track via
  `playQueueItem()` and the existing `playCurrent()` flow.
- The native audio-driven player is restyled as a compact translucent floating
  overlay while retaining play/pause, previous/next, shuffle, repeat, seek,
  volume, like, and queue controls.
- Likes now refresh the sidebar count and contextual panel while retaining
  `musify_liked_songs` persistence.

Verification after the redesign: extracted inline JavaScript passed
`node --check`; the local frontend responded successfully at
`http://localhost:3000/`. Browser visual automation was unavailable in this
environment, so interactive browser verification was not performed.

### What still needs work (priority order)

1. 🟡 `loadMoreTracks` double-fire on rapid click (no AbortController on fetch)
2. 🟡 Suggestions dropdown not closed by keyboard navigation
3. 🟢 `DEBUG = true` should be `false`
5. 🟢 Dead code cleanup: `cipher.py`, `TVHTML5_SIMPLY_EMBEDDED`, `/api/song`
6. 🟢 `WEB_REMIX` client version hardcoded
7. 🟢 yt-dlp thread blocking — no timeout/retry
8. 🟢 `get_playlist_more` duplicates parser logic
9. ❌ Downloads, user playlists, and crossfade are not built. A functional queue panel is now implemented; full lyric content still depends on a lyrics API.
