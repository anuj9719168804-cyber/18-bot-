import re
import os
import urllib.parse
import aiohttp
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Supported TeraBox and mirror domains
TERABOX_DOMAINS = [
    "terabox.com",
    "teraboxapp.com",
    "terabox.app",
    "1024tera.com",
    "1024terabox.com",
    "teraboxlink.com",
    "terasharelink.com",
    "terafileshare.com",
    "teraboxshare.com",
    "mirrobox.com",
    "nephobox.com",
    "freeterabox.com",
    "4funbox.com",
    "momerybox.com",
    "tibibox.com",
    "dubox.com",
]

def is_terabox_url(url: str) -> bool:
    """Checks if a given string contains a recognized TeraBox URL."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        netloc = parsed.netloc.lower()
        if not netloc:
            return False
        if any(domain in netloc for domain in TERABOX_DOMAINS):
            return True
        return bool(re.search(r"(terabox|1024tera|mirrobox|nephobox|freeterabox|4funbox|momerybox|tibibox|dubox|terashare)", netloc))
    except Exception:
        return False

def format_size(size_bytes: int) -> str:
    """Formats bytes into human readable size."""
    if not size_bytes or size_bytes <= 0:
        return "Unknown size"
    units = ["B", "KB", "MB", "GB", "TB"]
    unit_idx = 0
    size = float(size_bytes)
    while size >= 1024.0 and unit_idx < len(units) - 1:
        size /= 1024.0
        unit_idx += 1
    return f"{size:.2f} {units[unit_idx]}"

async def resolve_url(session: aiohttp.ClientSession, url: str) -> str:
    """Follows redirects to obtain the final expanded URL."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    }
    try:
        async with session.get(url, headers=headers, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            return str(resp.url)
    except Exception:
        return url

def extract_surl(url: str) -> Optional[str]:
    """Extracts the surl / key from standard, shortened, and mirror TeraBox URLs."""
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url.strip())
        query_params = urllib.parse.parse_qs(parsed.query)
        if "surl" in query_params and query_params["surl"]:
            return query_params["surl"][0].strip()

        s_match = re.search(r"/s/([a-zA-Z0-9_-]+)", url)
        if s_match:
            return s_match.group(1).strip()
    except Exception:
        pass
    return None

def parse_file_item(item: Dict[str, Any], engine: str) -> Optional[Dict[str, Any]]:
    """Extracts standardized file information from an API response object."""
    if not isinstance(item, dict):
        return None
        
    print(f"KEYS: {list(item.keys())}", flush=True)
    if 'fast_play' in item:
        print(f"FAST PLAY: {item['fast_play']}", flush=True)
    
    dlink = item.get("dlink") or item.get("download_url") or item.get("direct_link") or item.get("url")
    if not dlink:
        return None

    filename = (
        item.get("server_filename")
        or item.get("filename")
        or item.get("file_name")
        or item.get("name")
        or "Video File"
    )
    
    try:
        size = int(item.get("size", 0))
    except (ValueError, TypeError):
        size = 0

    thumb = None
    thumbs = item.get("thumbs")
    if isinstance(thumbs, dict):
        thumb = thumbs.get("url3") or thumbs.get("url2") or thumbs.get("url1")
    elif isinstance(item.get("thumbnail"), str):
        thumb = item.get("thumbnail")
    elif isinstance(item.get("thumb"), str):
        thumb = item.get("thumb")

    return {
        "ok": True,
        "filename": filename,
        "size": size,
        "formatted_size": format_size(size),
        "download_url": dlink,
        "thumbnail": thumb,
        "fs_id": item.get("fs_id"),
        "engine": engine,
    }

