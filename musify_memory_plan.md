# Musify — Memory Reduction Implementation Plan

> Target: Cut peak RAM from ~4–6 MB per home load down to ~400–600 KB steady state.  
> All fixes are self-contained. Each pass can be done and tested independently.

---

## The Problem in Numbers

| Source | Current Cost | After Fix |
|---|---|---|
| 9 concurrent YouTube JSON responses held simultaneously | ~4 MB peak | ~500 KB (3 at a time) |
| Recursive thumbnail scanner on every item | O(n²) allocations | O(1) fast-path |
| `homeSectionData` / `detailSectionData` never cleared | grows unbounded per session | zeroed on navigation |
| `seen_item_keys` set across all tasks | small but never freed | scoped per-request |
| `DEBUG=true` console floods | CPU waste on every audio tick | env-gated |
| `/api/home` caching disabled | 4 MB spike on every visit | one spike per 10 min |

---

## Pass 1 — Backend: Cap Concurrent Requests with a Semaphore

**File:** `backend/main.py`  
**Effort:** 10 minutes  
**Impact:** Peak RAM drops from ~4 MB to ~1 MB per home load

### Why
`generate_home()` fires all 9 `asyncio.create_task()` calls at once. Python holds the full JSON response of every completed task in memory while `as_completed` is still iterating. At 300–500 KB per response × 9 = up to 4.5 MB peak.

A semaphore of 3 means at most 3 responses sit in memory at any time.

### Exact Change

Find this block in `generate_home()`:

```python
# Combine all tasks
tasks = []
for bid in browse_ids:
    tasks.append(asyncio.create_task(innertube.browse(bid)))
for q in rec_queries:
    tasks.append(asyncio.create_task(innertube.search(q, params="EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D")))

# Process as they complete
for completed_task in asyncio.as_completed(tasks):
```

Replace with:

```python
# Semaphore: max 3 YouTube responses in memory at once
_sem = asyncio.Semaphore(3)

async def _limited(coro):
    async with _sem:
        return await coro

tasks = []
for bid in browse_ids:
    tasks.append(asyncio.create_task(_limited(innertube.browse(bid))))
for q in rec_queries:
    tasks.append(asyncio.create_task(
        _limited(innertube.search(q, params="EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D"))
    ))

for completed_task in asyncio.as_completed(tasks):
```

---

## Pass 2 — Backend: Re-enable Caching on the NDJSON Stream

**File:** `backend/main.py`  
**Effort:** 25 minutes  
**Impact:** The 4-task + 5-search spike happens once per 10 minutes instead of every visit

### Why
The comment says caching is "temporarily disabled for streaming mode". But you can cache and stream together — cache the assembled section list, and on a cache hit stream each cached section out as NDJSON instead of hitting YouTube. This means returning users get instant first paint with zero memory spike.

### Exact Change

Replace the entire `get_home` function with this:

```python
@app.get("/api/home")
async def get_home(refresh: bool = False):
    """Aggregated home feed. Streams as NDJSON.
    On cache hit, streams cached sections directly — zero upstream calls."""

    # --- Cache hit: stream stored sections without touching YouTube ---
    if not refresh:
        cached = cache_get("home")
        if cached is not None:
            print("Serving /api/home from cache (NDJSON)")

            async def stream_cached():
                for section in cached:
                    yield json.dumps(section) + "\n"
                    await asyncio.sleep(0)  # yield to event loop between chunks

            return StreamingResponse(stream_cached(), media_type="application/x-ndjson")

    # --- Cache miss: fetch, stream, and accumulate for caching ---
    async def generate_and_cache():
        seen_item_keys = set()
        accumulated_sections = []

        _sem = asyncio.Semaphore(3)

        async def _limited(coro):
            async with _sem:
                return await coro

        browse_ids = [
            "FEmusic_home", "FEmusic_explore",
            "FEmusic_new_releases", "FEmusic_charts"
        ]
        rec_queries = [
            "top hindi hits",
            "best english pop songs",
            "chill lofi songs",
            "bollywood romantic songs",
            "top workout songs",
        ]

        tasks = (
            [asyncio.create_task(_limited(innertube.browse(bid))) for bid in browse_ids]
            + [asyncio.create_task(
                _limited(innertube.search(q, params="EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D"))
               ) for q in rec_queries]
        )

        recommended_items = []

        for completed_task in asyncio.as_completed(tasks):
            try:
                response = await completed_task
                if not isinstance(response, dict):
                    continue

                contents = response.get("contents", {})

                if "tabbedSearchResultsRenderer" in contents:
                    # Search response — collect recommended songs
                    results = parse_search_results(response)
                    songs = [r for r in results if r.get("type") == "song" and r.get("videoId")]
                    for song in songs[:5]:
                        key = song.get("videoId")
                        if key and key not in seen_item_keys:
                            seen_item_keys.add(key)
                            recommended_items.append(song)
                else:
                    # Browse response — emit sections as they arrive
                    for s in parse_home_feed(response):
                        unique_items = []
                        for item in s.get("items", []):
                            item_key = (
                                item.get("videoId") or
                                item.get("browseId") or
                                item.get("title")
                            )
                            if item_key and item_key not in seen_item_keys:
                                seen_item_keys.add(item_key)
                                unique_items.append(item)
                        if unique_items:
                            section = {"title": (s.get("title") or "").strip(), "items": unique_items}
                            accumulated_sections.append(section)
                            yield json.dumps(section) + "\n"
                            await asyncio.sleep(0.01)

            except Exception as e:
                print(f"Parallel task failed: {e}")
                continue

        # Emit recommended section last (after all search tasks complete)
        if recommended_items:
            rec_section = {"title": "Recommended For You", "items": recommended_items}
            accumulated_sections.insert(0, rec_section)
            yield json.dumps(rec_section) + "\n"

        # Store in cache so the next visit streams from memory
        if accumulated_sections:
            cache_set("home", accumulated_sections)

    return StreamingResponse(generate_and_cache(), media_type="application/x-ndjson")
```

