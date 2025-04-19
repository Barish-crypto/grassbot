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
from aiohttp import ClientSession, TCPConnector
from loguru import logger
from websockets_proxy import Proxy, proxy_connect
import platform

# Fix for Windows: Set SelectorEventLoop
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Constants
CONFIG_FILE = "config.json"
DEVICE_FILE = "devices.json"
PROXY_FILE = "proxy.txt"
PING_INTERVAL = 30
CHECKIN_INTERVAL = 300
DIRECTOR_SERVER = "https://director.getgrass.io"

# HTTP headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "chrome-extension://lkbnfiajjmbhnfledhphioinpickokdi"
}

# Error patterns for banning proxies
ERROR_PATTERNS = [
    "Host unreachable", "[SSL: WRONG_VERSION_NUMBER]", "invalid length of packed IP address string",
    "Empty connect reply", "Device creation limit exceeded", "sent 1011 (internal error) keepalive ping timeout",
    "aiodns needs a SelectorEventLoop"
]

BANNED_PROXIES = {}

async def get_ws_endpoints(device_id: str, user_id: str, proxy_url: str = None) -> tuple[list, str]:
    """Fetch WebSocket endpoints and token."""
    url = f"{DIRECTOR_SERVER}/checkin"
    payload = {
        "browserId": device_id, "userId": user_id, "version": "5.1.1",
        "extensionId": "lkbnfiajjmbhnfledhphioinpickokdi", "userAgent": HEADERS["User-Agent"], "deviceType": "extension"
    }
    async with ClientSession(connector=TCPConnector(ssl=False)) as session:
        try:
            async with session.post(url, json=payload, headers=HEADERS, proxy=proxy_url) as response:
                if response.status == 201:
                    result = await response.json(content_type=None)
                    return [f"wss://{dest}" for dest in result.get("destinations", [])], result.get("token", "")
                logger.error(f"Check-in failed: Status {response.status}")
                return [], ""
        except Exception as e:
            logger.error(f"Check-in error: {e}")
            return [], ""

class WebSocketClient:
    """Manages WebSocket connection for a device and proxy."""
    def __init__(self, proxy_url: str, device_id: str, user_id: str):
        self.proxy_url = proxy_url
        self.device_id = device_id
        self.user_id = user_id
        self.uri = None

    async def connect(self) -> bool:
        """Connect to WebSocket and handle messages."""
        logger.info(f"Device: {self.device_id}")
        while True:
            try:
                endpoints, token = await get_ws_endpoints(self.device_id, self.user_id, self.proxy_url)
                if not endpoints or not token:
                    logger.error("No endpoints or token")
                    return False

                self.uri = f"{endpoints[0]}?token={token}"
                logger.info(f"Connecting: {self.uri}")

                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                async with proxy_connect(self.uri, proxy=Proxy.from_url(self.proxy_url), ssl=ssl_context, extra_headers=HEADERS) as ws:
                    ping_task = asyncio.create_task(self._send_ping(ws))
                    checkin_task = asyncio.create_task(self._periodic_checkin())
                    try:
                        await self._handle_messages(ws)
                    finally:
                        ping_task.cancel()
                        checkin_task.cancel()
                        await asyncio.gather(ping_task, checkin_task, return_exceptions=True)
            except Exception as e:
                if any(pattern in str(e) for pattern in ERROR_PATTERNS) or "Rate limited" in str(e):
                    logger.info(f"Banning proxy: {self.proxy_url}")
                    BANNED_PROXIES[self.proxy_url] = time.time() + 3600
                    return False
                logger.error(f"Proxy error {self.proxy_url}: {e}")
                await asyncio.sleep(5)

    async def _send_ping(self, ws) -> None:
        """Send periodic PING messages."""
        while True:
            try:
                await ws.send(json.dumps({"id": str(uuid.uuid4()), "version": "1.0.0", "action": "PING", "data": {}}))
                await asyncio.sleep(PING_INTERVAL + 60)
            except Exception as e:
                logger.error(f"Ping error: {e}")
                break

    async def _periodic_checkin(self) -> None:
        """Periodically check in."""
        while True:
            await asyncio.sleep(CHECKIN_INTERVAL)
            await get_ws_endpoints(self.device_id, self.user_id, self.proxy_url)

    async def _handle_messages(self, ws) -> None:
        """Handle incoming WebSocket messages."""
        handlers = {"AUTH": self._handle_auth, "PONG": self._handle_pong, "HTTP_REQUEST": self._handle_http_request}
        while True:
            message = json.loads(await ws.recv())
            logger.info(f"Received: {message}")
            if handler := handlers.get(message.get("action")):
                await handler(ws, message)
            else:
                logger.error(f"Unknown action: {message.get('action')}")

    async def _handle_auth(self, ws, message) -> None:
        """Handle AUTH message."""
        await ws.send(json.dumps({
            "id": message["id"], "origin_action": "AUTH",
            "result": {
                "browser_id": self.device_id, "user_id": self.user_id, "user_agent": HEADERS["User-Agent"],
                "timestamp": int(time.time()), "device_type": "extension", "version": "5.1.1"
            }
        }))

    async def _handle_pong(self, ws, message) -> None:
        """Handle PONG message."""
        await ws.send(json.dumps({"id": message["id"], "origin_action": "PONG"}))

    async def _handle_http_request(self, ws, message) -> None:
        """Handle HTTP_REQUEST message."""
        data = message.get("data", {})
        method = data.get("method", "GET").upper()
        url = data.get("url")
        headers = data.get("headers", {})
        body = data.get("body")

        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=headers, data=body) as resp:
                status = resp.status
                if status == 429:
                    logger.error(f"Rate limited: {self.proxy_url}")
                    raise Exception("Rate limited")
                reply = {
                    "id": message.get("id"), "origin_action": "HTTP_REQUEST",
                    "result": {
                        "url": url, "status": status, "status_text": "", "headers": dict(resp.headers),
                        "body": base64.b64encode(await resp.read()).decode()
                    }
                }
                await ws.send(json.dumps(reply))

