from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from scrapers.innertube import InnerTube, YouTubeClient
from scrapers.parsers import parse_home_feed, parse_search_results, parse_artist_page, parse_album_page, parse_playlist_page, parse_song_info, ParserUtils
from typing import Optional, Dict, Any
import uvicorn
import asyncio
import yt_dlp
import time

app = FastAPI()

# Logging middleware to debug request flow
@app.middleware("http")
async def log_requests(request: Request, call_next):
    print(f"Incoming request: {request.method} {request.url}")
    try:
        response = await call_next(request)
        print(f"Response status: {response.status_code}")
        return response
    except Exception as e:
        print(f"Request failed: {e}")
        raise e

# Enable CORS for all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)

innertube = InnerTube()

# --- Simple in-memory TTL cache ---
# Stores {key: {"data": ..., "ts": unix_timestamp}}
# Used for /api/home (9 upstream calls) and /api/explore.
_cache: dict = {}
CACHE_TTL = 600  # seconds (10 minutes)

def cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < CACHE_TTL:
        return entry["data"]
    return None

def cache_set(key: str, data):
    _cache[key] = {"data": data, "ts": time.time()}

@app.get("/api/home")
async def get_home(refresh: bool = False):
    """Aggregated home feed. Cached for 10 minutes. Pass ?refresh=true to bypass."""
    if not refresh:
        cached = cache_get("home")
        if cached is not None:
            print("Serving /api/home from cache")
            return cached
    try:
        # Fetch from multiple sources to increase content density
        browse_ids = ["FEmusic_home", "FEmusic_explore", "FEmusic_new_releases", "FEmusic_charts"]
        browse_tasks = [innertube.browse(bid) for bid in browse_ids]

        # --- Recommended For You ---
        # Search for actual songs across a range of moods/genres so the
        # shelf contains directly-playable song cards (type="song") instead
        # of playlist cards that need a second navigation step.
        # Songs filter param: EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D
        rec_queries = [
            "top hindi hits",
            "best english pop songs",
            "chill lofi songs",
            "bollywood romantic songs",
            "top workout songs",
        ]
        rec_tasks = [
            innertube.search(q, params="EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D")
            for q in rec_queries
        ]

        # return_exceptions=True: if ANY single source fails, skip it
        # instead of wiping the whole home page.
        browse_responses = await asyncio.gather(*browse_tasks, return_exceptions=True)
        mix_responses = await asyncio.gather(*rec_tasks, return_exceptions=True)

        all_sections = []
        sections_by_key = {}  # lower(title) -> section dict, for merging duplicates
        seen_item_keys = set()
        recommended_items = []

        for bid, response in zip(browse_ids, browse_responses):
            if isinstance(response, Exception):
                print(f"Home source '{bid}' failed, skipping: {response}")
                continue
            for s in parse_home_feed(response):
                title = (s.get("title") or "").strip()
                key = title.lower() if title else None
                if key and key in sections_by_key:
                    # MERGE into the existing section instead of dropping it.
                    # Previously, any shelf whose title we'd already seen from
                    # an earlier source (e.g. "New releases" appearing in both
                    # FEmusic_home and FEmusic_new_releases) was discarded
                    # entirely - silently losing all of its playlists/songs.
                    existing = sections_by_key[key]
                    for item in s.get("items", []):
                        item_key = item.get("videoId") or item.get("browseId") or item.get("title")
                        if item_key and item_key not in seen_item_keys:
                            seen_item_keys.add(item_key)
                            existing["items"].append(item)
                else:
                    for item in s.get("items", []):
                        item_key = item.get("videoId") or item.get("browseId") or item.get("title")
                        if item_key:
                            seen_item_keys.add(item_key)
                    all_sections.append(s)
                    if key:
                        sections_by_key[key] = s

        for q, response in zip(rec_queries, mix_responses):
            if isinstance(response, Exception):
                print(f"Rec search '{q}' failed, skipping: {response}")
                continue
            results = parse_search_results(response)
            # Keep only actual songs (type=="song") — skip any playlist/album
            # cards that sneak in as the top result.
            songs = [r for r in results if r.get("type") == "song" and r.get("videoId")]
            for song in songs[:5]:  # up to 5 songs per query
                key = song.get("videoId")
                if key and key not in seen_item_keys:
                    seen_item_keys.add(key)
                    recommended_items.append(song)

        if recommended_items:
            all_sections.insert(0, {"title": "Recommended For You", "items": recommended_items})

        result = {"sections": all_sections}
        cache_set("home", result)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/explore")