Also fix `cache_get` to evict expired entries so they don't sit in RAM forever:

```python
def cache_get(key: str):
    entry = _cache.get(key)
    if not entry:
        return None
    if (time.time() - entry["ts"]) >= CACHE_TTL:
        del _cache[key]   # ← evict stale entry
        return None
    return entry["data"]
```

---

## Pass 3 — Backend: Fast-Path the Thumbnail Scanner

**File:** `backend/scrapers/parsers.py`  
**Effort:** 15 minutes  
**Impact:** Eliminates O(n²) recursive allocation on every home/search parse

### Why
The new `get_thumbnail()` always runs its `find_thumbnails()` recursive DFS even when standard key paths would have worked. For a home feed with 200 items, each with a nested renderer dict, this creates thousands of intermediate lists.

### Exact Change

Replace the entire `get_thumbnail` static method with this two-path version:

```python
@staticmethod
def get_thumbnail(data: Any) -> str:
    """Extract the largest thumbnail URL. Fast-path first, recursive scan as fallback."""
    if not data:
        return ""

    # ── Fast path (covers ~95 % of responses) ──────────────────────────────
    if isinstance(data, dict):
        renderer = (
            data.get("musicThumbnailRenderer") or
            data.get("croppedSquareThumbnailRenderer") or
            data
        )
        if isinstance(renderer, dict):
            thumb_obj = renderer.get("thumbnail") or renderer
            if isinstance(thumb_obj, dict):
                thumbnails = thumb_obj.get("thumbnails", [])
            elif isinstance(thumb_obj, list):
                thumbnails = thumb_obj
            else:
                thumbnails = []
            if thumbnails:
                return ParserUtils._resolve_url(thumbnails)

    elif isinstance(data, list):
        if data and isinstance(data[0], dict) and "url" in data[0]:
            return ParserUtils._resolve_url(data)

    # ── Slow path: recursive scan only when fast path misses ───────────────
    def _find(obj):
        if isinstance(obj, list):
            if obj and isinstance(obj[0], dict) and "url" in obj[0]:
                return obj
            for item in obj:
                r = _find(item)
                if r: return r
        elif isinstance(obj, dict):
            if "thumbnails" in obj and isinstance(obj["thumbnails"], list):
                return obj["thumbnails"]
            for v in obj.values():
                r = _find(v)
                if r: return r
        return None

    thumbnails = _find(data) or []
    return ParserUtils._resolve_url(thumbnails) if thumbnails else ""

@staticmethod
def _resolve_url(thumbnails: list) -> str:
    """Pick highest-res URL from a thumbnails list and apply CDN resizing."""
    try:
        url = sorted(thumbnails, key=lambda x: x.get("width", 0), reverse=True)[0].get("url", "")
        if not url: return ""
        if url.startswith("//"): url = "https:" + url
        if "googleusercontent.com" in url or "ggpht.com" in url:
            url = url.split("=")[0] + "=w512-h512-l90-rj"
        elif "i.ytimg.com" in url and "hqdefault" in url:
            url = url.replace("hqdefault", "maxresdefault")
        return url
    except (IndexError, TypeError, KeyError):
        return ""
```

Also update `get_header_thumbnail` to call the same method (it already does via `ParserUtils.get_thumbnail` — no change needed there).

---

## Pass 4 — Backend: Fix the Double `except` Block

**File:** `backend/main.py`  
**Effort:** 2 minutes  
**Impact:** Prevents silent exception swallowing in `/api/stream`

Find and delete the second duplicate except pair at the bottom of `get_stream`. The function currently ends with:

```python
        return StreamingResponse(
            stream_audio(),
            status_code=upstream_res.status_code,
            headers=res_headers
        )

    except HTTPException:        # ← KEEP this one
        raise
    except Exception as e:
        print(f"Stream error: {e}")
        raise HTTPException(status_code=503, detail=f"Streaming failed: {str(e)}")

    except HTTPException:        # ← DELETE from here
        raise
    except Exception as e:
        print(f"Stream error: {e}")
        raise HTTPException(status_code=503, detail=f"Streaming failed: {str(e)}")
```

Delete the second block entirely.

---

## Pass 5 — Frontend: Zero State Arrays on Navigation

**File:** `frontend/index.html`  
**Effort:** 5 minutes  
**Impact:** Prevents large track arrays accumulating across a full session

Find the `showView` function and add zeroing at the top:

```javascript
function showView(id) {
    // Free memory from views we're leaving
    if (id !== 'detail-view') {
        currentViewTracks = [];
        detailSectionData = [];
        currentDetailBrowseId = null;
        currentContinuation = null;
    }
    if (id !== 'home-view')    homeSectionData = [];
    if (id !== 'explore-view') exploreSectionData = [];

    ['home-view', 'explore-view', 'search-view', 'library-view', 'detail-view'].forEach(v => {
        const el = document.getElementById(v);
        if (el) el.style.display = (v === id) ? 'block' : 'none';
    });
    // ... rest of showView unchanged
}
```

---

## Pass 6 — Frontend: Fix the NDJSON Reader Abort Leak

**File:** `frontend/index.html`  
**Effort:** 10 minutes  
**Impact:** Stops the browser from pumping network data into RAM after navigation

Find the `while (true)` reader loop inside `loadHome()` and wrap it:

```javascript
const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = '';

try {
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        // Navigation happened — stop reading from the network immediately
        if (version !== navigationVersion) {
            reader.cancel('navigated away');
            return;
        }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
            if (!line.trim()) continue;
            try {
                const section = JSON.parse(line);
                // ... existing appendShelf logic unchanged
            } catch (parseErr) {
                console.warn('NDJSON parse error:', parseErr);
            }
        }
    }
} catch (e) {
    if (e.name === 'AbortError') return;   // clean navigation abort
    throw e;                               // real error — let outer catch handle it
}
```

---

## Pass 7 — Frontend: Gate DEBUG Behind Hostname Check

**File:** `frontend/index.html`  
**Effort:** 1 minute  
**Impact:** Stops `console.log` firing on every audio tick in production

Find:
```javascript
const DEBUG = true;
```

Replace with:
```javascript
const DEBUG = ['localhost', '127.0.0.1'].includes(window.location.hostname);
```

---

## Pass 8 — Frontend: Cap `queue` Array Size

**File:** `frontend/index.html`  
**Effort:** 5 minutes  
**Impact:** Prevents a 500-track playlist from living in RAM all session

The `queue` array is loaded wholesale from every playlist. Add a cap inside `playFromCurrentView` and `playFromShelf`:

```javascript
const MAX_QUEUE = 150;

function playFromCurrentView(idx) {
    const allTracks = currentViewTracks || [];
    const clickedTrack = allTracks[idx];
    // Only keep MAX_QUEUE tracks, centered around the clicked index
    const start = Math.max(0, idx - 20);
    const sliced = allTracks.slice(start, start + MAX_QUEUE);
    queue = sliced.filter(t => t.videoId);
    const remappedIdx = idx - start;
    queueIndex = queue.findIndex(t => t.videoId === (clickedTrack?.videoId));
    if (queue.length === 0 || queueIndex === -1) queueIndex = 0;
    playbackFailures.clear();
    playCurrent();
}
```

Apply the same slice logic in `playFromShelf` for shelf-based queues.

---

## Do These in Order

| Pass | File | Time | RAM Saved |
|---|---|---|---|
| 1 | `main.py` | 10 min | ~3 MB peak eliminated |
| 2 | `main.py` | 25 min | ~4 MB per-visit spike → once per 10 min |
| 3 | `parsers.py` | 15 min | O(n²) → O(n) per page parse |
| 4 | `main.py` | 2 min | Bug fix (exception leak) |
| 5 | `index.html` | 5 min | Session accumulation cleared |
| 6 | `index.html` | 10 min | Network buffer leak on navigation |
| 7 | `index.html` | 1 min | CPU waste on every audio tick |
| 8 | `index.html` | 5 min | Large playlist queue capped at 150 |

**Total time: ~73 minutes.  
Expected peak memory after all passes: ~400–600 KB steady state vs ~4–6 MB today.**

---

## What NOT to Do

- **Do not** switch to Redis or an external cache — the in-memory dict is fine for a single-user local app. The TTL eviction fix in Pass 2 is enough.
- **Do not** reduce the number of YouTube browse IDs below 3 — you'll get a visibly thin home feed.
- **Do not** use `response.json()` streaming chunk-by-chunk from YouTube — their API always returns full JSON blobs, not NDJSON. The streaming architecture you built is correct; it just needs the reader abort and semaphore fixes above.
