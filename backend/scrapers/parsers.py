from typing import List, Dict, Any, Optional
import traceback

class ParserUtils:
    @staticmethod
    def get_text(runs) -> str:
        if not runs: return ""
        if isinstance(runs, str): return runs
        return "".join([run.get("text", "") for run in runs if isinstance(run, dict)])

    @staticmethod
    def get_thumbnail(thumbnails: List[Dict[str, Any]]) -> str:
        if not thumbnails: return ""
        sorted_thumbs = sorted(thumbnails, key=lambda x: x.get("width", 0), reverse=True)
        return sorted_thumbs[0].get("url", "")

    @staticmethod
    def get_header_thumbnail(thumbnail_obj: Dict) -> str:
        """Header thumbnails (album/playlist detail headers) can appear under
        different renderer keys depending on the page - musicThumbnailRenderer
        is the common case, croppedSquareThumbnailRenderer shows up too.
        Checking only one of them is why some playlist/album covers render
        blank while everything else on the page loads fine."""
        if not thumbnail_obj:
            return ""
        for key in ("musicThumbnailRenderer", "croppedSquareThumbnailRenderer"):
            renderer = thumbnail_obj.get(key)
            if renderer:
                url = ParserUtils.get_thumbnail(renderer.get("thumbnail", {}).get("thumbnails", []))
                if url:
                    return url
        return ""

    @staticmethod
    def get_nav_endpoint(renderer: Dict) -> Dict:
        """Try all known navigation endpoint locations.
        YouTube uses 'thumbnailOverlay' (not 'overlay') in musicTwoRowItemRenderer
        as confirmed by debug/home/items response."""
        return (
            renderer.get("navigationEndpoint") or
            renderer.get("thumbnailOverlay", {}).get("musicItemThumbnailOverlayRenderer", {}).get("content", {}).get("musicPlayButtonRenderer", {}).get("playNavigationEndpoint") or
            renderer.get("overlay", {}).get("musicItemThumbnailOverlayRenderer", {}).get("content", {}).get("musicPlayButtonRenderer", {}).get("playNavigationEndpoint") or
            {}
        )

    @staticmethod
    def get_video_id(renderer: Dict) -> str:
        """Extract videoId from all known locations in a renderer.
        YouTube uses 'thumbnailOverlay' (not 'overlay') in musicTwoRowItemRenderer
        as confirmed by debug/home/items response. Check both keys for safety."""
        # thumbnailOverlay path — used by musicTwoRowItemRenderer (home feed)
        overlay_ep = renderer.get("thumbnailOverlay", {}).get("musicItemThumbnailOverlayRenderer", {}).get("content", {}).get("musicPlayButtonRenderer", {}).get("playNavigationEndpoint", {})
        vid = overlay_ep.get("watchEndpoint", {}).get("videoId")
        if vid: return vid
        # overlay path — older/other renderers, kept as fallback
        overlay_ep = renderer.get("overlay", {}).get("musicItemThumbnailOverlayRenderer", {}).get("content", {}).get("musicPlayButtonRenderer", {}).get("playNavigationEndpoint", {})
        vid = overlay_ep.get("watchEndpoint", {}).get("videoId")
        if vid: return vid
        # navigationEndpoint — browseEndpoint for albums/playlists, watchEndpoint for songs
        nav = renderer.get("navigationEndpoint", {})
        vid = nav.get("watchEndpoint", {}).get("videoId")
        if vid: return vid
        return None

    @staticmethod
    def get_browse_id(renderer: Dict) -> str:
        nav = renderer.get("navigationEndpoint", {})
        return nav.get("browseEndpoint", {}).get("browseId")

    @staticmethod
    def guess_type(video_id, browse_id, subtitle="") -> str:
        if browse_id:
            if browse_id.startswith("UC"): return "artist"
            if browse_id.startswith("MPRE") or browse_id.startswith("FEmusic_album"): return "album"
            if browse_id.startswith("VLRD") or browse_id.startswith("RDCLAK") or browse_id.startswith("VL"): return "playlist"
        if video_id: return "song"
        lower = subtitle.lower()
        if "artist" in lower: return "artist"
        if "album" in lower: return "album"
        if "playlist" in lower: return "playlist"
        return "song"

