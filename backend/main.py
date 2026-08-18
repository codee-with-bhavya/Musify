from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from scrapers.innertube import InnerTube, YouTubeClient
from scrapers.parsers import parse_home_feed, parse_search_results, parse_artist_page, parse_album_page, parse_playlist_page, parse_song_info, ParserUtils
from typing import Optional, Dict, Any
import uvicorn
import asyncio
import time
import json
import yt_dlp
from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=4)

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

# --- Stream URL cache (5 minute TTL) ---
# Avoids re-running yt-dlp for the same song played repeatedly.
_stream_cache: dict = {}
STREAM_CACHE_TTL = 300  # seconds

def stream_cache_get(video_id: str):
    entry = _stream_cache.get(video_id)
    if not entry:
        return None
    if (time.time() - entry["ts"]) >= STREAM_CACHE_TTL:
        del _stream_cache[video_id]
        return None
    return entry["url"], entry["ext"]

def stream_cache_set(video_id: str, url: str, ext: str):
    _stream_cache[video_id] = {"url": url, "ext": ext, "ts": time.time()}

# --- Simple in-memory TTL cache ---
# Stores {key: {"data": ..., "ts": unix_timestamp}}
# Used for /api/home (9 upstream calls) and /api/explore.
_cache: dict = {}
CACHE_TTL = 600  # seconds (10 minutes)

def cache_get(key: str):
    entry = _cache.get(key)
    if not entry:
        return None
    if (time.time() - entry["ts"]) >= CACHE_TTL:
        del _cache[key]   # evict stale entry
        return None
    return entry["data"]

def cache_set(key: str, data):
    _cache[key] = {"data": data, "ts": time.time()}

