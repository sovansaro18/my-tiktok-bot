import os
import asyncio
import yt_dlp
from concurrent.futures import ThreadPoolExecutor

# កំណត់កន្លែងទុក File (ប្រើ /tmp/ បើនៅលើ Render ឬ Linux Server មួយចំនួន)
DOWNLOAD_PATH = "/tmp/" if os.name != "nt" else "downloads/"

if not os.path.exists(DOWNLOAD_PATH):
    os.makedirs(DOWNLOAD_PATH)

class Downloader:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=2) # កំណត់ឱ្យ Download ព្រមគ្នាបានតែ 2 នាក់ទេ ដើម្បីកុំឱ្យគាំង Server

    def _download_sync(self, url: str, is_audio: bool = False) -> dict:
        """Function នេះដំណើរការនៅ Background (Sync)"""
        
        # កំណត់ Option សម្រាប់ yt-dlp
        ydl_opts = {
            'outtmpl': f'{DOWNLOAD_PATH}%(id)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'socket_timeout': 30,
            # កំណត់ Format (យក MP4 សម្រាប់ Video, M4A សម្រាប់ Audio)
            'format': 'bestaudio[ext=m4a]/bestaudio/best' if is_audio else 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            # បន្លំថាជា Browser (User Agent)
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # 1. ទាញយកព័ត៌មានវីដេអូសិន (មិនទាន់ Download)
                info = ydl.extract_info(url, download=False)
                
                # 2. ពិនិត្យទំហំ File (បើធំជាង 50MB គឺ Telegram មិនឱ្យផ្ញើទេ)
                filesize = info.get('filesize') or info.get('filesize_approx') or 0
                if filesize > 49 * 1024 * 1024: # 49MB Limit
                    return {"status": "error", "message": "file_too_large", "size": filesize}

                # 3. ចាប់ផ្តើម Download
                ydl.download([url])
                
                # 4. យកឈ្មោះ File ដែលបាន Download
                filename = ydl.prepare_filename(info)
                return {"status": "success", "path": filename, "title": info.get('title', 'Video')}

        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def download(self, url: str, is_audio: bool = False):
        """Function នេះហៅ _download_sync មកដំណើរការបែប Asynchronous"""
        loop = asyncio.get_running_loop()
        # Run នៅក្នុង Thread ដាច់ដោយឡែក ដើម្បីកុំឱ្យ Bot គាំង
        result = await loop.run_in_executor(self.executor, self._download_sync, url, is_audio)
        return result

# បង្កើត Object សម្រាប់ប្រើ
downloader = Downloader()