def parse_home_feed(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    sections = []
    try:
        tabs = response.get("contents", {}).get("singleColumnBrowseResultsRenderer", {}).get("tabs", [])
        tab_content = tabs[0].get("tabRenderer", {}).get("content", {}) if tabs else {}
        contents = tab_content.get("sectionListRenderer", {}).get("contents", [])

        for section in contents:
            carousel = section.get("musicCarouselShelfRenderer") or \
                       section.get("musicShelfRenderer") or \
                       section.get("musicImmersiveCarouselShelfRenderer")
            if not carousel:
                continue

            # Get title from header (carousel) or directly (shelf)
            header = carousel.get("header", {})
            header_renderer = (
                header.get("musicCarouselShelfBasicHeaderRenderer") or
                header.get("musicImmersiveCarouselShelfBasicHeaderRenderer") or
                header.get("musicShelfBasicHeaderRenderer") or
                {}
            )
            title = ParserUtils.get_text(header_renderer.get("title", {}).get("runs", [])) or \
                    ParserUtils.get_text(carousel.get("title", {}).get("runs", [])) or \
                    "Recommended"

            items = []
            contents_list = carousel.get("contents", [])
            # Some shelves have items directly, some wrapped in 'musicResponsiveListItemRenderer'
            for item in contents_list:
                renderer = item.get("musicTwoRowItemRenderer") or \
                           item.get("musicResponsiveListItemRenderer") or \
                           item.get("musicItemRenderer")

                if not renderer:
                    # Fallback: maybe the item IS the renderer
                    if "title" in item and ("navigationEndpoint" in item or "videoId" in item):
                        renderer = item
                    else:
                        continue

                # Common fields extraction
                video_id = ParserUtils.get_video_id(renderer)
                browse_id = ParserUtils.get_browse_id(renderer)

                # Title extraction
                title_text = ""
                if "title" in renderer:
                    title_text = ParserUtils.get_text(renderer["title"].get("runs", []))

                # Subtitle extraction
                subtitle_text = ""
                if "subtitle" in renderer:
                    subtitle_text = ParserUtils.get_text(renderer["subtitle"].get("runs", []))
                elif "description" in renderer:
                    subtitle_text = ParserUtils.get_text(renderer["description"].get("runs", []))

                # Thumbnail extraction
                thumb_renderer = renderer.get("thumbnailRenderer") or renderer.get("thumbnail")
                thumbnails = []
                if thumb_renderer:
                    if "musicThumbnailRenderer" in thumb_renderer:
                        thumbnails = thumb_renderer["musicThumbnailRenderer"].get("thumbnail", {}).get("thumbnails", [])
                    else:
                        thumbnails = thumb_renderer.get("thumbnails", [])

                thumbnail = ParserUtils.get_thumbnail(thumbnails)

                if not title_text and not video_id: continue

                items.append({
                    "title": title_text,
                    "subtitle": subtitle_text,
                    "videoId": video_id,
                    "browseId": browse_id,
                    "thumbnail": thumbnail,
                    "type": ParserUtils.guess_type(video_id, browse_id, subtitle_text)
                })

            if items:
                sections.append({"title": title, "items": items})

    except Exception as e:
        print(f"Error parsing home feed: {e}")
        traceback.print_exc()
    return sections

def parse_search_results(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    results = []
    try:
        tabs = response.get("contents", {}).get("tabbedSearchResultsRenderer", {}).get("tabs", [])
        tab_content = tabs[0].get("tabRenderer", {}).get("content", {}) if tabs else {}
        contents = tab_content.get("sectionListRenderer", {}).get("contents", [])

        for section in contents:
            # Handle top result card (musicCardShelfRenderer)
            card = section.get("musicCardShelfRenderer")
            if card:
                # Parse the top result card itself
                header_runs = card.get("title", {}).get("runs", [])
                title_run = header_runs[0] if header_runs else {}
                nav = title_run.get("navigationEndpoint", {})
                video_id = nav.get("watchEndpoint", {}).get("videoId")
                browse_id = nav.get("browseEndpoint", {}).get("browseId")
                # Mix/radio playlists (Daily Mix, Chill Mix, etc.) use a watchEndpoint
                # with a playlistId instead of a browseEndpoint, so browse_id comes
                # back None and guess_type falls through to 'song'. Detect this here:
                # if the watchEndpoint carries a playlistId, promote browse_id to
                # 'VL{playlistId}' so it routes to the playlist page on click.
                if not browse_id:
                    playlist_id = nav.get("watchEndpoint", {}).get("playlistId", "")
                    if playlist_id:
                        browse_id = playlist_id if playlist_id.startswith("VL") else f"VL{playlist_id}"
                        video_id = None  # treat as playlist, not a song
                subtitle = ParserUtils.get_text(card.get("subtitle", {}).get("runs", []))
                thumbnail = ParserUtils.get_thumbnail(
                    card.get("thumbnail", {}).get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [])
                )
                if not video_id:
                    video_id = ParserUtils.get_video_id(card)
                if title_run.get("text"):
                    results.append({
                        "title": title_run.get("text", ""),
                        "subtitle": subtitle,
                        "videoId": video_id,
                        "browseId": browse_id,
                        "thumbnail": thumbnail,
                        "type": ParserUtils.guess_type(video_id, browse_id, subtitle)
                    })
                # Also parse nested items inside the card
                for sub_item in card.get("contents", []):
                    r = sub_item.get("musicResponsiveListItemRenderer")
                    if not r:
                        continue
                    flex = r.get("flexColumns", [])
                    col0_runs = flex[0].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", []) if flex else []
                    col1_runs = flex[1].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", []) if len(flex) > 1 else []
                    t_run = col0_runs[0] if col0_runs else {}
                    t_text = t_run.get("text", "")
                    t_nav = t_run.get("navigationEndpoint", {})
                    t_vid = t_nav.get("watchEndpoint", {}).get("videoId") or ParserUtils.get_video_id(r)
                    t_bid = t_nav.get("browseEndpoint", {}).get("browseId")
                    t_sub = ParserUtils.get_text(col1_runs)
                    t_thumb = ParserUtils.get_thumbnail(
                        r.get("thumbnail", {}).get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [])
                    )
                    if t_text:
                        results.append({
                            "title": t_text,
                            "subtitle": t_sub,
                            "videoId": t_vid,
                            "browseId": t_bid,
                            "thumbnail": t_thumb,
                            "type": ParserUtils.guess_type(t_vid, t_bid, t_sub)
                        })
                continue

            shelf = section.get("musicShelfRenderer")
            if not shelf:
                continue
            for item in shelf.get("contents", []):
                renderer = item.get("musicResponsiveListItemRenderer")
                if not renderer:
                    continue

                flex = renderer.get("flexColumns", [])
                col0_runs = flex[0].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", []) if flex else []
                col1_runs = flex[1].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", []) if len(flex) > 1 else []

                title_run = col0_runs[0] if col0_runs else {}
                title_text = title_run.get("text", "")
                subtitle = ParserUtils.get_text(col1_runs)

                # videoId: check title run nav first, then overlay
                nav = title_run.get("navigationEndpoint", {})
                video_id = nav.get("watchEndpoint", {}).get("videoId")
                browse_id = nav.get("browseEndpoint", {}).get("browseId")

                # Also check overlay play button for videoId
                if not video_id:
                    video_id = ParserUtils.get_video_id(renderer)

                # Thumbnail
                thumbnail = ParserUtils.get_thumbnail(
                    renderer.get("thumbnail", {}).get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [])
                )

                # Detect type from subtitle text tokens
                item_type = ParserUtils.guess_type(video_id, browse_id, subtitle)

                if title_text:
                    results.append({
                        "title": title_text,
                        "subtitle": subtitle,
                        "videoId": video_id,
                        "browseId": browse_id,
                        "thumbnail": thumbnail,
                        "type": item_type
                    })

    except Exception as e:
        print(f"Error parsing search results: {e}")
        traceback.print_exc()
    return results

