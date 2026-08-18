import httpx
import random
import string
from typing import Optional, Dict, Any

class YouTubeClient:
    WEB_REMIX = {
        "clientName": "WEB_REMIX",
        "clientVersion": "1.20241121.01.00",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "osName": "Windows",
        "osVersion": "10.0",
        "platform": "DESKTOP",
        "gl": "US",
        "hl": "en"
    }

    ANDROID_MUSIC = {
        "clientName": "ANDROID_MUSIC",
        "clientVersion": "6.19.52",
        "userAgent": "com.google.android.apps.youtube.music/6.19.52 (Linux; U; Android 11; en_US; Pixel 4 XL; Build/RP1A.200720.009) gzip",
        "osName": "Android",
        "osVersion": "11",
        "platform": "MOBILE",
        "gl": "US",
        "hl": "en",
        "androidSdkVersion": 30
    }

    TVHTML5_SIMPLY_EMBEDDED = {
        "clientName": "TVHTML5_SIMPLY_EMBEDDED_PLAYER",
        "clientVersion": "2.0",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "osName": "Windows",
        "osVersion": "10.0",
        "platform": "TV",
        "gl": "US",
        "hl": "en"
    }

    API_URL = "https://music.youtube.com/youtubei/v1/"
    API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
    ORIGIN = "https://music.youtube.com"
    REFERER = "https://music.youtube.com/"

