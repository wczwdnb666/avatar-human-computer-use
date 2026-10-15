# -*- coding: utf-8 -*-
"""网络请求监听：``Network.enable`` + 事件捕获。

用途：**打榜取证**。要证明"我确实提交了这一票"，
光看 UI 变化不够 —— 服务端返回的成功响才是硬证据。
本模块把整个请求/响应生命周期捕获下来，供验证层引用。

## 事件时序（必须按序配对）

::

    Network.requestWillBeSent      → requestId 诞生，带 url/method/headers/postData
    Network.requestWillBeSentExtraInfo  → 实际发出的头（含 cookie，可能晚到）
    Network.responseReceived       → 响应头到了，带 status/mimeType/headers
    Network.dataReceived           → 响应体分片（可能多次，也可能没有）
    Network.loadingFinished        → 请求结束，带 encodedDataLength
    Network.loadingFailed          → 失败，带 errorText

配对靠 ``requestId``。**关键坑**：

1. ``responseReceived`` 只表示**响应头**到了，body 还没。
   要拿 body 必须再调 ``Network.getResponseBody {requestId}``，
   而且**必须在 ``loadingFinished`` 之后**调 —— 早了会报
   "No resource with given identifier found"。
2. **重定向**会产生新的 requestId（``redirectResponse`` 字段指示
   "这是重定向来的"）。如果只按 url 匹配会漏掉链路的前半段。
3. ``dataReceived`` 的 body 是 **base64**（CDP 的编码约定），
   有些响应拿不到 body（Service Worker 拦截、跨进程、304 缓存命中）——
   ``getResponseBody`` 会抛错，那时应该用 ``responseReceived`` 里的头
   做判据，而不是当作失败。
4. ``Network.enable`` 之前发出的请求**捕获不到**。所以要在导航前开。

## 缓冲上限

长会话里事件会积累。默认只保留最近 ``max_records`` 条，
超出丢最老的 —— 打榜场景够用，且不会因为开着页面几小时把内存吃光。
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Callable, Dict, List, Optional

from ..errors import AvatarError
from .session import CdpError, CdpEvent, CdpTimeout

__all__ = [
    "NetworkError",
    "RequestRecord",
    "NetworkMonitor",
    "enable_network",
    "disable_network",
    "get_response_body",
    "capture_requests",
    "find_request",
    "wait_for_request",
    "wait_for_response",
    "RequestPattern",
]


class NetworkError(AvatarError):
    """网络监听相关错误。"""

    exit_code = 17


# --------------------------------------------------------------------------
# 请求记录
# --------------------------------------------------------------------------
class RequestRecord:
    """一次请求的完整生命周期。

    字段在事件到达过程中**逐步填充** —— 拿到 ``requestWillBeSent`` 时
    只有 url/method，``responseReceived`` 后才有 status，
    ``loadingFinished`` 后 ``finished`` 才为 True。
    """

    __slots__ = ("request_id", "url", "method", "resource_type",
                 "request_headers", "post_data",
                 "status", "status_text", "response_headers",
                 "mime_type", "remote_ip",
                 "encoded_data_length", "from_cache", "from_service_worker",
                 "failed", "error_text", "redirected_from", "redirect_url",
                 "started_at", "finished_at", "body_available", "_body",
                 "frame_id", "loader_id", "document_url")

    def __init__(self, request_id: str, url: str = "", method: str = "",
                 resource_type: str = "") -> None:
        self.request_id = request_id
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.request_headers: dict = {}
        self.post_data: Optional[str] = None

        self.status = 0
        self.status_text = ""
        self.response_headers: dict = {}
        self.mime_type = ""
        self.remote_ip = ""

        self.encoded_data_length = 0
        self.from_cache = False
        self.from_service_worker = False

        self.failed = False
        self.error_text = ""

        self.redirected_from = ""
        self.redirect_url = ""

        self.started_at = time.time()
        self.finished_at = 0.0
        self.body_available: Optional[bool] = None
        self._body: Optional[bytes] = None

        self.frame_id = ""
        self.loader_id = ""
        self.document_url = ""

    # -- 状态 ---------------------------------------------------------------
    @property
    def finished(self) -> bool:
        return self.finished_at > 0.0 or self.failed

    @property
    def ok(self) -> bool:
        """成功判据：有状态码、2xx/3xx、没失败。"""
        if self.failed:
            return False
        if not self.status:
            return False
        return 200 <= self.status < 400

    @property
    def duration(self) -> float:
        if not self.finished_at:
            return 0.0
        return self.finished_at - self.started_at

    @property
    def body(self) -> Optional[bytes]:
        return self._body

    def set_body(self, data: Optional[bytes]) -> None:
        self._body = data
        self.body_available = data is not None

    @property
    def body_text(self) -> str:
        if self._body is None:
            return ""
        try:
            return self._body.decode("utf-8", errors="replace")
        except Exception:
            return ""

    def body_json(self) -> Any:
        """把 body 当 JSON 解析。失败返回 None。"""
        t = self.body_text
        if not t:
            return None
        try:
            return json.loads(t)
        except ValueError:
            return None

    def match(self, pattern) -> bool:
        """URL 是否匹配。``pattern`` 可以是字符串/正则/可调用。"""
        if callable(pattern):
            return bool(pattern(self.url))
        if hasattr(pattern, "search"):
            return bool(pattern.search(self.url))
        return str(pattern) in self.url

    def describe(self, *, with_body: bool = False,
                 max_body: int = 400) -> dict:
        d = {
            "request_id": self.request_id,
            "url": self.url,
            "method": self.method,
            "type": self.resource_type,
            "status": self.status,
            "status_text": self.status_text,
            "mime_type": self.mime_type,
            "ok": self.ok,
            "failed": self.failed,
            "error_text": self.error_text,
            "finished": self.finished,
            "duration": round(self.duration, 3),
            "encoded_data_length": self.encoded_data_length,
            "from_cache": self.from_cache,
            "from_service_worker": self.from_service_worker,
            "redirect_url": self.redirect_url,
        }
        if self.post_data:
            d["post_data"] = self.post_data[:max_body]
        if with_body:
            d["body"] = self.body_text[:max_body]
        return d

    def __repr__(self) -> str:
        return "<Req %s %s %s %s>" % (
            self.method or "?",
            self.status or "-",
            self.url[:56],
            "(failed)" if self.failed else "")


#: URL 匹配器包装，便于类型标注与复用
class RequestPattern:
    """请求匹配条件。

    ``url`` 可以是字符串（子串）、编译好的正则、或可调用。
    ``method`` / ``status`` / ``resource_type`` 做精确或集合匹配。
    """

    __slots__ = ("url", "method", "status", "resource_type", "body_contains")

    def __init__(self, url=None, *, method=None, status=None,
                 resource_type=None, body_contains=None) -> None:
        self.url = url
        self.method = method
        self.status = status
        self.resource_type = resource_type
        self.body_contains = body_contains

    def match(self, rec: RequestRecord) -> bool:
        if self.url is not None and not rec.match(self.url):
            return False
        if self.method is not None:
            want = self.method.upper()
            if isinstance(self.method, (list, tuple, set)):
                if rec.method.upper() not in {m.upper() for m in self.method}:
                    return False
            elif rec.method.upper() != want:
                return False
        if self.status is not None:
            if isinstance(self.status, (list, tuple, set)):
                if rec.status not in set(self.status):
                    return False
            elif rec.status != int(self.status):
                return False
        if self.resource_type is not None:
            if rec.resource_type != self.resource_type:
                return False
        if self.body_contains is not None:
            if self.body_contains not in rec.body_text:
                return False
        return True

    def __repr__(self) -> str:
        return "<RequestPattern url=%r method=%r status=%r>" % (
            self.url, self.method, self.status)


# --------------------------------------------------------------------------
# 监听器
# --------------------------------------------------------------------------
class NetworkMonitor:
    """挂在 ``CdpSession`` 上的网络监听器。

    用法::

        mon = NetworkMonitor(sess, session_id=sid)
        mon.start()
        sess.call("Page.navigate", {"url": ...}, session_id=sid)
        reqs = mon.wait_for(url_contains="api/submit", timeout=10)
        mon.stop()

    监听器通过 ``subscribe`` 注册到会话上，**不阻塞** —— 事件在
    会话的读循环里入队，本对象只是把它们组装成 RequestRecord。
    """

    #: 默认最多保留多少条记录
    DEFAULT_MAX_RECORDS = 2000

    def __init__(self, sess, *, session_id: str = "",
                 max_records: int = DEFAULT_MAX_RECORDS,
                 capture_types: Optional[List[str]] = None) -> None:
        self.sess = sess
        self.session_id = session_id
        self.max_records = max_records
        #: 只记录这些资源类型；None 表示全记
        self.capture_types = set(capture_types) if capture_types else None

        self._records: Dict[str, RequestRecord] = {}
        self._order: List[str] = []
        self._active = False
        #: 丢弃的最老记录数（诚实报告，别让调用方以为数据全在）
        self.dropped = 0

        self._handlers = []

    # -- 生命周期 -----------------------------------------------------------
    def start(self, *, extra_domains: Optional[List[str]] = None) -> None:
        """注册订阅并 ``Network.enable``。"""
        if self._active:
            return

        subs = {
            "Network.requestWillBeSent": self._on_request,
            "Network.responseReceived": self._on_response,
            "Network.loadingFinished": self._on_finished,
            "Network.loadingFailed": self._on_failed,
        }
        for method, cb in subs.items():
            self.sess.subscribe(method, cb)
            self._handlers.append((method, cb))

        try:
            self.sess.call("Network.enable", {
                "maxTotalBufferSize": 100 * 1024 * 1024,
                "maxResourceBufferSize": 10 * 1024 * 1024,
            }, session_id=self.session_id or None, timeout=10.0)
        except Exception:
            # enable 失败时退订，避免留一堆无效回调
            self._unsubscribe_all()
            raise

        for d in (extra_domains or []):
            try:
                self.sess.call("%s.enable" % d, session_id=self.session_id or None,
                               timeout=5.0)
            except Exception:
                pass

        self._active = True

    def stop(self) -> None:
        """退订。enable 的状态不用关（不影响功能，只是不再收事件）。"""
        self._unsubscribe_all()
        self._active = False

    def _unsubscribe_all(self) -> None:
        for method, cb in self._handlers:
            try:
                self.sess.unsubscribe(method, cb)
            except Exception:
                pass
        self._handlers = []

    def __enter__(self) -> "NetworkMonitor":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- 事件处理 -----------------------------------------------------------
    def _accept_type(self, rtype: str) -> bool:
        if self.capture_types is None:
            return True
        return rtype in self.capture_types

    def _on_request(self, ev: CdpEvent) -> None:
        p = ev.params
        rid = p.get("requestId") or ""
        if not rid:
            return
        req = p.get("request") or {}
        rtype = p.get("type") or ""

        if not self._accept_type(rtype):
            return

        # 重定向：同一个 requestId 复用，但记录下链路
        rec = self._records.get(rid)
        redirect_resp = p.get("redirectResponse")
        if rec is None:
            rec = RequestRecord(rid, req.get("url", ""),
                                req.get("method", ""), rtype)
            self._records[rid] = rec
            self._order.append(rid)
            self._trim()
        else:
            rec.url = req.get("url", rec.url)
            rec.method = req.get("method", rec.method)

        rec.request_headers = _flatten_headers(req.get("headers") or {})
        pd = req.get("postData")
        if pd is not None:
            rec.post_data = pd
        rec.frame_id = p.get("frameId") or rec.frame_id
        rec.loader_id = p.get("loaderId") or rec.loader_id
        rec.document_url = p.get("documentURL") or rec.document_url

        if redirect_resp:
            rec.redirected_from = (redirect_resp.get("url") or "")
            rec.redirect_url = req.get("url", "")

    def _on_response(self, ev: CdpEvent) -> None:
        p = ev.params
        rid = p.get("requestId") or ""
        rec = self._records.get(rid)
        if rec is None:
            return
        resp = p.get("response") or {}
        rec.status = int(resp.get("status", 0) or 0)
        rec.status_text = resp.get("statusText", "") or ""
        rec.response_headers = _flatten_headers(resp.get("headers") or {})
        rec.mime_type = resp.get("mimeType", "") or ""
        rec.remote_ip = resp.get("remoteIPAddress", "") or ""
        rec.from_cache = bool(resp.get("fromDiskCache")
                              or resp.get("fromPrefetchCache"))
        rec.from_service_worker = bool(resp.get("fromServiceWorker"))
        # responseReceived 的 type 有时比 requestWillBeSent 更准
        rtype = p.get("type")
        if rtype:
            rec.resource_type = rtype

    def _on_finished(self, ev: CdpEvent) -> None:
        p = ev.params
        rid = p.get("requestId") or ""
        rec = self._records.get(rid)
        if rec is None:
            return
        rec.finished_at = time.time()
        rec.encoded_data_length = int(p.get("encodedDataLength", 0) or 0)

    def _on_failed(self, ev: CdpEvent) -> None:
        p = ev.params
        rid = p.get("requestId") or ""
        rec = self._records.get(rid)
        if rec is None:
            return
        rec.failed = True
        rec.error_text = p.get("errorText", "") or "unknown"
        rec.finished_at = time.time()

    def _trim(self) -> None:
        while len(self._order) > self.max_records:
            old = self._order.pop(0)
            self._records.pop(old, None)
            self.dropped += 1

    # -- 查询 ---------------------------------------------------------------
    @property
    def records(self) -> List[RequestRecord]:
        """按时间顺序返回全部记录（副本，防止调用方改内部列表）。"""
        return [self._records[r] for r in self._order if r in self._records]

    def find(self, pattern=None, **kw) -> List[RequestRecord]:
        """找所有匹配的请求。"""
        if pattern is None:
            pattern = RequestPattern(**kw)
        elif not isinstance(pattern, RequestPattern):
            pattern = RequestPattern(pattern)
        return [r for r in self.records if pattern.match(r)]

    def first(self, pattern=None, **kw) -> Optional[RequestRecord]:
        """找第一个匹配，没有返回 None。"""
        hits = self.find(pattern, **kw)
        return hits[0] if hits else None

    def last(self, pattern=None, **kw) -> Optional[RequestRecord]:
        """找最后一个匹配。"""
        hits = self.find(pattern, **kw)
        return hits[-1] if hits else None

    def wait_for(self, pattern=None, *, timeout: float = 15.0,
                 require_finished: bool = True,
                 poll: float = 0.1, **kw) -> Optional[RequestRecord]:
        """等一个匹配的请求出现（可选要求已完成）。

        超时返回 None（不抛）—— 因为"这个请求没发出去"本身是
        一个**有信息量的观测结果**，验证层需要拿它做判据。
        """
        if pattern is None:
            pattern = RequestPattern(**kw)
        elif not isinstance(pattern, RequestPattern):
            pattern = RequestPattern(pattern)

        deadline = time.monotonic() + timeout
        while True:
            for rec in self.records:
                if not pattern.match(rec):
                    continue
                if require_finished and not rec.finished:
                    continue
                return rec
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None
            # 主动拉一条网络事件（会话的事件队列可能还没被 pump）
            try:
                self._pump(remain)
            except Exception:
                pass
            time.sleep(min(poll, max(0.01, remain)))

    def _pump(self, timeout: float) -> None:
        """让会话读一条消息（事件会被回调消化）。"""
        # 用 wait_event 反着等一个**必然不来的**事件是不行的，
        # 所以借 drain_events：会触发一次读循环的副作用。
        # 更干净的做法是显式调私有 pump —— 但那是 session 的内部约定。
        try:
            self.sess._pump_once(min(0.05, timeout))
        except Exception:
            raise

    def clear(self) -> None:
        """清空记录（开始新一轮取证前用）。"""
        self._records.clear()
        self._order.clear()
        self.dropped = 0

    # -- 统计 ---------------------------------------------------------------
    def summary(self) -> dict:
        recs = self.records
        by_status: Dict[str, int] = {}
        by_type: Dict[str, int] = {}
        for r in recs:
            key = str(r.status) if r.status else ("failed" if r.failed else "pending")
            by_status[key] = by_status.get(key, 0) + 1
            by_type[r.resource_type or "?"] = by_type.get(
                r.resource_type or "?", 0) + 1
        return {
            "total": len(recs),
            "finished": sum(1 for r in recs if r.finished),
            "failed": sum(1 for r in recs if r.failed),
            "ok": sum(1 for r in recs if r.ok),
            "by_status": by_status,
            "by_type": by_type,
            "dropped": self.dropped,
            "active": self._active,
        }

    def describe(self) -> dict:
        return self.summary()


# --------------------------------------------------------------------------
# 便捷函数
# --------------------------------------------------------------------------
def _flatten_headers(headers) -> dict:
    """CDP 头是 dict（可能值不是 str，比如数字）。统一成 str→str。"""
    if not isinstance(headers, dict):
        return {}
    return {str(k): (v if isinstance(v, str) else str(v))
            for k, v in headers.items()}


def enable_network(sess, *, session_id: str = "",
                   capture_types: Optional[List[str]] = None,
                   max_records: int = NetworkMonitor.DEFAULT_MAX_RECORDS
                   ) -> NetworkMonitor:
    """开一个监听器并 start。"""
    mon = NetworkMonitor(sess, session_id=session_id,
                         capture_types=capture_types,
                         max_records=max_records)
    mon.start()
    return mon


def disable_network(mon: Optional[NetworkMonitor]) -> None:
    """停监听器（``None`` 安全）。"""
    if mon is not None:
        mon.stop()


def get_response_body(sess, request_id: str, *, session_id: str = "",
                      timeout: float = 10.0) -> Optional[bytes]:
    """取响应体。

    ⚠️ 必须在 ``loadingFinished`` 之后调 —— 早了会抛
    "No resource with given identifier found"。
    拿不到（304 / Service Worker / body 未保留）返回 None，不抛。
    """
    try:
        res = sess.call("Network.getResponseBody", {"requestId": request_id},
                        session_id=session_id or None, timeout=timeout)
    except Exception:
        return None
    body = res.get("body")
    if body is None:
        return None
    if res.get("base64Encoded"):
        try:
            return base64.b64decode(body)
        except Exception:
            return None
    return body.encode("utf-8", errors="replace")


def find_request(mon: NetworkMonitor, pattern=None, **kw) -> Optional[RequestRecord]:
    """在监听器里找第一个匹配请求（``mon`` 为 None 时返回 None）。"""
    if mon is None:
        return None
    return mon.first(pattern, **kw)


def wait_for_request(sess, url, *, session_id: str = "",
                     timeout: float = 15.0,
                     capture_types: Optional[List[str]] = None,
                     require_finished: bool = True,
                     with_body: bool = False) -> Optional[RequestRecord]:
    """一次性便利函数：临时开监听、等请求、关监听。

    适合"发一个动作，看它有没有触发某个请求"的场景。
    ⚠️ 每次调用都会 enable/disable —— 高频场景请自己持有监听器。
    """
    mon = NetworkMonitor(sess, session_id=session_id,
                         capture_types=capture_types)
    try:
        mon.start()
        rec = mon.wait_for(url, timeout=timeout,
                           require_finished=require_finished)
        if rec is not None and with_body:
            rec.set_body(get_response_body(sess, rec.request_id,
                                           session_id=session_id))
        return rec
    finally:
        mon.stop()


def wait_for_response(sess, url, *, session_id: str = "",
                      timeout: float = 15.0,
                      status=None, with_body: bool = True
                      ) -> Optional[RequestRecord]:
    """等一个匹配 URL 的**已完成响应**，可选带 body。"""
    mon = NetworkMonitor(sess, session_id=session_id)
    try:
        mon.start()
        pattern = RequestPattern(url, status=status)
        rec = mon.wait_for(pattern, timeout=timeout, require_finished=True)
        if rec is not None and with_body:
            rec.set_body(get_response_body(sess, rec.request_id,
                                           session_id=session_id))
        return rec
    finally:
        mon.stop()


def capture_requests(sess, action: Callable[[], Any], *, url=None,
                     session_id: str = "", timeout: float = 20.0,
                     capture_types: Optional[List[str]] = None,
                     with_body: bool = True) -> List[RequestRecord]:
    """跑一个动作并捕获它触发的所有请求。

    ``action`` 是零参可调用（比如 lambda 包住的点击）。

    实现要点：**先 start 监听再执行动作** —— 反过来的话
    动作触发的请求会在 ``Network.enable`` 之前发出，捕获不到。
    """
    mon = NetworkMonitor(sess, session_id=session_id,
                         capture_types=capture_types)
    mon.start()
    try:
        try:
            action()
        except Exception:
            # 动作失败也要把已捕获的请求返回 —— 失败的请求也是证据
            pass
        # 给异步请求一点时间完成
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(r.finished for r in mon.records):
                break
            try:
                mon._pump(0.1)
            except Exception:
                break
            time.sleep(0.05)

        out = mon.find(RequestPattern(url)) if url is not None else mon.records
        if with_body:
            for r in out:
                if r.finished and not r.failed:
                    r.set_body(get_response_body(sess, r.request_id,
                                                 session_id=session_id))
        return out
    finally:
        mon.stop()


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    """网络监听自检（**不联网**）。

    用假 session 喂 CDP 网络事件，验证记录组装、配对、
    重定向跟踪、缓冲上限、匹配器语义、summary 统计。
    """
    checks = []
    failures = []

    def _add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # -- 假 session：只实现 subscribe/call -------------------------------
    class _FakeSess:
        def __init__(self):
            self.subs: Dict[str, list] = {}
            self.calls: List[tuple] = []
            self.fail_enable = False

        def subscribe(self, method, cb):
            self.subs.setdefault(method, []).append(cb)

        def unsubscribe(self, method, cb=None):
            if cb is None:
                self.subs.pop(method, None)
            else:
                lst = self.subs.get(method) or []
                if cb in lst:
                    lst.remove(cb)

        def call(self, method, params=None, **kw):
            if method == "Network.enable" and self.fail_enable:
                raise CdpError("enable 失败", method=method)
            self.calls.append((method, params))
            return {}

        def _pump_once(self, timeout):
            return None

        def emit(self, method, params):
            ev = CdpEvent(method, params)
            for cb in list(self.subs.get(method, [])):
                cb(ev)

    fs = _FakeSess()
    mon = NetworkMonitor(fs, max_records=10)
    mon.start()

    _add("start 注册了四个事件", len(fs.subs) == 4,
         "%d 个" % len(fs.subs))
    _add("start 调用了 Network.enable",
         any(c[0] == "Network.enable" for c in fs.calls), "enable")

    # -- 完整生命周期 -------------------------------------------------------
    fs.emit("Network.requestWillBeSent", {
        "requestId": "R1",
        "request": {"url": "https://api.example.com/submit",
                    "method": "POST",
                    "headers": {"Content-Type": "application/json"},
                    "postData": '{"vote":1}'},
        "type": "XHR",
        "frameId": "F1",
        "loaderId": "L1",
    })
    recs = mon.records
    _add("requestWillBeSent 建记录",
         len(recs) == 1 and recs[0].url.endswith("/submit"), "%d 条" % len(recs))
    r0 = recs[0]
    _add("记录 method/type/postData",
         r0.method == "POST" and r0.resource_type == "XHR"
         and r0.post_data == '{"vote":1}', "%s %s" % (r0.method, r0.resource_type))
    _add("未收到响应时 ok=False", r0.ok is False, "ok=%s" % r0.ok)
    _add("未完成时 finished=False", r0.finished is False, "finished")

    fs.emit("Network.responseReceived", {
        "requestId": "R1",
        "response": {"status": 200, "statusText": "OK",
                     "mimeType": "application/json",
                     "headers": {"Content-Type": "application/json"},
                     "remoteIPAddress": "1.2.3.4"},
        "type": "XHR",
    })
    _add("responseReceived 填状态", r0.status == 200, str(r0.status))
    _add("有 2xx 状态后 ok=True", r0.ok is True, "ok")
    _add("mimeType 记录", r0.mime_type == "application/json", r0.mime_type)
    _add("remoteIP 记录", r0.remote_ip == "1.2.3.4", r0.remote_ip)

    fs.emit("Network.loadingFinished", {
        "requestId": "R1", "encodedDataLength": 1234,
    })
    _add("loadingFinished 后 finished=True", r0.finished is True, "finished")
    _add("encodedDataLength 记录",
         r0.encoded_data_length == 1234, str(r0.encoded_data_length))
    _add("duration 为正", r0.duration >= 0, "%.4f" % r0.duration)

    # -- 失败请求 -----------------------------------------------------------
    fs.emit("Network.requestWillBeSent", {
        "requestId": "R2",
        "request": {"url": "https://api.example.com/blocked", "method": "GET"},
        "type": "XHR",
    })
    fs.emit("Network.loadingFailed", {
        "requestId": "R2", "errorText": "net::ERR_BLOCKED_BY_CLIENT",
    })
    r2 = [r for r in mon.records if r.request_id == "R2"][0]
    _add("loadingFailed 标 failed", r2.failed is True, "failed")
    _add("失败请求 ok=False", r2.ok is False, "ok")
    _add("errorText 记录",
         r2.error_text == "net::ERR_BLOCKED_BY_CLIENT", r2.error_text)
    _add("失败后 finished=True", r2.finished is True, "finished")

    # -- 重定向 -------------------------------------------------------------
    fs.emit("Network.requestWillBeSent", {
        "requestId": "R3",
        "request": {"url": "https://a.com/old", "method": "GET"},
        "type": "Document",
        "redirectResponse": {"url": "https://a.com/start", "status": 302},
    })
    r3 = [r for r in mon.records if r.request_id == "R3"][0]
    _add("重定向记录 redirected_from",
         r3.redirected_from == "https://a.com/start", r3.redirected_from)

    # -- 未知 requestId 的事件被忽略（不炸）--------------------------------
    try:
        fs.emit("Network.responseReceived",
                {"requestId": "GHOST", "response": {"status": 200}})
        fs.emit("Network.loadingFinished", {"requestId": "GHOST"})
        fs.emit("Network.loadingFailed", {"requestId": "GHOST"})
        _add("未知 requestId 被忽略", True, "不抛")
    except Exception as exc:      # noqa: BLE001
        _add("未知 requestId 被忽略", False, "%s" % type(exc).__name__)

    # -- 缓冲上限 -----------------------------------------------------------
    fs2 = _FakeSess()
    mon2 = NetworkMonitor(fs2, max_records=5)
    mon2.start()
    for i in range(20):
        fs2.emit("Network.requestWillBeSent", {
            "requestId": "X%d" % i,
            "request": {"url": "https://x/%d" % i, "method": "GET"},
            "type": "XHR",
        })
    _add("超出上限被裁剪", len(mon2.records) == 5,
         "%d 条（上限 5）" % len(mon2.records))
    _add("裁剪数如实记录", mon2.dropped == 15, "dropped=%d" % mon2.dropped)
    _add("保留的是最新的",
         mon2.records[-1].url.endswith("/19"), mon2.records[-1].url)

    # -- 匹配器 -------------------------------------------------------------
    fs3 = _FakeSess()
    mon3 = NetworkMonitor(fs3)
    mon3.start()
    fs3.emit("Network.requestWillBeSent", {
        "requestId": "A", "type": "XHR",
        "request": {"url": "https://s/api/vote?x=1", "method": "POST"}})
    fs3.emit("Network.responseReceived", {
        "requestId": "A", "response": {"status": 201}, "type": "XHR"})
    fs3.emit("Network.loadingFinished", {"requestId": "A"})
    fs3.emit("Network.requestWillBeSent", {
        "requestId": "B", "type": "Document",
        "request": {"url": "https://s/page", "method": "GET"}})
    fs3.emit("Network.responseReceived", {
        "requestId": "B", "response": {"status": 404}, "type": "Document"})
    fs3.emit("Network.loadingFinished", {"requestId": "B"})

    _add("按 URL 子串匹配",
         len(mon3.find(RequestPattern("api/vote"))) == 1,
         "%d 条" % len(mon3.find(RequestPattern("api/vote"))))
    _add("按 method 匹配",
         len(mon3.find(RequestPattern("https://s", method="POST"))) == 1,
         "POST")
    _add("按 status 匹配",
         len(mon3.find(RequestPattern("https://s", status=201))) == 1,
         "201")
    _add("按 type 匹配",
         len(mon3.find(RequestPattern("https://s",
                                      resource_type="Document"))) == 1,
         "Document")
    _add("多条件 AND",
         len(mon3.find(RequestPattern("https://s", method="POST",
                                      status=201))) == 1, "AND")

    # -- wait_for 行为 ------------------------------------------------------
    got = mon3.wait_for("api/vote", timeout=0.5)
    _add("wait_for 命中已有记录", got is not None and got.status == 201,
         "%s" % got)
    t0 = time.monotonic()
    missed = mon3.wait_for("never-happens", timeout=0.3)
    _add("wait_for 超时返回 None（不抛）", missed is None,
         "%.2fs" % (time.monotonic() - t0))

    # -- summary ------------------------------------------------------------
    s = mon3.summary()
    _add("summary 统计 total", s["total"] == 2, str(s["total"]))
    _add("summary 统计 ok", s["ok"] == 1, str(s["ok"]))
    _add("summary 按状态分组", "201" in s["by_status"], str(s["by_status"]))

    # -- enable 失败时退订（不留垃圾回调）----------------------------------
    fs4 = _FakeSess()
    fs4.fail_enable = True
    mon4 = NetworkMonitor(fs4)
    try:
        mon4.start()
        _add("enable 失败时抛异常", False, "居然成功了")
    except CdpError:
        _add("enable 失败时抛异常", True, "CdpError")
        _add("enable 失败后退订干净",
             all(len(v) == 0 for v in fs4.subs.values()),
             "%s" % {k: len(v) for k, v in fs4.subs.items()})

    # -- stop 幂等 ----------------------------------------------------------
    mon3.stop()
    mon3.stop()
    _add("stop 幂等", True, "调两次不炸")

    # -- RequestRecord.describe 契约 ----------------------------------------
    d = r0.describe()
    _add("describe 含关键字段",
         {"url", "method", "status", "ok", "finished"} <= set(d),
         "%d 个字段" % len(d))

    # -- body 装载 ----------------------------------------------------------
    r0.set_body(b'{"ok":true}')
    _add("set_body 后 body_available=True",
         r0.body_available is True, "True")
    _add("body_json 可解析", r0.body_json() == {"ok": True},
         "%s" % r0.body_json())
    r2.set_body(None)
    _add("set_body(None) 标为不可用",
         r2.body_available is False and r2.body_text == "", "None")

    # -- 头规范化（非 str 值）----------------------------------------------
    h = _flatten_headers({"Content-Length": 123, "X-A": "b"})
    _add("头值非 str 被转成 str",
         h.get("Content-Length") == "123", "%r" % h)

    return {"checks": checks, "failures": failures, "ok": not failures}