async def _extract_share_list(session: aiohttp.ClientSession, surl: str, headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Method 1: Official sharing list API across mirror endpoints."""
    keys = [surl]
    if surl.startswith("1"):
        keys.append(surl[1:])
    else:
        keys.append(f"1{surl}")

    domains = ["https://www.terabox.app", "https://1024tera.com"]
    for domain in domains:
        for k in keys:
            api_url = f"{domain}/share/list?app_id=250528&shorturl={k}&root=1"
            req_headers = {**headers, "Referer": f"{domain}/sharing/link?surl={keys[-1]}"}
            try:
                async with session.get(api_url, headers=req_headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if data.get("errno") == 0 and "list" in data and data["list"]:
                            # Look for non-directory downloadable item first
                            for item in data["list"]:
                                if not item.get("isdir"):
                                    res = parse_file_item(item, engine="get (share/list)")
                                    if res:
                                        return res
                            # Fallback to first item if none matched
                            res = parse_file_item(data["list"][0], engine="get (share/list)")
                            if res:
                                return res
            except Exception:
                continue
    return None

async def _extract_shorturlinfo(session: aiohttp.ClientSession, surl: str, headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Method 2: shorturlinfo endpoint across mirror endpoints."""
    keys = [surl if surl.startswith("1") else f"1{surl}", surl.lstrip("1")]
    domains = ["https://www.terabox.app", "https://1024tera.com"]
    for domain in domains:
        for k in keys:
            info_url = f"{domain}/api/shorturlinfo?shorturl={k}&root=1"
            try:
                async with session.get(info_url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if data.get("errno") == 0 and "list" in data and data["list"]:
                            for item in data["list"]:
                                if not item.get("isdir"):
                                    res = parse_file_item(item, engine="exp (shorturlinfo)")
                                    if res:
                                        return res
                            res = parse_file_item(data["list"][0], engine="exp (shorturlinfo)")
                            if res:
                                return res
            except Exception:
                continue
    return None

async def _extract_custom_api(session: aiohttp.ClientSession, target_url: str) -> Optional[Dict[str, Any]]:
    """Method 3: Custom or third-party fallback extraction API."""
    fallback_api = os.getenv("TERABOX_API_URL", "").strip()
    if not fallback_api:
        return None
    try:
        separator = "&" if "?" in fallback_api else "?"
        endpoint = f"{fallback_api}{separator}url={urllib.parse.quote(target_url)}"
        async with session.get(endpoint, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                item = None
                if isinstance(data, list) and data:
                    item = data[0]
                elif isinstance(data, dict):
                    if "data" in data and isinstance(data["data"], list) and data["data"]:
                        item = data["data"][0]
                    elif "data" in data and isinstance(data["data"], dict):
                        item = data["data"]
                    else:
                        item = data

                if item:
                    res = parse_file_item(item, engine="custom_api")
                    if res:
                        return res
    except Exception:
        pass
    return None

async def _extract_playwright(url: str, cookie: str) -> Optional[Dict[str, Any]]:
    """Method 4: Playwright Headless Browser fallback to bypass verify_v2 and get fast stream."""
    try:
        from playwright.async_api import async_playwright
        logger.info(f"[Playwright] Launching Chromium to extract: {url}")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled'
                ]
            )
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                viewport={'width': 1280, 'height': 720}
            )
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            if cookie:
                cookie_val = cookie.replace("ndus=", "").strip()
                domains = [".terabox.com", ".terabox.app", ".1024tera.com", ".1024terabox.com", ".teraboxlink.com", "www.terabox.app", "www.1024tera.com"]
                cookies_payload = [
                    {"name": "ndus", "value": cookie_val, "domain": d, "path": "/"}
                    for d in domains
                ]
                await context.add_cookies(cookies_payload)
                
            page = await context.new_page()
            found_res = None
            file_meta = {}
            
            async def handle_response(response):
                nonlocal found_res, file_meta
                # Capture metadata and dlink from share/list or shorturlinfo
                if 'share/list' in response.url or 'shorturlinfo' in response.url:
                    try:
                        d = await response.json()
                        if d.get("list"):
                            item = d["list"][0]
                            file_meta["filename"] = item.get("server_filename") or file_meta.get("filename") or "Video.mp4"
                            if item.get("size"):
                                file_meta["size"] = int(item["size"])
                            if item.get("dlink"):
                                file_meta["dlink"] = item["dlink"]
                            logger.info(f"[Playwright] Found file metadata: {file_meta.get('filename')} ({file_meta.get('size')} bytes)")
                    except Exception:
                        pass
                
                # Capture M3U8 video stream or derive 480p from subtitle stream
                if 'share/streaming' in response.url:
                    if 'M3U8_SUBTITLE' not in response.url and ('M3U8' in response.url or 'm3u8' in response.url):
                        fn = file_meta.get("filename", "Video.mp4")
                        sz = file_meta.get("size", 0)
                        found_res = {
                            "ok": True,
                            "filename": fn,
                            "size": sz,
                            "formatted_size": format_size(sz) if sz else "Direct Stream",
                            "download_url": response.url,
                            "is_m3u8": True,
                            "engine": "Playwright Streaming"
                        }
                        logger.info(f"[Playwright] Captured direct video M3U8 stream for: {fn}")
                    elif 'M3U8_SUBTITLE' in response.url:
                        # Auto-derive 480p video M3U8
                        derived_url = re.sub(r'type=M3U8_SUBTITLE_[A-Z0-9]+', 'type=M3U8_AUTO_480', response.url)
                        if not found_res:
                            fn = file_meta.get("filename", "Video.mp4")
                            sz = file_meta.get("size", 0)
                            found_res = {
                                "ok": True,
                                "filename": fn,
                                "size": sz,
                                "formatted_size": format_size(sz) if sz else "Direct Stream",
                                "download_url": derived_url,
                                "is_m3u8": True,
                                "engine": "Playwright Auto 480p"
                            }
                            logger.info(f"[Playwright] Derived 480p video M3U8 stream for: {fn}")
                    
            page.on('response', handle_response)
            
            try:
                try:
                    await page.goto(url, wait_until='load', timeout=18000)
                    logger.info("[Playwright] Page loaded successfully.")
                except Exception as e:
                    logger.warning(f"[Playwright] Page goto timeout/warning: {e}")
                
                # Trigger video playback to fire streaming endpoint
                for sel in ['.vjs-big-play-button', 'video', '.play-icon', '.video-player', '.wp-btn']:
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            await el.click()
                            logger.info(f"[Playwright] Clicked {sel}")
                            break
                    except Exception:
                        pass

                for _ in range(16):
                    if found_res and found_res.get('is_m3u8'):
                        break
                    await asyncio.sleep(0.5)
                    
            except Exception as e:
                logger.warning(f"[Playwright] Page interaction error: {e}")
            finally:
                # Fallback to direct dlink from share/list if no M3U8 was triggered
                if not found_res and file_meta.get("dlink"):
                    fn = file_meta.get("filename", "Video.mp4")
                    sz = file_meta.get("size", 0)
                    found_res = {
                        "ok": True,
                        "filename": fn,
                        "size": sz,
                        "formatted_size": format_size(sz) if sz else "Direct Link",
                        "download_url": file_meta["dlink"],
                        "is_m3u8": False,
                        "engine": "Playwright Direct"
                    }
                    logger.info(f"[Playwright] Fallback to direct link for: {fn}")
                    
                try:
                    if found_res:
                        cookies_list = await context.cookies()
                        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies_list])
                        found_res["cookie_str"] = cookie_str
                except Exception:
                    pass
                await browser.close()
                
            return found_res
    except ImportError as e:
        logger.error(f"[Playwright] Import error: {e}")
    except Exception as e:
        logger.error(f"[Playwright] Playwright error in terabox: {e}", exc_info=True)
    return None

