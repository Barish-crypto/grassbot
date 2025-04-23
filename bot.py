import argparse
import asyncio
import json
import random
import ssl
import time
import uuid
import base64
import aiohttp
import aiofiles
import sys
from aiohttp import ClientSession
from aiohttp_socks import ProxyConnector, ProxyType
from colorama import Fore, Style, init
from loguru import logger
from websockets_proxy import Proxy, proxy_connect
from urllib.parse import urlparse

# Thiết lập chính sách vòng lặp sự kiện cho Windows
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Khởi tạo colorama
init(autoreset=True)

# Cấu hình
CONFIG_FILE = "config.json"
DEVICE_FILE = "devices.json"
PROXY_FILE = "proxy.txt"
PING_INTERVAL = 90
CHECKIN_INTERVAL = 300
DIRECTOR_SERVER = "https://director.getgrass.io"

# Phân tích tham số dòng lệnh
parser = argparse.ArgumentParser(description="GrassBot - WebSocket Automation")
parser.add_argument("-p", "--proxy-file", help="Path to proxy file")
args = parser.parse_args()
if args.proxy_file:
    PROXY_FILE = args.proxy_file

# Tiêu đề HTTP
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/132.0.0.0 Safari/537.36 OPR/117.0.0.0",
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "chrome-extension://lkbnfiajjmbhnfledhphioinpickokdi",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive"
}

# Mẫu lỗi proxy
ERROR_PATTERNS = [
    "Host unreachable", "[SSL: WRONG_VERSION_NUMBER]", "invalid length of packed IP address string",
    "Empty connect reply", "Device creation limit exceeded", "sent 1011 (internal error) keepalive ping timeout",
    "SOCKS5 authentication failed", "SOCKS5 connection refused", "[SSL: CERTIFICATE_VERIFY_FAILED]"
]

# Proxy bị cấm
BANNED_PROXIES = {}

def parse_proxy(proxy_url: str) -> tuple:
    """Parse proxy URL to extract scheme, host, port, username, password."""
    parsed = urlparse(proxy_url)
    return parsed.scheme.lower(), parsed.hostname, parsed.port, parsed.username, parsed.password

async def get_ws_endpoints(device_id: str, user_id: str, proxy_url: str) -> tuple:
    """Fetch WebSocket endpoints and token from director server."""
    url = f"{DIRECTOR_SERVER}/checkin"
    data = {
        "browserId": device_id, "userId": user_id, "version": "5.1.1",
        "extensionId": "lkbnfiajjmbhnfledhphioinpickokdi", "userAgent": HEADERS["User-Agent"],
        "deviceType": "extension"
    }

    scheme, host, port, username, password = parse_proxy(proxy_url) if proxy_url else (None, None, None, None, None)
    ssl_context = ssl._create_unverified_context()

    try:
        connector = ProxyConnector(proxy_type=ProxyType.SOCKS5, host=host, port=port, username=username, password=password, rdns=True) if proxy_url and scheme == 'socks5' else None
        async with ClientSession(connector=connector) as session:
            async with session.post(url, json=data, headers=HEADERS, proxy=proxy_url if proxy_url and scheme != 'socks5' else None, ssl=ssl_context) as response:
                if response.status != 201:
                    logger.error(f"Check-in failed: Status {response.status}")
                    return [], ""
                result = await response.json(content_type=None)
                return [f"wss://{dest}" for dest in result.get("destinations", [])], result.get("token", "")
    except Exception as e:
        logger.error(f"Failed to fetch endpoints with proxy {proxy_url}: {e}")
        return [], ""

