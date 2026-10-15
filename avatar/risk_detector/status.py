# -*- coding: utf-8 -*-
"""风险检测结论的数据结构。

## 为什么不是一个裸 bool

阶段五的核心命题是"**检出验证题 → 优雅暂停 → 呼叫人类**"。
这里最容易犯的错，是把检测结果压成一个 ``detected: bool``：

    if detected: pause_for_human()

这个写法有三处致命缺陷：

1. **丢掉了"是什么"**。reCAPTCHA v3 的隐身评分和滑块验证题，
   处置方式完全不同 —— 前者根本不该打断人（它是静默打分的，
   人类来了也没用），后者必须打断。压成 bool 就没法区分。
2. **丢掉了"凭什么"**。检测是会误报的。没有 evidence，
   误报时人只看到"程序暂停了"，完全无法判断该不该继续。
   这是把人当成了黑盒的附属品。
3. **丢掉了"多确定"**。DOM 里命中 iframe 的 ``src`` 精确指纹，
   和 OCR 在截图里恍恍惚惚认出一个"验"字，可信度差一个数量级。

所以 :class:`RiskStatus` 是**四元组**：检出了没有 / 是哪一种 /
什么厂商 / 证据是什么。并且用一个显式的 :class:`Severity` 表达
"该怎么处置"——注意，**severity 表达的是处置策略，不是危险程度**。
reCAPTCHA v3 很危险（会给你打低分），但处置策略是"别打断，继续走"。

## 三态精神

和 ``verify.result.VerifyResult`` 保持一致：**未知 ≠ 没检出**。

- ``detected=False, reason="clean"`` —— 查过了，干净
- ``detected=False, reason="timeout"`` —— **没查成**，不是干净
- ``detected=False, reason="unsupported"`` —— 环境不支持这项检测

上层如果直接信 ``detected``，会在第二、三种情况下把"我不知道"
当成"没问题"——这正是阶段五最想避免的事故。所以本模块强制
每个 Status 都带 ``reason``，并且提供 :meth:`RiskStatus.is_clean`
（真的干净）与 :meth:`RiskStatus.is_unknown`（没查成）来区分。

## 能力边界

**做**：定义检测结论的数据形状，以及"怎么处置"的契约
（``ACTION_INTERFACE``）。是**纯数据结构 + 契约表**。

**不做**：

- **不检测**。本模块不知道 reCAPTCHA 长什么样，也不去读 DOM。
  它只负责把别人检测到的结果装好。
- **不处置**。``action`` 属性只是**说**该干什么（continue/pause/abort），
  不真的去暂停或终止。执行是 :mod:`guard` 的事。
- **不替调用方做终止决定**。未知状态的 ``action`` 是 ``continue`` ——
  这是刻意的：本对象不越权。要不要因未知而中止，是调用方的判断
  （:mod:`guard` 的 ``should_intervene`` 就选了"中止"）。
- **不持久化**。``to_dict()`` 只负责序列化，落盘是调用方的事。
- **不重试**。检测失败就是失败，重试策略在 :mod:`detector`。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "Severity",
    "RiskType",
    "SEVERITY_ACTION",
    "DEFAULT_RISK_TIMEOUT",
    "RiskStatus",
    "merge_status",
    "ACTION_CONTINUE",
    "ACTION_PAUSE",
    "ACTION_ABORT",
    "ALL_ACTIONS",
    "ACTION_INTERFACE",
    "action_interface",
    "describe_actions",
]


# --------------------------------------------------------------------------
# 严重度 / 处置策略
# --------------------------------------------------------------------------
class Severity(object):
    """检测结果的处置等级。

    **这不是"危险程度"，是"该做什么"。** 命名成 severity 是因为
    与业界习惯对齐，但语义锚点在下面这张表。

    ==============  ==================================================
    ``INFO``        只是个信号，记进日志就够了，不改变流程
    ``BYPASSABLE``  可绕过 —— 隐身验证，继续走，但标记本次会话
                    "已被打分"，事后可解释
    ``INTERRUPT``   **必须打断** —— 图像验证码 / 滑块 / 点选，
                    机器不硬解，叫人来
    ``BLOCK``       连人都未必能过（IP 封禁、账号封禁页面），
                    直接终止任务，不要空耗
    ==============  ==================================================
    """

    INFO = "info"
    BYPASSABLE = "bypassable"
    INTERRUPT = "interrupt"
    BLOCK = "block"


#: 合法 severity 集合。
ALL_SEVERITIES = (Severity.INFO, Severity.BYPASSABLE,
                  Severity.INTERRUPT, Severity.BLOCK)

#: severity → 处置动作。**这是全项目唯一一处把"等级"翻译成"动作"的地方。**
#:
#: - ``"continue"``  —— 继续执行，只在 evidence / trace 里留下记录
#: - ``"pause"``     —— 抛 HumanInterventionRequired，等人
#: - ``"abort"``     —— 抛 RiskBlocked，任务终止（转 Blocked 状态）
#:
#: 放在模块级常量而不是各处的 if-else，是为了让策略可被审计：
#: 想知道"什么情况会打断人"，读这一个字典就够了。
SEVERITY_ACTION = {
    Severity.INFO: "continue",
    Severity.BYPASSABLE: "continue",
    Severity.INTERRUPT: "pause",
    Severity.BLOCK: "abort",
}


# --------------------------------------------------------------------------
# ACTION_INTERFACE —— 与 5C（guard）的接口契约
# --------------------------------------------------------------------------
# 这是**跨模块契约的单一定义点**。5C 的 guard 必须引用本表，不许再抄一份
# —— 两边各定义一套的话，"pause 到底啥意思"就会出现两种说法，
# 是会真出事故的那类漂移。
#
# 三个动作分别由谁消费、消费后发生什么、失败时抛什么，全部写死在这里。
#
# ⚠️ 本表的 ``consumer`` / ``on_consume`` / ``raises`` 三个字段是给
# **人读的契约文档**，也是 `selfcheck` 用来断言"5C 有没有按契约实现"的依据
# （5C 落地后由 detector/guard 的自检交叉验证）。
ACTION_CONTINUE = "continue"
ACTION_PAUSE = "pause"
ACTION_ABORT = "abort"

#: 三个动作的合法集合。
ALL_ACTIONS = (ACTION_CONTINUE, ACTION_PAUSE, ACTION_ABORT)

ACTION_INTERFACE = {
    ACTION_CONTINUE: {
        "consumer": "调用方自己（detector.detect 的返回处）",
        "on_consume": (
            "直接放行 —— 不抛异常、不暂停、不打日志噪声。"
            "流程照原样继续。**这是绝大多数情况的路径**，"
            "必须零开销，因为产品哲学是「除了必要全是自动」。"
        ),
        "raises": None,
        "guard_entry": None,
        "typical_severity": (Severity.INFO, Severity.BYPASSABLE),
        "examples": ("没有任何指纹命中", "reCAPTCHA v3 静默打分",
                     "Cloudflare 无感盾自动通过"),
        "note": "注意：`is_unknown`（检测没查成）也走 continue —— "
                "本表不替调用方决定要不要因未知而中止。",
    },
    ACTION_PAUSE: {
        "consumer": "guard.pause_for_human(status, ...)",
        "on_consume": (
            "1) 先把 InterventionContext 快照下来（截图存盘、"
            "记录当前步骤/URL/检测依据）；"
            "2) winsound.Beep + 可选桌面通知提醒人；"
            "3) 用 GetAsyncKeyState 轮询监听恢复热键（F9）；"
            "4) 人在超时内按了键 → 返回，调用方从断点继续；"
            "5) 超时 → 抛 HumanInterventionTimeout，任务转 Blocked。"
        ),
        #: 超时抛的异常名（5C 必须定义这个类）。
        "raises": "HumanInterventionTimeout",
        #: 进入点函数名（5C 必须提供这个函数）。
        "guard_entry": "pause_for_human",
        "typical_severity": (Severity.INTERRUPT,),
        "examples": ("图像验证码 / 滑块 / 点选（硬约束 #9：不硬解）",
                     "短信/邮箱验证码输入", "Cloudflare 五秒盾卡死",
                     "Windows 凭据对话框"),
        "note": "这是「唯一合法的人工打断」的落点。",
    },
    ACTION_ABORT: {
        "consumer": "调用方自己（通常直接 raise RiskBlocked）",
        "on_consume": (
            "不暂停、不等人 —— 人来了也没用。任务直接终止并转 Blocked 状态。"
            "**不要**走 pause_for_human，那只会白等 5 分钟。"
        ),
        #: 调用方应抛的异常（avatar/errors.py 里已存在，exit_code=5）。
        "raises": "RiskBlocked",
        #: 终止动作**不经过 guard** —— 明确写 None 免得有人去接 pause。
        "guard_entry": None,
        "typical_severity": (Severity.BLOCK,),
        "examples": ("操作过于频繁 / 触发限流",
                     "Access denied（Cloudflare 1020）",
                     "账号被封禁"),
        "note": "与 pause 的区别就一句话：**人能不能解决**。"
                "能 → pause；不能 → abort。",
    },
}


def action_interface(action: str) -> dict:
    """取某个动作的接口契约。未知动作返回兜底字典（不抛）。"""
    d = ACTION_INTERFACE.get(action)
    if d is None:
        return {"consumer": "未知动作", "on_consume": "无法确定处置方式",
                "raises": None, "guard_entry": None,
                "typical_severity": (), "examples": (), "note": "",
                "known": False}
    out = dict(d)
    out["known"] = True
    out["action"] = action
    return out


def describe_actions() -> str:
    """给人看的动作接口契约表。"""
    lines = ["动作接口契约（ACTION_INTERFACE）",
             "=" * 72]
    for act in ALL_ACTIONS:
        d = ACTION_INTERFACE[act]
        lines.append("")
        lines.append("[%s]  消费者：%s" % (act, d["consumer"]))
        lines.append("  失败抛出：%s" % (d["raises"] or "— 不抛异常"))
        lines.append("  guard 入口：%s" % (d["guard_entry"] or "— 不经过 guard"))
        lines.append("  典型 severity：%s" % ", ".join(d["typical_severity"]))
        lines.append("  举例：%s" % "；".join(d["examples"]))
        lines.append("  说明：%s" % d["note"])
    return "\n".join(lines)


class RiskType(object):
    """风险类别 —— 回答"这是什么"。

    与 :class:`Severity` 正交。同一个 type 在不同场景下 severity 可能不同
    （比如 ``CHALLENGE_PAGE`` 在 Cloudflare 无感盾下是 BYPASSABLE，
    在 "Checking your browser" 卡死时是 INTERRUPT）。
    """

    #: 人机验证（reCAPTCHA / hCaptcha / Turnstile 等矩形容器）
    CAPTCHA = "captcha"
    #: 滑块 / 拼图拖动
    SLIDER = "slider"
    #: 点选（"请点击图中所有红绿灯"）
    CLICK_POINT = "click_point"
    #: 短信 / 邮件验证码输入（属于人类凭证，机器不该猜）
    OTP = "otp"
    #: 整页挑战（Cloudflare / 5 秒盾 / "正在检查您的浏览器"）
    CHALLENGE_PAGE = "challenge_page"
    #: 登录墙（需要人类账号密码）
    LOGIN_WALL = "login_wall"
    #: 明确的封禁 / 限流页
    RATE_LIMIT = "rate_limit"
    #: 无法归类，但命中关键词
    UNKNOWN = "unknown"


ALL_RISK_TYPES = (
    RiskType.CAPTCHA, RiskType.SLIDER, RiskType.CLICK_POINT, RiskType.OTP,
    RiskType.CHALLENGE_PAGE, RiskType.LOGIN_WALL, RiskType.RATE_LIMIT,
    RiskType.UNKNOWN,
)


#: 默认检测耗时上限（秒）。硬约束 #7：**所有检测都必须带 timeout**。
#: 3 秒的取法：一个动作前的检测如果超过 3 秒，用户已经能感觉到卡顿，
#: 而"没检出"的代价远小于"每次动作多等 3 秒"。
DEFAULT_RISK_TIMEOUT = 3.0


# --------------------------------------------------------------------------
# RiskStatus
# --------------------------------------------------------------------------
class RiskStatus(object):
    """一次风险检测的完整结论。

    构造推荐用类方法，语义一目了然::

        RiskStatus.clean(source="dom", elapsed=0.02)
        RiskStatus.hit(RiskType.SLIDER, vendor="geetest",
                       severity=Severity.INTERRUPT,
                       evidence={"dom": "class*='geetest_slider'"})
        RiskStatus.unknown_(source="pixel", reason="timeout")

    字段速查：

    - ``detected``   检出没有。**未知时也是 False** —— 必须配 reason 看
    - ``type``       哪一类（``RiskType``）
    - ``vendor``     厂商（``"recaptcha"`` / ``"hcaptcha"`` / ``"cloudflare"`` …）
    - ``severity``   处置等级（``Severity``）
    - ``evidence``   凭什么 —— 原始命中材料，给人看也给模型看
    - ``source``     哪一层检出的（``"dom"`` / ``"window"`` / ``"pixel"``）
    - ``reason``     ``"clean"`` / ``"hit"`` / ``"timeout"`` / ``"error"`` …
    - ``elapsed``    耗时
    ``__slots__`` 而非 dataclass：和 ``VerifyResult`` 保持一致的风格，
    且这个对象在热路径上会被高频创建。
    """

    __slots__ = ("detected", "type", "vendor", "severity", "evidence",
                 "source", "reason", "elapsed", "checked_at", "extra",
                 "confidence", "fingerprint_id")

    def __init__(
        self,
        detected: bool = False,
        *,
        type: str = "",                      # noqa: A002 —— 对齐业界命名
        vendor: str = "",
        severity: str = Severity.INFO,
        evidence: Optional[Dict[str, Any]] = None,
        source: str = "",
        reason: str = "",
        elapsed: float = 0.0,
        confidence: float = 1.0,
        fingerprint_id: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.detected = bool(detected)
        self.type = type or (RiskType.UNKNOWN if detected else "")
        self.vendor = vendor or ""
        self.severity = severity if severity in ALL_SEVERITIES else Severity.INFO
        self.evidence = dict(evidence or {})
        self.source = source or ""
        self.reason = reason or ("hit" if detected else "clean")
        self.elapsed = float(elapsed)
        self.checked_at = time.time()
        self.confidence = _clamp01(confidence)
        self.fingerprint_id = fingerprint_id or ""
        self.extra = dict(extra or {})

    # ---------------- 三态判定 ----------------

    @property
    def is_clean(self) -> bool:
        """**确认干净** —— 查过了，没命中。

        注意 ``reason`` 为 timeout / error / unsupported 时返回 False，
        即使 ``detected`` 是 False。这就是"未知 ≠ 没检出"的落点。
        """
        return (not self.detected) and self.reason in (
            "clean", "no_fingerprint", "below_threshold")

    @property
    def is_unknown(self) -> bool:
        """**没查成** —— 既不能说干净，也不能说命中。

        上层拿到这个应该：① 记 trace；② 视场景决定要不要用更贵的
        检测手段兜一次；③ **绝不**当成"没问题"放行高风险动作。
        """
        return (not self.detected) and not self.is_clean

    @property
    def needs_human(self) -> bool:
        """是否必须叫人。等价于 ``SEVERITY_ACTION[severity] == "pause"``。"""
        return SEVERITY_ACTION.get(self.severity) == "pause"

    @property
    def action(self) -> str:
        """处置动作：``"continue"`` / ``"pause"`` / ``"abort"``。

        干净或未知时恒为 ``"continue"``（未知的处置由调用方决定，
        本对象不越权替它做终止决定）。
        """
        if not self.detected:
            return "continue"
        return SEVERITY_ACTION.get(self.severity, "continue")

    # ---------------- 构造 ----------------

    @classmethod
    def clean(cls, *, source: str = "", elapsed: float = 0.0,
              reason: str = "clean", **kw) -> "RiskStatus":
        """确认干净。"""
        return cls(False, source=source, elapsed=elapsed, reason=reason, **kw)

    @classmethod
    def unknown_(cls, *, source: str = "", reason: str = "unknown",
                 elapsed: float = 0.0, **kw) -> "RiskStatus":
        """没查成。

        名字带下划线 —— 和 ``VerifyResult.unknown_`` 同样的理由：
        实例可能有同名属性，``__slots__`` 下会同名遮蔽。
        """
        kw.setdefault("confidence", 0.0)
        return cls(False, source=source, reason=reason, elapsed=elapsed, **kw)

    @classmethod
    def timeout(cls, *, source: str = "",
                limit: float = DEFAULT_RISK_TIMEOUT) -> "RiskStatus":
        """超时（硬约束 #7 的载体）。

        ``detected=False`` —— **超时不是检出**。但 ``reason="timeout"``
        让 :attr:`is_clean` 返回 False，上层不会误判成"干净"。
        """
        return cls.unknown_(source=source, reason="timeout", elapsed=limit,
                            extra={"timeout": limit})

    @classmethod
    def error(cls, exc: Any, *, source: str = "",
              elapsed: float = 0.0) -> "RiskStatus":
        """检测过程本身抛异常。异常消息进 ``extra``，不进 ``reason``。

        这样调用方可以 ``status.reason == "error"`` 做分类，
        同时 ``status.extra["error"]`` 拿到细节用于诊断。
        """
        return cls.unknown_(source=source, reason="error", elapsed=elapsed,
                            extra={"error": "%s: %s" % (type(exc).__name__, exc)})

    @classmethod
    def hit(cls, type: str, *, vendor: str = "",      # noqa: A002
            severity: str = Severity.INTERRUPT,
            evidence: Optional[Dict[str, Any]] = None,
            source: str = "", elapsed: float = 0.0,
            confidence: float = 1.0, fingerprint_id: str = "",
            **kw) -> "RiskStatus":
        """检出。"""
        return cls(True, type=type, vendor=vendor, severity=severity,
                   evidence=evidence, source=source, elapsed=elapsed,
                   reason="hit", confidence=confidence,
                   fingerprint_id=fingerprint_id, **kw)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RiskStatus":
        """从 :meth:`to_dict` 的输出还原（trace 回放 / 跨进程传递用）。"""
        if not isinstance(d, dict):
            return cls.unknown_(reason="bad_payload")
        return cls(
            bool(d.get("detected")),
            type=str(d.get("type") or ""),
            vendor=str(d.get("vendor") or ""),
            severity=str(d.get("severity") or Severity.INFO),
            evidence=d.get("evidence") if isinstance(d.get("evidence"), dict) else {},
            source=str(d.get("source") or ""),
            reason=str(d.get("reason") or ""),
            elapsed=float(d.get("elapsed") or 0.0),
            confidence=float(d.get("confidence", 1.0)),
            fingerprint_id=str(d.get("fingerprint_id") or ""),
            extra=d.get("extra") if isinstance(d.get("extra"), dict) else {},
        )

    # ---------------- 输出 ----------------

    def to_dict(self) -> Dict[str, Any]:
        """JSON 安全的字典。**所有值都是基础类型**，可直接 ``json.dumps``。"""
        return {
            "detected": self.detected,
            "type": self.type,
            "vendor": self.vendor,
            "severity": self.severity,
            "action": self.action,
            "needs_human": self.needs_human,
            "is_clean": self.is_clean,
            "is_unknown": self.is_unknown,
            "reason": self.reason,
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "fingerprint_id": self.fingerprint_id,
            "evidence": _jsonable(self.evidence),
            "elapsed": round(self.elapsed, 4),
            "checked_at": self.checked_at,
            "extra": _jsonable(self.extra),
        }

    def describe(self) -> str:
        """一行人类可读说明。用于日志与 CLI 输出。"""
        if self.detected:
            head = "⚠ 检出 %s" % (self.type or RiskType.UNKNOWN)
            if self.vendor:
                head += "(%s)" % self.vendor
            bits = [head, "→%s" % self.action]
            if self.source:
                bits.append("@%s" % self.source)
            if self.reason:
                bits.append(self.reason)
            return " ".join(bits)
        if self.is_clean:
            return "✓ 干净%s" % (" @%s" % self.source if self.source else "")
        return "? 未判定(%s)%s" % (self.reason,
                                   " @%s" % self.source if self.source else "")

    def explain(self) -> str:
        """多行详细说明。给人看的 —— 熔断时打印这个。"""
        lines = [self.describe()]
        if self.fingerprint_id:
            lines.append("  指纹: %s" % self.fingerprint_id)
        if self.confidence < 1.0:
            lines.append("  置信度: %.2f" % self.confidence)
        for k, v in sorted(self.evidence.items()):
            lines.append("  证据·%s: %s" % (k, _short(v)))
        for k, v in sorted(self.extra.items()):
            lines.append("  附加·%s: %s" % (k, _short(v)))
        return "\n".join(lines)

    def __bool__(self) -> bool:
        """``if status`` 等价于 ``status.detected``。

        **不要用它做处置决策** —— 显式读 ``.action``。提供它只是
        为了日志里写 ``if st:`` 方便，和 ``VerifyResult.__bool__``
        的取舍一致。
        """
        return self.detected

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, RiskStatus):
            return (self.detected == other.detected
                    and self.type == other.type
                    and self.vendor == other.vendor
                    and self.severity == other.severity)
        if isinstance(other, bool):
            return self.detected == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.detected, self.type, self.vendor, self.severity))

    def __repr__(self) -> str:  # pragma: no cover
        return "<RiskStatus %s>" % self.describe()


# --------------------------------------------------------------------------
# 合并（三层检测的汇合点）
# --------------------------------------------------------------------------
#: severity 的优先级权重。合并时取最高的。
_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.BYPASSABLE: 1,
    Severity.INTERRUPT: 2,
    Severity.BLOCK: 3,
}


def merge_status(statuses: Sequence[RiskStatus],
                 *, source: str = "") -> RiskStatus:
    """把三层检测的结果合成一个结论。

    规则（**顺序即优先级**）：

    1. 有任何一个 ``detected`` → 取 severity 最高的那个当主结论，
       但**把其余的 evidence 全部并进来**。理由：三层都命中时，
       人需要看到全部依据才能判断；而处置动作必须唯一，否则
       "DOM 说可绕过、像素说必须打断"就没人知道该听谁的了。
    2. 没有 detected，但**有任何 unknown** → 返回 unknown。
       理由：三层里有一层没查成，就不能宣称"干净"。这是三态精神的
       直接推论，也是最容易被写错的一处。
    3. 全干净 → 干净。

    空序列返回 :meth:`RiskStatus.unknown_`（没检测 ≠ 干净）。
    """
    items = [s for s in (statuses or ()) if isinstance(s, RiskStatus)]
    if not items:
        return RiskStatus.unknown_(source=source, reason="no_source")

    hits = [s for s in items if s.detected]
    if hits:
        best = max(hits, key=lambda s: (_SEVERITY_RANK.get(s.severity, 0),
                                        s.confidence))
        # 并集 evidence：每个来源带自己的前缀，避免同名键互相覆盖
        merged_evidence: Dict[str, Any] = {}
        sources = []
        for s in hits:
            tag = s.source or "?"
            sources.append(tag)
            for k, v in s.evidence.items():
                merged_evidence["%s.%s" % (tag, k)] = v
        extra = dict(best.extra)
        extra["all_sources"] = sources
        extra["hit_count"] = len(hits)
        return RiskStatus(
            True,
            type=best.type,
            vendor=best.vendor,
            severity=best.severity,
            evidence=merged_evidence,
            source=source or best.source,
            reason="hit",
            elapsed=sum(s.elapsed for s in items),
            confidence=best.confidence,
            fingerprint_id=best.fingerprint_id,
            extra=extra,
        )

    unknown = [s for s in items if s.is_unknown]
    if unknown:
        why = ",".join(sorted({s.reason for s in unknown}))
        return RiskStatus.unknown_(
            source=source,
            reason=why or "unknown",
            elapsed=sum(s.elapsed for s in items),
            extra={"unknown_sources": [s.source for s in unknown],
                   "clean_sources": [s.source for s in items
                                     if s.is_clean]},
        )

    return RiskStatus.clean(source=source,
                            elapsed=sum(s.elapsed for s in items),
                            extra={"sources": [s.source for s in items]})


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------
def _clamp01(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 1.0
    if f != f:            # NaN
        return 0.0
    return max(0.0, min(1.0, f))


def _jsonable(obj: Any, depth: int = 3) -> Any:
    """尽力把任意值转成 JSON 可序列化的形式（有深度上限，防环）。"""
    if depth <= 0:
        return _short(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, depth - 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_jsonable(v, depth - 1) for v in obj]
    return _short(obj)


def _short(v: Any, limit: int = 160) -> str:
    """截断成一行短文本。"""
    try:
        s = v if isinstance(v, str) else repr(v)
    except Exception:      # noqa: BLE001 —— repr 也可能抛
        s = "<不可表示>"
    s = s.replace("\r", " ").replace("\n", " ")
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    return s


def selfcheck() -> dict:
    """status 模块自检：只测数据结构不变量，**不碰系统**（恒快）。"""
    import time as _t
    t0 = _t.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # 1) severity 表覆盖完整
    _add("SEVERITY_ACTION 覆盖全部 severity",
         all(s in SEVERITY_ACTION for s in ALL_SEVERITIES),
         "severity=%d 动作=%d" % (len(ALL_SEVERITIES), len(SEVERITY_ACTION)))

    # 2) 干净态的语义
    c = RiskStatus.clean(source="dom", elapsed=0.01)
    _add("clean: detected=False", c.detected is False, c.describe())
    _add("clean: is_clean=True", c.is_clean is True, "reason=%s" % c.reason)
    _add("clean: action=continue", c.action == "continue", c.action)

    # 3) 超时态必须**不**是干净 —— 这条是三态精神的锚
    to = RiskStatus.timeout(source="pixel")
    _add("timeout: detected=False", to.detected is False, to.describe())
    _add("timeout: is_clean=False（未知≠干净）", to.is_clean is False,
         "reason=%s" % to.reason)
    _add("timeout: is_unknown=True", to.is_unknown is True, to.describe())
    _add("timeout: action=continue（不越权终止）",
         to.action == "continue", to.action)

    # 4) 异常态
    try:
        raise ValueError("模拟检测器内部错误")
    except ValueError as exc:
        er = RiskStatus.error(exc, source="dom")
    _add("error: reason=error", er.reason == "error", er.reason)
    _add("error: is_clean=False", er.is_clean is False, er.describe())
    _add("error: 细节进 extra", "ValueError" in str(er.extra.get("error", "")),
         er.extra.get("error"))

    # 5) 命中态 + 处置映射
    h = RiskStatus.hit(RiskType.SLIDER, vendor="geetest",
                       severity=Severity.INTERRUPT,
                       evidence={"dom": ".geetest_slider"})
    _add("hit: detected=True", h.detected is True, h.describe())
    _add("hit: needs_human=True", h.needs_human is True, h.severity)
    _add("hit: action=pause", h.action == "pause", h.action)

    b = RiskStatus.hit(RiskType.RATE_LIMIT, severity=Severity.BLOCK)
    _add("block: action=abort", b.action == "abort", b.describe())

    v3 = RiskStatus.hit(RiskType.CAPTCHA, vendor="recaptcha",
                        severity=Severity.BYPASSABLE)
    _add("bypassable: 不打断人", v3.needs_human is False and v3.action == "continue",
         v3.describe())

    # 6) to_dict 必须 JSON 安全
    import json
    try:
        payload = json.dumps(h.to_dict(), ensure_ascii=False)
        ok_json, why = True, "%d 字节" % len(payload)
    except Exception as exc:      # noqa: BLE001
        ok_json, why = False, "%s: %s" % (type(exc).__name__, exc)
    _add("hit.to_dict() 可 json.dumps", ok_json, why)

    # 7) to_dict 往返
    back = RiskStatus.from_dict(h.to_dict())
    _add("from_dict(to_dict(s)) 往返一致", back == h,
         "%s vs %s" % (back, h))

    # 8) merge：命中优先
    h_dom = RiskStatus.hit(RiskType.SLIDER, vendor="geetest",
                           severity=Severity.INTERRUPT,
                           evidence={"sel": ".geetest_slider"}, source="dom")
    h_px = RiskStatus.hit(RiskType.SLIDER, vendor="geetest",
                          severity=Severity.INFO,
                          evidence={"ocr": "拖动滑块"}, source="pixel")
    m = merge_status([c, h_dom])
    _add("merge(clean, hit) → detected", m.detected and m.severity == h_dom.severity,
         m.describe())
    _add("merge: 并集 evidence 带来源前缀",
         "dom.sel" in m.evidence, sorted(m.evidence))
    m_hits = merge_status([h_dom, h_px])
    _add("merge: 多个命中取最高 severity 且并集证据",
         m_hits.severity == Severity.INTERRUPT
         and "dom.sel" in m_hits.evidence
         and "pixel.ocr" in m_hits.evidence,
         "%s evidence=%s" % (m_hits.severity, sorted(m_hits.evidence)))
    _add("merge: 记录命中来源清单",
         sorted(m_hits.extra.get("all_sources", [])) == ["dom", "pixel"],
         m_hits.extra.get("all_sources"))

    # 9) merge：**有未知就不能说干净** —— 这是最易写错的一处
    m2 = merge_status([c, to])
    _add("merge(clean, timeout) → 未知而非干净",
         (not m2.detected) and m2.is_unknown,
         "%s reason=%s" % (m2.describe(), m2.reason))

    # 10) merge：全干净
    m3 = merge_status([c, RiskStatus.clean(source="window")])
    _add("merge(clean, clean) → 干净", m3.is_clean, m3.describe())

    # 11) merge 空输入 ≠ 干净
    m4 = merge_status([])
    _add("merge([]) → 未知（没检测≠干净）", m4.is_unknown, m4.describe())

    # 12) 非法 severity 被夹回 INFO（不让脏数据流进来）
    dirty = RiskStatus(True, severity="胡说八道")
    _add("非法 severity 夹回 INFO", dirty.severity == Severity.INFO,
         dirty.severity)

    # 13) explain() 不抛
    try:
        txt = h.explain()
        _add("explain() 可调用", "检出" in txt, txt.splitlines()[0])
    except Exception as exc:      # noqa: BLE001
        _add("explain() 可调用", False, "%s: %s" % (type(exc).__name__, exc))

    # 14) _jsonable 抗环
    ring: Dict[str, Any] = {}
    ring["self"] = ring
    try:
        _jsonable(ring)
        _add("_jsonable 抗自引用", True, "未爆栈")
    except RecursionError:
        _add("_jsonable 抗自引用", False, "RecursionError")

    # 15) 常量存在
    _add("DEFAULT_RISK_TIMEOUT 是正数",
         isinstance(DEFAULT_RISK_TIMEOUT, float) and DEFAULT_RISK_TIMEOUT > 0,
         DEFAULT_RISK_TIMEOUT)
    _add("ALL_RISK_TYPES 非空", len(ALL_RISK_TYPES) >= 5,
         "共 %d 类" % len(ALL_RISK_TYPES))

    # ==================================================================
    # 追问 3：ACTION_INTERFACE 契约与 SEVERITY_ACTION 的一致性
    # ==================================================================
    _add("ACTION_INTERFACE 覆盖全部三个动作",
         set(ACTION_INTERFACE) == set(ALL_ACTIONS) == {
             ACTION_CONTINUE, ACTION_PAUSE, ACTION_ABORT},
         "共 %d 个" % len(ACTION_INTERFACE))

    # 关键一致性：SEVERITY_ACTION 用到的动作值，必须都在 ALL_ACTIONS 里。
    # 这条断言就是防"两边各定义一套"的护城河 —— 一旦有人在 status.py 里
    # 加了新动作却忘了登记契约，或者反过来，这里立刻红。
    used_actions = set(SEVERITY_ACTION.values())
    _add("SEVERITY_ACTION 的动作值 ⊂ ALL_ACTIONS（防两边漂移）",
         used_actions <= set(ALL_ACTIONS),
         "用到 %s" % sorted(used_actions))

    # 每个动作的典型 severity 必须真的映射到自己 —— 反向自证
    for act in ALL_ACTIONS:
        sevs = ACTION_INTERFACE[act]["typical_severity"]
        _add("契约自洽：%s 的典型 severity 都映射回 %s" % (act, act),
             bool(sevs) and all(SEVERITY_ACTION.get(s) == act for s in sevs),
             ", ".join("%s→%s" % (s, SEVERITY_ACTION.get(s)) for s in sevs))

    # pause 必须点名 guard 入口 —— 5C 要按这个函数名实现
    _add("pause 契约点名 guard.pause_for_human",
         ACTION_INTERFACE[ACTION_PAUSE]["guard_entry"] == "pause_for_human",
         ACTION_INTERFACE[ACTION_PAUSE]["guard_entry"])
    _add("pause 契约点名抛出 HumanInterventionTimeout",
         ACTION_INTERFACE[ACTION_PAUSE]["raises"] == "HumanInterventionTimeout",
         ACTION_INTERFACE[ACTION_PAUSE]["raises"])
    # continue 必须零异常 —— 这是"除了必要全是自动"的锚
    _add("continue 契约：不抛异常、不经过 guard",
         ACTION_INTERFACE[ACTION_CONTINUE]["raises"] is None
         and ACTION_INTERFACE[ACTION_CONTINUE]["guard_entry"] is None,
         "raises=None guard_entry=None")
    # abort 必须点名 RiskBlocked，且**不**走 guard
    _add("abort 契约：抛 RiskBlocked 且不经过 guard",
         ACTION_INTERFACE[ACTION_ABORT]["raises"] == "RiskBlocked"
         and ACTION_INTERFACE[ACTION_ABORT]["guard_entry"] is None,
         "raises=%s guard_entry=%s" % (
             ACTION_INTERFACE[ACTION_ABORT]["raises"],
             ACTION_INTERFACE[ACTION_ABORT]["guard_entry"]))

    # 契约表每条字段齐备
    need = {"consumer", "on_consume", "raises", "guard_entry",
            "typical_severity", "examples", "note"}
    _add("契约表字段齐备",
         all(need <= set(v) for v in ACTION_INTERFACE.values()),
         "已规范")

    # RiskBlocked 在 errors.py 里确实存在（abort 契约的载体）
    # 注意：直接 `python status.py` 跑时没有父包，相对导入会失败 ——
    # 那种情况下退回按文件路径直接加载，保证自检在两种跑法下都绿。
    try:
        try:
            from ..errors import RiskBlocked
        except (ImportError, ValueError):
            import importlib.util
            import os
            _p = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "errors.py")
            _spec = importlib.util.spec_from_file_location("_avatar_errors", _p)
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            RiskBlocked = _mod.RiskBlocked
        _add("abort 契约的载体 RiskBlocked 已存在",
             getattr(RiskBlocked, "exit_code", None) == 5,
             "exit_code=%s" % getattr(RiskBlocked, "exit_code", None))
    except Exception as exc:      # noqa: BLE001
        _add("abort 契约的载体 RiskBlocked 已存在", False,
             "%s: %s" % (type(exc).__name__, exc))

    # action_interface / describe_actions 可调用
    _add("action_interface 未知动作返回兜底",
         action_interface("__nope__").get("known") is False,
         action_interface("__nope__")["consumer"])
    try:
        txt = describe_actions()
        _add("describe_actions 可调用", "pause" in txt and "abort" in txt,
             txt.splitlines()[0])
    except Exception as exc:      # noqa: BLE001
        _add("describe_actions 可调用", False, "%s: %s" % (type(exc).__name__, exc))

    return {
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "module": __name__,
        "elapsed": round(_t.perf_counter() - t0, 3),
    }


def _main() -> int:
    import sys
    r = selfcheck()
    for c in r["checks"]:
        print("  %s %-42s %s" % ("✓" if c["ok"] else "✗", c["name"], c["detail"]))
    print("status 自检：%d/%d 通过，%.3fs"
          % (len(r["checks"]) - len(r["failures"]), len(r["checks"]), r["elapsed"]))
    return 0 if r["ok"] else 1


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(_main())
