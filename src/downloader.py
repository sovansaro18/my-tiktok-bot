import asyncio
import logging
import os
import yt_dlp
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional, Any

from src.config import MAX_FILE_SIZE

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.txt")


class Downloader:
    """
    Hybrid video/audio downloader with pre-download size checking.
    
    Platform Priority:
    - TikTok: Cobalt API → yt-dlp
    - Facebook: Multi-API → yt-dlp
    - Others: yt-dlp
    """

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    EXTRACTOR_ARGS = {
        'youtube': {
            'player_client': ['ios', 'web', 'android'],
            'skip': ['dash', 'hls'],
        },
        'instagram': {
            'api_hostname': 'i.instagram.com',
        }
    }

    def __init__(self, max_workers: int = 2):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.max_retries = 3
        self._shutdown = False

    def shutdown(self, wait: bool = True) -> None:
        if not self._shutdown:
            logger.info("🔒 Shutting down downloader thread pool...")
            self.executor.shutdown(wait=wait)
            self._shutdown = True
            logger.info("✅ Downloader thread pool shut down.")

    def __del__(self):
        if not self._shutdown:
            self.shutdown(wait=False)

    def _detect_platform(self, url: str) -> str:
        """Detect platform from URL."""
        url_lower = url.lower()
        
        if any(domain in url_lower for domain in ['youtube.com', 'youtu.be']):
            return 'youtube'
        elif any(domain in url_lower for domain in ['tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com']):
            return 'tiktok'
        elif any(domain in url_lower for domain in ['facebook.com', 'fb.watch', 'fb.com']):
            return 'facebook'
        elif any(domain in url_lower for domain in ['instagram.com', 'instagr.am']):
            return 'instagram'
        elif any(domain in url_lower for domain in ['twitter.com', 'x.com', 't.co']):
            return 'twitter'
        elif any(domain in url_lower for domain in ['pinterest.com', 'pin.it']):
            return 'pinterest'
        
        return 'other'

    def _get_opts(self, download_type: str = "video", url: str = "", check_only: bool = False) -> Dict[str, Any]:
        """Get yt-dlp options based on platform and download type."""
        platform = self._detect_platform(url)
        logger.info(f"🔍 Platform: {platform}")

        common_opts: Dict[str, Any] = {
            'quiet': False,
            'no_warnings': False,
            'noplaylist': True,
            'socket_timeout': 30,
            'retries': 10,
            'fragment_retries': 10,
            'verbose': True,
            'logger': logger,
            'nocheckcertificate': True,
            
            'http_headers': {
                'User-Agent': self.USER_AGENT,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Cache-Control': 'max-age=0',
            },
            
            'extractor_args': self.EXTRACTOR_ARGS,
            'sleep_interval_requests': 1,
            'ignoreerrors': False,
            'no_color': True,
        }

        # If only checking metadata, don't download
        if not check_only:
            common_opts['outtmpl'] = f'{DOWNLOAD_DIR}/%(id)s.%(ext)s'
            common_opts['max_filesize'] = MAX_FILE_SIZE

        if os.path.exists(COOKIES_FILE):
            common_opts['cookiefile'] = COOKIES_FILE
            logger.info(f"🍪 Using cookies from: {COOKIES_FILE}")

        if platform == 'youtube':
            common_opts.update({
                'age_limit': None,
                'sleep_interval': 2,
                'geo_bypass': True,
            })
            
        elif platform == 'instagram':
            common_opts.update({
                'http_headers': {
                    'User-Agent': self.USER_AGENT,
                    'Accept': '*/*',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Referer': 'https://www.instagram.com/',
                    'Origin': 'https://www.instagram.com',
                    'X-IG-App-ID': '936619743392459',
                },
                'format': 'best',
            })
            
        elif platform == 'facebook':
            common_opts.update({
                'http_headers': {
                    'User-Agent': self.USER_AGENT,
                    'Referer': 'https://www.facebook.com/',
                    'Origin': 'https://www.facebook.com',
                },
                'format': 'best',
            })

        # Format selection
        if download_type == "audio":
            common_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'm4a',
                }] if not check_only else []
            })
        elif platform == 'youtube':
            common_opts['format'] = (
                'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/'
                'bestvideo[height<=1080]+bestaudio/'
                'best[height<=1080][ext=mp4]/'
                'best[ext=mp4]/'
                'best'
            )
            if not check_only:
                common_opts['merge_output_format'] = 'mp4'

        return common_opts

    def _check_size_sync(self, url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
        """
        Check video size WITHOUT downloading.
        Extract metadata only to verify file size.
        """
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                logger.info(f"🔍 Checking video size for: {url}")
                info = ydl.extract_info(url, download=False)
                
                if not info:
                    return {"status": "error", "message": "Cannot extract video info"}
                
                if 'entries' in info:
                    if not info['entries']:
                        return {"status": "error", "message": "No videos found"}
                    info = info['entries'][0]
                
                # Get file size
                filesize = info.get('filesize') or info.get('filesize_approx')
                
                if filesize and filesize > MAX_FILE_SIZE:
                    size_mb = filesize / 1024 / 1024
                    limit_mb = MAX_FILE_SIZE / 1024 / 1024
                    return {
                        "status": "error",
                        "message": f"File too large: {size_mb:.1f}MB (limit: {limit_mb:.0f}MB)",
                        "size": filesize
                    }
                
                # Return metadata
                return {
                    "status": "ok",
                    "size": filesize,
                    "title": info.get('title', 'Unknown'),
                    "duration": info.get('duration', 0)
                }
                
            except Exception as e:
                logger.error(f"❌ Size check error: {e}")
                # If size check fails, allow download (will be caught later)
                return {"status": "ok", "size": None}

    def _download_sync(self, url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
        """Synchronous yt-dlp download (runs in thread pool)."""
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                logger.info(f"🔄 Downloading: {url}")
                info = ydl.extract_info(url, download=True)
                
                if not info:
                    return {"status": "error", "message": "Cannot extract video info"}
                
                if 'entries' in info:
                    info = info['entries'][0]

                filename = ydl.prepare_filename(info)
                
                if opts.get('postprocessors'):
                    base, _ = os.path.splitext(filename)
                    filename = f"{base}.m4a"

                if not os.path.exists(filename):
                    return {"status": "error", "message": "File not found after download"}

                return {
                    "status": "success",
                    "file_path": filename,
                    "title": info.get('title', 'Unknown'),
                    "duration": info.get('duration', 0),
                    "uploader": info.get('uploader', 'Unknown')
                }

            except yt_dlp.utils.DownloadError as e:
                error_msg = str(e)
                logger.error(f"❌ DownloadError: {error_msg}")
                
                if "File is larger than" in error_msg or "too large" in error_msg.lower():
                    return {"status": "error", "message": "File too large (>49MB)"}
                elif "Video unavailable" in error_msg or "Private video" in error_msg:
                    return {"status": "error", "message": "Video unavailable or private"}
                elif "Sign in to confirm" in error_msg:
                    return {"status": "error", "message": "Age-restricted. Need cookies.txt"}
                elif "HTTP Error 429" in error_msg:
                    return {"status": "error", "message": "Rate limited. Try in 5 minutes"}
                elif "HTTP Error 403" in error_msg:
                    return {"status": "error", "message": "Access forbidden. May be region-blocked"}
                else:
                    return {"status": "error", "message": f"Download failed: {error_msg[:200]}"}
                    
            except Exception as e:
                logger.error(f"❌ Error: {e}", exc_info=True)
                return {"status": "error", "message": f"Error: {str(e)[:200]}"}

    async def download_with_ytdlp(self, url: str, type: str = "video") -> Dict[str, Any]:
        """Download using yt-dlp with pre-download size check."""
        loop = asyncio.get_running_loop()

        # STEP 1: Check file size first (without downloading)
        logger.info("📏 Checking file size before download...")
        check_opts = self._get_opts(type, url, check_only=True)
        
        size_check = await loop.run_in_executor(
            self.executor,
            self._check_size_sync,
            url,
            check_opts
        )
        
        if size_check["status"] == "error":
            return size_check
        
        # STEP 2: Proceed with actual download
        opts = self._get_opts(type, url)

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(f"⬇️ yt-dlp download [Attempt {attempt}/{self.max_retries}]")
                
                result = await loop.run_in_executor(
                    self.executor, 
                    self._download_sync, 
                    url, 
                    opts
                )

                if result["status"] == "success":
                    return result
                
                # Don't retry certain errors
                non_retryable = [
                    "File too large", "unavailable", "private",
                    "Age-restricted", "region-blocked"
                ]
                
                if any(err in result["message"] for err in non_retryable):
                    return result

                logger.warning(f"⚠️ Attempt {attempt} failed: {result['message']}")

            except Exception as e:
                logger.error(f"❌ Error (attempt {attempt}): {e}")
                if attempt == self.max_retries:
                    return {"status": "error", "message": "System error"}
            
            if attempt < self.max_retries:
                await asyncio.sleep(min(2 ** attempt, 10))

        return {"status": "error", "message": f"Failed after {self.max_retries} attempts"}

    async def download(self, url: str, type: str = "video") -> Dict[str, Any]:
        """
        Main download function with intelligent platform routing.
        """
        
        platform = self._detect_platform(url)
        
        # Route 1: TikTok → Cobalt API
        if platform == 'tiktok':
            logger.info("🎵 Routing to Cobalt API for TikTok")
            try:
                from src.cobalt_api import cobalt_downloader
                result = await cobalt_downloader.download(url, type)
                
                if result["status"] == "success":
                    return result
                
                logger.warning("⚠️ Cobalt failed, falling back to yt-dlp...")
                return await self.download_with_ytdlp(url, type)
                
            except ImportError:
                logger.error("❌ cobalt_api.py not found! Using yt-dlp")
                return await self.download_with_ytdlp(url, type)
            except Exception as e:
                logger.error(f"❌ Cobalt API error: {e}")
                logger.info("⚠️ Falling back to yt-dlp...")
                return await self.download_with_ytdlp(url, type)
        
        # Route 2: Facebook → Facebook Multi-API System
        elif platform == 'facebook':
            logger.info("📱 Routing to Facebook Multi-API System")
            try:
                from src.facebook_api import facebook_downloader
                result = await facebook_downloader.download(url, type)
                
                if result["status"] == "success":
                    logger.info("✅ Facebook API succeeded!")
                    return result
                
                logger.warning("⚠️ All Facebook APIs failed, trying yt-dlp as last resort...")
                ytdlp_result = await self.download_with_ytdlp(url, type)
                
                if ytdlp_result["status"] == "error":
                    return result
                
                return ytdlp_result
                
            except ImportError:
                logger.error("❌ facebook_api.py not found! Using yt-dlp only")
                return await self.download_with_ytdlp(url, type)
            except Exception as e:
                logger.error(f"❌ Facebook API error: {e}")
                logger.info("⚠️ Falling back to yt-dlp...")
                return await self.download_with_ytdlp(url, type)
        
        # Route 3: All other platforms → yt-dlp
        else:
            logger.info(f"📹 Using yt-dlp for {platform}")
            return await self.download_with_ytdlp(url, type)


downloader = Downloader()