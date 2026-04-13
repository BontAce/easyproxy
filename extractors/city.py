import asyncio
import logging
import re
import json
import base64
from typing import Dict, Any
import random
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector
from urllib.parse import urlparse, parse_qs, urljoin
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    """Eccezione personalizzata per errori di estrazione."""
    pass

class CityExtractor:
    
    def __init__(self, request_headers: dict, proxies: list = None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.5",
            "accept-encoding": "gzip, deflate",
            "connection": "keep-alive",
        }
        self.session = None
        self.mediaflow_endpoint = "hls_manifest_proxy"
        self._session_lock = asyncio.Lock()
        self.proxies = proxies or []

    def _get_random_proxy(self):
        """Restituisce un proxy casuale dalla lista."""
        return random.choice(self.proxies) if self.proxies else None

    async def _get_session(self):
        """Ottiene una sessione HTTP persistente."""
        if self.session is None or self.session.closed:
            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            proxy = self._get_random_proxy()
            if proxy:
                logger.info(f"Using proxy {proxy} for City session.")
                connector = ProxyConnector.from_url(proxy)
            else:
                connector = TCPConnector(
                    limit=0,
                    limit_per_host=0,
                    keepalive_timeout=30,
                    enable_cleanup_closed=True,
                    force_close=False,
                    use_dns_cache=True
                )
            self.session = ClientSession(
                timeout=timeout,
                connector=connector,
                headers=self.base_headers,
                cookie_jar=aiohttp.CookieJar()
            )
        return self.session

    async def _make_robust_request(self, url: str, headers: dict = None, retries=3, initial_delay=2):
        """Effettua richieste HTTP robuste con retry automatico."""
        final_headers = headers or {}
        
        for attempt in range(retries):
            try:
                session = await self._get_session()
                logger.info(f"Attempt {attempt + 1}/{retries} for URL: {url}")
                
                async with session.get(url, headers=final_headers) as response:
                    # Non usiamo raise_for_status() per catturare il corpo della risposta anche in caso di errore
                    text = await response.text()
                    
                    class MockResponse:
                        def __init__(self, text_content, status, headers_dict, url):
                            self.text = text_content
                            self.status = status
                            self.headers = headers_dict
                            self.url = url
                        
                        def raise_for_status(self):
                            if self.status >= 400:
                                raise aiohttp.ClientResponseError(
                                    request_info=None,
                                    history=None,
                                    status=self.status
                                )
                    
                    if response.status == 200:
                        logger.info(f"✅ Request successful for {url} at attempt {attempt + 1}")
                        return MockResponse(text, response.status, response.headers, response.url)
                    else:
                        logger.warning(f"⚠️ Request failed for {url} with status {response.status} at attempt {attempt + 1}")
                        if attempt == retries - 1:
                            raise ExtractorError(f"Failed to load page: {response.status}")
                        
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientPayloadError,
                asyncio.TimeoutError,
                OSError,
                ConnectionResetError
            ) as e:
                logger.warning(f"⚠️ Connection error attempt {attempt + 1} for {url}: {str(e)}")
                if attempt < retries - 1:
                    delay = initial_delay * (2 ** attempt)
                    await asyncio.sleep(delay)
                else:
                    raise ExtractorError(f"All {retries} attempts failed for {url}: {str(e)}")
            except Exception as e:
                logger.error(f"❌ Non-network error attempt {attempt + 1} for {url}: {str(e)}")
                if attempt == retries - 1:
                    raise ExtractorError(f"Final error for {url}: {str(e)}")
                await asyncio.sleep(initial_delay)

    def atob_fixed(self, data: str) -> str:
        try:
            return base64.b64decode(data).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    def extract_json_array(self, decoded: str):
        start = decoded.find("file:")
        if start == -1:
            start = decoded.find("sources:")
        if start == -1:
            return None

        start = decoded.find("[", start)
        if start == -1:
            return None

        depth = 0
        for i in range(start, len(decoded)):
            if decoded[i] == "[":
                depth += 1
            elif decoded[i] == "]":
                depth -= 1
            if depth == 0:
                return decoded[start : i + 1]

        return None

    def pick_stream(self, file_data, season: int = 1, episode: int = 1):
        if isinstance(file_data, str):
            return file_data

        if isinstance(file_data, list):
            if all(isinstance(x, dict) and "file" in x for x in file_data):
                idx = max(0, episode - 1)
                return file_data[idx]["file"]

            selected_season = None
            for s in file_data:
                if not isinstance(s, dict):
                    continue
                folder = s.get("folder")
                if not folder:
                    continue
                title = (s.get("title") or "").lower()
                if re.search(rf"(season|s)\s*0*{season}\b", title):
                    selected_season = folder
                    break

            if not selected_season:
                for s in file_data:
                    folder = s.get("folder")
                    if folder:
                        selected_season = folder
                        break

            if not selected_season:
                return None

            idx = max(0, episode - 1)
            # Fix: access through index safely
            if isinstance(selected_season, list):
                target = selected_season[idx] if idx < len(selected_season) else selected_season[0]
                return target.get("file") if isinstance(target, dict) else None
            return None

        return None

    async def extract(self, url: str, season: int = 1, episode: int = 1, **kwargs) -> Dict[str, Any]:
        """Main extraction entry point"""
        try:
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            if "s" in query:
                try:
                    season = int(query["s"][0])
                except Exception:
                    pass
            if "e" in query:
                try:
                    episode = int(query["e"][0])
                except Exception:
                    pass

            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

            # Cookie hardcoded (base64)
            cookie_b64 = "ZGxlX3VzZXJfaWQ9MzI3Mjk7IGRsZV9wYXNzd29yZD04OTQxNzFjNmE4ZGFiMThlZTU5NGQ1YzY1MjAwOWEzNTs="
            hardcoded_cookie = base64.b64decode(cookie_b64).decode()

            # Cattura i cookie e l'User-Agent in entrata
            req_headers = kwargs.get("request_headers", {}) or self.request_headers
            incoming_cookie = next((v for k, v in req_headers.items() if k.lower() == "cookie"), None)
            incoming_ua = next((v for k, v in req_headers.items() if k.lower() == "user-agent"), self.base_headers.get("user-agent"))
            
            # Unisci i cookie
            final_cookie = hardcoded_cookie
            if incoming_cookie:
                if not final_cookie.endswith(";") and final_cookie:
                    final_cookie += ";"
                final_cookie = f"{final_cookie} {incoming_cookie}"

            headers = {
                "User-Agent": incoming_ua,
                "Referer": clean_url,
                "Cookie": final_cookie,
            }

            response = await self._make_robust_request(clean_url, headers=headers)
            
            soup = BeautifulSoup(response.text, "lxml")
            
            # ✅ NUOVO: Cerca l'URL del player per usarlo come Referer
            player_referer = clean_url
            iframe = soup.find("iframe", src=re.compile(r"player\.php"))
            if iframe:
                player_referer = urljoin(clean_url, iframe["src"])
                logger.info(f"📍 Found player referer: {player_referer}")

            file_data = None

            for script in soup.find_all("script"):
                if file_data:
                    break

                script_html = script.string or script.text or ""
                if "atob" not in script_html:
                    continue

                matches = re.finditer(r'atob\(\s*[\'"](.*?)[\'"]\s*\)', script_html)
                for match in matches:
                    encoded = match.group(1)
                    decoded = self.atob_fixed(encoded)
                    if not decoded:
                        continue

                    raw_json = self.extract_json_array(decoded)
                    if raw_json:
                        try:
                            raw_json = re.sub(r'\\(.)', r'\1', raw_json)
                            file_data = json.loads(raw_json)
                        except Exception:
                            file_data = raw_json
                        break

                    file_match = re.search(r'file\s*:\s*[\'"](.*?)[\'"]', decoded, re.S)
                    if file_match:
                        file_data = file_match.group(1)
                        break

            if not file_data:
                raise ExtractorError("No stream found")

            stream_url = self.pick_stream(file_data, season=season, episode=episode)
            if not stream_url:
                raise ExtractorError("Stream extraction failed")

            # Prepara headers finali includendo i cookie per l'upstream e Referer specifico
            stream_headers = {
                "User-Agent": incoming_ua,
                "Referer": player_referer,
                "Origin": f"{parsed.scheme}://{parsed.netloc}",
                "Accept": "*/*",
                "Accept-Language": req_headers.get("Accept-Language", "en-US,en;q=0.5"),
                "Connection": "keep-alive",
            }
            if final_cookie:
                stream_headers["Cookie"] = final_cookie

            logger.info(f"✅ City URL extracted successfully: {stream_url}")

            return {
                "destination_url": stream_url,
                "request_headers": stream_headers,
                "mediaflow_endpoint": self.mediaflow_endpoint,
            }

        except Exception as e:
            logger.error(f"❌ City extraction failed: {str(e)}")
            if isinstance(e, ExtractorError):
                raise e
            raise ExtractorError(f"City extraction completely failed: {str(e)}")

    async def close(self):
        """Chiude definitivamente la sessione."""
        if self.session and not self.session.closed:
            try:
                await self.session.close()
            except:
                pass
            self.session = None
