import asyncio
import logging
import os
import re
import uuid
import time
import shutil
from urllib.parse import parse_qs, urlparse

import aiohttp
import yt_dlp
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional, Any, List

from src.config import MAX_FILE_SIZE

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp"}


class Downloader:
    """
    Hybrid video/audio downloader with pre-download size checking.

    Platform Priority:
    - TikTok  : Cobalt API v7 → yt-dlp (H.264 forced)
    - Facebook: Multi-API    → yt-dlp
    - Pinterest: Direct MP4  → yt-dlp
    - Others  : yt-dlp
    """

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    USER_AGENTS = [
        USER_AGENT,
        (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.6 Mobile/15E148 Safari/604.1"
        ),
    ]

    EXTRACTOR_ARGS = {
        "youtube": {
            # Rotate player clients per retry attempt
            "player_client": ["tv", "android_sdkless", "web_safari", "ios", "android"],
            "skip": ["dash", "hls"],
        },
        "instagram": {
            "api_hostname": "i.instagram.com",
        },
    }

    def __init__(self, max_workers: int = 4):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.max_retries = 3
        self._shutdown = False
        # ✅ FIX: Copy cookies to writable /tmp/ at startup
        # Render.com mounts /etc/secrets/ as read-only → yt-dlp crashes
        self._cookies_file = self._prepare_cookies_file()

    def _prepare_cookies_file(self) -> Optional[str]:
        """
        Copy cookies from read-only mount (e.g. Render /etc/secrets/)
        to writable /tmp/ so yt-dlp can read without crashing.

        Returns writable path, or None if no cookies found.
        """
        candidates = [
            os.getenv("YTDLP_COOKIES"),
            os.getenv("COOKIES_FILE"),
            "/etc/secrets/cookies.txt",
            "cookies.txt",
        ]

        source = None
        for path in candidates:
            if path and os.path.exists(path) and os.path.getsize(path) > 0:
                source = path
                break

        if not source:
            logger.warning("⚠️ No cookies.txt found — YouTube may fail bot detection")
            return None

        # If already in a writable location, use directly
        writable_prefixes = ("/tmp", "/home", "/app", DOWNLOAD_DIR)
        if any(source.startswith(p) for p in writable_prefixes):
            logger.info(f"🍪 Cookies already writable: {source}")
            return source

        # Copy to /tmp/ to avoid read-only filesystem crash
        tmp_cookies = "/tmp/yt_cookies.txt"
        try:
            shutil.copy2(source, tmp_cookies)
            os.chmod(tmp_cookies, 0o600)
            logger.info(f"🍪 Cookies copied: {source} → {tmp_cookies}")
            return tmp_cookies
        except Exception as e:
            logger.error(f"❌ Failed to copy cookies to /tmp/: {e}")
            # Return original path — yt-dlp may still work read-only
            return source

    def shutdown(self, wait: bool = True) -> None:
        if not self._shutdown:
            logger.info("🔒 Shutting down downloader thread pool...")
            self.executor.shutdown(wait=wait)
            self._shutdown = True
            logger.info("✅ Downloader thread pool shut down.")

    def __del__(self):
        if not self._shutdown:
            self.shutdown(wait=False)

    # ─────────────────────────────────────────────
    # Platform Detection & URL Normalisation
    # ─────────────────────────────────────────────

    def _detect_platform(self, url: str) -> str:
        """Detect platform from URL string."""
        url_lower = url.lower()
        if any(d in url_lower for d in ["youtube.com", "youtu.be"]):
            return "youtube"
        if any(d in url_lower for d in ["tiktok.com", "vm.tiktok.com", "vt.tiktok.com"]):
            return "tiktok"
        if any(d in url_lower for d in ["facebook.com", "fb.watch", "fb.com"]):
            return "facebook"
        if any(d in url_lower for d in ["instagram.com", "instagr.am"]):
            return "instagram"
        if any(d in url_lower for d in ["twitter.com", "x.com", "t.co"]):
            return "twitter"
        if any(d in url_lower for d in ["pinterest.com", "pin.it"]):
            return "pinterest"
        return "other"

    def _normalize_youtube_url(self, url: str) -> str:
        """Convert YouTube Shorts URLs to standard watch?v= format."""
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            path = parsed.path or ""
            if host.endswith("youtube.com") and path.startswith("/shorts/"):
                video_id = path.split("/shorts/", 1)[1].split("/", 1)[0]
                if video_id:
                    qs = parse_qs(parsed.query)
                    si = qs.get("si", [None])[0]
                    new_url = f"https://www.youtube.com/watch?v={video_id}"
                    if si:
                        new_url += f"&si={si}"
                    return new_url
        except Exception:
            pass
        return url

    async def _resolve_redirect(self, url: str) -> str:
        """Follow URL redirects (e.g., pin.it short links)."""
        timeout = aiohttp.ClientTimeout(total=20)
        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            try:
                async with session.get(url, allow_redirects=True) as resp:
                    return str(resp.url)
            except Exception:
                return url

    # ─────────────────────────────────────────────
    # yt-dlp Options Builder
    # ─────────────────────────────────────────────

    def _get_opts(
        self,
        download_type: str = "video",
        url: str = "",
        check_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Build yt-dlp options tailored to platform and download type.

        Key fixes:
        ✅ Cookies copied to writable /tmp/ — no more read-only crash
        ✅ Audio block clears all video postprocessor_args
        ✅ retries reduced 10→5 to prevent hang
        """
        platform = self._detect_platform(url)
        logger.info(f"🔍 Platform: {platform} | Type: {download_type}")

        common_opts: Dict[str, Any] = {
            "quiet": False,
            "no_warnings": False,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": 5,               # ✅ Reduced from 10 to prevent hang
            "fragment_retries": 5,
            "verbose": True,
            "logger": logger,
            "nocheckcertificate": True,
            "http_headers": {
                "User-Agent": self.USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Cache-Control": "max-age=0",
            },
            "extractor_args": self.EXTRACTOR_ARGS,
            "sleep_interval_requests": 1,
            "ignoreerrors": False,
            "no_color": True,
            "http_chunk_size": 10 * 1024 * 1024,  # 10MB chunks
        }

        if not check_only:
            common_opts["outtmpl"] = f"{DOWNLOAD_DIR}/%(id)s.%(ext)s"
            common_opts["max_filesize"] = MAX_FILE_SIZE

        # ✅ FIX: Use writable cookies path (copied from /etc/secrets/)
        if self._cookies_file and os.path.exists(self._cookies_file):
            common_opts["cookiefile"] = self._cookies_file
            logger.info(f"🍪 Using cookies: {self._cookies_file}")
        else:
            logger.warning("⚠️ Cookies unavailable — proceeding without authentication")

        # ── Platform-specific overrides ──────────────────────────────

        if platform == "youtube":
            common_opts.update({
                "age_limit": None,
                "sleep_interval": 1,
                "geo_bypass": True,
            })

        elif platform == "tiktok":
            # Force H.264 (AVC) codec — H.265 shows black screen on Telegram
            common_opts["format"] = (
                "bestvideo[vcodec^=avc1][height<=1080][ext=mp4]"
                "+bestaudio[ext=m4a]/"
                "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio/"
                "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
                "best[ext=mp4]/best"
            )
            if not check_only:
                common_opts["merge_output_format"] = "mp4"
                common_opts["postprocessors"] = [
                    {
                        "key": "FFmpegVideoConvertor",
                        "preferedformat": "mp4",
                    }
                ]
                common_opts["postprocessor_args"] = {
                    # Force libx264 + AAC for Telegram compatibility
                    "videoconvertor": [
                        "-vcodec", "libx264",
                        "-acodec", "aac",
                        "-crf", "23",
                        "-preset", "fast",
                        "-movflags", "+faststart",
                    ]
                }

        elif platform == "instagram":
            common_opts.update({
                "http_headers": {
                    "User-Agent": self.USER_AGENT,
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.instagram.com/",
                    "Origin": "https://www.instagram.com",
                    "X-IG-App-ID": "936619743392459",
                },
                "format": "best",
            })

        elif platform == "facebook":
            common_opts.update({
                "http_headers": {
                    "User-Agent": self.USER_AGENT,
                    "Referer": "https://www.facebook.com/",
                    "Origin": "https://www.facebook.com",
                },
                "format": "best",
            })

        # ── Download type overrides ───────────────────────────────────
        # ✅ IMPORTANT: Audio block runs LAST and overrides ALL platform opts
        # This prevents TikTok video codec args from leaking into audio

        if download_type == "audio":
            common_opts["format"] = (
                "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
            )
            common_opts["postprocessors"] = (
                [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ]
                if not check_only
                else []
            )
            # ✅ Clear TikTok video postprocessor_args leak
            common_opts["postprocessor_args"] = {}
            # ✅ Remove video-only options
            common_opts.pop("merge_output_format", None)
            common_opts.pop("max_filesize", None)  # Audio always small
            common_opts["prefer_ffmpeg"] = True
            common_opts["keepvideo"] = False

        elif platform == "youtube" and download_type == "video":
            # YouTube: prefer H.264 mp4 up to 1080p
            common_opts["format"] = (
                "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo[height<=1080]+bestaudio/"
                "best[height<=1080][ext=mp4]/"
                "best[ext=mp4]/best"
            )
            if not check_only:
                common_opts["merge_output_format"] = "mp4"

        return common_opts

    # ─────────────────────────────────────────────
    # Size Check (yt-dlp metadata probe)
    # ─────────────────────────────────────────────

    def _check_size_sync(self, url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
        """Probe video metadata WITHOUT downloading to validate file size."""
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                logger.info(f"📏 Probing size for: {url}")
                info = ydl.extract_info(url, download=False)

                if not info:
                    return {"status": "error", "message": "Cannot extract video info"}

                if "entries" in info:
                    if not info["entries"]:
                        return {"status": "error", "message": "No videos found"}
                    info = info["entries"][0]

                filesize = info.get("filesize") or info.get("filesize_approx")

                if filesize and filesize > MAX_FILE_SIZE:
                    size_mb = filesize / 1024 / 1024
                    limit_mb = MAX_FILE_SIZE / 1024 / 1024
                    return {
                        "status": "error",
                        "message": (
                            f"File too large: {size_mb:.1f}MB "
                            f"(limit: {limit_mb:.0f}MB)"
                        ),
                        "size": filesize,
                    }

                return {
                    "status": "ok",
                    "size": filesize,
                    "title": info.get("title", "Unknown"),
                    "duration": info.get("duration", 0),
                }

            except Exception as e:
                logger.error(f"❌ Size probe error: {e}")
                # Allow download to proceed — size enforced during download
                return {"status": "ok", "size": None}

    def _probe_sync(self, url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
        """Lightweight metadata probe (no download)."""
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    # ─────────────────────────────────────────────
    # Core yt-dlp Download
    # ─────────────────────────────────────────────

    def _download_sync(self, url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
        """Blocking yt-dlp download — must be run inside executor."""
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                logger.info(f"⬇️ yt-dlp downloading: {url}")
                info = ydl.extract_info(url, download=True)

                if not info:
                    return {"status": "error", "message": "Cannot extract video info"}

                if "entries" in info:
                    info = info["entries"][0]

                filename = ydl.prepare_filename(info)

                # Resolve final filename after postprocessing (e.g., .mp3)
                if opts.get("postprocessors"):
                    base, _ = os.path.splitext(filename)
                    try:
                        pp = (opts.get("postprocessors") or [])[0] or {}
                        ext = (
                            pp.get("preferredcodec")
                            or pp.get("preferedformat")
                            or "mp4"
                        ).strip().lower()
                    except Exception:
                        ext = "mp4"
                    filename = f"{base}.{ext}"

                # ✅ FIX: Check multiple extensions — not just .mp4
                if not os.path.exists(filename):
                    base, _ = os.path.splitext(filename)
                    found = False

                    for candidate_ext in ["mp3", "mp4", "m4a", "opus", "webm"]:
                        candidate = f"{base}.{candidate_ext}"
                        if os.path.exists(candidate):
                            filename = candidate
                            found = True
                            logger.info(f"✅ Resolved filename: {candidate}")
                            break

                    # Last resort: newest file in downloads/ within 60s
                    if not found:
                        try:
                            all_files = [
                                os.path.join(DOWNLOAD_DIR, f)
                                for f in os.listdir(DOWNLOAD_DIR)
                                if os.path.isfile(os.path.join(DOWNLOAD_DIR, f))
                            ]
                            if all_files:
                                latest = max(all_files, key=os.path.getmtime)
                                age = time.time() - os.path.getmtime(latest)
                                if age < 60:
                                    logger.warning(f"⚠️ Fallback file: {latest}")
                                    filename = latest
                                    found = True
                        except Exception as scan_err:
                            logger.error(f"Folder scan error: {scan_err}")

                    if not found:
                        return {
                            "status": "error",
                            "message": "File not found after download",
                        }

                return {
                    "status": "success",
                    "file_path": filename,
                    "title": info.get("title", "Unknown"),
                    "duration": info.get("duration", 0),
                    "uploader": info.get("uploader", "Unknown"),
                }

            except yt_dlp.utils.DownloadError as e:
                error_msg = str(e)
                logger.error(f"❌ DownloadError: {error_msg}")

                if "File is larger than" in error_msg or "too large" in error_msg.lower():
                    return {"status": "error", "message": "File too large (>49MB)"}
                if "Video unavailable" in error_msg or "Private video" in error_msg:
                    return {"status": "error", "message": "Video unavailable or private"}
                if "Sign in to confirm" in error_msg:
                    return {
                        "status": "error",
                        "message": "Age-restricted. Need cookies.txt",
                    }
                if "HTTP Error 429" in error_msg:
                    return {"status": "error", "message": "Rate limited. Try in 5 minutes"}
                if "HTTP Error 403" in error_msg:
                    return {
                        "status": "error",
                        "message": "Access forbidden. May be region-blocked",
                    }
                if "Failed to extract any player response" in error_msg:
                    return {
                        "status": "error",
                        "message": (
                            "YouTube បានប្តូររចនាសម្ព័ន្ធ។ "
                            "សូមព្យាយាមម្ដងទៀតក្រោយ។"
                        ),
                    }
                return {
                    "status": "error",
                    "message": f"Download failed: {error_msg[:200]}",
                }

            except Exception as e:
                logger.error(f"❌ Unexpected error: {e}", exc_info=True)
                return {"status": "error", "message": f"Error: {str(e)[:200]}"}

    # ─────────────────────────────────────────────
    # TikTok Slideshow / Photo Post Handling
    # ─────────────────────────────────────────────

    def _is_slideshow_info(self, info: Dict[str, Any]) -> bool:
        """Return True if yt-dlp info looks like a TikTok photo slideshow."""
        if not isinstance(info, dict):
            return False

        if info.get("_type") == "playlist" and isinstance(info.get("entries"), list):
            for entry in info.get("entries") or []:
                if not isinstance(entry, dict):
                    continue
                ext = (entry.get("ext") or "").lower()
                if ext in IMAGE_EXTS:
                    return True
                url = entry.get("url")
                if isinstance(url, str) and any(
                    url.lower().endswith("." + x) for x in IMAGE_EXTS
                ):
                    return True

        ext = (info.get("ext") or "").lower()
        return ext in IMAGE_EXTS

    def _download_tiktok_slideshow_sync(
        self, url: str, base_opts: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Download a TikTok photo/slideshow post as individual image files."""
        folder = os.path.join(DOWNLOAD_DIR, f"tiktok_slideshow_{uuid.uuid4().hex}")
        os.makedirs(folder, exist_ok=True)

        opts = dict(base_opts)
        opts.update({
            "noplaylist": False,
            "outtmpl": os.path.join(
                folder, "%(title).80s_%(playlist_index)02d.%(ext)s"
            ),
            "playlist_items": "1-50",
            "postprocessors": [],
            "postprocessor_args": {},
        })

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = "TikTok Photo"
            duration = 0
            if isinstance(info, dict):
                title = info.get("title") or title
                duration = info.get("duration") or 0

        files = [
            os.path.join(folder, name)
            for name in sorted(os.listdir(folder))
            if os.path.splitext(name)[1].lstrip(".").lower() in IMAGE_EXTS
        ]

        if not files:
            return {"status": "error", "message": "No images found for this TikTok link"}

        return {
            "status": "success",
            "media_kind": "slideshow",
            "file_paths": files,
            "title": title,
            "duration": duration,
            "uploader": "TikTok",
        }

    # ─────────────────────────────────────────────
    # Pinterest Direct MP4 Fallback
    # ─────────────────────────────────────────────

    async def _download_direct_mp4(
        self, mp4_url: str, title: str = "Pinterest Video"
    ) -> Dict[str, Any]:
        """Download a known direct MP4 URL via aiohttp (Pinterest fallback)."""
        timeout = aiohttp.ClientTimeout(total=120)
        headers = {"User-Agent": self.USER_AGENT}
        out_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4().hex}.mp4")

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            try:
                async with session.head(mp4_url, allow_redirects=True) as head:
                    size = head.headers.get("Content-Length")
                    if size and size.isdigit() and int(size) > MAX_FILE_SIZE:
                        size_mb = int(size) / 1024 / 1024
                        limit_mb = MAX_FILE_SIZE / 1024 / 1024
                        return {
                            "status": "error",
                            "message": (
                                f"File too large: {size_mb:.1f}MB "
                                f"(limit: {limit_mb:.0f}MB)"
                            ),
                            "size": int(size),
                        }
            except Exception:
                pass

            async with session.get(mp4_url, allow_redirects=True) as resp:
                if resp.status >= 400:
                    return {
                        "status": "error",
                        "message": f"HTTP {resp.status} while fetching media",
                    }
                total = 0
                with open(out_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_FILE_SIZE:
                            try:
                                os.remove(out_path)
                            except Exception:
                                pass
                            return {
                                "status": "error",
                                "message": (
                                    f"File too large "
                                    f"(limit: {MAX_FILE_SIZE / 1024 / 1024:.0f}MB)"
                                ),
                            }
                        f.write(chunk)

        return {
            "status": "success",
            "file_path": out_path,
            "title": title or "Pinterest Video",
            "duration": 0,
            "uploader": "Pinterest",
        }

    async def _download_pinterest(
        self, url: str, download_type: str = "video"
    ) -> Dict[str, Any]:
        """
        Pinterest-specific fallback:
        1. Resolve pin.it short links
        2. Fetch HTML page
        3. Extract direct mp4 URL from pinimg.com CDN
        """
        if download_type != "video":
            return {"status": "error", "message": "Pinterest supports video only"}

        final_url = await self._resolve_redirect(url)
        m = re.search(r"/pin/(\d+)", final_url)
        if m:
            final_url = f"https://www.pinterest.com/pin/{m.group(1)}/"

        timeout = aiohttp.ClientTimeout(total=25)
        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            try:
                async with session.get(final_url, allow_redirects=True) as resp:
                    html = await resp.text(errors="ignore")
            except Exception as e:
                return {"status": "error", "message": f"Pinterest fetch failed: {e}"}

        title_m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = title_m.group(1).strip() if title_m else "Pinterest Video"

        mp4_candidates: List[str] = []
        mp4_candidates += re.findall(r"https://v\.pinimg\.com[^\"\\\s]+\.mp4", html)
        mp4_candidates += re.findall(r"https://video\.pinimg\.com[^\"\\\s]+\.mp4", html)
        mp4_candidates += re.findall(r"https://i\.pinimg\.com[^\"\\\s]+\.mp4", html)

        if not mp4_candidates:
            mp4_candidates += [
                u.replace("\\u002F", "/").replace("\\/", "/")
                for u in re.findall(
                    r"https:\\/\\/(?:v|video|i)\\.pinimg\\.com[^\"]+?\\.mp4", html
                )
            ]

        if not mp4_candidates:
            m3u8_candidates: List[str] = []
            m3u8_candidates += re.findall(
                r"https://(?:v|video|i)\.pinimg\.com[^\"\s]+\.m3u8", html
            )
            if m3u8_candidates:
                return await self.download_with_ytdlp(m3u8_candidates[0], download_type)
            return {
                "status": "error",
                "message": (
                    "Pinterest is blocking automated downloads. "
                    "Try again later or provide cookies.txt."
                ),
            }

        return await self._download_direct_mp4(mp4_candidates[0], title=title)

    # ─────────────────────────────────────────────
    # Main yt-dlp Download Orchestrator
    # ─────────────────────────────────────────────

    async def download_with_ytdlp(
        self, url: str, type: str = "video"
    ) -> Dict[str, Any]:
        """
        Download via yt-dlp with:
        - Slideshow detection for TikTok
        - Size check SKIPPED for audio (always small, and can hang)
        - Size check SKIPPED for TikTok (Cobalt handles it)
        - Retry loop with user-agent + client rotation
        """
        loop = asyncio.get_running_loop()
        platform = self._detect_platform(url)

        if platform == "youtube":
            url = self._normalize_youtube_url(url)

        # ── TikTok slideshow detection ──────────────────────────────
        if platform == "tiktok" and type == "video":
            try:
                probe_opts = self._get_opts(type, url, check_only=True)
                probe_opts["noplaylist"] = False
                info = await loop.run_in_executor(
                    self.executor, self._probe_sync, url, probe_opts
                )
                if isinstance(info, dict) and self._is_slideshow_info(info):
                    logger.info("🖼️ TikTok slideshow detected → downloading images")
                    base_opts = self._get_opts(type, url)
                    return await loop.run_in_executor(
                        self.executor,
                        self._download_tiktok_slideshow_sync,
                        url,
                        base_opts,
                    )
            except Exception as e:
                logger.warning(f"TikTok slideshow probe failed, continuing: {e}")

        # ✅ FIX HANG: Skip size check for audio and TikTok
        # - Audio files are almost always < 10MB → no risk
        # - Size check itself can hang on YouTube bot-detection loop
        skip_size_check = (type == "audio") or (platform == "tiktok")

        if not skip_size_check:
            logger.info("📏 Checking file size before download...")
            check_opts = self._get_opts(type, url, check_only=True)
            size_check = await loop.run_in_executor(
                self.executor, self._check_size_sync, url, check_opts
            )
            if size_check["status"] == "error":
                return size_check

        # ── Retry loop ──────────────────────────────────────────────
        for attempt in range(1, self.max_retries + 1):
            opts = self._get_opts(type, url)

            # Rotate user-agent per attempt
            ua = self.USER_AGENTS[(attempt - 1) % len(self.USER_AGENTS)]
            opts.setdefault("http_headers", {})["User-Agent"] = ua

            # Rotate YouTube player clients per attempt
            if platform == "youtube":
                clients = [
                    ["tv", "android_sdkless", "web_safari"],
                    ["android_sdkless", "tv", "ios"],
                    ["ios", "android_sdkless", "tv"],
                ]
                extractor_args = dict(opts.get("extractor_args") or {})
                yt_args = dict(extractor_args.get("youtube") or {})
                yt_args["player_client"] = clients[(attempt - 1) % len(clients)]
                extractor_args["youtube"] = yt_args
                opts["extractor_args"] = extractor_args

            try:
                logger.info(
                    f"⬇️ yt-dlp attempt {attempt}/{self.max_retries} | "
                    f"platform={platform} | type={type}"
                )
                result = await loop.run_in_executor(
                    self.executor, self._download_sync, url, opts
                )

                if result["status"] == "success":
                    return result

                # Do not retry permanent errors
                non_retryable = [
                    "File too large",
                    "unavailable",
                    "private",
                    "Age-restricted",
                    "region-blocked",
                ]
                if any(err in result["message"] for err in non_retryable):
                    return result

                logger.warning(f"⚠️ Attempt {attempt} failed: {result['message']}")

            except Exception as e:
                logger.error(f"❌ Attempt {attempt} exception: {e}")
                if attempt == self.max_retries:
                    return {"status": "error", "message": "System error"}

            if attempt < self.max_retries:
                await asyncio.sleep(min(2 ** attempt, 8))

        return {
            "status": "error",
            "message": f"Failed after {self.max_retries} attempts",
        }

    # ─────────────────────────────────────────────
    # Public Entry Point
    # ─────────────────────────────────────────────

    async def download(self, url: str, type: str = "video") -> Dict[str, Any]:
        """
        Route download request to appropriate handler based on platform.

        Routes:
          TikTok   → Cobalt API v7 → yt-dlp (H.264 forced)
          Facebook → Facebook Multi-API → yt-dlp
          Pinterest→ Direct MP4 fallback
          Others   → yt-dlp
        """
        platform = self._detect_platform(url)

        # ── TikTok ─────────────────────────────────────────────────
        if platform == "tiktok":
            if type == "audio":
                logger.info("🎵 TikTok audio → yt-dlp")
                return await self.download_with_ytdlp(url, type)

            logger.info("🎬 TikTok video → Cobalt API v7")
            try:
                from src.cobalt_api import cobalt_downloader
                result = await cobalt_downloader.download(url, type)
                if result.get("status") == "success":
                    logger.info("✅ TikTok via Cobalt API v7")
                    return result
                logger.warning("⚠️ Cobalt failed → yt-dlp (H.264 forced)")
                return await self.download_with_ytdlp(url, type)
            except ImportError:
                logger.error("❌ cobalt_api.py not found — using yt-dlp")
                return await self.download_with_ytdlp(url, type)
            except Exception as e:
                logger.error(f"❌ Cobalt error: {e}")
                return await self.download_with_ytdlp(url, type)

        # ── Facebook ───────────────────────────────────────────────
        elif platform == "facebook":
            logger.info("📱 Facebook → Multi-API system")
            try:
                from src.facebook_api import facebook_downloader
                result = await facebook_downloader.download(url, type)
                if result["status"] == "success":
                    logger.info("✅ Facebook via Multi-API")
                    return result
                logger.warning("⚠️ Facebook APIs failed → yt-dlp fallback")
                ytdlp_result = await self.download_with_ytdlp(url, type)
                return ytdlp_result if ytdlp_result["status"] == "success" else result
            except ImportError:
                return await self.download_with_ytdlp(url, type)
            except Exception as e:
                logger.error(f"❌ Facebook API error: {e}")
                return await self.download_with_ytdlp(url, type)

        # ── Pinterest ──────────────────────────────────────────────
        elif platform == "pinterest":
            logger.info("📌 Pinterest → direct MP4 fallback")
            result = await self._download_pinterest(url, type)
            if result.get("status") != "success":
                logger.warning(f"⚠️ Pinterest direct failed: {result.get('message')}")
            return result

        # ── All other platforms ────────────────────────────────────
        else:
            logger.info(f"📹 {platform} → yt-dlp | type={type}")
            return await self.download_with_ytdlp(url, type)


# Global singleton
downloader = Downloader()
