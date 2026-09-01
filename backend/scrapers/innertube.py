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
        "clientVersion": "7.27.52",
        "userAgent": "com.google.android.apps.youtube.music/7.27.52 (Linux; U; Android 11; en_US; Pixel 4 XL; Build/RP1A.200720.009) gzip",
        "osName": "Android",
        "osVersion": "11",
        "platform": "MOBILE",
        "gl": "US",
        "hl": "en",
        "androidSdkVersion": 30
    }

    TVHTML5_SIMPLY_EMBEDDED = {
        "clientName": "TVHTML5_SIMPLY_EMBEDDED",
        "clientVersion": "2.20230608.05.00",
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "osName": "Windows",
        "osVersion": "10.0",
        "platform": "TV",
        "gl": "US",
        "hl": "en"
    }

    API_URL = "https://music.youtube.com/youtubei/v1/"
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
        # Generic client for streaming and non-API requests (avoids JSON headers)
        self.stream_client = httpx.AsyncClient(timeout=60.0)

    def _get_context(self, client_config: Dict[str, Any]) -> Dict[str, Any]:
        context = {
            "client": {
                "clientName": client_config["clientName"],
                "clientVersion": client_config["clientVersion"],
                "hl": client_config.get("hl", "en"),
                "gl": client_config.get("gl", "US"),
                "userAgent": client_config["userAgent"],
            }
        }
        if "androidSdkVersion" in client_config:
            context["client"]["androidSdkVersion"] = client_config["androidSdkVersion"]
        return context

    async def post(self, endpoint: str, body: Dict[str, Any], client_config: Dict[str, Any]) -> Dict[str, Any]:
        full_body = {
            "context": self._get_context(client_config),
            **body
        }
        headers = {
            "User-Agent": client_config["userAgent"],
            "X-YouTube-Client-Name": {"WEB_REMIX": "67", "ANDROID_MUSIC": "21", "TVHTML5_SIMPLY_EMBEDDED": "85"}.get(client_config["clientName"], "1"),
            "X-YouTube-Client-Version": client_config["clientVersion"]
        }
        response = await self.client.post(f"{endpoint}?prettyPrint=false", json=full_body, headers=headers)
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
            "params": "CgIQBg=="
        }
        # Android client needs extra headers to get pre-signed URLs.
        # cpn (Client Playback Nonce) must be a fresh random 16-char alphanumeric
        # string per request — a hardcoded value causes YouTube to reject/throttle.
        if client_config["clientName"] == "ANDROID_MUSIC":
            body["cpn"] = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        return await self.post("player", body, client_config)

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