@app.get("/api/home")
async def get_home(refresh: bool = False):
    """Aggregated home feed. Returns JSON array.
    On cache hit, returns instantly. On miss, fetches and caches."""

    if not refresh:
        cached = cache_get("home")
        if cached is not None:
            print("Serving /api/home from cache")
            return {"sections": cached}

    seen_item_keys = set()
    accumulated_sections = []
    recommended_items = []

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

    for completed_task in asyncio.as_completed(tasks):
        try:
            response = await completed_task
            if not isinstance(response, dict):
                continue
            contents = response.get("contents", {})
            if "tabbedSearchResultsRenderer" in contents:
                results = parse_search_results(response)
                songs = [r for r in results if r.get("type") == "song" and r.get("videoId")]
                for song in songs[:5]:
                    key = song.get("videoId")
                    if key and key not in seen_item_keys:
                        seen_item_keys.add(key)
                        recommended_items.append(song)
            else:
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
        except Exception as e:
            print(f"Parallel task failed: {e}")
            continue

    if recommended_items:
        rec_section = {"title": "Recommended For You", "items": recommended_items}
        accumulated_sections.insert(0, rec_section)

    if accumulated_sections:
        cache_set("home", accumulated_sections)

    return {"sections": accumulated_sections}

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
            # Use ParserUtils.get_thumbnail() so continuation thumbnails go through
            # the same CDN-resizing logic (_resolve_url) as first-page tracks.
            # Previously used an inline sort that bypassed the =w512-h512-l90-rj
            # suffix injection and the "//" → "https:" prefix fix.
            thumb_url = ParserUtils.get_thumbnail(renderer.get("thumbnail", {}))
            if not thumb_url and video_id:
                thumb_url = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
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
    try:
        print(f"Extracting stream for: {videoId}")

        # Use InnerTube player first for speed and direct URLs (Pass 15)
        response = await innertube.player(videoId)

        streaming_data = response.get("streamingData", {})
        formats = (streaming_data.get("adaptiveFormats", []) +
                  streaming_data.get("formats", []))

        # Filter for audio formats with a direct URL
        audio_formats = [f for f in formats if "url" in f and "audio" in f.get("mimeType", "")]

        selected_format = None
        if audio_formats:
            # Prefer Opus (251) then AAC (140)
            selected_format = next((f for f in audio_formats if f.get("itag") == 251), None)
            if not selected_format:
                selected_format = next((f for f in audio_formats if f.get("itag") == 140), None)
            if not selected_format:
                selected_format = audio_formats[0]

        if selected_format and "url" in selected_format:
            stream_url = selected_format["url"]
            mime_type = selected_format["mimeType"]
            print(f"Direct stream found via InnerTube for {videoId}")
        else:
            # Fallback to yt-dlp if direct URL not found (handles signatureCipher)
            print(f"No direct URL via InnerTube for {videoId}, trying yt-dlp with Node.js...")

            def _extract(vid_id: str):
                # Extremely robust extraction with Node.js and client fallback
                ydl_opts = {
                    "format": "bestaudio/best",
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "javascript_runtimes": ["node:C:\\Program Files\\nodejs\\node.exe"],
                    "extractor_args": {
                        "youtube": {
                            "player_client": ["android", "web", "ios", "mweb"],
                            "skip": ["hls", "dash"]
                        }
                    }
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    # Retry with different clients if first attempt fails
                    try:
                        info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid_id}", download=False)
                    except Exception:
                        # Final attempt: Very high success client
                        ydl.params['extractor_args']['youtube']['player_client'] = ['tv']
                        info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid_id}", download=False)

                    return info["url"], info.get("ext", "webm")

            loop = asyncio.get_event_loop()
            stream_url, ext = await loop.run_in_executor(_executor, _extract, videoId)
            mime_type = "audio/webm" if ext == "webm" else "audio/mp4"

        range_header = request.headers.get("range", "bytes=0-")
        headers = {
            "Range": range_header,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://music.youtube.com/",
        }

        ctx = innertube.stream_client.stream(
            "GET", stream_url, headers=headers
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
            try:
                async for chunk in upstream_res.aiter_bytes(chunk_size=1024 * 64):
                    yield chunk
            finally:
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

@app.get("/api/prefetch/{videoId}")
async def prefetch_stream(videoId: str):
    """Pre-extract and cache the stream URL for a video in the background.
    Called by the frontend after a song starts playing to warm up the next track."""
    if stream_cache_get(videoId):
        return {"status": "cached"}
    try:
        def _extract(vid_id: str):
            ydl_opts = {
                "format": "bestaudio/best",
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "javascript_runtimes": ["node:C:\\Program Files\\nodejs\\node.exe"],
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android", "web", "ios", "mweb"],
                        "skip": ["hls", "dash"]
                    }
                }
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid_id}", download=False)
                except Exception:
                    ydl.params['extractor_args']['youtube']['player_client'] = ['tv']
                    info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid_id}", download=False)
                return info["url"], info.get("ext", "webm")
            ydl_opts = {
                "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio",
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid_id}", download=False)
                formats = info.get("formats", [])
                audio = [f for f in formats if f.get("acodec") != "none" and f.get("vcodec") == "none" and f.get("url")]
                if not audio:
                    audio = [f for f in formats if f.get("acodec") != "none" and f.get("url")]
                if not audio:
                    return None, None
                best = sorted(audio, key=lambda f: (
                    0 if "opus" in (f.get("acodec") or "") else 1 if f.get("ext") == "m4a" else 2,
                    -(f.get("abr") or f.get("tbr") or 0)
                ))[0]
                return best["url"], best.get("ext", "webm")

        loop = asyncio.get_event_loop()
        url, ext = await loop.run_in_executor(_executor, _extract, videoId)
        if url:
            stream_cache_set(videoId, url, ext)
            print(f"Prefetched stream for {videoId}")
            return {"status": "ok"}
        return {"status": "failed"}
    except Exception as e:
        print(f"Prefetch failed for {videoId}: {e}")
        return {"status": "error"}


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
