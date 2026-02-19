import asyncio
import logging
import os
import aiohttp
from typing import Dict

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)


class CobaltDownloader:
    """
    Multi-API TikTok downloader with fallback support.
    Updated to Cobalt API v7 format (2025).
    Tries: Cobalt API v7 → TikWM API
    """

    # ✅ FIX 1.2: Updated to Cobalt API v7 endpoints (old /api/json path is deprecated)
    COBALT_ENDPOINTS = [
        "https://api.cobalt.tools/",
        "https://cobalt.tools/api/",
        "https://cobalt-api.kwiatekmiki.com/",
    ]

    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=60)

    async def _download_file(self, url: str, filename: str) -> bool:
        """Download file from URL with streaming and size validation."""
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                )
            }
            async with aiohttp.ClientSession(
                timeout=self.timeout, headers=headers
            ) as session:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status != 200:
                        logger.error(f"Download failed: HTTP {response.status}")
                        return False

                    # Pre-check Content-Length before streaming
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > 49 * 1024 * 1024:
                        logger.warning("File too large (>49MB) — skipping")
                        return False

                    total_size = 0
                    with open(filename, "wb") as f:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            total_size += len(chunk)
                            if total_size > 49 * 1024 * 1024:
                                logger.warning("File exceeded 49MB during download")
                                # Remove partial file
                                try:
                                    os.remove(filename)
                                except Exception:
                                    pass
                                return False
                            f.write(chunk)

                    logger.info(
                        f"✅ Downloaded: {filename} "
                        f"({total_size / 1024 / 1024:.2f}MB)"
                    )
                    return True

        except asyncio.TimeoutError:
            logger.error("Download timed out")
            return False
        except Exception as e:
            logger.error(f"Download error: {e}")
            return False

    async def _try_cobalt_api(
        self, url: str, download_type: str
    ) -> Dict[str, any]:
        """
        Try Cobalt API v7 endpoints.
        
        v7 payload format:
        {
            "url": "...",
            "videoQuality": "1080",
            "audioFormat": "mp3",
            "downloadMode": "auto" | "audio" | "mute"
        }
        """
        # ✅ FIX 1.2: Updated payload to Cobalt API v7 schema
        payload = {
            "url": url,
            "videoQuality": "1080",
            "audioFormat": "mp3",
            "downloadMode": "audio" if download_type == "audio" else "auto",
            "filenameStyle": "basic",
        }

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        for endpoint in self.COBALT_ENDPOINTS:
            try:
                logger.info(f"🔄 Trying Cobalt v7: {endpoint}")

                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.post(
                        endpoint, json=payload, headers=headers
                    ) as response:

                        if response.status == 400:
                            # Bad request — log body for debugging
                            body = await response.text()
                            logger.warning(f"Cobalt 400 Bad Request: {body[:200]}")
                            continue

                        if response.status == 429:
                            logger.warning("Cobalt rate limited — trying next endpoint")
                            continue

                        if response.status not in (200, 201):
                            logger.warning(
                                f"Cobalt returned {response.status} at {endpoint}"
                            )
                            continue

                        data = await response.json()
                        status = data.get("status")

                        if status == "error":
                            err_code = data.get("error", {}).get("code", "unknown")
                            logger.error(f"Cobalt error code: {err_code}")
                            continue

                        # Handle redirect or tunnel response
                        if status in ("redirect", "tunnel"):
                            download_url = data.get("url")
                            if not download_url:
                                logger.warning("Cobalt: no url in response")
                                continue

                            file_ext = "mp3" if download_type == "audio" else "mp4"
                            ts = int(asyncio.get_event_loop().time())
                            filename = os.path.join(
                                DOWNLOAD_DIR,
                                f"tiktok_{abs(hash(url))}_{ts}.{file_ext}",
                            )

                            if await self._download_file(download_url, filename):
                                return {
                                    "status": "success",
                                    "file_path": filename,
                                    "title": "TikTok Video",
                                    "duration": 0,
                                    "uploader": "TikTok",
                                }

                        # Handle picker (carousel/slideshow)
                        elif status == "picker":
                            picker_items = data.get("picker", [])
                            if picker_items and "url" in picker_items[0]:
                                download_url = picker_items[0]["url"]
                                filename = os.path.join(
                                    DOWNLOAD_DIR,
                                    f"tiktok_{abs(hash(url))}.mp4",
                                )
                                if await self._download_file(download_url, filename):
                                    return {
                                        "status": "success",
                                        "file_path": filename,
                                        "title": "TikTok Carousel",
                                        "duration": 0,
                                        "uploader": "TikTok",
                                    }

            except asyncio.TimeoutError:
                logger.warning(f"Timeout connecting to: {endpoint}")
                continue
            except aiohttp.ClientConnectorError as e:
                logger.warning(f"Cannot connect to {endpoint}: {e}")
                continue
            except Exception as e:
                logger.error(f"Unexpected error with {endpoint}: {e}")
                continue

        return {"status": "error", "message": "All Cobalt endpoints failed"}

    async def _try_tikwm_api(self, url: str) -> Dict[str, any]:
        """
        Fallback: TikWM API.
        More stable than SnapTik for TikTok video-only downloads.
        """
        try:
            logger.info("🔄 Trying TikWM fallback...")

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36"
                ),
                "Accept": "application/json",
            }

            api_url = f"https://www.tikwm.com/api/?url={url}&hd=1"

            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(api_url, headers=headers) as response:
                    if response.status != 200:
                        logger.warning(f"TikWM returned {response.status}")
                        return {"status": "error", "message": "TikWM API failed"}

                    data = await response.json()

                    if data.get("code") != 0:
                        logger.warning(f"TikWM error code: {data.get('code')}")
                        return {"status": "error", "message": "TikWM API error"}

                    video_data = data.get("data", {})

                    # Prefer HD, fallback to SD
                    download_url = (
                        video_data.get("hdplay") or video_data.get("play")
                    )

                    if not download_url:
                        logger.warning("No video URL in TikWM response")
                        return {"status": "error", "message": "No video URL"}

                    filename = os.path.join(
                        DOWNLOAD_DIR,
                        f"tiktok_tikwm_{abs(hash(url))}.mp4",
                    )

                    if await self._download_file(download_url, filename):
                        return {
                            "status": "success",
                            "file_path": filename,
                            "title": video_data.get("title", "TikTok Video"),
                            "duration": video_data.get("duration", 0),
                            "uploader": (
                                video_data.get("author", {}).get(
                                    "nickname", "TikTok"
                                )
                            ),
                        }

        except Exception as e:
            logger.error(f"TikWM error: {e}")

        return {"status": "error", "message": "TikWM failed"}

    async def download(
        self, url: str, download_type: str = "video"
    ) -> Dict[str, any]:
        """
        Download TikTok video with fallback strategy.

        Priority:
          1. Cobalt API v7 (best quality)
          2. TikWM API (reliable fallback)

        Args:
            url: TikTok video URL
            download_type: "video" or "audio"

        Returns:
            Dict with keys: status, file_path, title, duration, uploader
        """
        # Try Cobalt v7 first
        result = await self._try_cobalt_api(url, download_type)
        if result["status"] == "success":
            logger.info("✅ Downloaded via Cobalt API v7")
            return result

        # Audio-only: yt-dlp handles this better (called from downloader.py)
        if download_type == "audio":
            return {
                "status": "error",
                "message": "Audio download failed via Cobalt. Will retry via yt-dlp.",
            }

        # Video fallback: TikWM
        logger.info("⚠️ Cobalt failed → trying TikWM...")
        result = await self._try_tikwm_api(url)
        if result["status"] == "success":
            logger.info("✅ Downloaded via TikWM")
            return result

        logger.error("❌ All TikTok download methods failed")
        return {
            "status": "error",
            "message": (
                "Cannot download TikTok video. All methods failed.\n"
                "Video may be private, deleted, or region-locked."
            ),
        }


# Global singleton instance
cobalt_downloader = CobaltDownloader()