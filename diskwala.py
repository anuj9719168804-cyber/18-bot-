import re
import os
import urllib.parse
import aiohttp
from typing import Dict, Any, Optional

# Supported DiskWala domains
DISKWALA_DOMAINS = [
    "diskwala.com",
    "www.diskwala.com",
    "playdiskwala.in",
    "thediskwala.com",
]

def is_diskwala_url(url: str) -> bool:
    """Checks if a given string contains a recognized DiskWala URL."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        netloc = parsed.netloc.lower()
        if not netloc:
            return False
        return any(d in netloc for d in DISKWALA_DOMAINS) or "diskwala" in netloc
    except Exception:
        return False

def extract_diskwala_id(url: str) -> Optional[str]:
    """Extracts the file or content ID from standard DiskWala URLs."""
    if not url:
        return None
    try:
        # Match standard /app/<id>, /c/<code>, /creator/<id>, /playlist/<id>
        match = re.search(r"/(?:app|c|creator|playlist)/([a-zA-Z0-9_-]+)", url)
        if match:
            return match.group(1).strip()

        # Match generic trailing ID or query parameter
        parsed = urllib.parse.urlparse(url.strip())
        qs = urllib.parse.parse_qs(parsed.query)
        if "id" in qs and qs["id"]:
            return qs["id"][0].strip()

        # Fallback for direct alphanumeric hash in path
        match = re.search(r"diskwala[^/]+/([a-zA-Z0-9_-]{8,})", url)
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    return None

async def get_diskwala_info(url: str, mode: str = "dw") -> Dict[str, Any]:
    """
    Fetches video info for a DiskWala link ONLY if direct stream can be resolved.
    """
    file_id = extract_diskwala_id(url)
    if not file_id:
        return {
            "ok": False,
            "error": "Could not extract DiskWala content ID from the provided link."
        }

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 1. Custom / proxy fallback API if set in environment
        custom_api = (
            os.getenv("DISKWALA_API_URL", "").strip()
            or os.getenv("TERABOX_API_URL", "").strip()
        )
        
        proxy_api_key = os.getenv("DISKWALA_API_KEY", "").strip()
        headers = {}
        if proxy_api_key:
            headers["x-api-key"] = proxy_api_key

        if custom_api:
            try:
                sep = "&" if "?" in custom_api else "?"
                target = f"{custom_api}{sep}url={urllib.parse.quote(url)}"
                async with session.get(target, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        stream_url = (
                            data.get("url")
                            or data.get("download_url")
                            or data.get("dlink")
                        )
                        if stream_url:
                            filename = data.get("title") or f"DiskWala Video ({file_id[:8]})"
                            size = int(data.get("size", 0))
                            return {
                                "ok": True,
                                "platform": "DiskWala",
                                "filename": filename,
                                "download_url": stream_url,
                                "size": size,
                                "formatted_size": data.get("formatted_size", "Direct Stream"),
                                "thumbnail": data.get("thumb") or data.get("thumbnail"),
                                "engine": "dw (API)",
                                "file_id": file_id,
                            }
            except Exception:
                pass

        # 2. Attempt to query high-speed web player API endpoints (often blocked by AppiCrypt/Cloudflare)
        gateways = [
            f"https://playdiskwala.in/api/diskwala-free?url={urllib.parse.quote(url)}",
            f"https://thediskwala.com/api/diskwala-free?url={urllib.parse.quote(url)}",
        ]
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": "https://playdiskwala.in/"
        }
        for gw in gateways:
            try:
                async with session.get(gw, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if data.get("success") and data.get("url"):
                            filename = data.get("title") or f"DiskWala Video ({file_id[:8]})"
                            return {
                                "ok": True,
                                "platform": "DiskWala",
                                "filename": filename,
                                "download_url": data.get("url"),
                                "size": int(data.get("size", 0)),
                                "formatted_size": "Direct Stream",
                                "thumbnail": data.get("thumb"),
                                "engine": "dw (Direct Stream)",
                                "file_id": file_id,
                            }
            except Exception:
                pass

    # 3. Attempt Playwright Headless Browser extraction (Bypasses AppiCrypt)
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
                if 'temp_info' in response.url and response.request.method == 'GET':
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
                filename = found_filename or f"DiskWala Video ({file_id[:8]})"
                from terabox import format_size
                return {
                    "ok": True,
                    "platform": "DiskWala",
                    "filename": filename,
                    "download_url": found_url,
                    "size": found_size,
                    "formatted_size": format_size(found_size) if found_size else "Direct Stream",
                    "thumbnail": found_thumb,
                    "engine": "dw (Playwright)",
                    "file_id": file_id,
                }
    except ImportError:
        pass
    except Exception as e:
        print(f"Playwright error: {e}")

    # DiskWala uses AppiCrypt which prevents direct URL scraping without a valid proxy or headless browser.
    return {
        "ok": False,
        "error": "DiskWala extraction failed. AppiCrypt blocked the request. Please ensure Playwright is installed or configure a 'DISKWALA_API_URL'.",
        "platform": "DiskWala",
        "file_id": file_id
    }