async def get_explore(refresh: bool = False):
    """Explore tab feed. Cached for 10 minutes. Pass ?refresh=true to bypass."""
    if not refresh:
        cached = cache_get("explore")
        if cached is not None:
            print("Serving /api/explore from cache")
            return cached
    try:
        response = await innertube.browse("FEmusic_explore")
        sections = parse_home_feed(response)
        result = {"sections": sections}
        cache_set("explore", result)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/search")
async def search(q: str, filter: Optional[str] = None):
    try:
        filters = {
            "songs": "EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D",
            "albums": "EgWKAQIYAWoKEAkQChAFEAMQBA%3D%3D",
            "artists": "EgWKAQIgAWoKEAkQChAFEAMQBA%3D%3D",
            "playlists": "EgWKAQIoAWoKEAkQChAFEAMQBA%3D%3D"
        }
        params = filters.get(filter)
        response = await innertube.search(q, params)
        results = parse_search_results(response)
        return {"results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/suggestions")
async def suggestions(q: str):
    try:
        response = await innertube.get_suggestions(q)
        results = []
        contents = response.get("contents", [{}])[0].get("searchSuggestionsSectionRenderer", {}).get("contents", [])
        for content in contents:
            suggestion = content.get("searchSuggestionRenderer", {}).get("suggestion", {}).get("runs", [])
            if suggestion:
                results.append(ParserUtils.get_text(suggestion))
        return {"suggestions": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/artist/{browseId}")
async def get_artist(browseId: str):
    try:
        response = await innertube.browse(browseId)
        artist_data = parse_artist_page(response)
        return artist_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/album/{browseId}")
async def get_album(browseId: str):
    try:
        response = await innertube.browse(browseId)
        album_data = parse_album_page(response)
        return album_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/playlist/{browseId}/more")
async def get_playlist_more(browseId: str, continuation: str = Query(...)):
    """Fetch the next page of tracks for a playlist using a continuation token."""
    try:
        # Continuation-only browse: YouTube expects EITHER browseId (first page)
        # OR continuation token (subsequent pages), never both together.
        response = await innertube.browse("", continuation=continuation)
        tracks = []
        next_continuation = None

        # Continuation responses are structured differently — content is at top level
        cont_contents = response.get("continuationContents", {})
        shelf = cont_contents.get("musicPlaylistShelfContinuation", {})
        for content in shelf.get("contents", []):
            renderer = content.get("musicResponsiveListItemRenderer")
            if not renderer:
                continue
            flex_cols = renderer.get("flexColumns", [])
            if not flex_cols:
                continue
            title_run = flex_cols[0].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [{}])[0]
            video_id = title_run.get("navigationEndpoint", {}).get("watchEndpoint", {}).get("videoId")
            if not video_id:
                video_id = ParserUtils.get_video_id(renderer)
            artist_sub = ""
            if len(flex_cols) > 1:
                artist_sub = "".join(r.get("text", "") for r in flex_cols[1].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", []))
            thumb_url = ""
            thumb_data = renderer.get("thumbnail", {}).get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [])
            if thumb_data:
                thumb_url = sorted(thumb_data, key=lambda x: x.get("width", 0), reverse=True)[0].get("url", "")
            if title_run.get("text"):
                tracks.append({"title": title_run["text"], "subtitle": artist_sub, "videoId": video_id, "thumbnail": thumb_url, "type": "song"})

        # Extract next continuation token if present
        continuations = shelf.get("continuations", [])
        if continuations:
            next_continuation = continuations[0].get("nextContinuationData", {}).get("continuation")

        return {"tracks": tracks, "continuation": next_continuation}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/playlist/{browseId}")
