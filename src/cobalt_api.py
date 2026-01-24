import asyncio
import logging
import os
import aiohttp
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)


class CobaltDownloader:
    """
    Multi-API TikTok downloader with fallback support.
    Tries: Cobalt API → SnapTik API → TikWM API
    """
    
    # Multiple Cobalt API instances for reliability
    COBALT_ENDPOINTS = [
        "https://api.cobalt.tools/api/json",
        "https://co.wuk.sh/api/json",
        "https://cobalt-api.kwiatekmiki.com/api/json",
    ]
    
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=60)
        self.max_retries = 2
    
    async def _download_file(self, url: str, filename: str) -> bool:
        """Download file from URL."""
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status != 200:
                        logger.error(f"Download failed: HTTP {response.status}")
                        return False
                    
                    # Check file size
                    content_length = response.headers.get('Content-Length')
                    if content_length and int(content_length) > 49 * 1024 * 1024:
                        logger.warning("File too large (>49MB)")
                        return False
                    
                    # Download
                    total_size = 0
                    with open(filename, 'wb') as f:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            total_size += len(chunk)
                            if total_size > 49 * 1024 * 1024:
                                logger.warning("File exceeded size limit during download")
                                return False
                            f.write(chunk)
                    
                    logger.info(f"✅ Downloaded: {filename} ({total_size / 1024 / 1024:.2f}MB)")
                    return True
                    
        except Exception as e:
            logger.error(f"Download error: {e}")
            return False
    
    async def _try_cobalt_api(self, url: str, download_type: str) -> Dict[str, any]:
        """Try Cobalt API endpoints."""
        payload = {
            "url": url,
            "vCodec": "h264",
            "vQuality": "1080",
            "aFormat": "mp3" if download_type == "audio" else "best",
            "isAudioOnly": download_type == "audio",
            "disableMetadata": False,
        }
        
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        
        for endpoint in self.COBALT_ENDPOINTS:
            try:
                logger.info(f"🔄 Trying Cobalt: {endpoint}")
                
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.post(endpoint, json=payload, headers=headers) as response:
                        
                        if response.status != 200:
                            logger.warning(f"Cobalt returned {response.status}")
                            continue
                        
                        data = await response.json()
                        status = data.get("status")
                        
                        if status == "error":
                            logger.error(f"Cobalt error: {data.get('text')}")
                            continue
                        
                        if status in ["redirect", "tunnel"]:
                            download_url = data.get("url")
                            if not download_url:
                                continue
                            
                            file_ext = "mp3" if download_type == "audio" else "mp4"
                            filename = os.path.join(DOWNLOAD_DIR, f"tiktok_{abs(hash(url))}_{int(asyncio.get_event_loop().time())}.{file_ext}")
                            
                            if await self._download_file(download_url, filename):
                                return {
                                    "status": "success",
                                    "file_path": filename,
                                    "title": "TikTok Video",
                                    "duration": 0,
                                    "uploader": "TikTok"
                                }
                        
                        elif status == "picker":
                            picker_items = data.get("picker", [])
                            if picker_items and "url" in picker_items[0]:
                                download_url = picker_items[0]["url"]
                                filename = os.path.join(DOWNLOAD_DIR, f"tiktok_{abs(hash(url))}.mp4")
                                
                                if await self._download_file(download_url, filename):
                                    return {
                                        "status": "success",
                                        "file_path": filename,
                                        "title": "TikTok Carousel",
                                        "duration": 0,
                                        "uploader": "TikTok"
                                    }
                
            except asyncio.TimeoutError:
                logger.warning(f"Timeout: {endpoint}")
                continue
            except Exception as e:
                logger.error(f"Error with {endpoint}: {e}")
                continue
        
        return {"status": "error", "message": "All Cobalt endpoints failed"}
    
    async def _try_snaptik_api(self, url: str) -> Dict[str, any]:
        """Fallback: SnapTik API (web scraping method)."""
        try:
            logger.info("🔄 Trying SnapTik fallback...")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': '*/*',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': 'https://snaptik.app',
                'Referer': 'https://snaptik.app/',
            }
            
            payload = {'url': url}
            
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post('https://snaptik.app/abc2.php', data=payload, headers=headers) as response:
                    if response.status != 200:
                        logger.warning(f"SnapTik returned {response.status}")
                        return {"status": "error", "message": "SnapTik API failed"}
                    
                    html = await response.text()
                    
                    # Extract download link from HTML
                    # SnapTik returns HTML with download links
                    match = re.search(r'href="(https://[^"]+\.tikcdn\.io/[^"]+)"', html)
                    if not match:
                        logger.warning("No download link found in SnapTik response")
                        return {"status": "error", "message": "No download link"}
                    
                    download_url = match.group(1)
                    filename = os.path.join(DOWNLOAD_DIR, f"tiktok_snaptik_{abs(hash(url))}.mp4")
                    
                    if await self._download_file(download_url, filename):
                        return {
                            "status": "success",
                            "file_path": filename,
                            "title": "TikTok Video (SnapTik)",
                            "duration": 0,
                            "uploader": "TikTok"
                        }
        
        except Exception as e:
            logger.error(f"SnapTik error: {e}")
        
        return {"status": "error", "message": "SnapTik failed"}
    
    async def _try_tikwm_api(self, url: str) -> Dict[str, any]:
        """Fallback: TikWM API."""
        try:
            logger.info("🔄 Trying TikWM fallback...")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json',
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
                    
                    # Try HD first, then SD
                    download_url = video_data.get("hdplay") or video_data.get("play")
                    
                    if not download_url:
                        logger.warning("No video URL in TikWM response")
                        return {"status": "error", "message": "No video URL"}
                    
                    filename = os.path.join(DOWNLOAD_DIR, f"tiktok_tikwm_{abs(hash(url))}.mp4")
                    
                    if await self._download_file(download_url, filename):
                        return {
                            "status": "success",
                            "file_path": filename,
                            "title": video_data.get("title", "TikTok Video (TikWM)"),
                            "duration": video_data.get("duration", 0),
                            "uploader": video_data.get("author", {}).get("nickname", "TikTok")
                        }
        
        except Exception as e:
            logger.error(f"TikWM error: {e}")
        
        return {"status": "error", "message": "TikWM failed"}
    
    async def download(self, url: str, download_type: str = "video") -> Dict[str, any]:
        """
        Download TikTok video with multiple fallback options.
        
        Priority:
        1. Cobalt API (best quality, most reliable)
        2. SnapTik API (good fallback)
        3. TikWM API (last resort)
        
        Args:
            url: TikTok video URL
            download_type: "video" or "audio"
            
        Returns:
            Dictionary with status, file_path, and metadata
        """
        
        # Try Cobalt API first
        result = await self._try_cobalt_api(url, download_type)
        if result["status"] == "success":
            logger.info("✅ Downloaded via Cobalt API")
            return result
        
        # If audio only, don't try other APIs (they don't support it well)
        if download_type == "audio":
            return {
                "status": "error",
                "message": "Audio download not available for TikTok. All APIs failed."
            }
        
        # Fallback 1: SnapTik
        logger.info("⚠️ Cobalt failed, trying SnapTik...")
        result = await self._try_snaptik_api(url)
        if result["status"] == "success":
            logger.info("✅ Downloaded via SnapTik")
            return result
        
        # Fallback 2: TikWM
        logger.info("⚠️ SnapTik failed, trying TikWM...")
        result = await self._try_tikwm_api(url)
        if result["status"] == "success":
            logger.info("✅ Downloaded via TikWM")
            return result
        
        # All APIs failed
        logger.error("❌ All TikTok download methods failed")
        return {
            "status": "error",
            "message": "Cannot download TikTok video. All methods failed. Video may be private or region-locked."
        }


# Global instance
cobalt_downloader = CobaltDownloader()