def parse_artist_page(response: Dict[str, Any]) -> Dict[str, Any]:
    try:
        header = response.get("header", {}).get("musicImmersiveHeaderRenderer", {})
        if not header:
             header = response.get("header", {}).get("musicVisualHeaderRenderer", {})

        if not header and "header" in response:
            # Fallback for other header types
            header = list(response["header"].values())[0] if response["header"] else {}

        title = ParserUtils.get_text(header.get("title", {}).get("runs", []))
        thumbnail = ParserUtils.get_thumbnail(header.get("thumbnail", {}).get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", []))
        if not thumbnail:
             thumbnail = ParserUtils.get_thumbnail(header.get("foregroundThumbnail", {}).get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", []))

        sections = []
        tabs = response.get("contents", {}).get("singleColumnBrowseResultsRenderer", {}).get("tabs", [])
        if not tabs: return {"title": title, "thumbnail": thumbnail, "sections": []}

        contents = tabs[0].get("tabRenderer", {}).get("content", {}).get("sectionListRenderer", {}).get("contents", [])

        for section in contents:
            shelf = section.get("musicShelfRenderer") or section.get("musicCarouselShelfRenderer")
            if shelf:
                shelf_title = ParserUtils.get_text(shelf.get("title", {}).get("runs", []))
                items = []
                for content in shelf.get("contents", []):
                    renderer = content.get("musicResponsiveListItemRenderer") or content.get("musicTwoRowItemRenderer")
                    if renderer:
                        if "musicTwoRowItemRenderer" in content:
                            r = content["musicTwoRowItemRenderer"]
                            v_id = r.get("navigationEndpoint", {}).get("watchEndpoint", {}).get("videoId")
                            b_id = r.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId")
                            sub = ParserUtils.get_text(r.get("subtitle", {}).get("runs", []))
                            items.append({
                                "title": ParserUtils.get_text(r.get("title", {}).get("runs", [])),
                                "subtitle": sub,
                                "browseId": b_id,
                                "videoId": v_id,
                                "thumbnail": ParserUtils.get_thumbnail(r.get("thumbnailRenderer", {}).get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [])),
                                "type": ParserUtils.guess_type(v_id, b_id, sub)
                            })
                        else:
                            flex_cols = renderer.get("flexColumns", [])
                            if not flex_cols: continue
                            title_run = flex_cols[0].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [{}])[0]
                            v_id = title_run.get("navigationEndpoint", {}).get("watchEndpoint", {}).get("videoId")
                            sub = ParserUtils.get_text(flex_cols[1].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [])) if len(flex_cols) > 1 else ""
                            items.append({
                                "title": title_run.get("text", ""),
                                "subtitle": sub,
                                "videoId": v_id,
                                "thumbnail": ParserUtils.get_thumbnail(renderer.get("thumbnail", {}).get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [])),
                                "type": "song"
                            })
                if items:
                    sections.append({"title": shelf_title, "items": items, "layout": "carousel"})

        return {"title": title, "thumbnail": thumbnail, "sections": sections}
    except Exception as e:
        print(f"Error parsing artist page: {e}")
        traceback.print_exc()
        return {}

