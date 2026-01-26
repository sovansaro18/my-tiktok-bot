import asyncio
import logging
import os
import re
import uuid
from urllib.parse import parse_qs, urlparse

import aiohttp
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

    USER_AGENTS = [
        USER_AGENT,
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    ]

    EXTRACTOR_ARGS = {
        'youtube': {
            # Prefer clients that are typically more resilient to YouTube changes.
            # See yt-dlp docs for supported clients.
            'player_client': ['tv', 'android_sdkless', 'web_safari', 'ios', 'android'],
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

    def _normalize_youtube_url(self, url: str) -> str:
        """Normalize YouTube Shorts URLs into watch?v= URLs for better extractor stability."""
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or '').lower()
            path = parsed.path or ''

            if host.endswith('youtube.com') and path.startswith('/shorts/'):
                video_id = path.split('/shorts/', 1)[1].split('/', 1)[0]
                if video_id:
                    qs = parse_qs(parsed.query)
                    # preserve 'si' if present (not required, but harmless)
                    si = qs.get('si', [None])[0]
                    new = f"https://www.youtube.com/watch?v={video_id}"
                    if si:
                        new += f"&si={si}"
                    return new
        except Exception:
            return url
        return url

    async def _resolve_redirect(self, url: str) -> str:
        """Resolve URL shorteners (e.g., pin.it) to their final destination."""
        timeout = aiohttp.ClientTimeout(total=20)
        headers = {
            'User-Agent': self.USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            try:
                async with session.get(url, allow_redirects=True) as resp:
                    return str(resp.url)
            except Exception:
                return url

    async def _download_direct_mp4(self, mp4_url: str, title: str = "pinterest") -> Dict[str, Any]:
        """Download a direct MP4 URL via aiohttp (used for Pinterest fallback)."""
        timeout = aiohttp.ClientTimeout(total=120)
        headers = {'User-Agent': self.USER_AGENT}
        file_id = uuid.uuid4().hex
        out_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.mp4")

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            # Pre-check size when possible
            try:
                async with session.head(mp4_url, allow_redirects=True) as head:
                    size = head.headers.get('Content-Length')
                    if size and size.isdigit() and int(size) > MAX_FILE_SIZE:
                        size_mb = int(size) / 1024 / 1024
                        limit_mb = MAX_FILE_SIZE / 1024 / 1024
                        return {
                            "status": "error",
                            "message": f"File too large: {size_mb:.1f}MB (limit: {limit_mb:.0f}MB)",
                            "size": int(size),
                        }
            except Exception:
                pass

            async with session.get(mp4_url, allow_redirects=True) as resp:
                if resp.status >= 400:
                    return {"status": "error", "message": f"HTTP {resp.status} while fetching media"}
                total = 0
                with open(out_path, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_FILE_SIZE:
                            try:
                                os.remove(out_path)
                            except Exception:
                                pass
                            limit_mb = MAX_FILE_SIZE / 1024 / 1024
                            return {"status": "error", "message": f"File too large (limit: {limit_mb:.0f}MB)"}
                        f.write(chunk)

        return {
            "status": "success",
            "file_path": out_path,
            "title": title or "Pinterest Video",
            "duration": 0,
            "uploader": "Pinterest",
        }

    async def _download_pinterest(self, url: str, download_type: str = "video") -> Dict[str, Any]:
        """Pinterest fallback downloader.

        Pinterest often blocks automated API calls used by yt-dlp. This fallback tries to:
        1) resolve pin.it short links
        2) fetch the pin HTML page
        3) extract a direct mp4 URL (v.pinimg.com / video.pinimg.com)
        """
        if download_type != "video":
            return {"status": "error", "message": "Pinterest supports video only"}

        final_url = await self._resolve_redirect(url)
        # Try to construct a canonical pin URL if an ID is visible
        m = re.search(r"/pin/(\d+)", final_url)
        if m:
            final_url = f"https://www.pinterest.com/pin/{m.group(1)}/"

        timeout = aiohttp.ClientTimeout(total=25)
        headers = {
            'User-Agent': self.USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            try:
                async with session.get(final_url, allow_redirects=True) as resp:
                    html = await resp.text(errors='ignore')
            except Exception as e:
                return {"status": "error", "message": f"Pinterest fetch failed: {e}"}

        # Try to get a title
        title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip() if title_match else "Pinterest Video"

        # Extract direct MP4 URL
        mp4_candidates = []
        mp4_candidates += re.findall(r"https://v\.pinimg\.com[^\"\\\s]+\.mp4", html)
        mp4_candidates += re.findall(r"https://video\.pinimg\.com[^\"\\\s]+\.mp4", html)
        mp4_candidates += re.findall(r"https://i\.pinimg\.com[^\"\\\s]+\.mp4", html)

        if not mp4_candidates:
            # Sometimes URLs are escaped in JSON
            mp4_candidates += [
                u.replace('\\u002F', '/').replace('\\/', '/')
                for u in re.findall(r"https:\\/\\/(?:v|video|i)\\.pinimg\\.com[^\"]+?\\.mp4", html)
            ]

        # If no MP4 found, try to extract an HLS manifest (.m3u8) and let yt-dlp/ffmpeg handle it
        m3u8_candidates = []
        if not mp4_candidates:
            m3u8_candidates += re.findall(r"https://(?:v|video|i)\.pinimg\.com[^\"\s]+\.m3u8", html)
            if not m3u8_candidates:
                m3u8_candidates += [
                    u.replace('\\u002F', '/').replace('\\/', '/')
                    for u in re.findall(r"https:\\/\\/(?:v|video|i)\.pinimg\.com[^\"]+?\.m3u8", html)
                ]

        if not mp4_candidates and m3u8_candidates:
            # Use yt-dlp on a direct manifest URL (avoids Pinterest extractor)
            return await self.download_with_ytdlp(m3u8_candidates[0], download_type)

        if not mp4_candidates:
            return {
                "status": "error",
                "message": (
                    "Pinterest is blocking automated downloads right now. "
                    "Try again later or provide cookies.txt (COOKIES_FILE) for authenticated access."
                ),
            }

        # Use the first candidate
        mp4_url = mp4_candidates[0]
        return await self._download_direct_mp4(mp4_url, title=title)

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
                elif "Failed to extract any player response" in error_msg:
                    return {
                        "status": "error",
                        "message": "YouTube បានប្តូររចនាសម្ព័ន្ធ។ សូមព្យាយាមម្ដងទៀតក្រោយ (bot នឹង update ឲ្យស្វ័យប្រវត្តិលើ Render)។",
                    }
                else:
                    return {"status": "error", "message": f"Download failed: {error_msg[:200]}"}
                    
            except Exception as e:
                logger.error(f"❌ Error: {e}", exc_info=True)
                return {"status": "error", "message": f"Error: {str(e)[:200]}"}

    async def download_with_ytdlp(self, url: str, type: str = "video") -> Dict[str, Any]:
        """Download using yt-dlp with pre-download size check."""
        loop = asyncio.get_running_loop()

        # Normalize some URLs first
        if self._detect_platform(url) == 'youtube':
            url = self._normalize_youtube_url(url)

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
        for attempt in range(1, self.max_retries + 1):
            # Refresh options to allow per-attempt tweaks
            opts = self._get_opts(type, url)
            # Rotate user-agent per attempt
            ua = self.USER_AGENTS[(attempt - 1) % len(self.USER_AGENTS)]
            opts.setdefault('http_headers', {})['User-Agent'] = ua

            # Rotate YouTube client order per attempt (local copy - avoid global mutation)
            if self._detect_platform(url) == 'youtube':
                clients = [
                    ['tv', 'android_sdkless', 'web_safari'],
                    ['android_sdkless', 'tv', 'ios'],
                    ['ios', 'android_sdkless', 'tv'],
                ]
                extractor_args = dict(opts.get('extractor_args') or {})
                yt_args = dict(extractor_args.get('youtube') or {})
                yt_args['player_client'] = clients[(attempt - 1) % len(clients)]
                extractor_args['youtube'] = yt_args
                opts['extractor_args'] = extractor_args
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
        
        # Route 3: Pinterest → try direct fallback first, then yt-dlp
        elif platform == 'pinterest':
            logger.info("📌 Routing to Pinterest direct fallback")
            direct = await self._download_pinterest(url, type)
            if direct.get("status") != "success":
                logger.warning(f"⚠️ Pinterest direct fallback failed: {direct.get('message')}")
            return direct

        # Route 4: All other platforms → yt-dlp
        else:
            logger.info(f"📹 Using yt-dlp for {platform}")
            return await self.download_with_ytdlp(url, type)


downloader = Downloader()