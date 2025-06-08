import argparse
import asyncio
import json
import random
import ssl
import time
from typing import Dict, List, Optional, Tuple, Final, Set
import uuid
import base64
import aiohttp
import aiofiles
from aiohttp import ClientSession, ClientError
from aiohttp_socks import ProxyConnector, ProxyType
from colorama import Fore, Style, init
from loguru import logger
from websockets_proxy import Proxy, proxy_connect
from urllib.parse import urlparse
from prompt_toolkit import PromptSession
from prompt_toolkit.validation import Validator, ValidationError
from uuid import UUID
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Thiết lập chính sách vòng lặp sự kiện cho Windows
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Khởi tạo colorama
init(autoreset=True)

# Constants
class Constants:
    CONFIG_FILE: Final[str] = "config.json"
    DEVICE_FILE: Final[str] = "devices.json"
    PROXY_FILE: Final[str] = "proxy.txt"
    PING_INTERVAL: Final[int] = 30
    CHECKIN_INTERVAL: Final[int] = 300
    DIRECTOR_SERVER: Final[str] = "https://director.getgrass.io"
    HEADERS: Final[Dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 OPR/117.0.0.0",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "chrome-extension://lkbnfiajjmbhnfledhphioinpickokdi",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Connection": "keep-alive"
    }
    ERROR_PATTERNS: Final[List[str]] = [
        "Host unreachable",
        "[SSL: WRONG_VERSION_NUMBER]",
        "invalid length of packed IP address string",
        "Empty connect reply",
        "Device creation limit exceeded",
        "sent 1011 (internal error) keepalive ping timeout",
        "SOCKS5 authentication failed",
        "SOCKS5 connection refused",
        "[SSL: CERTIFICATE_VERIFY_FAILED]"
    ]

# UUID Validator for user input
class UUIDValidator(Validator):
    def validate(self, document):
        text = document.text.strip()
        if not text:
            return
        for uid in text.split(","):
            try:
                UUID(uid.strip())
            except ValueError:
                raise ValidationError(message=f"Invalid UUID: {uid}")

# Config Manager
class ConfigManager:
    def __init__(self, config_file: str, device_file: str):
        self.config_file = config_file
        self.device_file = device_file

    async def load_user_config(self) -> Dict[str, List[str]]:
        try:
            async with aiofiles.open(self.config_file, "r") as file:
                content = await file.read()
                config_data = json.loads(content)
                return config_data if "user_ids" in config_data else {}
        except Exception as e:
            logger.error(f"❌ Failed to load config: {e}")
            return {}

    async def save_user_config(self, user_ids: List[str]) -> None:
        config_data = {"user_ids": user_ids}
        try:
            async with aiofiles.open(self.config_file, "w") as file:
                await file.write(json.dumps(config_data, indent=4))
            logger.info(f"✅ Saved user config: {user_ids}")
        except Exception as e:
            logger.error(f"❌ Failed to save config: {e}")

    async def load_device_ids(self) -> List[str]:
        try:
            async with aiofiles.open(self.device_file, "r") as file:
                content = await file.read()
                device_data = json.loads(content)
                return device_data.get("device_ids", [])
        except Exception as e:
            logger.error(f"❌ Failed to load device IDs: {e}")
            return []

    async def save_device_ids(self, device_ids: List[str]) -> None:
        try:
            async with aiofiles.open(self.device_file, "w") as file:
                await file.write(json.dumps({"device_ids": device_ids}, indent=4))
            logger.info("✅ Saved device IDs")
        except Exception as e:
            logger.error(f"❌ Failed to save device IDs: {e}")

# Proxy Parsing
def parse_proxy(proxy_url: str) -> Tuple[str, str, Optional[int], Optional[str], Optional[str]]:
    if not proxy_url:
        raise ValueError("Proxy URL cannot be empty")
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"Invalid proxy URL: {proxy_url}")
    return (
        parsed.scheme.lower(),
        parsed.hostname,
        parsed.port,
        parsed.username or None,
        parsed.password or None
    )