class WebSocketClient:
    """Handles WebSocket connection, maintains active proxy, and pings continuously."""
    def __init__(self, proxy_url: str, device_id: str, user_id: str):
        self.proxy_url = proxy_url
        self.device_id = device_id
        self.user_id = user_id
        self.uri = None
        self.is_connected = False

    async def connect(self) -> bool:
        """Connect to WebSocket server and maintain connection with active proxy."""
        logger.info(f"🖥️ Device: {self.device_id} | Proxy: {self.proxy_url}")
        while True:
            if self.is_connected:
                await asyncio.sleep(5)
                continue

            try:
                endpoints, token = await get_ws_endpoints(self.device_id, self.user_id, self.proxy_url)
                if not endpoints or not token:
                    logger.error("No valid WebSocket endpoints or token received")
                    return False
                self.uri = f"{endpoints[0]}?token={token}"
                logger.info(f"Connecting to {self.uri}")

                ssl_context = ssl._create_unverified_context()
                proxy = Proxy.from_url(self.proxy_url) if self.proxy_url else None
                async with proxy_connect(self.uri, proxy=proxy, ssl=ssl_context, extra_headers=HEADERS) as websocket:
                    self.is_connected = True
                    logger.info(f"✅ Connected with proxy {self.proxy_url}")
                    ping_task = asyncio.create_task(self._send_ping(websocket))
                    checkin_task = asyncio.create_task(self._periodic_checkin())
                    try:
                        await self._handle_messages(websocket)
                    finally:
                        self.is_connected = False
                        ping_task.cancel()
                        checkin_task.cancel()
                        await asyncio.gather(ping_task, checkin_task, return_exceptions=True)
            except Exception as e:
                self.is_connected = False
                logger.error(f"🚫 Proxy {self.proxy_url} error: {e}")
                if any(pattern in str(e) for pattern in ERROR_PATTERNS) or "Rate limited" in str(e):
                    logger.info(f"❌ Banning proxy {self.proxy_url}")
                    BANNED_PROXIES[self.proxy_url] = time.time() + 3600
                    return False
                await asyncio.sleep(5)

    async def _send_ping(self, websocket) -> None:
        """Send periodic PING to maintain connection."""
        while self.is_connected:
            try:
                await websocket.send(json.dumps({
                    "id": str(uuid.uuid4()), "version": "1.0.0", "action": "PING", "data": {}
                }))
                logger.debug(f"📤 Sent PING with proxy {self.proxy_url}")
                await asyncio.sleep(PING_INTERVAL)
            except Exception as e:
                logger.error(f"🚫 PING error: {e}")
                self.is_connected = False
                break

    async def _periodic_checkin(self) -> None:
        """Perform periodic check-in with director server."""
        while self.is_connected:
            await asyncio.sleep(CHECKIN_INTERVAL)
            await get_ws_endpoints(self.device_id, self.user_id, self.proxy_url)

    async def _handle_messages(self, websocket) -> None:
        """Process incoming WebSocket messages."""
        handlers = {"AUTH": self._handle_auth, "PONG": self._handle_pong, "HTTP_REQUEST": self._handle_http_request}
        while self.is_connected:
            try:
                message = json.loads(await websocket.recv())
                logger.info(f"📥 Received: {message}")
                if handler := handlers.get(message.get("action")):
                    await handler(websocket, message)
                else:
                    logger.error(f"No handler for action: {message.get('action')}")
            except Exception as e:
                logger.error(f"🚫 Message handling error: {e}")
                self.is_connected = False
                break

    async def _handle_auth(self, websocket, message) -> None:
        """Handle AUTH message."""
        await websocket.send(json.dumps({
            "id": message["id"], "origin_action": "AUTH",
            "result": {
                "browser_id": self.device_id, "user_id": self.user_id, "user_agent": HEADERS["User-Agent"],
                "timestamp": int(time.time()), "device_type": "extension", "version": "5.1.1"
            }
        }))

    async def _handle_pong(self, websocket, message) -> None:
        """Handle PONG message."""
        await websocket.send(json.dumps({"id": message["id"], "origin_action": "PONG"}))

    async def _handle_http_request(self, websocket, message) -> None:
        """Handle HTTP_REQUEST message."""
        data = message.get("data", {})
        method = data.get("method", "GET").upper()
        url = data.get("url")
        req_headers = data.get("headers", {})
        body = data.get("body")

        scheme, host, port, username, password = parse_proxy(self.proxy_url) if self.proxy_url else (None, None, None, None, None)
        ssl_context = ssl._create_unverified_context()

        try:
            connector = ProxyConnector(proxy_type=ProxyType.SOCKS5, host=host, port=port, username=username, password=password, rdns=True) if self.proxy_url and scheme == 'socks5' else None
            async with ClientSession(connector=connector) as session:
                async with session.request(method, url, headers=req_headers, data=body, proxy=self.proxy_url if self.proxy_url and scheme != 'socks5' else None, ssl=ssl_context) as resp:
                    if resp.status == 429:
                        logger.error(f"HTTP request rate limited for proxy {self.proxy_url}")
                        raise Exception("Rate limited")
                    resp_headers = dict(resp.headers)
                    body_b64 = base64.b64encode(await resp.read()).decode()
                    await websocket.send(json.dumps({
                        "id": message["id"], "origin_action": "HTTP_REQUEST",
                        "result": {"url": url, "status": resp.status, "status_text": "", "headers": resp_headers, "body": body_b64}
                    }))
        except Exception as e:
            logger.error(f"HTTP request error: {e}")
            raise

