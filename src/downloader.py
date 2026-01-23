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

class Downloader:
    def __init__(self, max_workers: int = 2):
        """
        Initialize Downloader with a thread pool.
        max_workers: Maximum number of concurrent downloads allowed.
        """
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.max_retries = 3

    def _get_opts(self, download_type: str = "video") -> Dict[str, Any]:
        """
        Configure yt-dlp options based on download type.
        """
        common_opts = {
            'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'max_filesize': 49 * 1024 * 1024,  # Limit 49MB (Telegram limit is 50MB)
            'geo_bypass': True,
        }

        if download_type == "audio":
            common_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'm4a',
                }]
            })
        else:
            # Video: Prefer MP4 for Telegram compatibility
            common_opts.update({
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'merge_output_format': 'mp4'
            })
        
        return common_opts

    def _download_sync(self, url: str, opts: Dict[str, Any]) -> Dict[str, Any]:

        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=True)
                
                if 'entries' in info:
                    info = info['entries'][0]

                filename = ydl.prepare_filename(info)
                
                if opts.get('postprocessors'):
                    base, _ = os.path.splitext(filename)
                    filename = f"{base}.m4a"

                return {
                    "status": "success",
                    "file_path": filename,
                    "title": info.get('title', 'Unknown'),
                    "duration": info.get('duration', 0),
                    "uploader": info.get('uploader', 'Unknown')
                }

            except yt_dlp.utils.DownloadError as e:
                error_msg = str(e)
                if "File is larger than" in error_msg:
                    return {"status": "error", "message": "File too large (>49MB)."}
                return {"status": "error", "message": f"Download failed: {error_msg}"}
            except Exception as e:
                return {"status": "error", "message": f"Unexpected error: {str(e)}"}

    async def download(self, url: str, type: str = "video") -> Dict[str, Any]:
        """
        Async wrapper for downloading media with Retry Logic.
        """
        opts = self._get_opts(type)
        loop = asyncio.get_running_loop()

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(f"⬇️ Downloading ({type}) [Attempt {attempt}]: {url}")
                
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
                
                if "File too large" in result["message"]:
                    logger.warning(f"⚠️ {result['message']}")
                    return result

                logger.warning(f"⚠️ Attempt {attempt} failed: {result['message']}")

            except Exception as e:
                logger.error(f"❌ Critical error in download wrapper: {e}")
                return {"status": "error", "message": "Internal server error."}
            
            if attempt < self.max_retries:
                await asyncio.sleep(attempt)

        return {"status": "error", "message": "Failed after 3 attempts. Please try again later."}

downloader = Downloader()