async def get_playlist(browseId: str):
    try:
        response = await innertube.browse(browseId)
        playlist_data = parse_playlist_page(response)
        return playlist_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stream/{videoId}")
async def get_stream(videoId: str, request: Request):
    def extract_url(video_id):
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'nocheckcertificate': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
            url = info['url']
            mime = info.get('audio_ext', 'm4a')
            # Normalize MIME type
            if mime == 'm4a' or mime == 'none': mime_type = 'audio/mp4'
            elif mime == 'webm': mime_type = 'audio/webm'
            else: mime_type = f'audio/{mime}'
            return url, mime_type

    try:
        print(f"Extracting stream for: {videoId}")
        loop = asyncio.get_running_loop()
        try:
            stream_url, mime_type = await asyncio.wait_for(
                loop.run_in_executor(None, extract_url, videoId),
                timeout=30
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="yt-dlp timed out after 30 seconds")

        range_header = request.headers.get("range", "bytes=0-")
        headers = {
            "Range": range_header,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://music.youtube.com/",
        }

        # Stream audio from YouTube through the proxy.
        # We need the httpx response context to stay open while the browser
        # reads chunks. The pattern below captures the response object and
        # uses an async generator that holds a reference to it, so the
        # connection stays alive for the full stream duration without
        # buffering the whole file into RAM (Bug #1 fix).
        #
        # We enter the context manually here and exit it inside the generator
        # finally block, which is the only safe pattern when the generator
        # outlives the enclosing scope.
        ctx = innertube.stream_client.stream(
            "GET", stream_url, headers=headers, follow_redirects=True
        )
        upstream_res = await ctx.__aenter__()

        if upstream_res.status_code >= 400:
            await ctx.__aexit__(None, None, None)
            print(f"Upstream error {upstream_res.status_code} for {videoId}")
            raise HTTPException(
                status_code=upstream_res.status_code,
                detail=f"YouTube returned {upstream_res.status_code}"
            )

        res_headers = {
            "Content-Type": upstream_res.headers.get("Content-Type", mime_type),
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
        }
        for h in ["Content-Length", "Content-Range", "ETag", "Last-Modified"]:
            if h in upstream_res.headers:
                res_headers[h] = upstream_res.headers[h]

        async def stream_audio():
            """Yield chunks and guarantee the httpx context is closed when
            the generator is exhausted OR abandoned (client disconnect)."""
            try:
                async for chunk in upstream_res.aiter_bytes(chunk_size=1024 * 64):
                    yield chunk
            finally:
                # Always close the upstream connection, even on client disconnect.
                await ctx.__aexit__(None, None, None)

        return StreamingResponse(
            stream_audio(),
            status_code=upstream_res.status_code,
            headers=res_headers
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"Stream error: {e}")
        raise HTTPException(status_code=503, detail=f"Streaming failed: {str(e)}")

@app.get("/api/song/{videoId}")
async def get_song(videoId: str):
    try:
        response = await innertube.get_next(videoId)
        song_data = parse_song_info(response)
        return song_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/debug/search")
async def debug_search(q: str):
    """Temporary debug endpoint - returns raw YTM response for a search query"""
    try:
        response = await innertube.search(q)
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/debug/home")
async def debug_home():
    """Debug endpoint - returns structure of raw YTM home response"""
    try:
        response = await innertube.browse("FEmusic_home")
        contents = response.get("contents", {})
        contents_keys = list(contents.keys()) if isinstance(contents, dict) else []
        # Peek into singleColumn path
        single = contents.get("singleColumnBrowseResultsRenderer", {})
        single_tabs = single.get("tabs", [])
        single_sections = []
        if single_tabs:
            single_sections = list(single_tabs[0].get("tabRenderer", {}).get("content", {}).get("sectionListRenderer", {}).get("contents", [{}])[0].keys()) if single_tabs[0].get("tabRenderer", {}).get("content", {}).get("sectionListRenderer", {}).get("contents") else []
        # Peek into twoColumn path
        two = contents.get("twoColumnBrowseResultsRenderer", {})
        two_keys = list(two.keys()) if two else []
        return {
            "top_level_keys": list(response.keys()),
            "contents_keys": contents_keys,
            "has_single": bool(single),
            "single_tab_count": len(single_tabs),
            "single_first_section_keys": single_sections,
            "has_two": bool(two),
            "two_keys": two_keys,
            "error": None
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "trace": traceback.format_exc()}

