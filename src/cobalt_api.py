import asyncio
import logging
import os
import aiohttp
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)


class CobaltDownloader:
    """
    TikTok downloader using Cobalt API.
    Cobalt is better at bypassing TikTok's bot protection.
    """
    
    # Public Cobalt API instances (rotate if one fails)
    API_ENDPOINTS = [
        "https://api.cobalt.tools/api/json",
        "https://co.wuk.sh/api/json",
    ]
    
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=60)
        self.max_retries = 2
    
    async def _download_file(self, url: str, filename: str) -> bool:
        """
        Download file from URL to local storage.
        
        Args:
            url: Direct download URL
            filename: Local filename to save
            
        Returns:
            True if successful
        """
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        logger.error(f"Failed to download file: HTTP {response.status}")
                        return False
                    
                    # Check file size (max 49MB)
                    content_length = response.headers.get('Content-Length')
                    if content_length and int(content_length) > 49 * 1024 * 1024:
                        logger.warning("File too large (>49MB)")
                        return False
                    
                    # Download file
                    with open(filename, 'wb') as f:
                        async for chunk in response.content.iter_chunked(1024 * 1024):  # 1MB chunks
                            f.write(chunk)
                    
                    logger.info(f"✅ File downloaded: {filename}")
                    return True
                    
        except asyncio.TimeoutError:
            logger.error("Download timeout")
            return False
        except Exception as e:
            logger.error(f"Download error: {e}")
            return False
    
    async def download(self, url: str, download_type: str = "video") -> Dict[str, any]:
        """
        Download TikTok video using Cobalt API.
        
        Args:
            url: TikTok video URL
            download_type: "video" or "audio"
            
        Returns:
            Dictionary with status, file_path, and metadata
        """
        # Cobalt API request payload
        payload = {
            "url": url,
            "vCodec": "h264",  # Compatible with most devices
            "vQuality": "1080",  # Max quality
            "aFormat": "mp3" if download_type == "audio" else "best",
            "isAudioOnly": download_type == "audio",
            "disableMetadata": False,
        }
        
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        
        # Try each API endpoint
        for endpoint in self.API_ENDPOINTS:
            for attempt in range(1, self.max_retries + 1):
                try:
                    logger.info(f"🔄 Cobalt API request [Endpoint: {endpoint}, Attempt: {attempt}]")
                    
                    async with aiohttp.ClientSession(timeout=self.timeout) as session:
                        async with session.post(endpoint, json=payload, headers=headers) as response:
                            
                            if response.status != 200:
                                logger.warning(f"API returned status {response.status}")
                                continue
                            
                            data = await response.json()
                            
                            # Check response status
                            status = data.get("status")
                            
                            if status == "error":
                                error_text = data.get("text", "Unknown error")
                                logger.error(f"Cobalt API error: {error_text}")
                                
                                # Check if it's a retryable error
                                if "rate limit" in error_text.lower():
                                    if attempt < self.max_retries:
                                        await asyncio.sleep(2)
                                        continue
                                
                                return {
                                    "status": "error",
                                    "message": f"API error: {error_text}"
                                }
                            
                            if status == "redirect" or status == "tunnel":
                                # Direct download URL available
                                download_url = data.get("url")
                                
                                if not download_url:
                                    logger.error("No download URL in response")
                                    continue
                                
                                # Generate filename
                                file_ext = "mp3" if download_type == "audio" else "mp4"
                                filename = os.path.join(DOWNLOAD_DIR, f"tiktok_{hash(url)}_{int(asyncio.get_event_loop().time())}.{file_ext}")
                                
                                # Download the file
                                success = await self._download_file(download_url, filename)
                                
                                if not success:
                                    if os.path.exists(filename):
                                        os.remove(filename)
                                    
                                    if attempt < self.max_retries:
                                        await asyncio.sleep(2)
                                        continue
                                    
                                    return {
                                        "status": "error",
                                        "message": "Failed to download file from Cobalt"
                                    }
                                
                                # Success!
                                return {
                                    "status": "success",
                                    "file_path": filename,
                                    "title": "TikTok Video",  # Cobalt doesn't provide title
                                    "duration": 0,
                                    "uploader": "TikTok"
                                }
                            
                            elif status == "picker":
                                # Multiple videos available (carousel)
                                picker_items = data.get("picker", [])
                                if picker_items:
                                    # Download first video
                                    download_url = picker_items[0].get("url")
                                    
                                    if download_url:
                                        file_ext = "mp4"
                                        filename = os.path.join(DOWNLOAD_DIR, f"tiktok_{hash(url)}_{int(asyncio.get_event_loop().time())}.{file_ext}")
                                        
                                        success = await self._download_file(download_url, filename)
                                        
                                        if success:
                                            return {
                                                "status": "success",
                                                "file_path": filename,
                                                "title": "TikTok Carousel (First Video)",
                                                "duration": 0,
                                                "uploader": "TikTok"
                                            }
                                
                                return {
                                    "status": "error",
                                    "message": "Multiple videos found. Showing first one."
                                }
                            
                            else:
                                logger.warning(f"Unknown Cobalt response status: {status}")
                                continue
                    
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout on attempt {attempt}")
                    if attempt < self.max_retries:
                        await asyncio.sleep(2)
                        continue
                    
                except aiohttp.ClientError as e:
                    logger.error(f"Network error: {e}")
                    if attempt < self.max_retries:
                        await asyncio.sleep(2)
                        continue
                    
                except Exception as e:
                    logger.error(f"Unexpected error: {e}", exc_info=True)
                    if attempt < self.max_retries:
                        await asyncio.sleep(2)
                        continue
            
            # If we get here, all retries failed for this endpoint
            logger.warning(f"All retries failed for endpoint: {endpoint}")
        
        # All endpoints failed
        return {
            "status": "error",
            "message": "All Cobalt API endpoints failed. TikTok may be blocking access. Try again later."
        }


# Global instance
cobalt_downloader = CobaltDownloader()