# WebSocket Endpoints
async def get_ws_endpoints(device_id: str, user_id: str, proxy_url: Optional[str]) -> Tuple[List[str], str]:
    url = f"{Constants.DIRECTOR_SERVER}/checkin"
    data = {
        "browserId": device_id,
        "userId": user_id,
        "version": "5.1.1",
        "extensionId": "lkbnfiajjmbhnfledhphioinpickokdi",
        "userAgent": Constants.HEADERS["User-Agent"],
        "deviceType": "extension"
    }
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    try:
        scheme, host, port, username, password = parse_proxy(proxy_url) if proxy_url else (None, None, None, None, None)
        if proxy_url and scheme == 'socks5':
            connector = ProxyConnector(
                proxy_type=ProxyType.SOCKS5,
                host=host,
                port=port,
                username=username,
                password=password,
                rdns=True
            )
            async with ClientSession(connector=connector) as session:
                async with session.post(url, json=data, headers=Constants.HEADERS, ssl=ssl_context) as response:
                    return await _process_response(response)
        else:
            async with ClientSession() as session:
                async with session.post(
                    url, json=data, headers=Constants.HEADERS, proxy=proxy_url, ssl=ssl_context
                ) as response:
                    return await _process_response(response)
    except ClientError as e:
        logger.error(f"Network error with proxy {proxy_url}: {e}")
        return [], ""
    except Exception as e:
        logger.error(f"Unexpected error with proxy {proxy_url}: {e}")
        return [], ""

async def _process_response(response: aiohttp.ClientResponse) -> Tuple[List[str], str]:
    if response.status != 201:
        logger.error(f"Check-in failed: Status {response.status}")
        return [], ""
    try:
        result = await response.json(content_type=None)
        destinations = [f"wss://{dest}" for dest in result.get("destinations", [])]
        token = result.get("token", "")
        return destinations, token
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode JSON response: {e}")
        return [], ""

# WebSocket Client
class WebSocketClient:
    def __init__(self, proxy_url: Optional[str], device_id: str, user_id: str):
        self.proxy_url = proxy_url
        self.device_id = device_id
        self.user_id = user_id
        self.uri: Optional[str] = None
        self.session: Optional[ClientSession] = None

    async def _get_session(self) -> ClientSession:
        if self.session is None or self.session.closed:
            scheme, host, port, username, password = parse_proxy(self.proxy_url) if self.proxy_url else (None, None, None, None, None)
            if self.proxy_url and scheme == 'socks5':
                connector = ProxyConnector(
                    proxy_type=ProxyType.SOCKS5,
                    host=host,
                    port=port,
                    username=username,
                    password=password,
                    rdns=True
                )
                self.session = ClientSession(connector=connector)
            else:
                self.session = ClientSession()
        return self.session

    async def connect(self) -> bool:
        logger.info(f"🖥️ Device ID: {self.device_id}")
        while True:
            try:
                endpoints, token = await get_ws_endpoints(self.device_id, self.user_id, self.proxy_url)
                if not endpoints or not token:
                    logger.error("No valid WebSocket endpoints or token received")
                    return False
                self.uri = f"{endpoints[0]}?token={token}"
                logger.info(f"Connecting to WebSocket URI: {self.uri}")

                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                proxy = Proxy.from_url(self.proxy_url) if self.proxy_url else None
                async with proxy_connect(
                    self.uri,
                    proxy=proxy,
                    ssl=ssl_context,
                    extra_headers=Constants.HEADERS
                ) as websocket:
                    ping_task = asyncio.create_task(self._send_ping(websocket))
                    checkin_task = asyncio.create_task(self._periodic_checkin())
                    try:
                        await self._handle_messages(websocket)
                    finally:
                        ping_task.cancel()
                        checkin_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
                        try:
                            await checkin_task
                        except asyncio.CancelledError:
                            pass
                        if self.session and not self.session.closed:
                            await self.session.close()
            except Exception as e:
                logger.error(f"🚫 Error with proxy {self.proxy_url}: {str(e)}")
                if any(pattern in str(e) for pattern in Constants.ERROR_PATTERNS) or "Rate limited" in str(e):
                    logger.info(f"❌ Banning proxy {self.proxy_url}")
                    return False
                await asyncio.sleep(5)

    async def _send_ping(self, websocket) -> None:
        while True:
            try:
                message = {
                    "id": str(uuid.uuid4()),
                    "version": "1.0.0",
                    "action": "PING",
                    "data": {}
                }
                await websocket.send(json.dumps(message))
                await asyncio.sleep(Constants.PING_INTERVAL)
            except Exception as e:
                logger.error(f"🚫 Error sending PING: {str(e)}")
                break

    async def _periodic_checkin(self) -> None:
        while True:
            await asyncio.sleep(Constants.CHECKIN_INTERVAL)
            await get_ws_endpoints(self.device_id, self.user_id, self.proxy_url)

    async def _handle_messages(self, websocket) -> None:
        handlers = {
            "AUTH": self._handle_auth,
            "PONG": self._handle_pong,
            "HTTP_REQUEST": self._handle_http_request
        }
        while True:
            response = await websocket.recv()
            message = json.loads(response)
            logger.info(f"📥 Received message: {message}")
            action = message.get("action")
            handler = handlers.get(action)
            if handler:
                await handler(websocket, message)
            else:
                logger.error(f"No handler for action: {action}")

    async def _handle_auth(self, websocket, message) -> None:
        auth_response = {
            "id": message["id"],
            "origin_action": "AUTH",
            "result": {
                "browser_id": self.device_id,
                "user_id": self.user_id,
                "user_agent": Constants.HEADERS["User-Agent"],
                "timestamp": int(time.time()),
                "device_type": "extension",
                "version": "5.1.1",
            }
        }
        await websocket.send(json.dumps(auth_response))

    async def _handle_pong(self, websocket, message) -> None:
        pong_response = {
            "id": message["id"],
            "origin_action": "PONG"
        }
        await websocket.send(json.dumps(pong_response))

    async def _handle_http_request(self, websocket, message) -> None:
        data = message.get("data", {})
        method = data.get("method", "GET").upper()
        url = data.get("url")
        req_headers = data.get("headers", {})
        body = data.get("body")

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        try:
            session = await self._get_session()
            async with session.request(
                method, url, headers=req_headers, data=body, proxy=self.proxy_url if self.proxy_url else None, ssl=ssl_context
            ) as resp:
                status = resp.status
                resp_headers = dict(resp.headers)
                resp_bytes = await resp.read()

            if status == 429:
                logger.error(f"HTTP request rate-limited for proxy {self.proxy_url}")
                raise Exception("Rate limited")

            body_b64 = base64.b64encode(resp_bytes).decode()
            result = {
                "url": url,
                "status": status,
                "status_text": "",
                "headers": resp_headers,
                "body": body_b64
            }
            reply = {
                "id": message.get("id"),
                "origin_action": "HTTP_REQUEST",
                "result": result
            }
            await websocket.send(json.dumps(reply))
        except Exception as e:
            logger.error(f"HTTP request error: {e}")
            raise