async def get_terabox_info(
    url: str,
    custom_cookie: Optional[str] = None,
    mode: str = "get"
) -> Dict[str, Any]:
    """
    Fetches file information and download link for a given TeraBox URL.
    Supports download modes: 'get', 'exp', 'exphd', 'all'.
    """
    timeout = aiohttp.ClientTimeout(total=30)
    cookie = custom_cookie or os.getenv("TERABOX_COOKIE", "").strip()

    async with aiohttp.ClientSession(timeout=timeout) as session:
        final_url = await resolve_url(session, url)
        surl = extract_surl(final_url) or extract_surl(url)

        if not surl:
            return {
                "ok": False,
                "error": "Could not extract sharing ID/surl from the provided link. Please ensure it is a valid TeraBox share link."
            }

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if cookie:
            headers["Cookie"] = f"ndus={cookie}" if "ndus=" not in cookie else cookie

        # Route according to mode
        mode_lower = (mode or "get").lower()

        playwright_url = f"https://www.terabox.app/sharing/link?surl={surl}" if surl else final_url

        # Playwright is the most reliable modern extractor across all modes
        methods = [
            lambda: _extract_playwright(playwright_url, cookie),
            lambda: _extract_custom_api(session, final_url),
            lambda: _extract_shorturlinfo(session, surl, headers),
            lambda: _extract_share_list(session, surl, headers),
        ]

        for method in methods:
            result = await method()
            if result and result.get("ok"):
                return result

        cookie_hint = "" if cookie else "\n💡 Tip: Adding your TeraBox `TERABOX_COOKIE` (ndus) in .env significantly increases link extraction success rate."
        return {
            "ok": False,
            "error": f"Failed to fetch video info with engine mode '{mode_lower}'. The link may have expired, been deleted, or requires an active session cookie (ndus).{cookie_hint}"
        }
