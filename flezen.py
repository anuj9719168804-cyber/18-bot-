import re
import os
import urllib.parse
import aiohttp
from typing import Dict, Any, Optional

FLEZEN_DOMAINS = [
    "flezen.com",
    "www.flezen.com",
]

def is_flezen_url(url: str) -> bool:
    """Checks if a given string contains a recognized Flezen URL."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        netloc = parsed.netloc.lower()
        if not netloc:
            return False
        return any(d in netloc for d in FLEZEN_DOMAINS) or "flezen" in netloc
    except Exception:
        return False

def extract_flezen_id(url: str) -> Optional[str]:
    """Extracts the file or share ID from Flezen URLs."""
    if not url:
        return None
    try:
        match = re.search(r"/(?:s|f|v|file)/([a-zA-Z0-9_-]+)", url)
        if match:
            return match.group(1).strip()
        match = re.search(r"flezen[^/]+/([a-zA-Z0-9_-]{6,})", url)
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    return None

async def get_flezen_info(url: str, mode: str = "fz") -> Dict[str, Any]:
    """
    Fetches video info, stream options, and direct players for a Flezen link.
    """
    file_id = extract_flezen_id(url)
    if not file_id:
        return {
            "ok": False,
            "error": "Could not extract Flezen file ID from the provided link."
        }

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 1. Custom / proxy fallback API if set
        custom_api = (
            os.getenv("FLEZEN_API_URL", "").strip()
            or os.getenv("TERABOX_API_URL", "").strip()
        )
        if custom_api:
            try:
                sep = "&" if "?" in custom_api else "?"
                target = f"{custom_api}{sep}url={urllib.parse.quote(url)}"
                async with session.get(target, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        stream_url = (
                            data.get("url")
                            or data.get("download_url")
                            or data.get("dlink")
                        )
                        if stream_url:
                            filename = data.get("title") or f"Flezen_Video_{file_id[:8]}.mp4"
                            size = int(data.get("size", 0))
                            return {
                                "ok": True,
                                "platform": "Flezen",
                                "filename": filename,
                                "download_url": stream_url,
                                "size": size,
                                "formatted_size": data.get("formatted_size", "Direct Video Stream"),
                                "thumbnail": data.get("thumb") or data.get("thumbnail"),
                                "engine": "fz (API)",
                                "file_id": file_id,
                            }
            except Exception:
                pass

    # 2. Attempt Playwright Headless Browser extraction (Bypasses AppiCrypt/Turnstile)
    try:
        from playwright.async_api import async_playwright
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )
            page = await browser.new_page(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
            )
            
            found_url = None
            found_size = 0
            found_filename = None
            found_thumb = None
            
            async def handle_response(response):
                nonlocal found_url, found_size, found_filename, found_thumb
                # Check for temp_info or similar file data endpoints in Flezen
                if 'temp_info' in response.url or 'file_info' in response.url:
                    if response.request.method == 'GET' or response.request.method == 'POST':
                        try:
                            data = await response.json()
                            if data.get('url') or data.get('download_url') or data.get('dlink'):
                                found_url = data.get('url') or data.get('download_url') or data.get('dlink')
                                found_size = int(data.get('size', 0))
                                found_filename = data.get('title')
                                found_thumb = data.get('thumb')
                        except Exception:
                            pass
            
            page.on('response', handle_response)
            
            try:
                await page.goto(url, wait_until='load', timeout=15000)
                import asyncio
                for _ in range(20):
                    if found_url:
                        break
                    await asyncio.sleep(0.5)
            except Exception:
                pass
            finally:
                await browser.close()
                
            if found_url:
                filename = found_filename or f"Flezen Video ({file_id[:8]})"
                from terabox import format_size
                return {
                    "ok": True,
                    "platform": "Flezen",
                    "filename": filename,
                    "download_url": found_url,
                    "size": found_size,
                    "formatted_size": format_size(found_size) if found_size else "Direct Stream",
                    "thumbnail": found_thumb,
                    "engine": "fz (Playwright)",
                    "file_id": file_id,
                }
    except ImportError:
        pass
    except Exception as e:
        print(f"Playwright error in flezen: {e}")

    return {
        "ok": False,
        "platform": "Flezen",
        "error": "Flezen extraction failed. Request blocked. Please ensure Playwright is installed or configure a 'FLEZEN_API_URL'.",
        "file_id": file_id
    }
