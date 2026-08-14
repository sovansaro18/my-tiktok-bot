import asyncio
import logging
import os
import re
import uuid
import time
import shutil
from urllib.parse import parse_qs, quote, urlparse

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

    def __init__(self, max_workers: int = 4):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.max_retries = 3
        self._shutdown = False
        self._cookies_file = self._prepare_cookies_file()

    def _prepare_cookies_file(self) -> Optional[str]:
        candidates = [
            os.getenv("YTDLP_COOKIES"),
            os.getenv("COOKIES_FILE"),
        ]
        source = None
        for path in candidates:
            if path and os.path.exists(path) and os.path.getsize(path) > 0:
                source = path
                break

        if not source:
            logger.warning("⚠️ No cookies configured (YTDLP_COOKIES/COOKIES_FILE)")
            return None

        tmp_cookies = "/tmp/yt_cookies.txt"
        try:
            shutil.copy2(source, tmp_cookies)
            os.chmod(tmp_cookies, 0o600)
            logger.info(f"🍪 Cookies staged: {source} → {tmp_cookies}")
            return tmp_cookies
        except Exception as e:
            logger.error(f"❌ Failed to stage cookies: {e}")
            return source

    def shutdown(self, wait: bool = True) -> None:
        if not self._shutdown:
            self.executor.shutdown(wait=wait)
            self._shutdown = True

    def __del__(self):
        if not self._shutdown:
            self.shutdown(wait=False)

    def _detect_platform(self, url: str) -> str:
        u = url.lower()
        if any(d in u for d in ["youtube.com", "youtu.be"]):
            return "youtube"
        if any(d in u for d in ["tiktok.com", "vm.tiktok.com", "vt.tiktok.com"]):
            return "tiktok"
        if any(d in u for d in ["facebook.com", "fb.watch", "fb.com"]):
            return "facebook"
        if any(d in u for d in ["instagram.com", "instagr.am"]):
            return "instagram"
        if any(d in u for d in ["twitter.com", "x.com", "t.co"]):
            return "twitter"
        if any(d in u for d in ["pinterest.com", "pin.it"]):
            return "pinterest"
        return "other"

    def _normalize_youtube_url(self, url: str) -> str:
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
        timeout = aiohttp.ClientTimeout(total=20)
        headers = {"User-Agent": self.USER_AGENT}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            try:
                async with session.get(url, allow_redirects=True) as resp:
                    return str(resp.url)
            except Exception:
                return url

    def _get_opts(
        self,
        download_type: str = "video",
        url: str = "",
        check_only: bool = False,
    ) -> Dict[str, Any]:
        platform = self._detect_platform(url)
        logger.info(f"🔍 Platform: {platform} | Type: {download_type}")

        common_opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 15,
            "retries": 3,
            "fragment_retries": 3,
            "concurrent_fragment_downloads": 8,  # ⚡ SPEEDUP: Download 8 fragments in parallel
            "verbose": False,
            "logger": logger,
            "nocheckcertificate": True,
            "http_headers": {
                "User-Agent": self.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
            },
            "extractor_args": {
                "youtube": {
                    "player_client": ["android_sdkless", "ios", "tv"],
                    "skip": ["dash", "hls"],
                },
                "instagram": {
                    "api_hostname": "i.instagram.com",
                },
            },
            "sleep_interval_requests": 0,
            "ignoreerrors": False,
            "no_color": True,
            "buffersize": 1024 * 1024,  # ⚡ 1MB Buffer for faster IO
        }

        if not check_only:
            common_opts["outtmpl"] = f"{DOWNLOAD_DIR}/%(id)s.%(ext)s"
            common_opts["max_filesize"] = MAX_FILE_SIZE

        if self._cookies_file and os.path.exists(self._cookies_file):
            common_opts["cookiefile"] = self._cookies_file

        # ── Platform-specific overrides ──────────────────────────────

        if platform == "youtube":
            common_opts.update({
                "age_limit": None,
                "geo_bypass": True,
            })

        elif platform == "tiktok":
            if download_type == "video":
                # ⚡ FAST FORMAT: Fast pre-formatted MP4 direct streams
                common_opts["format"] = (
                    "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best"
                )
                if not check_only:
                    common_opts["merge_output_format"] = "mp4"

        elif platform == "instagram":
            common_opts.update({
                "http_headers": {
                    "User-Agent": self.USER_AGENT,
                    "Accept": "*/*",
                    "X-IG-App-ID": "936619743392459",
                },
                "format": "best",
            })

        elif platform == "facebook":
            common_opts.update({
                "format": "best",
            })

        if platform == "youtube" and download_type == "video":
            common_opts["format"] = (
                "best[height<=1080][ext=mp4]/bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best"
            )
            if not check_only:
                common_opts["merge_output_format"] = "mp4"

        # ── AUDIO block ──────────────────────────────────────────────
        if download_type == "audio":
            common_opts["format"] = (
                "bestaudio[ext=m4a]/bestaudio/best"
            )
            common_opts["postprocessors"] = (
                [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "128",  # ⚡ 128kbps converts much faster
                    }
                ]
                if not check_only
                else []
            )
            common_opts["postprocessor_args"] = {}
            common_opts.pop("merge_output_format", None)
            common_opts.pop("max_filesize", None)

        return common_opts

    def _check_size_sync(self, url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
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
                        "message": f"File too large: {size_mb:.1f}MB (limit: {limit_mb:.0f}MB)",
                        "size": filesize,
                    }
                return {"status": "ok", "size": filesize}
            except Exception as e:
                logger.error(f"❌ Size probe error: {e}")
                return {"status": "ok", "size": None}

    def _probe_sync(self, url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def _download_sync(self, url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                logger.info(f"⬇️ yt-dlp downloading: {url}")
                info = ydl.extract_info(url, download=True)

                if not info:
                    return {"status": "error", "message": "Cannot extract video info"}
                if "entries" in info:
                    info = info["entries"][0]

                filename = ydl.prepare_filename(info)

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

                if not os.path.exists(filename):
                    base, _ = os.path.splitext(filename)
                    found = False
                    for candidate_ext in ["mp3", "mp4", "m4a", "opus", "webm"]:
                        candidate = f"{base}.{candidate_ext}"
                        if os.path.exists(candidate):
                            filename = candidate
                            found = True
                            break

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
                                    filename = latest
                                    found = True
                        except Exception as e:
                            logger.error(f"Folder scan error: {e}")

                    if not found:
                        return {"status": "error", "message": "File not found after download"}

                return {
                    "status": "success",
                    "file_path": filename,
                    "title": info.get("title", "Unknown"),
                    "duration": info.get("duration", 0),
                    "uploader": info.get("uploader", "Unknown"),
                }

            except Exception as e:
                logger.error(f"❌ Unexpected error: {e}", exc_info=True)
                return {"status": "error", "message": f"Error: {str(e)[:200]}"}

    def _is_slideshow_info(self, info: Dict[str, Any]) -> bool:
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
        folder = os.path.join(DOWNLOAD_DIR, f"tiktok_slideshow_{uuid.uuid4().hex}")
        os.makedirs(folder, exist_ok=True)
        opts = dict(base_opts)
        opts.update({
            "noplaylist": False,
            "outtmpl": os.path.join(folder, "%(title).80s_%(playlist_index)02d.%(ext)s"),
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
            return {
                "status": "error",
                "message": (
                    "រកមិនឃើញរូបភាពទេ។ "
                    "Link នេះអាចជាវីដេអូ — សូមសាកល្បង 🎬 Video ជំនួស។"
                ),
            }

        return {
            "status": "success",
            "media_kind": "slideshow",
            "file_paths": files,
            "title": title,
            "duration": duration,
            "uploader": "TikTok",
        }

    async def _try_tikwm_photo(self, url: str) -> Dict[str, Any]:
        try:
            headers = {
                "User-Agent": self.USER_AGENT,
                "Accept": "application/json",
            }
            api_url = f"https://www.tikwm.com/api/?url={quote(url, safe='')}&hd=1"
            timeout = aiohttp.ClientTimeout(total=20)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(api_url, headers=headers) as response:
                    if response.status != 200:
                        return {
                            "status": "error",
                            "message": f"TikWM returned {response.status}",
                        }

                    data = await response.json()

                    if data.get("code") != 0:
                        return {"status": "error", "message": "TikWM API error"}

                    video_data = data.get("data", {})
                    title = video_data.get("title", "TikTok Photo")

                    images = video_data.get("images") or []

                    if not images:
                        return {
                            "status": "error",
                            "message": "not_photo_post",
                        }

                    folder = os.path.join(
                        DOWNLOAD_DIR, f"tiktok_photo_{uuid.uuid4().hex}"
                    )
                    os.makedirs(folder, exist_ok=True)

                    downloaded_files = []
                    dl_timeout = aiohttp.ClientTimeout(total=30)

                    async with aiohttp.ClientSession(
                        timeout=dl_timeout, headers=headers
                    ) as dl_session:
                        for idx, img_url in enumerate(images):
                            try:
                                if img_url.startswith("//"):
                                    img_url = "https:" + img_url
                                elif not img_url.startswith("http"):
                                    img_url = "https://www.tikwm.com" + img_url

                                async with dl_session.get(
                                    img_url, allow_redirects=True
                                ) as img_resp:
                                    if img_resp.status != 200:
                                        continue

                                    content_type = img_resp.headers.get(
                                        "Content-Type", "image/jpeg"
                                    )
                                    ext = "jpg"
                                    if "png" in content_type:
                                        ext = "png"
                                    elif "webp" in content_type:
                                        ext = "webp"

                                    img_path = os.path.join(
                                        folder, f"photo_{idx+1:02d}.{ext}"
                                    )
                                    content = await img_resp.read()
                                    with open(img_path, "wb") as f:
                                        f.write(content)

                                    downloaded_files.append(img_path)

                            except Exception as img_err:
                                logger.error(f"Image {idx+1} error: {img_err}")
                                continue

                    if not downloaded_files:
                        return {
                            "status": "error",
                            "message": "Failed to download any images from TikWM",
                        }

                    return {
                        "status": "success",
                        "media_kind": "slideshow",
                        "file_paths": downloaded_files,
                        "title": title,
                        "duration": 0,
                        "uploader": "TikTok",
                    }

        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def _download_direct_mp4(
        self, mp4_url: str, title: str = "Pinterest Video"
    ) -> Dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=60)
        headers = {"User-Agent": self.USER_AGENT}
        out_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4().hex}.mp4")
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            try:
                async with session.head(mp4_url, allow_redirects=True) as head:
                    size = head.headers.get("Content-Length")
                    if size and size.isdigit() and int(size) > MAX_FILE_SIZE:
                        return {
                            "status": "error",
                            "message": f"File too large: {int(size)/1024/1024:.1f}MB",
                        }
            except Exception:
                pass
            async with session.get(mp4_url, allow_redirects=True) as resp:
                if resp.status >= 400:
                    return {"status": "error", "message": f"HTTP {resp.status}"}
                total = 0
                with open(out_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(128 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_FILE_SIZE:
                            try:
                                os.remove(out_path)
                            except Exception:
                                pass
                            return {"status": "error", "message": "File too large"}
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
        if download_type != "video":
            return {"status": "error", "message": "Pinterest supports video only"}

        final_url = await self._resolve_redirect(url)
        m = re.search(r"/pin/(\d+)", final_url)
        if m:
            final_url = f"https://www.pinterest.com/pin/{m.group(1)}/"

        timeout = aiohttp.ClientTimeout(total=15)
        headers = {
            "User-Agent": self.USER_AGENT,
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
            m3u8 = re.findall(
                r"https://(?:v|video|i)\.pinimg\.com[^\"\s]+\.m3u8", html
            )
            if m3u8:
                return await self.download_with_ytdlp(m3u8[0], download_type)
            return {"status": "error", "message": "Pinterest is blocking. Try again later."}

        return await self._download_direct_mp4(mp4_candidates[0], title=title)

    async def download_with_ytdlp(
        self, url: str, type: str = "video"
    ) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        platform = self._detect_platform(url)

        if platform == "youtube":
            url = self._normalize_youtube_url(url)

        if platform == "tiktok" and type == "photo":
            base_opts = self._get_opts("video", url)
            return await loop.run_in_executor(
                self.executor,
                self._download_tiktok_slideshow_sync,
                url,
                base_opts,
            )

        skip_size_check = (type == "audio") or (platform == "tiktok")
        if not skip_size_check:
            check_opts = self._get_opts(type, url, check_only=True)
            size_check = await loop.run_in_executor(
                self.executor, self._check_size_sync, url, check_opts
            )
            if size_check["status"] == "error":
                return size_check

        for attempt in range(1, self.max_retries + 1):
            opts = self._get_opts(type, url)
            ua = self.USER_AGENTS[(attempt - 1) % len(self.USER_AGENTS)]
            opts.setdefault("http_headers", {})["User-Agent"] = ua

            try:
                result = await loop.run_in_executor(
                    self.executor, self._download_sync, url, opts
                )
                if result["status"] == "success":
                    return result

                non_retryable = [
                    "File too large", "unavailable", "private",
                    "Age-restricted", "region-blocked",
                ]
                if any(e in result.get("message", "") for e in non_retryable):
                    return result

            except Exception as e:
                logger.error(f"❌ Attempt {attempt} exception: {e}")

            if attempt < self.max_retries:
                await asyncio.sleep(1)

        return {
            "status": "error",
            "message": f"Failed after {self.max_retries} attempts",
        }

    async def download(self, url: str, type: str = "video") -> Dict[str, Any]:
        platform = self._detect_platform(url)

        if platform == "tiktok":
            if type == "photo":
                from src.cobalt_api import cobalt_downloader

                cobalt_result = await cobalt_downloader.download(url, "photo")
                if cobalt_result.get("status") == "success":
                    if cobalt_result.get("media_kind") == "slideshow":
                        return cobalt_result
                    file_path = cobalt_result.get("file_path")
                    if isinstance(file_path, str):
                        try:
                            os.remove(file_path)
                        except OSError:
                            pass

                tikwm_result = await self._try_tikwm_photo(url)
                if tikwm_result.get("status") == "success":
                    return tikwm_result
                if tikwm_result.get("message") == "not_photo_post":
                    return {
                        "status": "error",
                        "user_message": (
                            "Link នេះជាវីដេអូ មិនមែនរូបភាពទេ។\n\n"
                            "សូមប្រើប៊ូតុង 🎬 <b>Video</b> ជំនួស។"
                        ),
                        "message": "not a photo post",
                    }

                return {
                    "status": "error",
                    "message": (
                        "TikTok photo download failed. "
                        "The photo service did not return any images."
                    ),
                }

            if type == "audio":
                return await self.download_with_ytdlp(url, type)

            try:
                from src.cobalt_api import cobalt_downloader
                result = await cobalt_downloader.download(url, type)
                if result.get("status") == "success":
                    return result
                return await self.download_with_ytdlp(url, type)
            except Exception as e:
                return await self.download_with_ytdlp(url, type)

        elif platform == "facebook":
            try:
                from src.facebook_api import facebook_downloader
                result = await facebook_downloader.download(url, type)
                if result["status"] == "success":
                    return result
                return await self.download_with_ytdlp(url, type)
            except Exception as e:
                return await self.download_with_ytdlp(url, type)

        elif platform == "pinterest":
            return await self._download_pinterest(url, type)

        else:
            return await self.download_with_ytdlp(url, type)


downloader = Downloader()