class InnerTube:
    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=YouTubeClient.API_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Goog-Api-Format-Version": "1",
                "X-Origin": YouTubeClient.ORIGIN,
                "Referer": YouTubeClient.REFERER,
            },
            timeout=30.0
        )
        # Generic client for streaming and non-API requests
        self.stream_client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)

    def _get_context(self, client_config: Dict[str, Any]) -> Dict[str, Any]:
        context = {
            "client": {
                "clientName": client_config["clientName"],
                "clientVersion": client_config["clientVersion"],
                "hl": client_config.get("hl", "en"),
                "gl": client_config.get("gl", "US"),
                "userAgent": client_config["userAgent"],
                "visitorData": "CgtEUlRINDFjdm1YayjX1pSaBg%3D%3D",
                "platform": client_config.get("platform", "DESKTOP"),
            }
        }
        if "androidSdkVersion" in client_config:
            context["client"]["androidSdkVersion"] = client_config["androidSdkVersion"]
        return context

    async def post(self, endpoint: str, body: Dict[str, Any], client_config: Dict[str, Any], mask: str = None) -> Dict[str, Any]:
        full_body = {
            "context": self._get_context(client_config),
            **body
        }
        headers = {
            "User-Agent": client_config["userAgent"],
            "X-Goog-Api-Key": YouTubeClient.API_KEY,
            "Content-Type": "application/json"
        }
        if mask:
            headers["X-Goog-FieldMask"] = mask

        response = await self.client.post(endpoint, json=full_body, headers=headers)
        response.raise_for_status()
        return response.json()

    async def search(self, query: str, params: Optional[str] = None) -> Dict[str, Any]:
        body = {"query": query}
        if params:
            body["params"] = params
        return await self.post("search", body, YouTubeClient.WEB_REMIX)

    async def player(self, video_id: str, client_config: Dict[str, Any] = None) -> Dict[str, Any]:
        if client_config is None:
            client_config = YouTubeClient.ANDROID_MUSIC

        body = {
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
        }

        # Android client needs a fresh random cpn (Client Playback Nonce) per request
        if client_config["clientName"] == "ANDROID_MUSIC":
            body["cpn"] = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

        mask = "playabilityStatus.status,playerConfig.audioConfig,streamingData.adaptiveFormats,videoDetails.videoId"

        def _has_plain_urls(resp: Dict[str, Any]) -> bool:
            """Returns True only if at least one audio format has a direct url (not ciphered)."""
            formats = resp.get("streamingData", {}).get("adaptiveFormats", [])
            return any("url" in f for f in formats)

        try:
            # 1. Primary: ANDROID_MUSIC
            response = await self.post("player", body, client_config, mask=mask)

            if response.get("playabilityStatus", {}).get("status") == "OK" and _has_plain_urls(response):
                return response

            # 2. Fallback: WEB_REMIX (against music API)
            print(f"Android resolution for {video_id} failed or ciphered, trying WEB_REMIX...")
            web_config = YouTubeClient.WEB_REMIX
            response = await self.post("player", body, web_config, mask=mask)

            if response.get("playabilityStatus", {}).get("status") == "OK" and _has_plain_urls(response):
                return response

            # 3. Fallback: TVHTML5 bypass
            print(f"Web resolution for {video_id} failed, trying TV bypass...")
            tv_config = YouTubeClient.TVHTML5_SIMPLY_EMBEDDED
            tv_context = self._get_context(tv_config)

            tv_body = {
                "videoId": video_id,
                "context": tv_context,
            }
            tv_body["context"]["thirdParty"] = {"embedUrl": f"https://www.youtube.com/watch?v={video_id}"}

            # Request from generic YouTube API for better success
            tv_headers = {
                "User-Agent": tv_config["userAgent"],
                "X-Goog-Api-Key": YouTubeClient.API_KEY,
                "Content-Type": "application/json",
                "X-Goog-FieldMask": mask,
            }
            tv_raw = await self.stream_client.post(
                "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                json=tv_body,
                headers=tv_headers,
                timeout=30.0
            )
            if tv_raw.status_code == 200:
                tv_response = tv_raw.json()
                if tv_response.get("playabilityStatus", {}).get("status") == "OK" and _has_plain_urls(tv_response):
                    return tv_response

            # 4. Fallback: Piped API
            print(f"InnerTube bypass failed for {video_id}, falling back to Piped...")
            # Piped instance list: api.piped.yt, pipedapi.kavin.rocks, pipedapi.lunar.icu
            for instance in ["pipedapi.kavin.rocks", "api.piped.yt", "pipedapi.leptons.xyz"]:
                try:
                    piped_res = await self.stream_client.get(f"https://{instance}/streams/{video_id}", timeout=10.0)
                    if piped_res.status_code == 200:
                        piped_data = piped_res.json()
                        audio_streams = piped_data.get("audioStreams", [])
                        if audio_streams:
                            formats = []
                            for s in audio_streams:
                                formats.append({
                                    "url": s["url"],
                                    "bitrate": s["bitrate"],
                                    "mimeType": s.get("mimeType", "audio/mp4"),
                                    "itag": 251 if "opus" in s.get("mimeType", "").lower() else 140
                                })
                            return {
                                "playabilityStatus": {"status": "OK"},
                                "streamingData": {"adaptiveFormats": formats},
                                "videoDetails": {"videoId": video_id}
                            }
                except Exception:
                    continue

            return response
        except Exception as e:
            print(f"Player request failed: {e}")
            raise e

    async def browse(self, browse_id: str, params: Optional[str] = None, continuation: Optional[str] = None) -> Dict[str, Any]:
        body = {}
        if browse_id:
            body["browseId"] = browse_id
        if params:
            body["params"] = params
        if continuation:
            body["continuation"] = continuation
        return await self.post("browse", body, YouTubeClient.WEB_REMIX)

    async def get_next(self, video_id: str, playlist_id: Optional[str] = None, is_audio_only: bool = True) -> Dict[str, Any]:
        body = {"videoId": video_id, "isAudioOnly": is_audio_only}
        if playlist_id:
            body["playlistId"] = playlist_id
        return await self.post("next", body, YouTubeClient.WEB_REMIX)

    async def get_suggestions(self, query: str) -> Dict[str, Any]:
        body = {"input": query}
        return await self.post("music/get_search_suggestions", body, YouTubeClient.WEB_REMIX)
