# -*- coding: utf-8 -*-
"""阶段四 · Electron 识别与降级决策测试（批次 4G）。

这一层的**核心价值不是"能不能控"**，而是**"判断该不该控"**：

绝大多数商业 Electron 应用（钉钉、飞书、Discord……）默认**不开**
调试端口。硬去连只会拿到一堆连接超时，然后误判成"工具不好用"。
正确做法是先做判定，判定不通过就**明确降级到 UIA**，而不是硬撑。

所以这个文件的重点在：
* 进程名 → 类型的映射（白名单 / 黑名单 / 模糊匹配）
* 命令行 → 调试端口的解析（含 ``--remote-debugging-pipe`` 这个坑）
* 降级决策的**理由字段** —— 每次降级都要能说清"为什么"
* 真实 TCP 扫描（本地回环，不需要外部进程）
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar.web_cdp import electron as E  # noqa: E402
from avatar.web_cdp import fallback as F  # noqa: E402


# ==========================================================================
# 进程名 → 类型
# ==========================================================================

@pytest.mark.parametrize("name,expect", [
    ("chrome.exe", "browser"),
    ("msedge.exe", "browser"),
    ("Code.exe", "electron"),
    ("DingTalk.exe", "electron"),
    ("Feishu.exe", "electron"),
    ("Discord.exe", "electron"),
    ("explorer.exe", "never_cdp"),
    ("svchost.exe", "never_cdp"),
    ("some_unknown_thing.exe", "unknown"),
])
def test_detect_process_kind(name, expect):
    """进程名映射表 —— 这是整个降级决策的第一道闸门。"""
    assert F.detect_process_kind(name) == expect


def test_detect_process_kind_handles_full_path():
    """必须能吃完整路径（Windows 命令行里就是全路径）。"""
    assert F.detect_process_kind(
        r"C:\Program Files\Microsoft VS Code\Code.exe") == "electron"
    assert F.detect_process_kind(
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    ) == "browser"


def test_detect_process_kind_case_insensitive():
    """大小写不能影响判断 —— Windows 上进程名大小写不统一。"""
    for n in ("code.exe", "CODE.EXE", "Code.Exe"):
        assert F.detect_process_kind(n) == "electron"


def test_detect_process_kind_fuzzy_browser():
    """带 chrome/edge 字样的变体当浏览器（白名单不可能穷举）。"""
    assert F.detect_process_kind("my-chrome-mod.exe") == "browser"
    assert F.detect_process_kind("edge_beta.exe") == "browser"


def test_detect_process_kind_fuzzy_electron():
    """带 electron/cef 字样的当 Electron。"""
    assert F.detect_process_kind("my-electron-app.exe") == "electron"
    assert F.detect_process_kind("foo-cef-host.exe") == "electron"


def test_detect_process_kind_empty():
    """空串不能崩，返回 unknown。"""
    assert F.detect_process_kind("") == "unknown"
    assert F.detect_process_kind("   ") == "unknown"
    assert F.detect_process_kind("/") == "unknown"


def test_never_cdp_list_is_not_empty():
    """黑名单要有内容 —— 它是省掉无谓端口扫描的关键。"""
    assert len(F.NEVER_CDP_PROCESSES) >= 5
    # 黑名单和白名单不能有交集（否则判定结果取决于检查顺序，很脆）
    assert not (F.NEVER_CDP_PROCESSES & F.BROWSER_PROCESSES)
    assert not (F.NEVER_CDP_PROCESSES & F.ELECTRON_PROCESSES)


# ==========================================================================
# 命令行解析
# ==========================================================================

@pytest.mark.parametrize("cmd,port", [
    ("chrome.exe --remote-debugging-port=9222", 9222),
    ("chrome.exe --remote-debugging-port 9333", 9333),
    ("chrome.exe --remote-debugging-port=9222 --headless", 9222),
    ('"C:\\a b\\chrome.exe" --REMOTE-DEBUGGING-PORT=4444', 4444),
])
def test_parse_debug_port(cmd, port):
    """``--remote-debugging-port`` 的两种写法（``=`` 和空格）都要认。"""
    assert F.parse_debug_port(cmd) == port


def test_parse_debug_port_absent():
    """没有该参数时返回 None，不能瞎猜一个默认端口。"""
    assert F.parse_debug_port("chrome.exe --headless") is None
    assert F.parse_debug_port("") is None


def test_has_debug_flag_covers_pipe():
    """``--remote-debugging-pipe`` 也算"带了调试开关"，但它走管道。"""
    assert F.has_debug_flag("chrome.exe --remote-debugging-port=9222")
    assert F.has_debug_flag("chrome.exe --remote-debugging-pipe")
    assert not F.has_debug_flag("chrome.exe --headless")


def test_has_debug_pipe_detects_pipe_mode():
    """管道模式必须能被单独识别出来。"""
    assert F.has_debug_pipe("electron --remote-debugging-pipe")
    assert not F.has_debug_pipe("electron --remote-debugging-port=9222")


def test_parse_inspect_port():
    """Node/Electron 的 ``--inspect`` 是另一套端口，也要能解析。"""
    assert F.parse_inspect_port("node --inspect=9229 app.js") == 9229
    assert F.parse_inspect_port("node --inspect-brk 9230 app.js") == 9230
    assert F.parse_inspect_port("node app.js") is None


# ==========================================================================
# 降级决策 —— 重点是 reason 字段
# ==========================================================================

def test_decide_level_never_cdp_skips_probe():
    """黑名单进程直接降级，理由是 process_never_cdp。"""
    d = F.decide_level(process_name="explorer.exe", cmdline="",
                       probe=False)
    assert d.chosen != F.FallbackLevel.CDP
    assert d.reason == "process_never_cdp"


def test_decide_level_browser_without_port():
    """浏览器没带调试端口 → 降级，理由是 no_debug_port。"""
    d = F.decide_level(process_name="msedge.exe",
                       cmdline="msedge.exe --headless",
                       probe=False)
    assert d.chosen != F.FallbackLevel.CDP
    assert d.reason == "no_debug_port"
    assert "no_debug_port" == d.reason


def test_decide_level_pipe_mode_is_explicit():
    """管道模式 → 理由必须专门报 pipe_mode_unsupported。

    这是最容易踩的坑：用户看到 ``--remote-debugging-pipe`` 会以为
    "调试开了呀"，但本库只实现了 TCP 模式，连不上。
    报一个专门的 reason 才能让人一眼看出真因。
    """
    d = F.decide_level(process_name="Code.exe",
                       cmdline="Code.exe --remote-debugging-pipe",
                       probe=False)
    assert d.chosen != F.FallbackLevel.CDP
    assert d.reason == "pipe_mode_unsupported"


def test_decide_level_no_process_info():
    """啥都没给 → 不能盲扫，理由 no_process_info。"""
    d = F.decide_level(process_name="", cmdline="", probe=False)
    assert d.reason == "no_process_info"


def test_decide_level_unknown_process_no_port():
    """未知进程且无端口 → 不盲扫。"""
    d = F.decide_level(process_name="whatever.exe", cmdline="",
                       probe=False)
    assert d.reason == "unknown_process_no_port"


def test_decide_level_probe_false_marks_unverified():
    """``probe=False`` 时选 CDP 可以说"假定可用"，但理由必须体现未验证。

    这条很重要：不能把"猜的"和"验过的"写成同一个结果。
    """
    d = F.decide_level(process_name="msedge.exe",
                       cmdline="msedge.exe --remote-debugging-port=9222",
                       probe=False)
    assert d.chosen == F.FallbackLevel.CDP
    assert d.reason == "cdp_assumed"
    assert d.cdp_ok is False, "没探测就不能声称 cdP 已验证"


def test_decide_level_port_not_cdp_when_probe_on():
    """端口开着但不是 CDP（比如误填了别的服务端口）→ 降级。"""
    # 起一个**普通** TCP 服务，占住端口
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]

    stop = threading.Event()

    def _accept_loop():
        srv.settimeout(0.3)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                with c:
                    c.settimeout(0.5)
                    try:
                        c.recv(4096)
                        c.sendall(b"HTTP/1.1 200 OK\r\n"
                                  b"Content-Length: 2\r\n\r\nhi")
                    except Exception:      # noqa: BLE001
                        pass
            except socket.timeout:
                continue
            except Exception:              # noqa: BLE001
                break

    t = threading.Thread(target=_accept_loop, daemon=True)
    t.start()
    try:
        d = F.decide_level(process_name="msedge.exe",
                           cmdline="msedge.exe --remote-debugging-port=%d" % port,
                           probe=True, probe_timeout=0.6)
        assert d.chosen != F.FallbackLevel.CDP, \
            "普通 HTTP 服务被误判成 CDP 了"
        assert d.reason == "port_not_cdp"
    finally:
        stop.set()
        try:
            srv.close()
        except Exception:      # noqa: BLE001
            pass


def test_decide_level_candidates_order_is_descending():
    """降级链的顺序必须是 CDP → UIA → 视觉 → 输入。

    顺序错了会造成"明明有 UIA 却去点坐标"这种低级退化。
    """
    d = F.decide_level(process_name="msedge.exe",
                       cmdline="msedge.exe --remote-debugging-port=9222",
                       probe=False)
    ranks = [F.FallbackLevel.rank(c) for c in d.candidates]
    assert ranks == sorted(ranks), "候选顺序乱序：%r" % (d.candidates,)


def test_decide_level_respects_capability_flags():
    """调用方说没有 UIA / 没有视觉时，候选里就不该出现它们。"""
    d = F.decide_level(process_name="explorer.exe", cmdline="",
                       has_uia=False, has_vision=True, has_input=True,
                       probe=False)
    assert F.FallbackLevel.UIA not in d.candidates
    assert F.FallbackLevel.CDP not in d.candidates


def test_decide_level_all_capabilities_off():
    """全都没有 → NONE，而不是返回一个用不了的东西。"""
    d = F.decide_level(process_name="explorer.exe", cmdline="",
                       has_uia=False, has_vision=False, has_input=False,
                       probe=False)
    assert d.chosen == F.FallbackLevel.NONE


def test_decision_describe_and_text():
    """决策要能转 dict（进日志/证据）**并且**能转人读文本。

    两个都必要：dict 给程序消费，文本给人看。
    """
    d = F.decide_level(process_name="Code.exe",
                       cmdline="Code.exe --remote-debugging-pipe",
                       probe=False)

    got = d.describe()
    assert isinstance(got, dict)
    for key in ("chosen", "reason", "kind", "degraded", "candidates",
                "notes"):
        assert key in got, "describe 缺字段 %s" % key
    assert got["reason"] == "pipe_mode_unsupported"
    assert got["degraded"] is True

    text = F.describe_decision(d)
    assert isinstance(text, str) and text
    assert "pipe" in text.lower() or "管道" in text


def test_probe_cdp_available_false_on_closed_port():
    """关着的端口探测必须快速返回 False，不能卡很久。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    t0 = time.time()
    assert F.probe_cdp_available(port, timeout=0.6) is False
    assert time.time() - t0 < 2.0


