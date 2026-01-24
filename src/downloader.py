import asyncio
import logging
import os
import yt_dlp
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional, Any

# កំណត់ការ Log
logger = logging.getLogger(__name__)

# កំណត់ Folder សម្រាប់ទុក File បណ្តោះអាសន្ន
DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# Cookies file path (optional - សម្រាប់ age-restricted content)
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.txt")


class Downloader:
    """
    Async-compatible video/audio downloader using yt-dlp.
    Supports: YouTube, TikTok, Facebook, Instagram, Twitter/X និង ច្រើនទៀត។
    """

    # User-Agent សម្រាប់គេចពី bot detection
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    # Platform-specific extractor arguments
    EXTRACTOR_ARGS = {
        'youtube': {
            'player_client': ['ios', 'web', 'android'],
            'skip': ['dash', 'hls'],
        },
        'tiktok': {
            'api_hostname': 'api22-normal-c-useast2a.tiktokv.com',
        },
        'instagram': {
            'api_hostname': 'i.instagram.com',
        }
    }

    def __init__(self, max_workers: int = 2):
        """
        Initialize Downloader with a thread pool.
        
        Args:
            max_workers: Maximum number of concurrent downloads allowed.
        """
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.max_retries = 3
        self._shutdown = False

    def shutdown(self, wait: bool = True) -> None:
        """
        Shutdown the thread pool executor.
        
        Args:
            wait: If True, wait for all pending tasks to complete.
        """
        if not self._shutdown:
            logger.info("🔒 Shutting down downloader thread pool...")
            self.executor.shutdown(wait=wait)
            self._shutdown = True
            logger.info("✅ Downloader thread pool shut down.")

    def __del__(self):
        """Cleanup on garbage collection."""
        if not self._shutdown:
            self.shutdown(wait=False)

    def _detect_platform(self, url: str) -> str:
        """
        Detect platform from URL for platform-specific optimizations.
        
        Args:
            url: The video URL.
            
        Returns:
            Platform name (youtube, tiktok, facebook, instagram, twitter, other)
        """
        url_lower = url.lower()
        
        if any(domain in url_lower for domain in ['youtube.com', 'youtu.be', 'youtube.com/shorts']):
            return 'youtube'
        elif any(domain in url_lower for domain in ['tiktok.com', 'vm.tiktok.com']):
            return 'tiktok'
        elif any(domain in url_lower for domain in ['facebook.com', 'fb.watch', 'fb.com']):
            return 'facebook'
        elif any(domain in url_lower for domain in ['instagram.com', 'instagr.am']):
            return 'instagram'
        elif any(domain in url_lower for domain in ['twitter.com', 'x.com', 't.co']):
            return 'twitter'
        
        return 'other'

    def _get_opts(self, download_type: str = "video", url: str = "") -> Dict[str, Any]:
        """
        Configure yt-dlp options based on download type and platform.
        
        Args:
            download_type: "video" or "audio"
            url: The URL to download (for platform detection)
            
        Returns:
            Dictionary of yt-dlp options.
        """
        platform = self._detect_platform(url)
        logger.info(f"🔍 Detected platform: {platform}")

        # Base options សម្រាប់គ្រប់ platforms
        common_opts: Dict[str, Any] = {
            'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
            'quiet': False,  # ✅ Changed to False for better debugging
            'no_warnings': False,  # ✅ Changed to False to see warnings
            'noplaylist': True,
            'max_filesize': 49 * 1024 * 1024,  # Limit 49MB (Telegram limit is 50MB)
            'geo_bypass': True,
            'socket_timeout': 30,
            'retries': 10,  # ✅ Increased from 5 to 10
            'fragment_retries': 10,  # ✅ Increased from 5 to 10
            
            # ✅ Added verbose logging for debugging
            'verbose': True,
            'logger': logger,
            
            # HTTP Headers សម្រាប់គេចពី bot detection
            'http_headers': {
                'User-Agent': self.USER_AGENT,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate, br',  # ✅ Added 'br' (Brotli)
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0',
            },
            
            # Extractor arguments
            'extractor_args': self.EXTRACTOR_ARGS,
            
            # Sleep between requests ដើម្បីជៀសវាង rate limiting
            'sleep_interval_requests': 1,
            
            # ✅ Added to handle errors better
            'ignoreerrors': False,  # Don't skip errors
            'no_color': True,  # Clean logs
        }

        # បន្ថែម cookies file ប្រសិនបើមាន
        if os.path.exists(COOKIES_FILE):
            common_opts['cookiefile'] = COOKIES_FILE
            logger.info(f"🍪 Using cookies from: {COOKIES_FILE}")

        # Platform-specific optimizations
        if platform == 'youtube':
            common_opts.update({
                'age_limit': None,  # Allow all content
                'sleep_interval': 2,  # Longer delay for YouTube
                'sleep_interval_subtitles': 1,
            })
            
        elif platform == 'tiktok':
            # TikTok-specific headers
            common_opts['http_headers'].update({
                'Referer': 'https://www.tiktok.com/',
                'Origin': 'https://www.tiktok.com',
            })
            # ✅ Add format for TikTok
            common_opts['format'] = 'best'
            
        elif platform == 'instagram':
            common_opts['http_headers'].update({
                'Referer': 'https://www.instagram.com/',
                'Origin': 'https://www.instagram.com',
                'X-Requested-With': 'XMLHttpRequest',
            })
            # ✅ Add format for Instagram
            common_opts['format'] = 'best'
            
        elif platform == 'facebook':
            common_opts['http_headers'].update({
                'Referer': 'https://www.facebook.com/',
                'Origin': 'https://www.facebook.com',
            })
            # ✅ Add format for Facebook
            common_opts['format'] = 'best'
            
        elif platform == 'twitter':
            common_opts['http_headers'].update({
                'Referer': 'https://twitter.com/',
                'Origin': 'https://twitter.com',
            })

        # Format options based on download type
        if download_type == "audio":
            common_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'm4a',
                }]
            })
        else:
            # ✅ Only override format if not already set by platform
            if 'format' not in common_opts or platform == 'youtube':
                if platform == 'youtube':
                    common_opts['format'] = (
                        'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/'
                        'bestvideo[height<=1080]+bestaudio/'
                        'best[height<=1080][ext=mp4]/'
                        'best[ext=mp4]/'
                        'best'
                    )
                    common_opts['merge_output_format'] = 'mp4'

        return common_opts

    def _download_sync(self, url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
        """
        Synchronous download function to run in thread pool.
        
        Args:
            url: The video/audio URL to download.
            opts: yt-dlp options dictionary.
            
        Returns:
            Dictionary with status, file_path, and metadata.
        """
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                logger.info(f"🔄 Starting extraction for: {url}")
                info = ydl.extract_info(url, download=True)
                
                if not info:
                    return {"status": "error", "message": "Failed to extract video information"}
                
                if 'entries' in info:
                    info = info['entries'][0]

                filename = ydl.prepare_filename(info)
                
                if opts.get('postprocessors'):
                    base, _ = os.path.splitext(filename)
                    filename = f"{base}.m4a"

                # ✅ Check if file actually exists
                if not os.path.exists(filename):
                    return {"status": "error", "message": "Download completed but file not found"}

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
                
                # ✅ Better error categorization
                if "File is larger than" in error_msg or "too large" in error_msg.lower():
                    return {"status": "error", "message": "File too large (>49MB). Try a shorter video."}
                elif "Video unavailable" in error_msg or "Private video" in error_msg:
                    return {"status": "error", "message": "Video is unavailable or private"}
                elif "Sign in to confirm your age" in error_msg:
                    return {"status": "error", "message": "Age-restricted video. Please provide cookies.txt file."}
                elif "HTTP Error 429" in error_msg or "Too Many Requests" in error_msg:
                    return {"status": "error", "message": "Rate limited. Please try again in a few minutes."}
                elif "unable to extract" in error_msg.lower():
                    return {"status": "error", "message": "Failed to extract video. URL may be invalid."}
                else:
                    return {"status": "error", "message": f"Download failed: {error_msg[:200]}"}
                    
            except Exception as e:
                logger.error(f"❌ Unexpected error: {str(e)}", exc_info=True)
                return {"status": "error", "message": f"Unexpected error: {str(e)[:200]}"}

    async def download(self, url: str, type: str = "video") -> Dict[str, Any]:
        """
        Async wrapper for downloading media with Retry Logic.
        
        Args:
            url: The video/audio URL to download.
            type: "video" or "audio"
            
        Returns:
            Dictionary with status, file_path, and metadata.
        """
        opts = self._get_opts(type, url)
        loop = asyncio.get_running_loop()

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(f"⬇️ Downloading ({type}) [Attempt {attempt}/{self.max_retries}]: {url}")
                
                # Run blocking code in thread pool
                result = await loop.run_in_executor(
                    self.executor, 
                    self._download_sync, 
                    url, 
                    opts
                )

                if result["status"] == "success":
                    logger.info(f"✅ Download complete: {result['file_path']}")
                    return result
                
                # ✅ Don't retry for these errors
                non_retryable_errors = [
                    "File too large",
                    "unavailable or private",
                    "Age-restricted",
                    "invalid"
                ]
                
                if any(err in result["message"] for err in non_retryable_errors):
                    logger.warning(f"⚠️ Non-retryable error: {result['message']}")
                    return result

                logger.warning(f"⚠️ Attempt {attempt}/{self.max_retries} failed: {result['message']}")

            except Exception as e:
                logger.error(f"❌ Critical error in download wrapper (attempt {attempt}): {e}", exc_info=True)
                if attempt == self.max_retries:
                    return {"status": "error", "message": "Internal server error. Please try again."}
            
            # ✅ Exponential backoff
            if attempt < self.max_retries:
                sleep_time = min(2 ** attempt, 10)  # Max 10 seconds
                logger.info(f"⏳ Waiting {sleep_time}s before retry...")
                await asyncio.sleep(sleep_time)

        return {"status": "error", "message": f"Failed after {self.max_retries} attempts. Please try again later."}

downloader = Downloader()