class ProxyManager:
    """Manages proxies, retains working proxies, and rotates on failure."""
    def __init__(self, device_ids: list, user_id: str):
        self.device_ids = device_ids
        self.user_id = user_id
        self.active_proxies = {}  # {device_id: proxy_url}
        self.all_proxies = set()

    async def load_proxies(self) -> None:
        """Load proxies from proxy.txt."""
        try:
            async with aiofiles.open(PROXY_FILE, "r") as file:
                self.all_proxies = {line.strip() for line in await file.readlines() if line.strip()}
        except Exception as e:
            logger.error(f"Failed to load proxies: {e}")

    async def start(self, max_proxies: int) -> None:
        """Start proxy management and WebSocket connections."""
        await self.load_proxies()
        if not self.all_proxies:
            logger.error("No proxies found in proxy.txt")
            return

        available_proxies = {p for p in self.all_proxies if p not in BANNED_PROXIES or time.time() >= BANNED_PROXIES[p]}
        if not available_proxies:
            logger.error("No available proxies (all banned)")
            return

        for device_id, proxy in zip(self.device_ids, random.sample(list(available_proxies), min(len(available_proxies), max_proxies))):
            self.active_proxies[device_id] = proxy

        tasks = {asyncio.create_task(self._run_client(self.active_proxies[device_id], device_id)): (self.active_proxies[device_id], device_id) for device_id in self.device_ids if device_id in self.active_proxies}

        while tasks:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                proxy, device_id = tasks.pop(task)
                if not task.result():
                    logger.error(f"Proxy {proxy} failed for device {device_id}; rotating")
                    self.active_proxies.pop(device_id, None)
                    await self.load_proxies()
                    remaining = {p for p in self.all_proxies if p not in self.active_proxies.values() and (p not in BANNED_PROXIES or time.time() >= BANNED_PROXIES[p])}
                    if remaining:
                        new_proxy = random.choice(list(remaining))
                        self.active_proxies[device_id] = new_proxy
                        tasks[asyncio.create_task(self._run_client(new_proxy, device_id))] = (new_proxy, device_id)
                    else:
                        logger.error(f"No available proxies for device {device_id}")

    async def _run_client(self, proxy: str, device_id: str) -> bool:
        """Run WebSocket client with proxy and device_id."""
        return await WebSocketClient(proxy, device_id, self.user_id).connect()

def setup_logger() -> None:
    """Configure logger for file and terminal output."""
    logger.remove()
    logger.add("bot.log", format="<level>{level}</level> | <cyan>{message}</cyan>", level="INFO", rotation="1 day")
    logger.add(sys.stdout, format="<level>{level}</level> | <cyan>{message}</cyan>", level="INFO", colorize=True)

async def load_user_config() -> dict:
    """Load user IDs from config.json."""
    try:
        with open(CONFIG_FILE, "r") as file:
            return json.load(file)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return {}

async def load_device_ids() -> list:
    """Load device IDs from devices.json."""
    try:
        with open(DEVICE_FILE, "r") as file:
            return json.load(file).get("device_ids", [])
    except Exception as e:
        logger.error(f"Failed to load device IDs: {e}")
        return []

async def save_device_ids(device_ids: list) -> None:
    """Save device IDs to devices.json."""
    try:
        with open(DEVICE_FILE, "w") as file:
            json.dump({"device_ids": device_ids}, file, indent=4)
        logger.info("✅ Saved device IDs")
    except Exception as e:
        logger.error(f"Failed to save device IDs: {e}")

async def user_input() -> dict:
    """Prompt for user IDs."""
    user_ids = [uid.strip() for uid in input(f"{Fore.YELLOW}🔑 Enter USER IDs (comma-separated): {Style.RESET_ALL}").split(",") if uid.strip()]
    config = {"user_ids": user_ids}
    with open(CONFIG_FILE, "w") as file:
        json.dump(config, file, indent=4)
    logger.info(f"✅ Saved config: {user_ids}")
    return config

async def device_input(existing_count: int) -> list:
    """Prompt for device count or use existing devices."""
    if existing_count and input(f"{Fore.LIGHTYELLOW_EX}🔑 Use {existing_count} existing devices? (yes/no): {Style.RESET_ALL}").strip().lower() == "yes":
        return await load_device_ids()
    num_devices = int(input(f"{Fore.LIGHTYELLOW_EX}🔑 Enter number of devices: {Style.RESET_ALL}"))
    device_ids = [str(uuid.uuid4()) for _ in range(num_devices)]
    await save_device_ids(device_ids)
    return device_ids

async def main() -> None:
    """Run GrassBot."""
    print(f"{Fore.YELLOW + Style.BRIGHT}GrassBot{Style.RESET_ALL}\n{Fore.RED}========================================{Style.RESET_ALL}")
    setup_logger()

    config = await load_user_config()
    if not config.get("user_ids"):
        config = await user_input()

    device_ids = await device_input(len(await load_device_ids()))
    max_proxies = len(device_ids)

    for user_id in config["user_ids"]:
        logger.info(f"🚀 Starting with USER_ID: {user_id} | Max proxies: {max_proxies} | PING interval: {PING_INTERVAL}s")
        asyncio.create_task(ProxyManager(device_ids, user_id).start(max_proxies))

    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Shutting down gracefully...")
    except Exception as e:
        logger.error(f"❌ Critical error: {e}")