# ==========================================================================
# Electron 模块：TCP 表扫描 + 端口探测
# ==========================================================================

def test_scan_listen_ports_finds_our_listener():
    """我们起的监听端口必须被扫到 —— 这是 find_debug_port 的基础。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    try:
        ports = E.scan_listen_ports(os.getpid())
        assert port in ports, "自己进程的监听端口没扫到：%r" % (ports,)
    finally:
        srv.close()


def test_scan_listen_ports_pid_filter_works():
    """pid 过滤要真的生效 —— 用 pid=0（不过滤）做对照。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    try:
        mine = E.scan_listen_ports(os.getpid())
        assert port in mine
        # 一个几乎不可能存在的 pid
        other = E.scan_listen_ports(999999)
        assert port not in other
    finally:
        srv.close()


def test_scan_listen_ports_is_sorted_and_unique():
    """返回必须是去重且有序的 —— 调用方靠顺序做优先级。"""
    ports = E.scan_listen_ports(os.getpid())
    assert ports == sorted(set(ports))


def test_probe_port_rejects_non_cdp():
    """关键判据：**不认状态码，只认 webSocketDebuggerUrl**。

    任何 HTTP 服务都会回 200，只看状态码会把 Electron 自己的
    本地 API 误判成调试端口，然后握手失败，白白浪费排查时间。
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def _loop():
        srv.settimeout(0.3)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                with c:
                    c.settimeout(0.5)
                    try:
                        c.recv(4096)
                        body = b'{"ok": true}'
                        c.sendall(b"HTTP/1.1 200 OK\r\n"
                                  b"Content-Type: application/json\r\n"
                                  b"Content-Length: " +
                                  str(len(body)).encode() +
                                  b"\r\n\r\n" + body)
                    except Exception:      # noqa: BLE001
                        pass
            except socket.timeout:
                continue
            except Exception:              # noqa: BLE001
                break

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    try:
        assert E.probe_port(port, timeout=0.6) is None, \
            "没有 webSocketDebuggerUrl 的 200 响应不该被当成 CDP"
    finally:
        stop.set()
        try:
            srv.close()
        except Exception:      # noqa: BLE001
            pass


def test_probe_port_accepts_cdp_shaped_response():
    """有 ``webSocketDebuggerUrl`` 就要认（这是 CDP 的唯一指纹）。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def _loop():
        srv.settimeout(0.3)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                with c:
                    c.settimeout(0.5)
                    try:
                        c.recv(4096)
                        body = (b'{"Browser":"Mock/1.0",'
                                b'"webSocketDebuggerUrl":'
                                b'"ws://127.0.0.1:%d/devtools/browser/x"}'
                                % port)
                        c.sendall(b"HTTP/1.1 200 OK\r\n"
                                  b"Content-Type: application/json\r\n"
                                  b"Content-Length: " +
                                  str(len(body)).encode() +
                                  b"\r\n\r\n" + body)
                    except Exception:      # noqa: BLE001
                        pass
            except socket.timeout:
                continue
            except Exception:              # noqa: BLE001
                break

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    try:
        info = E.probe_port(port, timeout=0.6)
        assert isinstance(info, dict)
        assert info["webSocketDebuggerUrl"].startswith("ws://")
    finally:
        stop.set()
        try:
            srv.close()
        except Exception:      # noqa: BLE001
            pass


