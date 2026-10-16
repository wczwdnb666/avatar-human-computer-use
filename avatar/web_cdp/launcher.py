# -*- coding: utf-8 -*-
"""Chrome / Edge 启动与调试端口探测。

**这个模块最重要的职责不是"启动浏览器"，而是"不要连错浏览器"。**

场景：用户开着日常使用的 Chrome（登录着网银、企业邮箱）。
如果我们的 ``--remote-debugging-port=9222`` 撞上它，或者更糟 ——
我们连上了一个**已经存在**的、用户自己的调试实例，那就是隐私事故。

所以流程是：

1. **先探测** ``GET /json/version``。200 且是 CDP 响应 → 说明端口上
   已经有个调试实例，**复用**（但要把它登记下来，让调用方知道这不是
   我们启动的）。
2. 探测失败 → 找一个**空闲端口**，启动**新实例**，
   并用独立的 ``--user-data-dir``（**绝对不能用用户的默认 profile**，
   否则会和正在运行的实例锁冲突，而且会污染用户数据）。
3. 启动后轮询等 ``/json/version`` 就绪，超时即失败。

为什么必须独立 ``--user-data-dir``：
- 同一 profile 不能被两个进程同时打开（Chrome 会直接把新进程转交给
  已有实例然后自己退出，于是你的调试端口根本没生效 —— 这是最隐蔽的
  失败模式）
- 独立 profile 保证我们不动用户的登录态、书签、扩展

## HTTP 用 http.client

CDP 的几个 HTTP 端点（``/json/version``、``/json/list``、``/json/new``）
用标准库 ``http.client`` 就够，**不引 requests**。

注意 ``/json/version`` 返回的 ``webSocketDebuggerUrl`` 里带的是
``ws://127.0.0.1:PORT/devtools/browser/<uuid>``，可以直接拿来建
``BrowserSession``。
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..errors import AvatarError, TimeoutError_

__all__ = [
    "BrowserLaunchError",
    "BrowserInfo",
    "find_browser",
    "find_chrome",
    "find_edge",
    "probe_debug_port",
    "is_port_free",
    "pick_free_port",
    "list_debug_instances",
    "launch_browser",
    "ensure_debug_browser",
    "DEFAULT_DEBUG_PORT",
    "DEBUG_PORT_RANGE",
]


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------
class BrowserLaunchError(AvatarError):
    """浏览器找不到、启动失败、或者调试端口没起来。"""

    exit_code = 13


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
#: 习惯用的调试端口。不强制 —— 被占用会自动换。
DEFAULT_DEBUG_PORT = 9222

#: 端口扫描范围。9222 起往后 20 个，避开常见服务。
DEBUG_PORT_RANGE = (9222, 9242)

#: Chrome / Edge 的常见安装路径（相对 Program Files 等根目录）
_CHROME_RELPATHS = [
    r"Google\Chrome\Application\chrome.exe",
    r"Google\Chrome Beta\Application\chrome.exe",
    r"Google\Chrome Dev\Application\chrome.exe",
    r"Chromium\Application\chrome.exe",
]

_EDGE_RELPATHS = [
    r"Microsoft\Edge\Application\msedge.exe",
    r"Microsoft\Edge Beta\Application\msedge.exe",
    r"Microsoft\Edge Dev\Application\msedge.exe",
]

_ROOTS = [
    os.environ.get("PROGRAMFILES", r"C:\Program Files"),
    os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
    os.environ.get("LOCALAPPDATA", ""),
]


# --------------------------------------------------------------------------
# 结果对象
# --------------------------------------------------------------------------
@dataclass
class BrowserInfo:
    """一个调试端点的完整描述。"""

    #: 形如 ``ws://127.0.0.1:9222/devtools/browser/<uuid>``
    websocket_url: str = ""
    #: CDP 的 ``Browser`` 字段，形如 ``Chrome/120.0.6099.109``
    browser: str = ""
    #: ``Protocol-Version``
    protocol_version: str = ""
    port: int = 0
    #: HTTP ``/json/version`` 的原始响应
    raw: dict = field(default_factory=dict)
    #: 我们自己启动的？False 表示复用了一个已存在的实例
    spawned: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.websocket_url)

    @property
    def kind(self) -> str:
        """粗略判断是 Chrome 还是 Edge（看 UA 串）。"""
        b = (self.browser or "").lower()
        if "edg" in b:
            return "edge"
        if "chrome" in b:
            return "chrome"
        if "electron" in b:
            return "electron"
        return "unknown"

    def describe(self) -> dict:
        return {
            "websocket_url": self.websocket_url,
            "browser": self.browser,
            "protocol_version": self.protocol_version,
            "port": self.port,
            "kind": self.kind,
            "spawned": self.spawned,
        }


# --------------------------------------------------------------------------
# 浏览器可执行文件探测
# --------------------------------------------------------------------------
def _which(name: str) -> Optional[str]:
    """走 PATH 找可执行文件。"""
    found = shutil.which(name)
    return found or None


def find_chrome() -> Optional[str]:
    """找 Chrome 的 exe 路径，找不到返回 None。"""
    p = _which("chrome.exe")
    if p:
        return p
    for root in _ROOTS:
        if not root:
            continue
        for rel in _CHROME_RELPATHS:
            cand = os.path.join(root, rel)
            if os.path.isfile(cand):
                return cand
    return None


def find_edge() -> Optional[str]:
    """找 Edge 的 exe 路径，找不到返回 None。

    Edge 在 Windows 10/11 上是**系统自带**的，所以这条几乎总能成功 ——
    这就是为什么 Web 能力不应该因为"用户没装 Chrome"而失败。
    """
    p = _which("msedge.exe")
    if p:
        return p
    for root in _ROOTS:
        if not root:
            continue
        for rel in _EDGE_RELPATHS:
            cand = os.path.join(root, rel)
            if os.path.isfile(cand):
                return cand
    return None


def find_browser(prefer: str = "chrome") -> Optional[str]:
    """按偏好找浏览器。``prefer`` 取 ``chrome`` / ``edge`` / ``any``。

    ``any`` 时 Chrome 优先（CDP 兼容性最好），退到 Edge。
    """
    p = (prefer or "any").lower()
    if p == "chrome":
        return find_chrome()
    if p == "edge":
        return find_edge()
    return find_chrome() or find_edge()


# --------------------------------------------------------------------------
# 端口探测
# --------------------------------------------------------------------------
def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """看某个端口能不能用。

    ⚠️ **不能只靠 bind 判定**。Windows 上 ``SO_REUSEADDR`` 会允许你
    绑定到一个已被监听的端口（Linux 上则不允许）—— 于是 bind 成功
    但端口其实被占着，探测出假阳性，最后启动的浏览器调试端口起不来。

    所以要两条独立路径交叉验证：

    1. ``connect_ex`` 能连上 → **一定有人在听** → 不空闲（最可靠）
    2. 连不上时再试着 bind 且**不带** ``SO_REUSEADDR`` →
       绑得上才算空闲（挡住 TIME_WAIT 之类的残留）
    """
    # 路径 1：有人监听就绝不空闲
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.25)
        if probe.connect_ex((host, port)) == 0:
            return False
    except OSError:
        pass
    finally:
        probe.close()

    # 路径 2：不带 SO_REUSEADDR 地 bind
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_free_port(lo: int = DEBUG_PORT_RANGE[0],
                   hi: int = DEBUG_PORT_RANGE[1]) -> int:
    """在范围内挑一个空闲端口。都占用就往上继续找，最多 100 个。"""
    for port in range(lo, hi):
        if is_port_free(port):
            return port
    for port in range(hi, hi + 100):
        if is_port_free(port):
            return port
    raise BrowserLaunchError("在 %d-%d 范围内找不到空闲端口" % (lo, hi + 100))


def probe_debug_port(port: int, *, host: str = "127.0.0.1",
                     timeout: float = 1.5) -> Optional[dict]:
    """``GET /json/version``。是 CDP 端点返回 dict，否则 None。

    判据（硬约束 #3）：HTTP 200 **且**返回体里有 ``webSocketDebuggerUrl``。
    只看状态码不够 —— 任何 web 服务都可能返回 200。
    """
    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", "/json/version")
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        body = resp.read()
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if not isinstance(data, dict):
        return None
    if not data.get("webSocketDebuggerUrl"):
        return None
    return data


def http_json(port: int, path: str, *, method: str = "GET",
              host: str = "127.0.0.1", timeout: float = 3.0):
    """调 CDP 的 HTTP 端点，返回解析后的 JSON（失败返回 None）。"""
    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request(method, path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 400:
            return None
        text = body.decode("utf-8", errors="replace").strip()
        return json.loads(text) if text else {}
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def list_debug_instances(ports=None) -> List[BrowserInfo]:
    """扫一批端口，返回所有可用的调试实例。

    默认扫 ``DEBUG_PORT_RANGE``。用于"用户已经开了一个调试 Chrome，
    我要找到它"的场景。
    """
    if ports is None:
        ports = range(DEBUG_PORT_RANGE[0], DEBUG_PORT_RANGE[1])
    out = []
    for port in ports:
        info = probe_debug_port(port)
        if info:
            out.append(BrowserInfo(
                websocket_url=info.get("webSocketDebuggerUrl", ""),
                browser=info.get("Browser", ""),
                protocol_version=info.get("Protocol-Version", ""),
                port=port,
                raw=info,
                spawned=False,
            ))
    return out


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def _default_profile_dir(port: int) -> str:
    """给一个调试实例分配独立的 profile 目录。

    放在临时目录下并带端口号 —— 不同调试实例互不干扰，且不会污染
    用户的默认 profile（硬性要求，见模块 docstring）。
    """
    base = os.path.join(tempfile.gettempdir(), "avatar-cdp")
    return os.path.join(base, "profile-%d" % port)


def launch_browser(exe: str, *, port: int = DEFAULT_DEBUG_PORT,
                   headless: bool = False,
                   url: str = "about:blank",
                   user_data_dir: Optional[str] = None,
                   extra_args: Optional[List[str]] = None,
                   wait_ready: bool = True,
                   ready_timeout: float = 20.0) -> Tuple[subprocess.Popen, BrowserInfo]:
    """启动一个带调试端口的浏览器实例。

    返回 ``(进程, BrowserInfo)``。``BrowserInfo.spawned=True``。

    ⚠️ 必须传独立的 ``--user-data-dir``。理由见模块 docstring ——
    撞上用户默认 profile 时，Chrome 会**静默地把请求转交给已运行的
    实例然后自己退出**，调试端口不会生效，而你会以为启动成功了。
    """
    if not exe or not os.path.isfile(exe):
        raise BrowserLaunchError("浏览器 exe 不存在：%r" % exe, exe=exe)

    if not is_port_free(port):
        # 端口被占：先看是不是已有 CDP 实例（那样应该复用而不是硬启）
        existing = probe_debug_port(port)
        if existing:
            raise BrowserLaunchError(
                "端口 %d 上已有一个调试实例，不应再启动新实例" % port,
                port=port, existing=existing.get("Browser", ""))
        raise BrowserLaunchError("端口 %d 被非 CDP 程序占用" % port, port=port)

    profile = user_data_dir or _default_profile_dir(port)
    os.makedirs(profile, exist_ok=True)

    args = [
        exe,
        "--remote-debugging-port=%d" % port,
        "--remote-allow-origins=http://127.0.0.1:%d" % port,
        "--user-data-dir=%s" % profile,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-extensions",
        "--disable-popup-blocking",
        "--disable-features=Translate,OptimizationHints,MediaRouter",
    ]
    if headless:
        # new headless 才是真 headless（旧的 --headless 在 112 之后行为不同）
        args.append("--headless=new")
        args.append("--disable-gpu")
        args.append("--window-size=1440,900")
    else:
        args.append("--new-window")

    args.extend(extra_args or [])
    if url:
        args.append(url)

    creationflags = 0
    if os.name == "nt":
        # 不加 CREATE_NO_WINDOW —— 我们要看到窗口（非 headless 时）
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise BrowserLaunchError("启动浏览器失败：%s" % exc, exe=exe)

    if not wait_ready:
        return proc, BrowserInfo(port=port, spawned=True)

    info = wait_for_debug_port(port, timeout=ready_timeout, proc=proc)
    return proc, info


def wait_for_debug_port(port: int, *, timeout: float = 20.0,
                        proc: Optional[subprocess.Popen] = None,
                        interval: float = 0.25) -> BrowserInfo:
    """轮询等 ``/json/version`` 就绪。

    启动到就绪通常 0.5–3 秒。超时抛 ``TimeoutError_``。
    """
    deadline = time.monotonic() + timeout
    last_err = ""
    while True:
        if proc is not None and proc.poll() is not None:
            raise BrowserLaunchError(
                "浏览器进程在调试端口就绪前退出（退出码 %s）" % proc.returncode,
                port=port)
        data = probe_debug_port(port, timeout=min(1.5, interval + 1.0))
        if data:
            return BrowserInfo(
                websocket_url=data.get("webSocketDebuggerUrl", ""),
                browser=data.get("Browser", ""),
                protocol_version=data.get("Protocol-Version", ""),
                port=port,
                raw=data,
                spawned=True,
            )
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise TimeoutError_(
                "等待调试端口 %d 就绪超时（%.0fs）%s"
                % (port, timeout, ("，" + last_err) if last_err else ""),
                elapsed=timeout, port=port)
        time.sleep(min(interval, remain))


# --------------------------------------------------------------------------
# 一站式入口
# --------------------------------------------------------------------------
def ensure_debug_browser(*, prefer: str = "chrome",
                         port: Optional[int] = None,
                         headless: bool = False,
                         url: str = "about:blank",
                         reuse: bool = True,
                         ready_timeout: float = 20.0,
                         extra_args: Optional[List[str]] = None):
    """确保有一个可用的调试浏览器，返回 ``(BrowserInfo, proc_or_None)``。

    逻辑（硬约束 #3）：

    1. ``reuse=True`` 时先扫 ``DEBUG_PORT_RANGE`` 找已有调试实例，
       找到就**复用**（``spawned=False``，proc 为 None）。
    2. 否则挑空闲端口、启动新实例。

    复用是有意设计的：用户可能已经开了一个调试 Chrome 放在那儿，
    再启一个纯属浪费资源，而且两个实例会争同一个 profile 目录。
    """
    if reuse:
        for info in list_debug_instances():
            if port is None or info.port == port:
                return info, None

    target_port = port if port is not None else pick_free_port()

    exe = find_browser(prefer)
    if not exe:
        raise BrowserLaunchError(
            "找不到 Chrome 或 Edge。装一个，或者手动启动浏览器时带上 "
            "--remote-debugging-port=<port> 再让本模块复用。",
            prefer=prefer)

    proc, info = launch_browser(
        exe, port=target_port, headless=headless, url=url,
        ready_timeout=ready_timeout, extra_args=extra_args)
    return info, proc


def close_browser(proc: Optional[subprocess.Popen], *,
                  graceful_timeout: float = 3.0) -> bool:
    """关掉我们启动的浏览器。复用来的实例（proc=None）不动。"""
    if proc is None:
        return False
    try:
        proc.terminate()
        try:
            proc.wait(timeout=graceful_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
        return True
    except Exception:
        return False

#: heavy 档的检查项名。quick 档会跳过它们，用这张表**如实记账**
#: （返回里的 ``skipped_heavy``），而不是默默不跑。
HEAVY_CHECK_NAMES = (
    "占用端口判为不空闲",
    "pick_free_port 跳过占用",
    "释放后判为空闲",
    "200 但非 CDP 判为不可用",
    "真 CDP 响应被识别",
    "webSocketDebuggerUrl 可读",
    "Protocol-Version 可读",
    "http_json 对 404 返回 None",
    "可执行文件探测不抛异常",
)



# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """启动器自检（**不启动浏览器**）。

    ## 两档

    ``quick=True``（默认）—— 只做**不碰实机**的检查：常量、
    公开函数存在性、纯逻辑契约、错误路径。实测 < 0.1s，随手可跑。

    ``quick=False``（CLI 的 ``--full``）—— 追加**实机探测**：
    绑端口验证 ``is_port_free`` / ``pick_free_port``、起本地假 HTTP
    服务验证 ``probe_debug_port`` 的判据、扫本机已安装浏览器。
    这些是真调用，耗时且**受机器负载影响**。

    ## 为什么必须分档

    CI 上 Python 3.10/3.11 曾因本项自检 3.41s 超过 3.0s 红线而红，
    而 3.12/3.13 恰好压线通过 —— 那是**机器性能抖动**，不是代码问题。
    默认档必须稳、必须快，否则红线会变成噪音，最后被无视。

    返回里 ``skipped_heavy`` 记录 quick 档跳过了几项重活，
    ``quick`` 记录本次跑的是哪一档。
    """
    checks = []
    failures = []
    skipped_heavy = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # ======================================================================
    # quick 档：常量 / 接口存在性 / 纯逻辑 / 错误路径（不碰实机）
    # ======================================================================

    # -- 端口范围 -----------------------------------------------------------
    lo, hi = DEBUG_PORT_RANGE
    _add("调试端口范围合理", 1024 < lo < hi < 65536,
         "%d-%d" % (lo, hi))
    _add("默认端口在范围内", lo <= DEFAULT_DEBUG_PORT < hi,
         str(DEFAULT_DEBUG_PORT))

    # -- 公开函数存在且可调用 ------------------------------------------------
    #    判据是"接口还在"，不是"实机能不能用" —— 后者归 heavy 档。
    _public = ("probe_debug_port", "find_chrome", "find_edge",
               "find_browser", "is_port_free", "pick_free_port",
               "list_debug_instances", "launch_browser",
               "ensure_debug_browser", "http_json")
    _missing = [n for n in _public if not callable(globals().get(n))]
    _add("公开函数齐备且可调用", not _missing,
         "%d 个%s" % (len(_public),
                      "" if not _missing else "，缺: %s" % ", ".join(_missing)))

    # -- 默认超时有界（防止有人把它调成 0 或 1e9）----------------------------
    import inspect as _inspect
    try:
        _d = _inspect.signature(http_json).parameters["timeout"].default
    except Exception:      # noqa: BLE001 - 签名反射失败不该让自检挂掉
        _d = None
    _add("http_json 默认超时有界",
         isinstance(_d, (int, float)) and 0 < _d <= 30, "timeout=%s" % _d)

    # -- 错误路径：不存在的 exe 必须抛，而不是崩或静默 -------------------------
    #    这条在**启动任何进程之前**就返回，所以是安全的纯校验。
    try:
        launch_browser(r"C:\definitely\not\here\chrome.exe", wait_ready=False)
        _add("launch_browser 对不存在的 exe 抛错", False, "没有抛异常")
    except BrowserLaunchError:
        _add("launch_browser 对不存在的 exe 抛错", True,
             "BrowserLaunchError（启动进程之前就拦下）")
    except Exception as exc:      # noqa: BLE001
        _add("launch_browser 对不存在的 exe 抛错", False,
             "抛了非预期异常：%s: %s" % (type(exc).__name__, exc))

    # ======================================================================
    # heavy 档：实机探测（绑端口 / 起本地假服务 / 扫本机浏览器）
    # ======================================================================
    if quick:
        for _nm in HEAVY_CHECK_NAMES:
            skipped_heavy.append(_nm)
    else:
        _selfcheck_heavy(_add)

    # -- BrowserInfo.kind 判定 ---------------------------------------------
    _add("kind 识别 chrome",
         BrowserInfo(browser="Chrome/120").kind == "chrome", "chrome")
    _add("kind 识别 edge",
         BrowserInfo(browser="Edg/120").kind == "edge", "edge")
    _add("kind 识别 electron",
         BrowserInfo(browser="Electron/28.0").kind == "electron", "electron")
    _add("ok 依赖 websocket_url",
         BrowserInfo().ok is False
         and BrowserInfo(websocket_url="ws://x").ok is True, "契约")

    # -- profile 目录必须带端口（避免两个实例共用）------------------------
    a, b = _default_profile_dir(9222), _default_profile_dir(9223)
    _add("profile 目录按端口隔离", a != b, "%s vs %s" % (a[-12:], b[-12:]))

    return {"checks": checks, "failures": failures,
            "skipped_heavy": len(skipped_heavy),
            "quick": bool(quick), "ok": not failures}



def _selfcheck_heavy(_add) -> None:
    """heavy 档：需要**碰实机**的检查。

    只在 ``selfcheck(quick=False)``（CLI 的 ``--full``）时调用。
    这些是真调用 —— 绑 socket、起本地假 HTTP 服务、扫本机文件系统，
    耗时且受机器负载影响，所以默认档不跑。

    ``_add`` 是 ``selfcheck`` 里的登记闭包，直接复用它的口径。
    """
    # 绑一个端口，验证 is_port_free 能识别出"被占"
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    busy_port = srv.getsockname()[1]
    try:
        _add("占用端口判为不空闲", not is_port_free(busy_port),
             "port=%d" % busy_port)

        # -- pick_free_port 能跳过占用的端口 --------------------------------
        # ⚠️ 这个断言必须在 srv **还开着**的时候做。关掉 srv 之后
        # busy_port 就是空闲的，pick 从 busy_port 开始扫，选中它反而是
        # 正确的 —— 曾经把断言写在 srv.close() 之后，误报过。
        got = pick_free_port(busy_port, busy_port + 5)
        _add("pick_free_port 跳过占用", got != busy_port and got <= busy_port + 5,
             "选中 %d（%d 被占）" % (got, busy_port))
    finally:
        srv.close()
    _add("释放后判为空闲", is_port_free(busy_port), "port=%d" % busy_port)

    # -- 非 CDP 端口必须判为 None（硬约束 #3 的"不只看状态码"）------------
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Fake(BaseHTTPRequestHandler):
        def do_GET(self):
            # 返回 200 但**不是** CDP 响应 —— 必须被拒绝
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"hello, I am not chrome")

        def log_message(self, *a):
            pass

    fake = HTTPServer(("127.0.0.1", 0), _Fake)
    fake_port = fake.server_address[1]
    th = threading.Thread(target=fake.serve_forever, daemon=True)
    th.start()
    try:
        _add("200 但非 CDP 判为不可用",
             probe_debug_port(fake_port, timeout=1.0) is None,
             "port=%d" % fake_port)
    finally:
        fake.shutdown()
        fake.server_close()

    # -- 真正的 /json/version 形态能被识别 ---------------------------------
    class _FakeCdp(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/json/version":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({
                "Browser": "Chrome/120.0.6099.109",
                "Protocol-Version": "1.3",
                "webSocketDebuggerUrl": "ws://127.0.0.1:%d/devtools/browser/abc"
                                        % self.server.server_address[1],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    cdp = HTTPServer(("127.0.0.1", 0), _FakeCdp)
    cdp_port = cdp.server_address[1]
    th2 = threading.Thread(target=cdp.serve_forever, daemon=True)
    th2.start()
    try:
        got = probe_debug_port(cdp_port, timeout=1.0)
        _add("真 CDP 响应被识别", got is not None and got.get("Browser"),
             got.get("Browser", "") if got else "None")
        _add("webSocketDebuggerUrl 可读",
             bool(got and got.get("webSocketDebuggerUrl", "").startswith("ws://")),
             (got or {}).get("webSocketDebuggerUrl", "")[:48])
        _add("Protocol-Version 可读",
             (got or {}).get("Protocol-Version") == "1.3",
             str((got or {}).get("Protocol-Version")))
    finally:
        cdp.shutdown()
        cdp.server_close()

    # -- http_json 对 404 返回 None ----------------------------------------
    class _Fake404(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    n404 = HTTPServer(("127.0.0.1", 0), _Fake404)
    p404 = n404.server_address[1]
    threading.Thread(target=n404.serve_forever, daemon=True).start()
    try:
        _add("http_json 对 404 返回 None",
             http_json(p404, "/json/list", timeout=1.0) is None,
             "port=%d" % p404)
    finally:
        n404.shutdown()
        n404.server_close()

    # -- 可执行文件探测（**不要求一定找到**，只要求不炸）-------------------
    try:
        chrome = find_chrome()
        edge = find_edge()
        found = [n for n, p in (("chrome", chrome), ("edge", edge)) if p]
        _add("可执行文件探测不抛异常", True,
             ("找到 " + " / ".join(found)) if found
             else "本机未装 Chrome/Edge（影响不大，可复用外部实例）")
    except Exception as exc:      # noqa: BLE001
        _add("可执行文件探测不抛异常", False, "%s: %s"
             % (type(exc).__name__, exc))
