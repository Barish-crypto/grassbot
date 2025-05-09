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

# Khởi tạo colorama để hiển thị màu trong terminal
init(autoreset=True)

# Các tệp cấu hình
CONFIG_FILE = "config.json"  # Tệp lưu USER_IDs
DEVICE_FILE = "devices.json"  # Tệp lưu Device IDs
PROXY_FILE = "proxy.txt"     # Tệp lưu danh sách proxy
PING_INTERVAL = 30           # Khoảng thời gian gửi PING (giây)
CHECKIN_INTERVAL = 300       # Khoảng thời gian check-in (giây)
DIRECTOR_SERVER = "https://director.getgrass.io"  # URL server director

# Phân tích tham số dòng lệnh
parser = argparse.ArgumentParser(description="GrassBot - Bot tự động hóa kết nối WebSocket")
parser.add_argument("-p", "--proxy-file", help="Đường dẫn đến tệp proxy")
args = parser.parse_args()

if args.proxy_file:
    PROXY_FILE = args.proxy_file

# Tiêu đề HTTP cho các yêu cầu
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 OPR/117.0.0.0",
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "chrome-extension://lkbnfiajjmbhnfledhphioinpickokdi",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive"
}

# Các mẫu lỗi để phát hiện vấn đề proxy
ERROR_PATTERNS = [
    "Host unreachable",
    "[SSL: WRONG_VERSION_NUMBER]",
    "invalid length of packed IP address string",
    "Empty connect reply",
    "Device creation limit exceeded",
    "sent 1011 (internal error) keepalive ping timeout",
    "SOCKS5 authentication failed",
    "SOCKS5 connection refused",
    "[SSL: CERTIFICATE_VERIFY_FAILED]"  # Thêm lỗi SSL để cấm proxy
]

# Danh sách proxy bị cấm (với thời gian cấm)
BANNED_PROXIES = {}

def parse_proxy(proxy_url: str) -> tuple:
    """
    Phân tích URL proxy để lấy scheme, host, port, username, password.
    Ví dụ: socks5://user:pass@host:port -> ('socks5', 'host', port, 'user', 'pass')
    """
    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    port = parsed.port
    username = parsed.username
    password = parsed.password
    return scheme, host, port, username, password

async def get_ws_endpoints(device_id: str, user_id: str, proxy_url: str):
    """
    Gửi yêu cầu POST đến server director để lấy danh sách endpoint WebSocket và token.
    Sử dụng ProxyConnector cho SOCKS5 hoặc proxy HTTP thông thường.
    Tắt xác minh SSL cho SOCKS5 để tránh lỗi CERTIFICATE_VERIFY_FAILED.
    """
    url = f"{DIRECTOR_SERVER}/checkin"
    data = {
        "browserId": device_id,
        "userId": user_id,
        "version": "5.1.1",
        "extensionId": "lkbnfiajjmbhnfledhphioinpickokdi",
        "userAgent": HEADERS["User-Agent"],
        "deviceType": "extension"
    }

    scheme, host, port, username, password = parse_proxy(proxy_url) if proxy_url else (None, None, None, None, None)

    # Tạo SSL context để tắt xác minh chứng chỉ
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    try:
        if proxy_url and scheme == 'socks5':
            # Khởi tạo ProxyConnector cho SOCKS5
            connector = ProxyConnector(
                proxy_type=ProxyType.SOCKS5,
                host=host,
                port=port,
                username=username,
                password=password,
                rdns=True
            )
            async with ClientSession(connector=connector) as session:
                async with session.post(url, json=data, headers=HEADERS, ssl=ssl_context) as response:
                    if response.status == 201:
                        try:
                            result = await response.json(content_type=None)
                        except Exception as e:
                            logger.error(f"Lỗi giải mã JSON: {e}")
                            text = await response.text()
                            result = json.loads(text)
                        destinations = result.get("destinations", [])
                        token = result.get("token", "")
                        destinations = [f"wss://{dest}" for dest in destinations]
                        return destinations, token
                    else:
                        logger.error(f"Check-in thất bại: Mã trạng thái {response.status}")
                        return [], ""
        else:
            # Sử dụng ClientSession mặc định cho proxy HTTP hoặc không proxy
            async with ClientSession() as session:
                async with session.post(
                    url,
                    json=data,
                    headers=HEADERS,
                    proxy=proxy_url if proxy_url else None,
                    ssl=ssl_context  # Tắt xác minh SSL cho HTTP proxy hoặc không proxy
                ) as response:
                    if response.status == 201:
                        try:
                            result = await response.json(content_type=None)
                        except Exception as e:
                            logger.error(f"Lỗi giải mã JSON: {e}")
                            text = await response.text()
                            result = json.loads(text)
                        destinations = result.get("destinations", [])
                        token = result.get("token", "")
                        destinations = [f"wss://{dest}" for dest in destinations]
                        return destinations, token
                    else:
                        logger.error(f"Check-in thất bại: Mã trạng thái {response.status}")
                        return [], ""
    except Exception as e:
        logger.error(f"Lỗi khi gửi yêu cầu POST với proxy {proxy_url}: {e}")
        return [], ""