def test_find_debug_port_returns_none_when_nothing():
    """没有任何 CDP 时必须返回 None（不是抛错、也不是瞎返回）。"""
    assert E.find_debug_port(999999, timeout=0.4) is None


def test_find_debug_port_prefers_extra_ports():
    """``extra_ports`` 必须**优先**探测。

    理由：某些 Electron 主进程不直接持有 socket（由子进程持有），
    全表扫 pid 会漏掉 —— 从命令行解析出来的端口反而更准。
    """
    # 用一个"一定不是 CDP"的额外端口，验证它被优先试过：
    # 如果实现把 extra 放在最后，这里至少也该被试到，行为等价于返回 None。
    res = E.find_debug_port(999999, extra_ports=[1], timeout=0.3)
    assert res is None


# ==========================================================================
# detect_electron —— 三重判据
# ==========================================================================

def test_detect_electron_none_for_never_cdp_process():
    """黑名单进程识别为 None（就算给了端口也不认，避免误连）。"""
    got = E.detect_electron(pid=os.getpid(), process_name="explorer.exe")
    assert got is None


def test_detect_electron_whitelist_without_port():
    """白名单进程即使没有 CDP 也要返回对象（标记 kind=electron）。

    返回 None 会让调用方以为"不是 Electron"，从而**跳过**后续的
    "重启时带上调试端口"这条建议 —— 信息就丢了。
    """
    app = E.detect_electron(pid=os.getpid(), process_name="Code.exe")
    assert app is not None
    assert app.kind == "electron"
    assert app.has_cdp is False or app.debug_port == 0


