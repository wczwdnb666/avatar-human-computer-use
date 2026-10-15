# -*- coding: utf-8 -*-
"""CDP 不可用时的降级路径。

Web 控制**不是**永远可用的。以下情况 CDP 拿不到：

1. 目标不是浏览器 / Electron（普通 Win32 程序）—— 没有调试端口。
2. 浏览器在跑但**没带** ``--remote-debugging-port`` 启动，
   而这是**已经运行中**的实例，你不能重启它（用户正用着）。
3. Electron 应用没开调试端口（绝大多数商业软件默认不开）。
4. 目标页面在 iframe 跨域隔离区 / 浏览器内部页（``chrome://``、``edge://``）
   —— CDP 拒绝注入。
5. 调试端口被安全软件拦了 / 端口连通但握手失败。

这时不能直接失败退出，而要**降级到桌面手段**：

============================  ==========================================
降级级别                        手段
============================  ==========================================
``cdp``                        原生 CDP（最优）
``uia``                        走阶段二的 UIA 树（能读控件名/值）
``vision``                     纯视觉：截图 + 模板/pHash 定位
``input``                      纯物理输入：只知道坐标时的盲操作
============================  ==========================================

本模块只负责**决策与编排** —— 具体执行仍由阶段二/三的
``uia`` / ``input`` / ``vision`` 模块做，这里不重复实现。

## 判断"能不能用 CDP"的正确顺序

1. **有 hwnd 吗？** 有 → 看它是不是浏览器/Electron（进程名白名单）。
2. **进程命令行里有 ``--remote-debugging-port=N`` 吗？**
   有 → 直接 ``probe_debug_port(N)``。
   没有 → **不要**盲目扫端口（会连到别的程序的调试端口，很危险），
   只有在明确知道目标时才扫。
3. **探测确认是 CDP 吗？** 判据是 ``/json/version`` 返回 200
   **且** body 里有 ``webSocketDebuggerUrl``。只看状态码会把普通 web 服务
   误判成 CDP。
4. 都过了才用 CDP。否则按 ``UIA → 视觉 → 物理输入`` 逐级降。

## 降级不是失败

降级要**如实记录**：``FallbackDecision.chosen`` 和 ``.reason``
会进验证证据链。用 UIA 冒充 CDP 而不标注，是最坏的做法 ——
后面排查会完全找不到线头。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from ..errors import AvatarError

__all__ = [
    "FallbackError",
    "FallbackLevel",
    "FallbackDecision",
    "BROWSER_PROCESSES",
    "ELECTRON_PROCESSES",
    "detect_process_kind",
    "has_debug_flag",
    "parse_debug_port",
    "probe_cdp_available",
    "decide_level",
    "describe_decision",
    "with_fallback",
    "selfcheck",
]


class FallbackError(AvatarError):
    """降级过程中出错。"""

    exit_code = 20


class FallbackLevel(object):
    """降级级别常量（用字符串，便于日志和序列化）。"""

    CDP = "cdp"
    UIA = "uia"
    VISION = "vision"
    INPUT = "input"
    NONE = "none"

    ORDER = (CDP, UIA, VISION, INPUT, NONE)

    @classmethod
    def rank(cls, level: str) -> int:
        try:
            return cls.ORDER.index(level)
        except ValueError:
            return len(cls.ORDER)

    @classmethod
    def weaker(cls, a: str, b: str) -> str:
        """返回更弱的那个级别。"""
        return a if cls.rank(a) >= cls.rank(b) else b


# 普通浏览器进程名（小写，不带 .exe）。
BROWSER_PROCESSES = frozenset({
    "chrome", "msedge", "firefox", "brave", "opera", "vivaldi",
    "chromium", "360se", "360chrome", "qqbrowser", "sogouexplorer",
    "liebao", "maxthon", "the world", "avant", "centbrowser",
})

# 基于 Electron / CEF 的桌面应用（小写，不带 .exe）。
# 这些**可能**有 CDP，取决于启动时是否带 --remote-debugging-port。
ELECTRON_PROCESSES = frozenset({
    "code", "code - insiders", "vscodium", "cursor", "windsurf",
    "dingtalk", "feishu", "lark", "wechat", "wxwork",
    "discord", "slack", "teams", "notion", "obsidian", "typora",
    "postman", "figma", "skype", "signal", "telegram",
    "spotify", "steam", "steamwebhelper", "battle.net",
    "github desktop", "gitkraken", "sourcetree",
    "docker desktop", "clash", "clash verge", "v2ray",
    "electron", "nwjs", "node-webkit",
    "powershell ise", "eudic", "youdao dict", "baidunetdisk",
    "quark", "thunder", "xunlei", "douyin", "bilibili",
})

# 明确**不可能**有 CDP 的（省一次无用的扫描）。
NEVER_CDP_PROCESSES = frozenset({
    "explorer", "winlogon", "csrss", "smss", "lsass", "services",
    "svchost", "taskhostw", "dwm", "system", "registry",
    "windowsterminal", "conhost", "mintty",
})

_DEBUG_FLAG_RE = re.compile(
    r"--remote-debugging-port(?:=|\s+)(\d{1,5})", re.IGNORECASE)
_DEBUG_PIPE_RE = re.compile(r"--remote-debugging-pipe", re.IGNORECASE)
_INSPECT_RE = re.compile(r"--inspect(?:-brk)?(?:=|\s+)(\d{1,5})",
                         re.IGNORECASE)


def _base(name: str) -> str:
    """``Code.exe`` → ``code``；``C:\\x\\chrome.exe`` → ``chrome``。"""
    n = (name or "").strip().strip('"')
    n = n.replace("\\", "/").rsplit("/", 1)[-1]
    if n.lower().endswith(".exe"):
        n = n[:-4]
    return n.strip().lower()


def detect_process_kind(process_name: str) -> str:
    """判断进程属于哪一类。

    返回 ``"browser"`` / ``"electron"`` / ``"never_cdp"`` / ``"unknown"``。

    **注意**：``electron`` 只表示"有可能是 Electron"，不代表一定有
    调试端口 —— 必须再验命令行。
    """
    b = _base(process_name)
    if not b:
        return "unknown"
    if b in NEVER_CDP_PROCESSES:
        return "never_cdp"
    if b in BROWSER_PROCESSES:
        return "browser"
    if b in ELECTRON_PROCESSES:
        return "electron"
    # 名字里带 browser/chrome/edge 的也当浏览器（变体太多，白名单跟不上）
    for kw in ("chrome", "edge", "browser", "chromium"):
        if kw in b:
            return "browser"
    for kw in ("electron", "nwjs", "cef"):
        if kw in b:
            return "electron"
    return "unknown"


def has_debug_flag(cmdline: str) -> bool:
    """命令行里是否带了 ``--remote-debugging-port`` 或 ``-pipe``。"""
    c = cmdline or ""
    if _DEBUG_FLAG_RE.search(c):
        return True
    if _DEBUG_PIPE_RE.search(c):
        return True
    # 浏览器有时用 --remote-debugging-port 无值形式（罕见），保守不认
    return False


def parse_debug_port(cmdline: str) -> Optional[int]:
    """从命令行提取调试端口号。没有返回 None。

    ``--remote-debugging-pipe`` 形式返回 ``None`` —— 管道模式没有端口，
    只能通过 fd 3/4 通信，本库不支持，要明确告知调用方。
    """
    m = _DEBUG_FLAG_RE.search(cmdline or "")
    if not m:
        return None
    try:
        p = int(m.group(1))
    except (TypeError, ValueError):
        return None
    if 1 <= p <= 65535:
        return p
    return None


def parse_inspect_port(cmdline: str) -> Optional[int]:
    """从命令行提取 Node/Electron 的 ``--inspect`` 端口。"""
    m = _INSPECT_RE.search(cmdline or "")
    if not m:
        return None
    try:
        p = int(m.group(1))
    except (TypeError, ValueError):
        return None
    return p if 1 <= p <= 65535 else None


def probe_cdp_available(port: int, *, timeout: float = 1.5) -> bool:
    """探测端口上是不是真的 CDP。

    判据：``/json/version`` 返回 200 **且** body 里含
    ``webSocketDebuggerUrl``。**只看状态码是错的** ——
    任何 HTTP 服务都会返回 200。
    """
    try:
        from .launcher import probe_debug_port
    except Exception:
        return False
    try:
        info = probe_debug_port(int(port), timeout=timeout)
    except Exception:
        return False
    return bool(info)


class FallbackDecision(object):
    """降级决策的结果。**必须**如实体现在日志/证据里。"""

    __slots__ = ("chosen", "reason", "kind", "port", "cdp_ok",
                 "candidates", "notes", "decided_at", "process_name")

    def __init__(
        self,
        chosen: str,
        *,
        reason: str = "",
        kind: str = "unknown",
        port: Optional[int] = None,
        cdp_ok: bool = False,
        candidates: Optional[List[str]] = None,
        notes: Optional[List[str]] = None,
        process_name: str = "",
    ) -> None:
        self.chosen = chosen
        self.reason = reason
        self.kind = kind
        self.port = port
        self.cdp_ok = bool(cdp_ok)
        self.candidates = list(candidates or [])
        self.notes = list(notes or [])
        self.decided_at = time.time()
        self.process_name = process_name

    @property
    def is_cdp(self) -> bool:
        return self.chosen == FallbackLevel.CDP

    @property
    def degraded(self) -> bool:
        """是否发生了降级（即没走 CDP）。"""
        return self.chosen != FallbackLevel.CDP

    @property
    def ok(self) -> bool:
        """是否有一个可用的手段（``none`` 表示彻底没辙）。"""
        return self.chosen != FallbackLevel.NONE

    def describe(self) -> Dict[str, Any]:
        return {
            "chosen": self.chosen,
            "reason": self.reason,
            "kind": self.kind,
            "port": self.port,
            "cdp_ok": self.cdp_ok,
            "degraded": self.degraded,
            "candidates": self.candidates,
            "notes": self.notes,
            "process_name": self.process_name,
            "decided_at": self.decided_at,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return "<Fallback %s (%s)>" % (self.chosen, self.reason)


def decide_level(
    *,
    process_name: str = "",
    cmdline: str = "",
    hwnd: int = 0,
    port: Optional[int] = None,
    has_uia: bool = True,
    has_vision: bool = True,
    has_input: bool = True,
    probe: bool = True,
    probe_timeout: float = 1.5,
) -> FallbackDecision:
    """决定该用哪一级手段。

    参数都能单独给（不依赖真实进程）—— 便于测试和离线推理。

    - ``process_name`` / ``cmdline``：进程信息（可为空）。
    - ``port``：显式指定的调试端口；不给则从 cmdline 里解析。
    - ``has_*``：后续级别是否可用（调用方按环境给）。
    - ``probe=False``：跳过真实端口探测（离线/测试用）。
    """
    notes = []
    candidates = []

    kind = detect_process_kind(process_name)
    if kind == "never_cdp":
        notes.append("进程在 NEVER_CDP 名单里，不做端口探测")
        candidates = _tail_levels(has_uia, has_vision, has_input)
        return FallbackDecision(
            candidates[0] if candidates else FallbackLevel.NONE,
            reason="process_never_cdp",
            kind=kind, process_name=process_name,
            candidates=candidates, notes=notes)

    if not process_name:
        # 没进程信息：不知道是不是浏览器。若显式给了端口才敢试 CDP。
        if not port:
            notes.append("无进程信息且无显式端口，无法用 CDP")
            candidates = _tail_levels(has_uia, has_vision, has_input)
            return FallbackDecision(
                candidates[0] if candidates else FallbackLevel.NONE,
                reason="no_process_info",
                kind="unknown", candidates=candidates, notes=notes)

    cmd = cmdline or ""
    explicit_port = port
    parsed = parse_debug_port(cmd)
    eff_port = explicit_port if explicit_port else parsed

    if parsed is None and explicit_port is None and has_debug_pipe(cmd):
        notes.append("目标用的是 --remote-debugging-pipe，本库不支持管道模式")
        candidates = _tail_levels(has_uia, has_vision, has_input)
        return FallbackDecision(
            candidates[0] if candidates else FallbackLevel.NONE,
            reason="pipe_mode_unsupported",
            kind=kind, process_name=process_name,
            candidates=candidates, notes=notes)

    if eff_port is None:
        if kind in ("browser", "electron"):
            notes.append(
                "%s 未带 --remote-debugging-port%s"
                % (kind, "（重启时可加）" if kind == "browser"
                   else "（多数商业 Electron 默认不开）"))
            candidates = _tail_levels(has_uia, has_vision, has_input)
            return FallbackDecision(
                candidates[0] if candidates else FallbackLevel.NONE,
                reason="no_debug_port",
                kind=kind, process_name=process_name,
                candidates=candidates, notes=notes)
        notes.append("未知进程类型且无调试端口，不盲扫")
        candidates = _tail_levels(has_uia, has_vision, has_input)
        return FallbackDecision(
            candidates[0] if candidates else FallbackLevel.NONE,
            reason="unknown_process_no_port",
            kind=kind, process_name=process_name,
            candidates=candidates, notes=notes)

    ok = False
    if probe:
        t0 = time.time()
        ok = probe_cdp_available(eff_port, timeout=probe_timeout)
        notes.append("探测端口 %d：%s（%.0fms）"
                     % (eff_port, "是 CDP" if ok else "不是 CDP",
                        (time.time() - t0) * 1000))
    else:
        # 不探测时按"端口已给出"算作可用，但**标注这是未经验证的**
        ok = True
        notes.append("跳过端口探测（probe=False），CDP 可用性未经验证")

    if ok:
        candidates = [FallbackLevel.CDP] + _tail_levels(
            has_uia, has_vision, has_input)
        return FallbackDecision(
            FallbackLevel.CDP,
            reason="cdp_available" if probe else "cdp_assumed",
            kind=kind, port=eff_port, cdp_ok=bool(probe),
            candidates=candidates, notes=notes, process_name=process_name)

    notes.append("端口 %d 上没有 CDP 服务（可能连错了进程）" % eff_port)
    candidates = _tail_levels(has_uia, has_vision, has_input)
    return FallbackDecision(
        candidates[0] if candidates else FallbackLevel.NONE,
        reason="port_not_cdp",
        kind=kind, port=eff_port, cdp_ok=False,
        candidates=candidates, notes=notes, process_name=process_name)


def has_debug_pipe(cmdline: str) -> bool:
    """是否是管道模式调试（没有端口）。"""
    return bool(_DEBUG_PIPE_RE.search(cmdline or ""))


def _tail_levels(has_uia: bool, has_vision: bool, has_input: bool) -> List[str]:
    out = []
    if has_uia:
        out.append(FallbackLevel.UIA)
    if has_vision:
        out.append(FallbackLevel.VISION)
    if has_input:
        out.append(FallbackLevel.INPUT)
    return out


def describe_decision(d: FallbackDecision) -> str:
    """给用户看的一行说明。"""
    if d.is_cdp:
        return "使用 CDP（端口 %s）" % d.port
    if not d.ok:
        return "无可用手段：%s" % d.reason
    return "降级到 %s（%s）" % (d.chosen.upper(), d.reason)


def with_fallback(
    primary: Any,
    fallbacks: Optional[Any] = None,
    *,
    verify: Optional[Any] = None,
    decision: Optional[FallbackDecision] = None,
    args: Optional[tuple] = None,
    kwargs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """依次尝试主路径与降级路径，第一个"执行成功**且**验证通过"的算数。

    ``primary`` / ``fallbacks`` 是**可调用对象**（返回 ActionResult 之类）。

    返回::

        {"ok": bool, "used": "cdp"|"uia"|..., "result": <最后一次结果>,
         "attempts": [{"level","ok","verified","error"}...],
         "decision": <FallbackDecision or None>}

    ``verify`` 给了的话，每个候选的返回值会被它验证；
    验证不通过会**继续往下降**而不是直接返回 —— 这是关键：
    "动作没报错"不等于"动作生效了"。
    """
    seq = []  # type: List[tuple]
    if primary is not None:
        lvl = FallbackLevel.CDP
        if decision is not None:
            lvl = decision.chosen
        seq.append((lvl, primary))
    if fallbacks:
        if callable(fallbacks):
            fb_list = [fallbacks]
        else:
            fb_list = list(fallbacks)
        start = FallbackLevel.rank(seq[0][0]) if seq else 0
        for i, fn in enumerate(fb_list):
            name = FallbackLevel.ORDER[min(start + 1 + i,
                                           len(FallbackLevel.ORDER) - 1)]
            seq.append((name, fn))

    a = tuple(args or ())
    k = dict(kwargs or {})
    attempts = []
    last = None
    last_level = FallbackLevel.NONE

    for lvl, fn in seq:
        last_level = lvl
        entry = {"level": lvl, "ok": False, "verified": None, "error": None}
        try:
            res = fn(*a, **k)
            last = res
            entry["ok"] = _result_ok(res)
        except Exception as exc:
            last = None
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            attempts.append(entry)
            continue

        # 动作自己报失败 → 继续降级（"没成功"和"验证不通过"一样要往下走）
        if not entry["ok"]:
            attempts.append(entry)
            continue

        if verify is not None:
            try:
                v = verify(res)
                entry["verified"] = _verify_ok(v)
            except Exception as exc:
                entry["verified"] = False
                entry["error"] = "verify: %s" % (exc,)
            if not entry["verified"]:
                # 没验证通过 → 继续降级
                attempts.append(entry)
                continue

        attempts.append(entry)
        return {"ok": True, "used": lvl, "result": last,
                "attempts": attempts, "decision": decision}

    return {"ok": False, "used": last_level, "result": last,
            "attempts": attempts, "decision": decision}


def _result_ok(res: Any) -> bool:
    """从各种结果对象里取 ok。"""
    if res is None:
        return False
    if isinstance(res, bool):
        return res
    if isinstance(res, dict):
        return bool(res.get("ok"))
    return bool(getattr(res, "ok", False))


def _verify_ok(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, dict):
        return bool(v.get("ok"))
    return bool(getattr(v, "ok", False))


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

def selfcheck() -> Dict[str, Any]:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    # --- 进程分类 ---
    add("chrome 判为 browser", detect_process_kind("chrome.exe") == "browser")
    add("msedge 判为 browser",
        detect_process_kind("C:\\Program Files (x86)\\Microsoft\\Edge\\"
                            "Application\\msedge.exe") == "browser")
    add("Code.exe 判为 electron",
        detect_process_kind("Code.exe") == "electron")
    add("DingTalk 判为 electron",
        detect_process_kind("DingTalk.exe") == "electron")
    add("explorer 判为 never_cdp",
        detect_process_kind("explorer.exe") == "never_cdp")
    add("svchost 判为 never_cdp",
        detect_process_kind("svchost.exe") == "never_cdp")
    add("空名判 unknown", detect_process_kind("") == "unknown")
    add("随机名判 unknown",
        detect_process_kind("mytool.exe") == "unknown")
    add("名字带 chrome 归 browser",
        detect_process_kind("ChromeCustom.exe") == "browser")
    add("大小写不敏感", detect_process_kind("CHROME.EXE") == "browser")
    add("路径分隔符兼容",
        detect_process_kind("C:/x/y/feishu.exe") == "electron")

    # --- 调试标志 ---
    add("识别 --remote-debugging-port=9222",
        has_debug_flag("chrome.exe --remote-debugging-port=9222") is True)
    add("识别空格形式",
        has_debug_flag("chrome.exe --remote-debugging-port 9223") is True)
    add("识别大写",
        has_debug_flag("--REMOTE-DEBUGGING-PORT=9222") is True)
    add("无标志判 False",
        has_debug_flag("chrome.exe --user-data-dir=x") is False)
    add("空串判 False", has_debug_flag("") is False)
    add("None 判 False", has_debug_flag(None) is False)
    add("识别 pipe 模式",
        has_debug_flag("--remote-debugging-pipe") is True)

    # --- 端口解析 ---
    add("解析端口 9222",
        parse_debug_port("x --remote-debugging-port=9222") == 9222)
    add("解析空格形式端口",
        parse_debug_port("x --remote-debugging-port 9223") == 9223)
    add("无端口返回 None", parse_debug_port("chrome.exe") is None)
    add("端口 0 拒绝",
        parse_debug_port("--remote-debugging-port=0") is None)
    add("端口 99999 拒绝",
        parse_debug_port("--remote-debugging-port=99999") is None)
    add("解析 inspect 端口",
        parse_inspect_port("node --inspect=5858 a.js") == 5858)
    add("解析 inspect-brk 端口",
        parse_inspect_port("node --inspect-brk=5859 a.js") == 5859)
    add("无 inspect 返回 None", parse_inspect_port("node a.js") is None)

    # --- 决策：never_cdp ---
    d = decide_level(process_name="explorer.exe", probe=False)
    add("never_cdp 降级 uia",
        d.chosen == FallbackLevel.UIA and d.reason == "process_never_cdp", d.reason)
    add("never_cdp 不算 cdp", d.is_cdp is False)
    add("never_cdp degraded True", d.degraded is True)
    add("never_cdp ok True", d.ok is True)
    add("never_cdp 带 notes", len(d.notes) >= 1)

    # --- 决策：无进程信息 ---
    d = decide_level(process_name="", probe=False)
    add("无进程信息降级 uia", d.chosen == FallbackLevel.UIA)
    add("无进程信息 reason 正确", d.reason == "no_process_info", d.reason)

    d = decide_level(process_name="", port=9222, probe=False)
    add("无进程信息但给端口 → 假定 CDP",
        d.chosen == FallbackLevel.CDP and d.reason == "cdp_assumed", d.reason)

    # --- 决策：浏览器无调试端口 ---
    d = decide_level(process_name="chrome.exe",
                     cmdline="chrome.exe --user-data-dir=x", probe=False)
    add("浏览器无端口降级 uia",
        d.chosen == FallbackLevel.UIA and d.reason == "no_debug_port",
        d.reason)
    add("浏览器无端口 kind=browser", d.kind == "browser")
    add("浏览器无端口提示可重启",
        any("重启" in n for n in d.notes), d.notes)

    # --- 决策：Electron 无端口 ---
    d = decide_level(process_name="Code.exe", cmdline="Code.exe .", probe=False)
    add("Electron 无端口降级 uia", d.chosen == FallbackLevel.UIA)
    add("Electron 提示默认不开",
        any("默认不开" in n for n in d.notes), d.notes)

    # --- 决策：有端口不探测 ---
    d = decide_level(process_name="chrome.exe",
                     cmdline="chrome.exe --remote-debugging-port=9999",
                     probe=False)
    add("有端口且不探测 → cdp_assumed",
        d.chosen == FallbackLevel.CDP and d.reason == "cdp_assumed", d.reason)
    add("有端口 cdp_ok False（未验证）", d.cdp_ok is False)
    add("有端口 port 记录正确", d.port == 9999, d.port)
    add("未探测有明确标注",
        any("未经验证" in n for n in d.notes), d.notes)

    # --- 决策：探测失败（用一个几乎不可能有人听的端口） ---
    d = decide_level(process_name="chrome.exe",
                     cmdline="chrome.exe --remote-debugging-port=49517",
                     probe=True, probe_timeout=0.3)
    add("探测不到 CDP 则降级",
        d.chosen == FallbackLevel.UIA and d.reason == "port_not_cdp", d.reason)
    add("探测失败 cdp_ok False", d.cdp_ok is False)
    add("探测失败提示可能连错",
        any("连错" in n for n in d.notes), d.notes)

    # --- 决策：pipe 模式 ---
    d = decide_level(process_name="Code.exe",
                     cmdline="Code.exe --remote-debugging-pipe", probe=False)
    add("pipe 模式降级", d.chosen == FallbackLevel.UIA)
    add("pipe 模式 reason 正确",
        d.reason == "pipe_mode_unsupported", d.reason)

    # --- 各级不可用 ---
    d = decide_level(process_name="explorer.exe", has_uia=False,
                     has_vision=False, has_input=False, probe=False)
    add("全无手段 → none", d.chosen == FallbackLevel.NONE)
    add("全无手段 ok False", d.ok is False)
    add("全无手段 degraded True", d.degraded is True)

    d = decide_level(process_name="explorer.exe", has_uia=False,
                     has_vision=False, has_input=True, probe=False)
    add("只剩 input → input", d.chosen == FallbackLevel.INPUT, d.chosen)

    d = decide_level(process_name="explorer.exe", has_uia=False,
                     has_vision=True, has_input=True, probe=False)
    add("无 uia → vision", d.chosen == FallbackLevel.VISION, d.chosen)

    # --- 候选序列 ---
    d = decide_level(process_name="explorer.exe", probe=False)
    add("候选序列 [uia,vision,input]",
        d.candidates == [FallbackLevel.UIA, FallbackLevel.VISION,
                         FallbackLevel.INPUT], d.candidates)
    d = decide_level(process_name="", port=9222, probe=False)
    add("CDP 候选含全部",
        d.candidates[0] == FallbackLevel.CDP and len(d.candidates) == 4,
        d.candidates)

    # --- 等级序 ---
    add("rank cdp 最小", FallbackLevel.rank(FallbackLevel.CDP) == 0)
    add("rank input 较大", FallbackLevel.rank(FallbackLevel.INPUT) > 0)
    add("rank 未知最大",
        FallbackLevel.rank("zzz") == len(FallbackLevel.ORDER))
    add("weaker 取更弱的",
        FallbackLevel.weaker(FallbackLevel.CDP, FallbackLevel.UIA) ==
        FallbackLevel.UIA)
    add("weaker 顺序无关",
        FallbackLevel.weaker(FallbackLevel.UIA, FallbackLevel.CDP) ==
        FallbackLevel.UIA)

    # --- describe ---
    d = decide_level(process_name="", port=9222, probe=False)
    add("describe CDP 带端口", "9222" in describe_decision(d),
        describe_decision(d))
    d = decide_level(process_name="explorer.exe", probe=False)
    add("describe 降级带级别", "UIA" in describe_decision(d),
        describe_decision(d))
    d = decide_level(process_name="explorer.exe", has_uia=False,
                     has_vision=False, has_input=False, probe=False)
    add("describe 无手段", "无可用手段" in describe_decision(d),
        describe_decision(d))

    # --- FallbackDecision 契约 ---
    d = decide_level(process_name="explorer.exe", probe=False)
    add("describe 字段齐",
        set(d.describe()) >= {"chosen", "reason", "kind", "port", "cdp_ok",
                              "degraded", "candidates", "notes",
                              "process_name", "decided_at"},
        sorted(d.describe()))
    add("decision 有 decided_at", d.decided_at > 0)

    # --- with_fallback ---
    class _R(object):
        def __init__(self, ok, tag=""):
            self.ok = ok
            self.tag = tag

    calls = []

    def prim():
        calls.append("prim")
        raise RuntimeError("primary boom")

    res = with_fallback(prim, [lambda: _R(True, "fb")])
    add("with_fallback 主路径异常后走备选",
        res["ok"] is True and res["used"] == FallbackLevel.UIA, res)
    add("with_fallback 记录异常",
        res["attempts"][0]["error"].startswith("RuntimeError"), res["attempts"][0])

    res = with_fallback(lambda: _R(False), [lambda: _R(True)])
    add("with_fallback 主路径 ok=False 继续",
        res["ok"] is True and res["used"] == FallbackLevel.UIA, res)

    res = with_fallback(lambda: _R(True), [lambda: _R(True)])
    add("with_fallback 主路径成功即停",
        res["used"] == FallbackLevel.CDP and len(res["attempts"]) == 1, res)

    # 验证不通过 → 继续降级
    res = with_fallback(lambda: _R(True, "p"),
                        [lambda: _R(True, "f")],
                        verify=lambda r: r.tag == "f")
    add("verify 不过则继续降级",
        res["used"] == FallbackLevel.UIA and len(res["attempts"]) == 2, res)
    add("verify 不过被记录",
        res["attempts"][0]["verified"] is False, res["attempts"][0])

    # 全通过
    res = with_fallback(lambda: _R(True, "p"), verify=lambda r: True)
    add("verify 通过即返回",
        res["ok"] is True and res["attempts"][0]["verified"] is True, res)

    # verify 抛异常 → 当失败
    def bad_verify(r):
        raise RuntimeError("vboom")

    res = with_fallback(lambda: _R(True), [lambda: _R(True)],
                        verify=bad_verify)
    add("verify 抛异常 → 全员弃用，used 为最后尝试级别",
        res["used"] == FallbackLevel.UIA and res["ok"] is False, res)
    add("verify 异常记在 error",
        "verify" in (res["attempts"][0]["error"] or ""), res["attempts"][0])
    add("verify 抛异常时两个候选都试过",
        len(res["attempts"]) == 2, len(res["attempts"]))

    # 全失败
    res = with_fallback(lambda: _R(False), [lambda: _R(False)])
    add("全失败 ok False", res["ok"] is False)
    add("全失败 used 为最后尝试的级别",
        res["used"] == FallbackLevel.UIA, res["used"])
    add("全失败 attempts 数对", len(res["attempts"]) == 2, len(res["attempts"]))

    # dict 返回值
    res = with_fallback(lambda: {"ok": True})
    add("兼容 dict 结果", res["ok"] is True)

    # 无 primary 只给 fallbacks
    res = with_fallback(None, [lambda: _R(True)])
    add("无 primary 直接从 uia 起", res["used"] == FallbackLevel.UIA, res)

    # 传参
    def takes(a, b=1):
        return _R(a == 5 and b == 2)

    res = with_fallback(takes, args=(5,), kwargs={"b": 2})
    add("with_fallback 透传 args/kwargs", res["ok"] is True)

    # decision 参与
    d = decide_level(process_name="explorer.exe", probe=False)
    res = with_fallback(lambda: _R(True), decision=d)
    add("with_fallback 用 decision 定级别",
        res["used"] == FallbackLevel.UIA, res["used"])
    add("with_fallback 回传 decision", res["decision"] is d)

    # None 结果算失败
    res = with_fallback(lambda: None, [lambda: _R(True)])
    add("None 结果算失败", res["used"] == FallbackLevel.UIA)

    # --- probe_cdp_available 在没人听的端口上必须 False ---
    add("probe 空端口返回 False",
        probe_cdp_available(49518, timeout=0.3) is False)

    failures = [c["name"] for c in checks if not c["ok"]]
    return {"checks": checks, "failures": failures,
            "ok": not failures, "module": "web_cdp.fallback"}


if __name__ == "__main__":  # pragma: no cover
    r = selfcheck()
    print("OK %d" % len(r["checks"]) if r["ok"] else "FAIL")
    for c in r["checks"]:
        if not c["ok"]:
            print("  [!!] %s | %s" % (c["name"], c["detail"]))