# Proxy Manager
class ProxyManager:
    def __init__(self, device_ids: List[str], user_id: str, proxy_file: str):
        self.device_ids = device_ids
        self.user_id = user_id
        self.proxy_file = proxy_file
        self.active_proxies: Set[str] = set()
        self.all_proxies: Set[str] = set()
        self.banned_proxies: Dict[str, float] = {}

    async def load_proxies(self) -> None:
        try:
            async with aiofiles.open(self.proxy_file, "r") as file:
                content = await file.read()
            self.all_proxies = {line.strip() for line in content.splitlines() if line.strip()}
            logger.info(f"Loaded {len(self.all_proxies)} proxies")
        except Exception as e:
            logger.error(f"Failed to load proxies: {e}")

    def ban_proxy(self, proxy: str, duration: float = 3600) -> None:
        self.banned_proxies[proxy] = time.time() + duration
        logger.info(f"Banned proxy {proxy} for {duration} seconds")

    async def cleanup_banned_proxies(self) -> None:
        while True:
            current_time = time.time()
            expired = [p for p, t in self.banned_proxies.items() if current_time >= t]
            for proxy in expired:
                del self.banned_proxies[proxy]
            await asyncio.sleep(600)

    def start_proxy_watcher(self) -> None:
        class ProxyFileHandler(FileSystemEventHandler):
            def on_modified(self, event):
                if not event.is_directory and event.src_path == self.proxy_file:
                    asyncio.create_task(self.load_proxies())

        observer = Observer()
        observer.schedule(ProxyFileHandler(), path=self.proxy_file, recursive=False)
        observer.start()

    async def start(self, max_proxies: int) -> None:
        await self.load_proxies()
        self.start_proxy_watcher()
        asyncio.create_task(self.cleanup_banned_proxies())
        if not self.all_proxies:
            logger.error("❌ No proxies found in proxy.txt")
            return
        available_proxies = {p for p in self.all_proxies if p not in self.banned_proxies or time.time() >= self.banned_proxies[p]}
        if not available_proxies:
            logger.error("❌ No available proxies (all banned)")
            return
        selected = random.sample(list(available_proxies), min(len(available_proxies), max_proxies))
        self.active_proxies = set(selected)
        tasks = {asyncio.create_task(self._run_client(proxy, device_id)): (proxy, device_id) for proxy, device_id in zip(self.active_proxies, self.device_ids)}

        while True:
            done, pending = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                proxy, device_id = tasks.pop(task)
                if task.result() is False:
                    logger.error(f"Proxy {proxy} failed; removing and rotating")
                    self.active_proxies.remove(proxy)
                    self.ban_proxy(proxy)
                    await self.load_proxies()
                    remaining = {p for p in self.all_proxies if p not in self.active_proxies and (p not in self.banned_proxies or time.time() >= self.banned_proxies[p])}
                    if remaining:
                        new_proxy = random.choice(list(remaining))
                        self.active_proxies.add(new_proxy)
                        new_task = asyncio.create_task(self._run_client(new_proxy, device_id))
                        tasks[new_task] = (new_proxy, device_id)

    async def _run_client(self, proxy: str, device_id: str) -> bool:
        client = WebSocketClient(proxy, device_id, self.user_id)
        return await client.connect()