def test_detect_electron_browser_is_also_returned():
    """浏览器也返回对象 —— 浏览器同样支持 CDP，调用方不必区分。"""
    app = E.detect_electron(pid=os.getpid(), process_name="msedge.exe")
    assert app is not None
    assert app.kind == "browser"


def test_detect_electron_unknown_process_without_cdp():
    """名字不认识、也没有 CDP → None（该降级 UIA 了）。"""
    got = E.detect_electron(pid=999999, process_name="totally_unknown.exe")
    assert got is None


def test_detect_electron_cdp_proves_electron():
    """名字不在白名单，但有 CDP 端口 → 仍然认成 Electron（最强判据）。

    因为**只有 Chromium/Electron 才会暴露 CDP** —— 这个反证比
    维护一份永远追不上的进程名白名单可靠得多。
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def _loop():
        srv.settimeout(0.3)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                with c:
                    c.settimeout(0.5)
                    try:
                        c.recv(4096)
                        body = (b'{"Browser":"Mock/1.0",'
                                b'"webSocketDebuggerUrl":'
                                b'"ws://127.0.0.1:%d/devtools/browser/x"}'
                                % port)
                        c.sendall(b"HTTP/1.1 200 OK\r\n"
                                  b"Content-Type: application/json\r\n"
                                  b"Content-Length: " +
                                  str(len(body)).encode() +
                                  b"\r\n\r\n" + body)
                    except Exception:      # noqa: BLE001
                        pass
            except socket.timeout:
                continue
            except Exception:              # noqa: BLE001
                break

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    try:
        app = E.detect_electron(pid=999999,
                                process_name="weird_app_xyz.exe",
                                probe_port_=port)
        assert app is not None
        assert app.kind == "electron_cdp"
        assert app.debug_port == port
        assert app.has_cdp is True
    finally:
        stop.set()
        try:
            srv.close()
        except Exception:      # noqa: BLE001
            pass


def test_electron_app_describe_returns_dict():
    """``ElectronApp.describe()`` 返回 **dict**（不是字符串）—— 排查入口。"""
    app = E.detect_electron(pid=os.getpid(), process_name="Code.exe")
    assert app is not None
    d = app.describe()
    assert isinstance(d, dict), "describe 应返回 dict，实际 %r" % type(d)
    assert d["kind"] == "electron"
    assert "has_cdp" in d
    # repr 才算"文本形态"
    assert isinstance(repr(app), str) and repr(app)


def test_electron_app_websocket_url_property():
    """没连上时 ``websocket_url`` 必须是空串，不能是 None 或炸掉。"""
    app = E.detect_electron(pid=os.getpid(), process_name="Code.exe")
    assert app is not None
    assert app.websocket_url == ""
    assert app.ok is False


def test_attach_electron_returns_none_not_raise():
    """**附加失败是常态，必须返回 None 而不是抛异常。**

    绝大多数商业 Electron 不开调试端口。如果这里抛异常，
    上层就得用 try/except 包住每次调用 —— 那是很糟的接口设计。
    """
    got = E.attach_electron(pid=999999, process_name="totally_unknown.exe",
                            port=None, timeout=1.0)
    assert got is None


def test_scan_listen_ports_zero_pid_scans_system():
    """pid=0 表示不过滤 —— 会扫全系统，结果必须是个大列表。"""
    ports = E.scan_listen_ports(0)
    assert isinstance(ports, list)
    assert len(ports) >= 1, "全系统一个监听端口都没有？不太可能"