class ProxyManager:
    """Manages proxies and devices."""
    def __init__(self, device_ids: list, user_id: str):
        self.device_ids = device_ids
        self.user_id = user_id
        self.active_proxies = set()
        self.all_proxies = set()

    async def load_proxies(self) -> None:
        """Load proxies from file."""
        try:
            async with aiofiles.open(PROXY_FILE, "r") as f:
                self.all_proxies = {line.strip() async for line in f if line.strip()}
        except Exception as e:
            logger.error(f"Error loading proxies: {e}")

    async def start(self, max_proxies: int) -> None:
        """Start proxy clients."""
        await self.load_proxies()
        if not self.all_proxies:
            logger.error("No proxies found")
            return

        available = {p for p in self.all_proxies if p not in BANNED_PROXIES or time.time() >= BANNED_PROXIES[p]}
        if not available:
            logger.error("All proxies banned")
            return

        self.active_proxies = set(random.sample(list(available), min(len(available), max_proxies)))
        tasks = {asyncio.create_task(self._run_client(p, d)): (p, d) for p, d in zip(self.active_proxies, self.device_ids)}

        while True:
            done, _ = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                proxy, device_id = tasks.pop(task)
                if not task.result():
                    self.active_proxies.discard(proxy)
                    await self.load_proxies()
                    remaining = {p for p in self.all_proxies if p not in self.active_proxies and (p not in BANNED_PROXIES or time.time() >= BANNED_PROXIES[p])}
                    if remaining:
                        new_proxy = random.choice(list(remaining))
                        self.active_proxies.add(new_proxy)
                        tasks[asyncio.create_task(self._run_client(new_proxy, device_id))] = (new_proxy, device_id)

    async def _run_client(self, proxy: str, device_id: str) -> bool:
        """Run WebSocket client."""
        return await WebSocketClient(proxy, device_id, self.user_id).connect()

async def load_config() -> dict:
    """Load user config."""
    try:
        async with aiofiles.open(CONFIG_FILE, "r") as f:
            return json.loads(await f.read())
    except Exception:
        return {}

async def save_config(user_ids: list) -> None:
    """Save user config."""
    async with aiofiles.open(CONFIG_FILE, "w") as f:
        await f.write(json.dumps({"user_ids": user_ids}, indent=4))

async def load_device_ids() -> list:
    """Load device IDs."""
    try:
        async with aiofiles.open(DEVICE_FILE, "r") as f:
            return json.loads(await f.read()).get("device_ids", [])
    except Exception:
        return []

async def save_device_ids(device_ids: list) -> None:
    """Save device IDs."""
    async with aiofiles.open(DEVICE_FILE, "w") as f:
        await f.write(json.dumps({"device_ids": device_ids}, indent=4))

async def main():
    """Main function."""
    print("GrassBot Starting...")
    logger.add("bot.log", level="INFO")

    parser = argparse.ArgumentParser(description="GrassBot")
    parser.add_argument("-p", "--proxy-file", help="Path to proxy file")
    args = parser.parse_args()
    if args.proxy_file:
        global PROXY_FILE
        PROXY_FILE = args.proxy_file

    config = await load_config()
    if not config.get("user_ids"):
        user_ids = input("Enter USER IDs (comma-separated): ").split(",")
        user_ids = [uid.strip() for uid in user_ids if uid.strip()]
        await save_config(user_ids)
        config = {"user_ids": user_ids}

    device_ids = await load_device_ids()
    if device_ids and input(f"Use {len(device_ids)} devices? (y/n): ").lower() == "y":
        pass
    else:
        num = int(input("Number of devices: "))
        device_ids = [str(uuid.uuid4()) for _ in range(num)]
        await save_device_ids(device_ids)

    for user_id in config["user_ids"]:
        logger.info(f"Starting USER_ID: {user_id}, Proxies: {len(device_ids)}")
        await ProxyManager(device_ids, user_id).start(len(device_ids))

    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")