"""
RoxyBrowser API 客户端
对接文档: https://faq.roxybrowser.com/zh/api-documentation/api-endpoint.html

功能：
- 浏览器窗口管理（创建/打开/关闭/删除）
- 账号-代理-指纹 三绑定 Profile 管理
- 与 ai-goofish-monitor 的 scraper.py 集成接口

用法：
    client = RoxyBrowserClient(api_url="http://127.0.0.1:PORT", workspace_id=1)
    profile = await client.open_profile(dir_id="xxx")
    # 用返回的 ws 地址连接 Playwright
    browser = await playwright.chromium.connect_over_cdp(profile["ws"])
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class ProxyInfo:
    """代理配置"""
    proxy_category: str = "noproxy"          # noproxy / HTTP / HTTPS / SOCKS5
    proxy_method: str = "custom"             # custom / choose
    host: str = ""
    port: str = ""
    proxy_user_name: str = ""
    proxy_password: str = ""
    ip_type: str = "IPV4"                    # IPV4 / IPV6
    refresh_url: str = ""
    check_channel: str = "IPRust.io"         # IPRust.io / IP-API / IP123.in

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proxyMethod": self.proxy_method,
            "proxyCategory": self.proxy_category,
            "ipType": self.ip_type,
            "host": self.host,
            "port": self.port,
            "proxyUserName": self.proxy_user_name,
            "proxyPassword": self.proxy_password,
            "refreshUrl": self.refresh_url,
            "checkChannel": self.check_channel,
        }

    @classmethod
    def from_url(cls, proxy_url: str) -> "ProxyInfo":
        """
        从代理 URL 解析，支持格式：
          http://user:pass@host:port
          socks5://host:port
        """
        import re
        m = re.match(
            r"(?P<proto>\w+)://((?P<user>[^:]+):(?P<pwd>[^@]+)@)?(?P<host>[^:]+):(?P<port>\d+)",
            proxy_url,
        )
        if not m:
            raise ValueError(f"无法解析代理 URL: {proxy_url}")
        proto = m.group("proto").upper()
        category = "SOCKS5" if "SOCKS" in proto else proto  # HTTP / HTTPS / SOCKS5
        return cls(
            proxy_category=category,
            host=m.group("host"),
            port=m.group("port"),
            proxy_user_name=m.group("user") or "",
            proxy_password=m.group("pwd") or "",
        )


@dataclass
class FingerInfo:
    """
    指纹配置（精简版，仅列出闲鱼场景常用项）
    全量字段见 API 文档 fingerInfo 节
    """
    # 语言/时区/地理位置跟随 IP
    is_language_base_ip: bool = True
    is_time_zone: bool = True
    is_position_base_ip: bool = True
    # 反检测关键项
    canvas: bool = True           # True=随机噪声
    web_gl: bool = True           # True=随机
    web_gl_info: bool = True
    audio_context: bool = True
    client_rects: bool = True
    device_info: bool = True
    mac_info: bool = True
    web_rtc: int = 0              # 0=替换, 1=真实, 2=禁止
    font_type: bool = False       # False=跟随系统（更真实）
    # 窗口
    open_width: str = "1366"
    open_height: str = "768"
    # 启动前清理
    clear_cache_file: bool = False
    clear_cookie: bool = False
    # 网络异常时停止打开
    stop_open_net: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "isLanguageBaseIp": self.is_language_base_ip,
            "isTimeZone": self.is_time_zone,
            "isPositionBaseIp": self.is_position_base_ip,
            "canvas": self.canvas,
            "webGL": self.web_gl,
            "webGLInfo": self.web_gl_info,
            "audioContext": self.audio_context,
            "clientRects": self.client_rects,
            "deviceInfo": self.device_info,
            "macInfo": self.mac_info,
            "webRTC": self.web_rtc,
            "fontType": self.font_type,
            "openWidth": self.open_width,
            "openHeight": self.open_height,
            "clearCacheFile": self.clear_cache_file,
            "clearCookie": self.clear_cookie,
            "stopOpenNet": self.stop_open_net,
        }


@dataclass
class BrowserProfile:
    """
    账号-代理-指纹 三绑定 Profile
    对应 RoxyBrowser 的一个浏览器窗口（dirId）
    """
    dir_id: str                              # RoxyBrowser 窗口 ID
    name: str = ""                           # 窗口名称（可读标识）
    state_file: str = ""                     # 闲鱼登录态 JSON 文件路径
    proxy: Optional[ProxyInfo] = None        # 绑定代理（None=不使用代理）
    finger: FingerInfo = field(default_factory=FingerInfo)
    # 运行时状态
    is_open: bool = False
    ws_endpoint: str = ""                    # 打开后的 CDP ws 地址
    http_endpoint: str = ""
    driver_path: str = ""
    pid: int = 0


# ---------------------------------------------------------------------------
# API 客户端
# ---------------------------------------------------------------------------

class RoxyBrowserClient:
    """
    RoxyBrowser 本地 API 客户端（异步）

    RoxyBrowser 客户端软件运行后会在本地暴露 HTTP API，
    默认端口可在软件设置中查看（通常为 5000 或自定义）。
    """

    def __init__(self, api_url: str, workspace_id: int, timeout: float = 30.0):
        """
        Args:
            api_url: RoxyBrowser 本地 API 地址，如 http://127.0.0.1:5000
            workspace_id: 工作空间 ID（通过 /browser/workspace 获取）
            timeout: 请求超时秒数
        """
        self.api_url = api_url.rstrip("/")
        self.workspace_id = workspace_id
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        url = f"{self.api_url}{path}"
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"RoxyBrowser API 错误 [{path}]: {data.get('msg')}")
        return data

    async def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.api_url}{path}"
        resp = await self._client.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"RoxyBrowser API 错误 [{path}]: {data.get('msg')}")
        return data

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    async def health(self) -> bool:
        """检查 RoxyBrowser 客户端是否在线"""
        try:
            data = await self._get("/health")
            return data.get("code") == 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 工作空间
    # ------------------------------------------------------------------

    async def list_workspaces(self) -> List[Dict]:
        data = await self._get("/browser/workspace", {"page_size": 100})
        return data["data"]["rows"]

    # ------------------------------------------------------------------
    # 浏览器窗口管理
    # ------------------------------------------------------------------

    async def list_windows(self, page_size: int = 100) -> List[Dict]:
        """获取当前工作空间所有浏览器窗口"""
        data = await self._get("/browser/list_v3", {
            "workspaceId": self.workspace_id,
            "page_size": page_size,
        })
        return data["data"]["rows"]

    async def get_window(self, dir_id: str) -> Dict:
        """获取单个窗口详情（含指纹、代理、Cookie 等）"""
        data = await self._get("/browser/detail", {
            "workspaceId": self.workspace_id,
            "dirId": dir_id,
        })
        return data["data"]["rows"][0]

    async def create_window(
        self,
        name: str,
        proxy: Optional[ProxyInfo] = None,
        finger: Optional[FingerInfo] = None,
        cookies: Optional[List[Dict]] = None,
        remark: str = "",
    ) -> str:
        """
        创建浏览器窗口，返回 dirId

        Args:
            name: 窗口名称
            proxy: 代理配置，None 表示不使用代理
            finger: 指纹配置，None 使用默认值
            cookies: Cookie 列表 [{"name":..., "value":..., "domain":...}]
            remark: 窗口备注
        Returns:
            dirId (str)
        """
        body: Dict[str, Any] = {
            "workspaceId": self.workspace_id,
            "windowName": name,
            "windowRemark": remark,
            "os": "Windows",
            "osVersion": "11",
            "cookie": cookies or [],
            "proxyInfo": proxy.to_dict() if proxy else {"proxyCategory": "noproxy"},
            "fingerInfo": (finger or FingerInfo()).to_dict(),
        }
        data = await self._post("/browser/create", body)
        dir_id = data["data"]["dirId"]
        logger.info(f"[RoxyBrowser] 创建窗口成功: {name} → dirId={dir_id}")
        return dir_id

    async def update_window(
        self,
        dir_id: str,
        proxy: Optional[ProxyInfo] = None,
        cookies: Optional[List[Dict]] = None,
        finger: Optional[FingerInfo] = None,
    ) -> None:
        """更新窗口的代理/Cookie/指纹"""
        body: Dict[str, Any] = {
            "workspaceId": self.workspace_id,
            "dirId": dir_id,
        }
        if proxy is not None:
            body["proxyInfo"] = proxy.to_dict()
        if cookies is not None:
            body["cookie"] = cookies
        if finger is not None:
            body["fingerInfo"] = finger.to_dict()
        await self._post("/browser/mdf", body)
        logger.info(f"[RoxyBrowser] 更新窗口: dirId={dir_id}")

    async def delete_window(self, dir_id: str, soft: bool = False) -> None:
        """删除窗口（soft=True 移入回收站）"""
        await self._post("/browser/delete", {
            "workspaceId": self.workspace_id,
            "dirIds": [dir_id],
            "isSoftDelete": soft,
        })
        logger.info(f"[RoxyBrowser] 删除窗口: dirId={dir_id}")

    async def random_fingerprint(self, dir_id: str) -> None:
        """随机刷新窗口指纹"""
        await self._post("/browser/random_env", {
            "workspaceId": self.workspace_id,
            "dirId": dir_id,
        })

    # ------------------------------------------------------------------
    # 打开 / 关闭
    # ------------------------------------------------------------------

    async def open_window(self, dir_id: str, headless: bool = False) -> Dict[str, Any]:
        """
        打开浏览器窗口，返回 CDP 连接信息

        Returns:
            {
                "ws": "ws://127.0.0.1:PORT/devtools/browser/...",
                "http": "127.0.0.1:PORT",
                "driver": "path/to/chromedriver",
                "pid": 1234,
                ...
            }
        """
        data = await self._post("/browser/open", {
            "workspaceId": self.workspace_id,
            "dirId": dir_id,
            "headless": headless,
        })
        info = data["data"]
        logger.info(f"[RoxyBrowser] 打开窗口: dirId={dir_id} ws={info.get('ws')}")
        return info

    async def close_window(self, dir_id: str) -> None:
        """关闭浏览器窗口"""
        await self._post("/browser/close", {"dirId": dir_id})
        logger.info(f"[RoxyBrowser] 关闭窗口: dirId={dir_id}")

    async def get_open_windows(self, dir_ids: Optional[List[str]] = None) -> List[Dict]:
        """获取已打开窗口的进程信息"""
        params: Dict[str, Any] = {}
        if dir_ids:
            params["dirIds"] = ",".join(dir_ids)
        data = await self._get("/browser/connection_info", params)
        return data.get("data", [])

    # ------------------------------------------------------------------
    # Profile 管理（账号-代理-指纹 三绑定）
    # ------------------------------------------------------------------

    async def ensure_profile(
        self,
        profile: BrowserProfile,
        cookies: Optional[List[Dict]] = None,
    ) -> BrowserProfile:
        """
        确保 Profile 对应的 RoxyBrowser 窗口存在。
        - 若 dir_id 为空，自动创建新窗口并回填 dir_id
        - 若 dir_id 已存在，更新代理/Cookie/指纹

        Args:
            profile: BrowserProfile 对象
            cookies: 闲鱼 Cookie 列表（从 state_file 读取后传入）
        Returns:
            更新后的 BrowserProfile
        """
        if not profile.dir_id:
            # 创建新窗口
            dir_id = await self.create_window(
                name=profile.name or f"xianyu-{id(profile)}",
                proxy=profile.proxy,
                finger=profile.finger,
                cookies=cookies,
                remark=profile.state_file,
            )
            profile.dir_id = dir_id
        else:
            # 更新已有窗口
            await self.update_window(
                dir_id=profile.dir_id,
                proxy=profile.proxy,
                cookies=cookies,
                finger=profile.finger,
            )
        return profile

    async def open_profile(self, profile: BrowserProfile) -> BrowserProfile:
        """
        打开 Profile 对应的浏览器窗口，填充 ws/http/driver/pid

        Returns:
            更新了运行时字段的 BrowserProfile
        """
        info = await self.open_window(profile.dir_id)
        profile.is_open = True
        profile.ws_endpoint = info.get("ws", "")
        profile.http_endpoint = info.get("http", "")
        profile.driver_path = info.get("driver", "")
        profile.pid = info.get("pid", 0)
        return profile

    async def close_profile(self, profile: BrowserProfile) -> None:
        """关闭 Profile 对应的浏览器窗口"""
        await self.close_window(profile.dir_id)
        profile.is_open = False
        profile.ws_endpoint = ""
        profile.pid = 0


# ---------------------------------------------------------------------------
# 与 ai-goofish-monitor 集成
# ---------------------------------------------------------------------------

class RoxyProfilePool:
    """
    Profile 池，替代原有的 RotationPool(account) + RotationPool(proxy) 随机组合。
    每个 Profile = 固定账号 + 固定代理 + 固定指纹，三者绑定，不随机错配。

    用法（在 scraper.py 中替换原有逻辑）：

        pool = RoxyProfilePool(client, profiles)
        profile = await pool.acquire()
        try:
            browser = await playwright.chromium.connect_over_cdp(profile.ws_endpoint)
            # ... 爬取逻辑 ...
            await pool.release(profile, success=True)
        except Exception as e:
            await pool.release(profile, success=False, error=str(e))
    """

    def __init__(self, client: RoxyBrowserClient, profiles: List[BrowserProfile]):
        self.client = client
        self._profiles = profiles
        self._blacklist: Dict[str, float] = {}   # dir_id → 解封时间戳
        self._blacklist_ttl: int = 300

    def available(self) -> List[BrowserProfile]:
        import time
        now = time.time()
        # 清理过期黑名单
        self._blacklist = {k: v for k, v in self._blacklist.items() if v > now}
        return [p for p in self._profiles if p.dir_id not in self._blacklist]

    async def acquire(self) -> BrowserProfile:
        """随机选一个可用 Profile 并打开浏览器"""
        import random, time
        candidates = self.available()
        if not candidates:
            raise RuntimeError("RoxyProfilePool: 所有 Profile 均已被拉黑，无可用窗口")
        profile = random.choice(candidates)
        # 若已打开则直接返回（复用）
        if profile.is_open and profile.ws_endpoint:
            return profile
        # 加载 Cookie 并打开
        cookies = self._load_cookies(profile.state_file)
        await self.client.ensure_profile(profile, cookies=cookies)
        await self.client.open_profile(profile)
        return profile

    async def release(self, profile: BrowserProfile, success: bool, error: str = "") -> None:
        """归还 Profile，失败时拉黑"""
        import time
        if success:
            logger.info(f"[RoxyProfilePool] Profile {profile.dir_id} 任务成功")
        else:
            logger.warning(f"[RoxyProfilePool] Profile {profile.dir_id} 失败: {error}，拉黑 {self._blacklist_ttl}s")
            self._blacklist[profile.dir_id] = time.time() + self._blacklist_ttl
        # 关闭浏览器（释放资源）
        try:
            await self.client.close_profile(profile)
        except Exception:
            pass

    @staticmethod
    def _load_cookies(state_file: str) -> List[Dict]:
        """从闲鱼 storage_state JSON 文件提取 Cookie 列表"""
        if not state_file:
            return []
        import json, os
        if not os.path.exists(state_file):
            return []
        with open(state_file, encoding="utf-8") as f:
            state = json.load(f)
        # Playwright storage_state 格式: {"cookies": [...], "origins": [...]}
        return state.get("cookies", [])

    @classmethod
    def from_config(
        cls,
        client: RoxyBrowserClient,
        account_profiles: List[Dict],
    ) -> "RoxyProfilePool":
        """
        从配置字典列表构建 Pool。

        配置格式（config.json 中）：
        {
          "roxy_browser": {
            "api_url": "http://127.0.0.1:5000",
            "workspace_id": 1,
            "account_profiles": [
              {
                "dir_id": "",                  // 留空则自动创建
                "name": "闲鱼账号A",
                "state_file": "state/account1.json",
                "proxy": "socks5://user:pass@host:port"  // 留空则不使用代理
              }
            ]
          }
        }
        """
        profiles = []
        for cfg in account_profiles:
            proxy_url = cfg.get("proxy", "")
            proxy = ProxyInfo.from_url(proxy_url) if proxy_url else None
            profiles.append(BrowserProfile(
                dir_id=cfg.get("dir_id", ""),
                name=cfg.get("name", ""),
                state_file=cfg.get("state_file", ""),
                proxy=proxy,
            ))
        return cls(client, profiles)


# ---------------------------------------------------------------------------
# 快速测试
# ---------------------------------------------------------------------------

async def _demo():
    """
    演示：连接 RoxyBrowser，列出所有窗口，打开第一个，连接 Playwright
    运行前请确保 RoxyBrowser 客户端已启动，并修改 API_URL 和 WORKSPACE_ID
    """
    API_URL = "http://127.0.0.1:5000"   # ← 修改为实际端口
    WORKSPACE_ID = 1                     # ← 修改为实际工作空间 ID

    async with RoxyBrowserClient(API_URL, WORKSPACE_ID) as client:
        # 1. 健康检查
        ok = await client.health()
        print(f"RoxyBrowser 在线: {ok}")
        if not ok:
            return

        # 2. 列出窗口
        windows = await client.list_windows()
        print(f"共 {len(windows)} 个窗口:")
        for w in windows[:5]:
            print(f"  [{w['dirId']}] {w['windowName']} ({w['os']} {w['osVersion']})")

        if not windows:
            print("无窗口，退出")
            return

        # 3. 打开第一个窗口
        dir_id = windows[0]["dirId"]
        info = await client.open_window(dir_id)
        print(f"已打开: ws={info['ws']}")

        # 4. 用 Playwright 连接（需安装 playwright）
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(info["ws"])
                page = browser.contexts[0].pages[0] if browser.contexts else await browser.new_page()
                await page.goto("https://www.goofish.com")
                title = await page.title()
                print(f"页面标题: {title}")
                await browser.close()
        except ImportError:
            print("playwright 未安装，跳过浏览器连接测试")

        # 5. 关闭窗口
        await client.close_window(dir_id)
        print("窗口已关闭")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_demo())
