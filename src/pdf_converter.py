import asyncio
import logging
import os
import uuid
from typing import Any, Dict, List, Tuple

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# Telegram photo limit is 10MB; stay safely under it.
MAX_IMAGE_SIZE = 9 * 1024 * 1024
# Abuse guard: don't render absurdly large PDFs.
MAX_PAGES = 50
# 150 DPI is a good balance of quality and file size for most PDFs.
RENDER_DPI = 150


class PdfConverter:
    """Render PDF pages into JPG/PNG images using PyMuPDF."""

    def _render_sync(
        self,
        pdf_path: str,
        image_format: str,
        pages: List[int],
        work_dir: str,
    ) -> Dict[str, Any]:
        out_files: List[str] = []
        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            logger.error(f"❌ Cannot open PDF: {e}")
            return {"status": "error", "message": f"មិនអាចបើក PDF បានទេ: {e}"}

        total = doc.page_count
        zoom = RENDER_DPI / 72.0
        matrix = fitz.Matrix(zoom, zoom)

        fmt_ext = "jpg" if image_format == "jpg" else "png"
        # PyMuPDF pixmap output: "jpg" or "png"
        pixmap_fmt = "jpg" if image_format == "jpg" else "png"

        try:
            for page_num in pages:
                if page_num < 1 or page_num > total:
                    continue
                page = doc.load_page(page_num - 1)
                pix = page.get_pixmap(matrix=matrix)

                out_path = os.path.join(
                    work_dir, f"page_{page_num:03d}.{fmt_ext}"
                )
                pix.save(out_path, pixmap_fmt)

                if os.path.getsize(out_path) > MAX_IMAGE_SIZE:
                    # Downscale once and retry.
                    logger.info(
                        f"🖼️ Page {page_num} too large, downscaling..."
                    )
                    os.remove(out_path)
                    smaller = fitz.Matrix(zoom * 0.6, zoom * 0.6)
                    pix2 = page.get_pixmap(matrix=smaller)
                    pix2.save(out_path, pixmap_fmt)

                if os.path.exists(out_path):
                    out_files.append(out_path)
        except Exception as e:
            logger.error(f"❌ Render error: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"មានបញ្ហាក្នុងការបំលែងទំព័រ: {e}",
            }
        finally:
            doc.close()

        if not out_files:
            return {
                "status": "error",
                "message": "មិនអាចបំលែងទំព័រណាមួយបានទេ។ សូមពិនិត្យលេខទំព័រឡើងវិញ។",
            }

        return {
            "status": "success",
            "file_paths": out_files,
            "total_pages": total,
        }

    async def convert(
        self,
        pdf_path: str,
        image_format: str,
        pages: List[int],
    ) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        work_dir = os.path.join(
            DOWNLOAD_DIR, f"pdf_images_{uuid.uuid4().hex}"
        )
        os.makedirs(work_dir, exist_ok=True)
        return await loop.run_in_executor(
            None, self._render_sync, pdf_path, image_format, pages, work_dir
        )

    @staticmethod
    def parse_page_selection(text: str, total_pages: int) -> Tuple[List[int], str]:
        """
        Parse user input like '1,3,5-7' into a sorted, unique list of
        1-indexed page numbers. Returns (pages, error_message).
        """
        text = (text or "").strip()
        if not text:
            return [], "សូមវាយបញ្ចូលលេខទំព័រ។"

        result: List[int] = []
        parts = [p.strip() for p in text.split(",") if p.strip()]
        for part in parts:
            if "-" in part:
                ends = part.split("-")
                if len(ends) != 2:
                    return [], f"ទម្រង់មិនត្រឹមត្រូវ៖ {part}"
                try:
                    start = int(ends[0].strip())
                    end = int(ends[1].strip())
                except ValueError:
                    return [], f"លេខមិនត្រឹមត្រូវ៖ {part}"
                if start > end:
                    start, end = end, start
                for n in range(start, end + 1):
                    result.append(n)
            else:
                try:
                    result.append(int(part))
                except ValueError:
                    return [], f"លេខមិនត្រឹមត្រូវ៖ {part}"

        result = sorted(set(result))
        invalid = [n for n in result if n < 1 or n > total_pages]
        if invalid:
            return [], (
                f"ទំព័រ {invalid[0]} មិនមានទេ។ "
                f"PDF នេះមាន {total_pages} ទំព័រប៉ុណ្ណោះ។"
            )

        if len(result) > MAX_PAGES:
            return [], (
                f"ចំនួនទំព័រធំពេក (អតិបរមា {MAX_PAGES} ទំព័រ)។ "
                f"អ្នកបានជ្រើសរើស {len(result)} ទំព័រ។"
            )

        return result, ""


pdf_converter = PdfConverter()