@app.get("/api/debug/home/items")
async def debug_home_items():
    """Dumps the raw keys of the first item inside the first carousel so we
    can see exactly what renderer key YouTube is using for home feed items."""
    try:
        response = await innertube.browse("FEmusic_home")
        tabs = response.get("contents", {}).get("singleColumnBrowseResultsRenderer", {}).get("tabs", [])
        tab_content = tabs[0].get("tabRenderer", {}).get("content", {}) if tabs else {}
        sections = tab_content.get("sectionListRenderer", {}).get("contents", [])
        result = []
        for i, section in enumerate(sections[:3]):  # first 3 shelves
            carousel = section.get("musicCarouselShelfRenderer") or \
                       section.get("musicShelfRenderer") or \
                       section.get("musicImmersiveCarouselShelfRenderer")
            if not carousel:
                result.append({"shelf_index": i, "shelf_key": list(section.keys()), "items": []})
                continue
            items_raw = carousel.get("contents", [])
            first_items = []
            for item in items_raw[:2]:  # first 2 items per shelf
                first_items.append({
                    "item_keys": list(item.keys()),
                    "inner_keys": list(list(item.values())[0].keys()) if item else []
                })
            # also grab header keys
            header = carousel.get("header", {})
            result.append({
                "shelf_index": i,
                "shelf_key": list(section.keys())[0],
                "item_count": len(items_raw),
                "header_keys": list(header.keys()),
                "first_items": first_items
            })
        return {"sections": result}
    except Exception as e:
        import traceback
        return {"error": str(e), "trace": traceback.format_exc()}

@app.get("/api/debug/home/shape")
async def debug_home_shape():
    """Returns only the top-level keys and the first two levels of structure
    so we can see where YouTube moved the content without loading megabytes."""
    try:
        response = await innertube.browse("FEmusic_home")
        def shape(obj, depth=0):
            if depth > 2: return "..."
            if isinstance(obj, dict):
                return {k: shape(v, depth+1) for k, v in list(obj.items())[:8]}
            if isinstance(obj, list):
                return [shape(obj[0], depth+1), f"...+{len(obj)-1}"] if obj else []
            return type(obj).__name__
        return {
            "top_level_keys": list(response.keys()),
            "shape": shape(response),
            "has_singleColumnBrowseResultsRenderer": "singleColumnBrowseResultsRenderer" in str(response.get("contents", {}))[:200],
            "contents_keys": list(response.get("contents", {}).keys()) if isinstance(response.get("contents"), dict) else "not a dict",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/debug/stream/{videoId}")
async def debug_stream(videoId: str):
    """Debug endpoint - shows raw player response to diagnose stream issues"""
    try:
        response = await innertube.player(videoId, YouTubeClient.ANDROID_MUSIC)
        streaming_data = response.get("streamingData", {})
        formats = streaming_data.get("adaptiveFormats", []) + streaming_data.get("formats", [])
        audio_formats = []
        for fmt in formats:
            if "audio" in fmt.get("mimeType", ""):
                audio_formats.append({
                    "mimeType": fmt.get("mimeType"),
                    "bitrate": fmt.get("bitrate"),
                    "hasUrl": "url" in fmt,
                    "hasCipher": "signatureCipher" in fmt,
                    "urlPreview": fmt.get("url", "")[:80] if "url" in fmt else None
                })
        return {
            "videoId": videoId,
            "status": response.get("playabilityStatus", {}).get("status"),
            "reason": response.get("playabilityStatus", {}).get("reason"),
            "audioFormats": audio_formats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