def parse_album_page(response: Dict[str, Any]) -> Dict[str, Any]:
    try:
        header = response.get("header", {}).get("musicDetailHeaderRenderer", {})
        title = ParserUtils.get_text(header.get("title", {}).get("runs", []))

        # Subtitle parsing for album: Artist • Year
        sub_runs = header.get("subtitle", {}).get("runs", [])
        subtitle = ParserUtils.get_text(sub_runs)

        # Try to extract specific artist browseId
        artist_id = None
        for run in sub_runs:
            b_id = run.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId")
            if b_id and b_id.startswith("UC"):
                artist_id = b_id
                break

        thumbnail = ParserUtils.get_header_thumbnail(header.get("thumbnail", {}))

        tracks = []
        contents = []
        if "contents" in response:
            c = response["contents"]
            if "singleColumnBrowseResultsRenderer" in c:
                contents = c["singleColumnBrowseResultsRenderer"].get("tabs", [{}])[0].get("tabRenderer", {}).get("content", {}).get("sectionListRenderer", {}).get("contents", [{}])[0].get("musicShelfRenderer", {}).get("contents", [])

        for content in contents:
            renderer = content.get("musicResponsiveListItemRenderer")
            if renderer:
                flex_cols = renderer.get("flexColumns", [])
                if not flex_cols: continue  # Bug #8 fix: guard against empty flex columns
                title_run = flex_cols[0].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [{}])[0]

                v_id = title_run.get("navigationEndpoint", {}).get("watchEndpoint", {}).get("videoId")
                if not v_id:
                    v_id = ParserUtils.get_video_id(renderer)

                tracks.append({
                    "title": title_run.get("text", ""),
                    "subtitle": ParserUtils.get_text(flex_cols[1].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [])) if len(flex_cols) > 1 else subtitle,
                    "videoId": v_id,
                    "thumbnail": thumbnail, # Use album thumb for tracks
                    "type": "song"
                })
        return {
            "title": title,
            "subtitle": subtitle,
            "thumbnail": thumbnail,
            "tracks": tracks,
            "artistId": artist_id,
            "type": "album"
        }
    except Exception as e:
        print(f"Error parsing album page: {e}")
        return {}

