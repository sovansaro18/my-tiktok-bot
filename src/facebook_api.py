import asyncio
import logging
import os
import aiohttp
import re
from typing import Dict, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)


class FacebookDownloader:
    """
    Multi-API Facebook video downloader with fallback support.
    Priority: SnapSave API → SaveFrom API → FbDownloader API
    """
    
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=60)
        self.max_retries = 2
    
    async def _download_file(self, url: str, filename: str) -> bool:
        """Download file from URL with size validation."""
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status != 200:
                        logger.error(f"Download failed: HTTP {response.status}")
                        return False
                    
                    # Check file size (max 49MB for Telegram)
                    content_length = response.headers.get('Content-Length')
                    if content_length and int(content_length) > 49 * 1024 * 1024:
                        logger.warning("File too large (>49MB)")
                        return False
                    
                    # Download in chunks
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
    
    async def _try_snapsave_api(self, url: str) -> Dict[str, any]:
        """
        Method 1: SnapSave API
        Best for: Public videos, share links
        """
        try:
            logger.info("🔄 Trying SnapSave API...")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': '*/*',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': 'https://snapsave.app',
                'Referer': 'https://snapsave.app/',
            }
            
            # SnapSave endpoint
            api_url = "https://www.snapsave.app/action.php?lang=en"
            payload = {'url': url}
            
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(api_url, data=payload, headers=headers) as response:
                    if response.status != 200:
                        logger.warning(f"SnapSave returned {response.status}")
                        return {"status": "error", "message": "SnapSave API failed"}
                    
                    html = await response.text()
                    
                    # Extract HD download link from HTML response
                    # SnapSave returns HTML with download links
                    hd_match = re.search(r'href="([^"]+)"[^>]*>Download.*?HD', html, re.IGNORECASE | re.DOTALL)
                    sd_match = re.search(r'href="([^"]+)"[^>]*>Download.*?SD', html, re.IGNORECASE | re.DOTALL)
                    
                    download_url = None
                    quality = "Unknown"
                    
                    if hd_match:
                        download_url = hd_match.group(1)
                        quality = "HD"
                    elif sd_match:
                        download_url = sd_match.group(1)
                        quality = "SD"
                    
                    if not download_url:
                        logger.warning("No download link found in SnapSave response")
                        return {"status": "error", "message": "No download link"}
                    
                    # Clean URL (remove HTML entities)
                    download_url = download_url.replace('&amp;', '&')
                    
                    filename = os.path.join(DOWNLOAD_DIR, f"fb_snapsave_{abs(hash(url))}.mp4")
                    
                    if await self._download_file(download_url, filename):
                        return {
                            "status": "success",
                            "file_path": filename,
                            "title": f"Facebook Video ({quality})",
                            "duration": 0,
                            "uploader": "Facebook"
                        }
        
        except Exception as e:
            logger.error(f"SnapSave error: {e}")
        
        return {"status": "error", "message": "SnapSave failed"}
    
    async def _try_savefrom_api(self, url: str) -> Dict[str, any]:
        """
        Method 2: SaveFrom API
        Best for: Various FB link formats
        """
        try:
            logger.info("🔄 Trying SaveFrom API...")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json',
            }
            
            # SaveFrom endpoint
            encoded_url = quote(url, safe='')
            api_url = f"https://api.savefrom.net/info.php?url={encoded_url}"
            
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(api_url, headers=headers) as response:
                    if response.status != 200:
                        logger.warning(f"SaveFrom returned {response.status}")
                        return {"status": "error", "message": "SaveFrom API failed"}
                    
                    data = await response.text()
                    
                    # Parse JSONP response
                    # SaveFrom returns: [{"url": "...", "quality": "hd", ...}]
                    json_match = re.search(r'\[(\{.+\})\]', data)
                    if not json_match:
                        logger.warning("Cannot parse SaveFrom response")
                        return {"status": "error", "message": "Parse error"}
                    
                    # Extract download URL
                    url_match = re.search(r'"url":"([^"]+)"', json_match.group(1))
                    if not url_match:
                        return {"status": "error", "message": "No video URL"}
                    
                    download_url = url_match.group(1).replace('\\/', '/')
                    
                    filename = os.path.join(DOWNLOAD_DIR, f"fb_savefrom_{abs(hash(url))}.mp4")
                    
                    if await self._download_file(download_url, filename):
                        return {
                            "status": "success",
                            "file_path": filename,
                            "title": "Facebook Video (SaveFrom)",
                            "duration": 0,
                            "uploader": "Facebook"
                        }
        
        except Exception as e:
            logger.error(f"SaveFrom error: {e}")
        
        return {"status": "error", "message": "SaveFrom failed"}
    
    async def _try_fbdownloader_api(self, url: str) -> Dict[str, any]:
        """
        Method 3: FbDownloader API
        Best for: Reels and watch links
        """
        try:
            logger.info("🔄 Trying FbDownloader API...")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': '*/*',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': 'https://fbdownloader.net',
                'Referer': 'https://fbdownloader.net/',
            }
            
            api_url = "https://v3.fdownloader.net/api/ajaxSearch?lang=en"
            payload = {
                'k_exp': '',
                'k_token': '',
                'q': url,
                'lang': 'en',
                'web': 'fdownloader.net',
                'v': 'v2',
                'w': ''
            }
            
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(api_url, data=payload, headers=headers) as response:
                    if response.status != 200:
                        logger.warning(f"FbDownloader returned {response.status}")
                        return {"status": "error", "message": "FbDownloader API failed"}
                    
                    data = await response.json()
                    
                    if data.get('status') != 'ok':
                        return {"status": "error", "message": "FbDownloader error"}
                    
                    html_data = data.get('data', '')
                    
                    # Extract HD or SD download link
                    hd_match = re.search(r'href="([^"]+)"[^>]*>Download.*?HD', html_data, re.IGNORECASE)
                    sd_match = re.search(r'href="([^"]+)"[^>]*>Download', html_data, re.IGNORECASE)
                    
                    download_url = None
                    if hd_match:
                        download_url = hd_match.group(1)
                    elif sd_match:
                        download_url = sd_match.group(1)
                    
                    if not download_url:
                        return {"status": "error", "message": "No download link"}
                    
                    filename = os.path.join(DOWNLOAD_DIR, f"fb_fbdl_{abs(hash(url))}.mp4")
                    
                    if await self._download_file(download_url, filename):
                        return {
                            "status": "success",
                            "file_path": filename,
                            "title": "Facebook Video (FbDownloader)",
                            "duration": 0,
                            "uploader": "Facebook"
                        }
        
        except Exception as e:
            logger.error(f"FbDownloader error: {e}")
        
        return {"status": "error", "message": "FbDownloader failed"}
    
    async def download(self, url: str, download_type: str = "video") -> Dict[str, any]:
        """
        Main download function with multiple fallback APIs.
        
        Priority:
        1. SnapSave API (best for share links)
        2. SaveFrom API (good compatibility)
        3. FbDownloader API (good for reels)
        
        Args:
            url: Facebook video URL
            download_type: "video" or "audio"
            
        Returns:
            Dictionary with status, file_path, and metadata
        """
        
        # Audio not supported for Facebook
        if download_type == "audio":
            return {
                "status": "error",
                "message": "Audio-only download not available for Facebook videos."
            }
        
        # Try SnapSave first (best success rate)
        logger.info("📱 Attempting Facebook download via SnapSave...")
        result = await self._try_snapsave_api(url)
        if result["status"] == "success":
            logger.info("✅ Downloaded via SnapSave API")
            return result
        
        # Fallback 1: SaveFrom
        logger.info("⚠️ SnapSave failed, trying SaveFrom...")
        result = await self._try_savefrom_api(url)
        if result["status"] == "success":
            logger.info("✅ Downloaded via SaveFrom API")
            return result
        
        # Fallback 2: FbDownloader
        logger.info("⚠️ SaveFrom failed, trying FbDownloader...")
        result = await self._try_fbdownloader_api(url)
        if result["status"] == "success":
            logger.info("✅ Downloaded via FbDownloader API")
            return result
        
        # All APIs failed
        logger.error("❌ All Facebook download methods failed")
        return {
            "status": "error",
            "message": (
                "Cannot download this Facebook video. Possible reasons:\n"
                "• Video is private or deleted\n"
                "• Age-restricted content\n"
                "• Regional restrictions\n"
                "• Login required\n\n"
                "Try copying the direct video link or use a different video."
            )
        }


# Global instance
facebook_downloader = FacebookDownloader()