# User Input
async def user_input() -> Dict[str, List[str]]:
    session = PromptSession("🔑 Enter USER IDs (comma-separated): ", validator=UUIDValidator())
    user_ids_input = await session.prompt_async()
    user_ids = [uid.strip() for uid in user_ids_input.split(",") if uid.strip()]
    config_manager = ConfigManager(Constants.CONFIG_FILE, Constants.DEVICE_FILE)
    await config_manager.save_user_config(user_ids)
    return {"user_ids": user_ids}

async def device_input(existing_count: int) -> List[str]:
    config_manager = ConfigManager(Constants.CONFIG_FILE, Constants.DEVICE_FILE)
    if existing_count > 0:
        use_existing = input(f"{Fore.LIGHTYELLOW_EX + Style.BRIGHT}🔑 You have {existing_count} devices configured. Use them? (yes/no): {Style.RESET_ALL}").strip().lower()
        if use_existing == "yes":
            return await config_manager.load_device_ids()

    num_devices_input = input(f"{Fore.LIGHTYELLOW_EX + Style.BRIGHT}🔑 Enter number of devices to create: {Style.RESET_ALL}")
    try:
        num_devices = int(num_devices_input)
        if num_devices <= 0:
            raise ValueError("Number of devices must be positive")
        device_ids = [str(uuid.uuid4()) for _ in range(num_devices)]
        await config_manager.save_device_ids(device_ids)
        return device_ids
    except ValueError as e:
        logger.error(f"Invalid input: {e}")
        return []

# Logger Setup
def setup_logger() -> None:
    logger.remove()
    logger.add("bot.log", format=" <level>{level}</level> | <cyan>{message}</cyan>", level="INFO", rotation="1 day")
    logger.add(lambda msg: print(msg, end=""), format=" <level>{level}</level> | <cyan>{message}</cyan>", level="INFO", colorize=True)

# Main
async def main() -> None:
    setup_logger()
    config_manager = ConfigManager(Constants.CONFIG_FILE, Constants.DEVICE_FILE)

    config = await config_manager.load_user_config()
    if not config or not config.get("user_ids"):
        config = await user_input()

    user_ids = config["user_ids"]
    existing_device_ids = await config_manager.load_device_ids()
    device_ids = await device_input(len(existing_device_ids))

    max_proxies = len(device_ids)
    for user_id in user_ids:
        logger.info(f"🚀 Starting with USER_ID: {user_id}")
        logger.info(f"📡 Using up to {max_proxies} proxies")
        logger.info(f"⏱️ PING interval: {Constants.PING_INTERVAL} seconds")
        manager = ProxyManager(device_ids, user_id, Constants.PROXY_FILE)
        asyncio.create_task(manager.start(max_proxies))

    await asyncio.Event().wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
        print("thành công")
    except KeyboardInterrupt:
        print("\n👋 Shutting down safely...")
    except Exception as e:
        logger.error(f"❌ Critical error: {str(e)}")