def _find_header_renderer(obj: Any, depth: int = 0, max_depth: int = 8) -> Optional[Dict]:
    """Last-resort recursive scan for any '...HeaderRenderer' dict that looks
    like a real page header (has a 'title' field). Used when YouTube moves
    the header to a location/renderer name we don't already check — keeps
    the parser working even if YouTube renames things again later."""
    if depth > max_depth:
        return None
    if isinstance(obj, dict):
        for key, val in obj.items():
            if isinstance(val, dict) and key.endswith("HeaderRenderer") and "title" in val:
                return val
        for val in obj.values():
            found = _find_header_renderer(val, depth + 1, max_depth)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_header_renderer(item, depth + 1, max_depth)
            if found:
                return found
    return None

def parse_playlist_page(response: Dict[str, Any]) -> Dict[str, Any]:
    try:
        # YouTube Music can return several different header renderer types for
        # playlists depending on playlist source and client version:
        #   musicResponsiveHeaderRenderer  – current default (2024+ redesign).
        #                                     CONFIRMED via raw response dump
        #                                     (Pass 8): response["header"] is {}
        #                                     for these playlists — the renderer
        #                                     instead lives inside the TABS path
        #                                     (same shape singleColumnBrowseResultsRenderer
        #                                     uses elsewhere), NOT primaryContents:
        #                                       contents.twoColumnBrowseResultsRenderer
        #                                         .tabs[0].tabRenderer.content
        #                                         .sectionListRenderer.contents[0]
        #                                         .musicResponsiveHeaderRenderer
        #                                     secondaryContents (same twoColumnBrowseResultsRenderer)
        #                                     is still where the track shelf lives — unchanged.
        #   musicDetailHeaderRenderer                 – older user/YTM playlists
        #   musicImmersiveHeaderRenderer               – some curated/editorial playlists
        #   musicEditablePlaylistDetailHeaderRenderer  – user-owned editable playlists
        raw_header = response.get("header", {})

        header = (
            raw_header.get("musicDetailHeaderRenderer")
            or raw_header.get("musicResponsiveHeaderRenderer")
            or raw_header.get("musicImmersiveHeaderRenderer")
            or raw_header.get("musicEditablePlaylistDetailHeaderRenderer", {})
                  .get("header", {}).get("musicDetailHeaderRenderer", {})
        )

        if not header:
            # response["header"] is empty on current-format playlists — the
            # header renderer lives inside the *first section* of the tabs
            # path, confirmed against a real raw response (Pass 8).
            two_col = response.get("contents", {}).get("twoColumnBrowseResultsRenderer", {})
            tabs = two_col.get("tabs", [])
            tab_sections = (
                tabs[0].get("tabRenderer", {}).get("content", {})
                       .get("sectionListRenderer", {}).get("contents", [])
                if tabs else []
            )
            for section in tab_sections:
                for key in ("musicResponsiveHeaderRenderer", "musicDetailHeaderRenderer", "musicImmersiveHeaderRenderer"):
                    if key in section:
                        header = section[key]
                        break
                if header:
                    break

        if not header:
            # Still nothing — recursively scan the whole response as a final
            # fallback so future YouTube renames don't reintroduce this bug.
            header = _find_header_renderer(response) or {}

        title = ParserUtils.get_text(header.get("title", {}).get("runs", []))

        # Subtitle parsing: musicResponsiveHeaderRenderer splits info across
        # two run groups — "subtitle" (e.g. "Playlist • 2023") and
        # "secondSubtitle" (e.g. "10M views • 25 tracks • 1 hour, 50 minutes").
        # Combine them so the UI shows the fuller YTM-style line instead of
        # just the first half.
        sub_runs = header.get("subtitle", {}).get("runs", [])
        subtitle = ParserUtils.get_text(sub_runs)
        second_subtitle = ParserUtils.get_text(header.get("secondSubtitle", {}).get("runs", []))
        if second_subtitle:
            subtitle = f"{subtitle} • {second_subtitle}" if subtitle else second_subtitle

        # This used to read response["header"]["description"] directly, which
        # is always {} on affected playlists (same root cause as the thumbnail
        # bug) — use the resolved `header` instead.
        description = ParserUtils.get_text(header.get("description", {}).get("runs", []))

        # Thumbnail: try the resolved header first (covers all renderer variants
        # above via get_header_thumbnail's musicThumbnailRenderer /
        # croppedSquareThumbnailRenderer check). If still empty, walk every
        # top-level raw_header renderer, then every tabs-path section, trying
        # to pull a thumbnail from any of them.
        thumbnail = ParserUtils.get_header_thumbnail(header.get("thumbnail", {}))
        if not thumbnail:
            candidates = list(raw_header.values())
            two_col = response.get("contents", {}).get("twoColumnBrowseResultsRenderer", {})
            tabs = two_col.get("tabs", [])
            if tabs:
                candidates += tabs[0].get("tabRenderer", {}).get("content", {}) \
                                     .get("sectionListRenderer", {}).get("contents", [])
            for renderer_val in candidates:
                if not isinstance(renderer_val, dict):
                    continue
                # renderer_val may itself be a wrapper (e.g. a section dict with
                # a single HeaderRenderer key) — check both it and its values.
                thumb_obj = renderer_val.get("thumbnail", {})
                thumbnail = ParserUtils.get_header_thumbnail(thumb_obj)
                if not thumbnail:
                    direct = thumb_obj.get("thumbnails", [])
                    if direct:
                        thumbnail = ParserUtils.get_thumbnail(direct)
                if not thumbnail:
                    for inner in renderer_val.values():
                        if isinstance(inner, dict) and "thumbnail" in inner:
                            thumbnail = ParserUtils.get_header_thumbnail(inner.get("thumbnail", {}))
                            if thumbnail:
                                break
                if thumbnail:
                    break

        tracks = []

        # Resolve the shelf renderer once — used for both track extraction and
        # continuation token. The first block (which computed `contents` directly)
        # was dead code because it was always overwritten below; removed to avoid
        # confusion and a fragile fallback path (Bug #7 fix).
        playlist_shelf = None
        if "contents" in response:
            c = response["contents"]
            if "twoColumnBrowseResultsRenderer" in c:
                secondary = c["twoColumnBrowseResultsRenderer"].get("secondaryContents", {})
                playlist_shelf = secondary.get("sectionListRenderer", {}).get("contents", [{}])[0].get("musicPlaylistShelfRenderer", {})
            elif "singleColumnBrowseResultsRenderer" in c:
                playlist_shelf = c["singleColumnBrowseResultsRenderer"].get("tabs", [{}])[0].get("tabRenderer", {}).get("content", {}).get("sectionListRenderer", {}).get("contents", [{}])[0].get("musicPlaylistShelfRenderer", {})

        contents = playlist_shelf.get("contents", []) if playlist_shelf else []

        for content in contents:
            renderer = content.get("musicResponsiveListItemRenderer")
            if renderer:
                flex_cols = renderer.get("flexColumns", [])
                if not flex_cols: continue

                title_run = flex_cols[0].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [{}])[0]
                video_id = title_run.get("navigationEndpoint", {}).get("watchEndpoint", {}).get("videoId")
                if not video_id:
                    video_id = ParserUtils.get_video_id(renderer)

                artist_sub = ""
                if len(flex_cols) > 1:
                    artist_sub = ParserUtils.get_text(flex_cols[1].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", []))

                tracks.append({
                    "title": title_run.get("text", ""),
                    "subtitle": artist_sub,
                    "videoId": video_id,
                    "thumbnail": ParserUtils.get_thumbnail(renderer.get("thumbnail", {}).get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [])),
                    "type": "song"
                })

        # Extract continuation token for "Load more" pagination
        continuation = None
        if playlist_shelf:
            continuations = playlist_shelf.get("continuations", [])
            if continuations:
                continuation = continuations[0].get("nextContinuationData", {}).get("continuation")

        return {
            "title": title,
            "subtitle": subtitle,
            "description": description,
            "thumbnail": thumbnail,
            "tracks": tracks,
            "continuation": continuation,
            "type": "playlist"
        }
    except Exception as e:
        print(f"Error parsing playlist page: {e}")
        traceback.print_exc()
        return {}

def parse_song_info(response: Dict[str, Any]) -> Dict[str, Any]:
    try:
        panel = response.get("singleColumnMusicWatchNextResultsRenderer", {}).get("tabbedRenderer", {}).get("watchNextTabbedResultsRenderer", {}).get("tabs", [{}])[0].get("tabRenderer", {}).get("content", {}).get("musicQueueRenderer", {}).get("content", {}).get("playlistPanelRenderer", {}).get("contents", [{}])[0].get("playlistPanelVideoRenderer", {})
        return {
            "title": ParserUtils.get_text(panel.get("title", {}).get("runs", [])),
            "subtitle": ParserUtils.get_text(panel.get("longBylineText", {}).get("runs", [])),
            "thumbnail": ParserUtils.get_thumbnail(panel.get("thumbnail", {}).get("thumbnails", [])),
            "videoId": panel.get("videoId")
        }
    except Exception as e:
        print(f"Error parsing song info: {e}")
        return {}