class WebSocketClient:
    """
    Lớp xử lý kết nối WebSocket, gửi PING, check-in định kỳ và xử lý các yêu cầu HTTP.
    """
    def __init__(self, proxy_url: str, device_id: str, user_id: str):
        self.proxy_url = proxy_url
        self.device_id = device_id
        self.user_id = user_id
        self.uri = None

    async def connect(self) -> bool:
        """Kết nối đến WebSocket server thông qua proxy."""
        logger.info(f"🖥️ Device ID: {self.device_id}")
        while True:
            try:
                endpoints, token = await get_ws_endpoints(self.device_id, self.user_id, self.proxy_url)
                if not endpoints or not token:
                    logger.error("Không nhận được endpoint WebSocket hoặc token hợp lệ")
                    return False
                self.uri = f"{endpoints[0]}?token={token}"
                logger.info(f"Kết nối đến WebSocket URI: {self.uri}")

                await asyncio.sleep(0.1)
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                proxy = Proxy.from_url(self.proxy_url) if self.proxy_url else None
                async with proxy_connect(
                    self.uri,
                    proxy=proxy,
                    ssl=ssl_context,
                    extra_headers=HEADERS
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
            except Exception as e:
                logger.error(f"🚫 Lỗi với proxy {self.proxy_url}: {str(e)}")
                if any(pattern in str(e) for pattern in ERROR_PATTERNS) or "Rate limited" in str(e):
                    logger.info(f"❌ Cấm proxy {self.proxy_url}")
                    BANNED_PROXIES[self.proxy_url] = time.time() + 3600
                    return False
                await asyncio.sleep(5)

    async def _send_ping(self, websocket) -> None:
        """Gửi thông điệp PING định kỳ để duy trì kết nối."""
        while True:
            try:
                message = {
                    "id": str(uuid.uuid4()),
                    "version": "1.0.0",
                    "action": "PING",
                    "data": {}
                }
                await websocket.send(json.dumps(message))
                await asyncio.sleep(PING_INTERVAL)
            except Exception as e:
                logger.error(f"🚫 Lỗi khi gửi PING: {str(e)}")
                break

    async def _periodic_checkin(self) -> None:
        """Thực hiện check-in định kỳ với server director."""
        while True:
            await asyncio.sleep(CHECKIN_INTERVAL)
            await get_ws_endpoints(self.device_id, self.user_id, self.proxy_url)

    async def _handle_messages(self, websocket) -> None:
        """Xử lý các thông điệp nhận được từ WebSocket."""
        handlers = {
            "AUTH": self._handle_auth,
            "PONG": self._handle_pong,
            "HTTP_REQUEST": self._handle_http_request
        }
        while True:
            response = await websocket.recv()
            message = json.loads(response)
            logger.info(f"📥 Nhận thông điệp: {message}")
            action = message.get("action")
            handler = handlers.get(action)
            if handler:
                await handler(websocket, message)
            else:
                logger.error(f"Không có trình xử lý cho hành động: {action}")

    async def _handle_auth(self, websocket, message) -> None:
        """Xử lý thông điệp xác thực (AUTH)."""
        auth_response = {
            "id": message["id"],
            "origin_action": "AUTH",
            "result": {
                "browser_id": self.device_id,
                "user_id": self.user_id,
                "user_agent": HEADERS["User-Agent"],
                "timestamp": int(time.time()),
                "device_type": "extension",
                "version": "5.1.1",
            }
        }
        await websocket.send(json.dumps(auth_response))

    async def _handle_pong(self, websocket, message) -> None:
        """Xử lý thông điệp PONG."""
        pong_response = {
            "id": message["id"],
            "origin_action": "PONG"
        }
        await websocket.send(json.dumps(pong_response))

    async def _handle_http_request(self, websocket, message) -> None:
        """Xử lý yêu cầu HTTP từ WebSocket."""
        data = message.get("data", {})
        method = data.get("method", "GET").upper()
        url = data.get("url")
        req_headers = data.get("headers", {})
        body = data.get("body")

        scheme, host, port, username, password = parse_proxy(self.proxy_url) if self.proxy_url else (None, None, None, None, None)

        # Tạo SSL context để tắt xác minh chứng chỉ
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        try:
            if self.proxy_url and scheme == 'socks5':
                connector = ProxyConnector(
                    proxy_type=ProxyType.SOCKS5,
                    host=host,
                    port=port,
                    username=username,
                    password=password,
                    rdns=True
                )
                async with ClientSession(connector=connector) as session:
                    async with session.request(
                        method,
                        url,
                        headers=req_headers,
                        data=body,
                        ssl=ssl_context  # Tắt xác minh SSL
                    ) as resp:
                        status = resp.status
                        resp_headers = dict(resp.headers)
                        resp_bytes = await resp.read()
            else:
                async with ClientSession() as session:
                    async with session.request(
                        method,
                        url,
                        headers=req_headers,
                        data=body,
                        proxy=self.proxy_url if self.proxy_url else None,
                        ssl=ssl_context  # Tắt xác minh SSL
                    ) as resp:
                        status = resp.status
                        resp_headers = dict(resp.headers)
                        resp_bytes = await resp.read()

            if status == 429:
                logger.error(f"Yêu cầu HTTP trả về 429 cho proxy {self.proxy_url}")
                raise Exception("Rate limited")

        except Exception as e:
            logger.error(f"Lỗi yêu cầu HTTP: {e}")
            raise e

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

    async def _remove_proxy_from_list(self) -> None:
        """Xóa proxy khỏi tệp proxy.txt."""
        try:
            async with aiofiles.open(PROXY_FILE, "r") as file:
                lines = await file.readlines()
            async with aiofiles.open(PROXY_FILE, "w") as file:
                await file.writelines(line for line in lines if line.strip() != self.proxy_url)
        except Exception as e:
            logger.error(f"🚫 Lỗi khi xóa proxy khỏi tệp: {str(e)}")

class ProxyManager:
    """
    Quản lý danh sách proxy, xoay vòng proxy khi gặp lỗi.
    """
    def __init__(self, device_ids: list, user_id: str):
        self.device_ids = device_ids
        self.user_id = user_id
        self.active_proxies = set()
        self.all_proxies = set()

    async def load_proxies(self) -> None:
        """Tải danh sách proxy từ tệp proxy.txt."""
        try:
            async with aiofiles.open(PROXY_FILE, "r") as file:
                content = await file.read()
            self.all_proxies = set(line.strip() for line in content.splitlines() if line.strip())
        except Exception as e:
            logger.error(f"❌ Lỗi khi tải proxy: {str(e)}")

    async def start(self, max_proxies: int) -> None:
        """Khởi động quản lý proxy và kết nối WebSocket."""
        await self.load_proxies()
        if not self.all_proxies:
            logger.error("❌ Không tìm thấy proxy trong proxy.txt")
            return
        available_proxies = {p for p in self.all_proxies if p not in BANNED_PROXIES or time.time() >= BANNED_PROXIES[p]}
        if not available_proxies:
            logger.error("❌ Không có proxy khả dụng (tất cả bị cấm).")
            return
        selected = random.sample(list(available_proxies), min(len(available_proxies), max_proxies))
        self.active_proxies = set(selected)
        tasks = {asyncio.create_task(self._run_client(proxy, device_id)): (proxy, device_id) for proxy, device_id in zip(self.active_proxies, self.device_ids)}

        while True:
            done, pending = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                proxy, device_id = tasks.pop(task)
                if task.result() is False:
                    logger.error(f"Proxy {proxy} thất bại; xóa và xoay vòng.")
                    self.active_proxies.remove(proxy)
                    await self.load_proxies()
                    remaining = {p for p in self.all_proxies if p not in self.active_proxies and (p not in BANNED_PROXIES or time.time() >= BANNED_PROXIES[p])}
                    if remaining:
                        new_proxy = random.choice(list(remaining))
                        self.active_proxies.add(new_proxy)
                        new_task = asyncio.create_task(self._run_client(new_proxy, device_id))
                        tasks[new_task] = (new_proxy, device_id)

    async def _run_client(self, proxy: str, device_id: str) -> bool:
        """Chạy client WebSocket với proxy và device_id."""
        client = WebSocketClient(proxy, device_id, self.user_id)
        return await client.connect()

def setup_logger() -> None:
    """Cấu hình logger để ghi log ra file và terminal."""
    logger.remove()
    logger.add("bot.log",
               format=" <level>{level}</level> | <cyan>{message}</cyan>",
               level="INFO", rotation="1 day")
    logger.add(lambda msg: print(msg, end=""),
               format=" <level>{level}</level> | <cyan>{message}</cyan>",
               level="INFO", colorize=True)

async def load_user_config() -> dict:
    """Tải cấu hình USER_IDs từ tệp config.json."""
    try:
        with open(CONFIG_FILE, "r") as config_file:
            config_data = json.load(config_file)
        return config_data if "user_ids" in config_data else {}
    except Exception as e:
        logger.error(f"❌ Lỗi khi tải cấu hình: {str(e)}")
        return {}

async def load_device_ids() -> list:
    """Tải danh sách Device IDs từ tệp devices.json."""
    try:
        with open(DEVICE_FILE, "r") as device_file:
            device_data = json.load(device_file)
        return device_data.get("device_ids", [])
    except Exception as e:
        logger.error(f"❌ Lỗi khi tải Device IDs: {str(e)}")
        return []

async def save_device_ids(device_ids: list) -> None:
    """Lưu danh sách Device IDs vào tệp devices.json."""
    try:
        with open(DEVICE_FILE, "w") as device_file:
            json.dump({"device_ids": device_ids}, device_file, indent=4)
        logger.info(f"✅ Đã lưu Device IDs!")
    except Exception as e:
        logger.error(f"❌ Lỗi khi lưu Device IDs: {str(e)}")

async def user_input() -> dict:
    """Nhập USER_IDs từ người dùng."""
    user_ids_input = input(f"{Fore.YELLOW}🔑 Nhập USER IDs (phân cách bằng dấu phẩy): {Style.RESET_ALL}")
    user_ids = [uid.strip() for uid in user_ids_input.split(",") if uid.strip()]
    config_data = {"user_ids": user_ids}
    with open(CONFIG_FILE, "w") as config_file:
        json.dump(config_data, config_file, indent=4)
    logger.info(f"✅ Đã lưu cấu hình! USER IDs: {user_ids}")
    return config_data

async def device_input(existing_count: int) -> list:
    """Nhập số lượng thiết bị hoặc sử dụng thiết bị hiện có."""
    if existing_count > 0:
        use_existing = input(f"{Fore.LIGHTYELLOW_EX + Style.BRIGHT}🔑 Bạn đã có {existing_count} thiết bị được cấu hình. Bạn muốn sử dụng chúng? (yes/no): {Style.RESET_ALL}").strip().lower()
        if use_existing == "yes":
            return await load_device_ids()

    num_devices_input = input(f"{Fore.LIGHTYELLOW_EX + Style.BRIGHT}🔑 Nhập số lượng thiết bị muốn tạo: {Style.RESET_ALL}")
    num_devices = int(num_devices_input)
    device_ids = [str(uuid.uuid4()) for _ in range(num_devices)]
    await save_device_ids(device_ids)
    return device_ids

async def main() -> None:
    """Hàm chính để chạy GrassBot."""
    print(f"""{Fore.YELLOW + Style.BRIGHT}
██████╗ ██╗   ██╗██████╗  ██████╗ ███████╗████████╗██████╗  █████╗ ███╗   ██╗████████╗███████╗██████╗ 
██╔══██╗██║   ██║██╔══██╗██╔════╝ ██╔════╝╚══██╔══╝██╔══██╗██╔══██╗████╗  ██║╚══██╔══╝██╔════╝██╔══██╗
██████╔╝██║   ██║██║  ██║██║  ███╗█████╗     ██║   ██████╔╝███████║██╔██╗ ██║   ██║   █████╗  ██████╔╝
██╔══██╗██║   ██║██║  ██║██║   ██║██╔══╝     ██║   ██╔══██╗██╔══██║██║╚██╗██║   ██║   ██╔══╝  ██╔══██╗
██████╔╝╚██████╔╝██████╔╝╚██████╔╝███████╗   ██║   ██████╔╝██║  ██║██║ ╚████║   ██║   ███████╗██║  ██║
╚═════╝  ╚═════╝ ╚═════╝  ╚═════╝ ╚══════╝   ╚═╝   ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝   ╚═╝   ╚══════╝╚═╝  ╚═╝
{Style.RESET_ALL}""")
    print(f"{Fore.LIGHTGREEN_EX}GrassBot{Style.RESET_ALL}")
    print(f"{Fore.RED}========================================{Style.RESET_ALL}")
    setup_logger()

    config = await load_user_config()
    if not config or not config.get("user_ids"):
        config = await user_input()

    user_ids = config["user_ids"]

    existing_device_ids = await load_device_ids()
    device_ids = await device_input(len(existing_device_ids))

    max_proxies = len(device_ids)
    for user_id in user_ids:
        logger.info(f"🚀 Bắt đầu với USER_ID: {user_id}")
        logger.info(f"📡 Sử dụng tối đa {max_proxies} proxy")
        logger.info(f"⏱️ Khoảng thời gian PING: {PING_INTERVAL} giây")
        manager = ProxyManager(device_ids, user_id)
        asyncio.create_task(manager.start(max_proxies))

    await asyncio.Event().wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Đang tắt chương trình một cách an toàn...")
    except Exception as e:
        logger.error(f"❌ Lỗi nghiêm trọng: {str(e)}")