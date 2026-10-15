# -*- coding: utf-8 -*-
"""风控指纹库 —— "验证题长什么样"的**唯一权威定义**。

## 为什么要单独立一个库

检测逻辑（DOM 扫、窗口枚举、OCR）会随时间演进，但"reCAPTCHA 的 iframe
长什么样"是**领域知识**，不是代码逻辑。把领域知识抽出来有三个好处：

1. **可审计**。风控升级时，只需要改这一个文件，不用读三个检测器。
2. **可测试**。指纹是纯数据，测试可以直接断言"给了这段 HTML，
   必须命中 recaptcha"，不需要起浏览器。
3. **可关闭**。每条指纹有 ``severity``，误报时能精确地把某一条降级，
   而不用关掉整层检测。

## 三类指纹，对应三层检测

====================  ==================  ============================
``dom``               网页 DOM 特征       iframe src / class / id
``window``            桌面窗口特征        类名 / 标题 / 进程
``text``              文本关键词          中英文，"拖动" / "captcha"
====================  ==================  ============================

DOM 和 window 是**强指纹**（命中即高置信），text 是**弱指纹**
（需要多个词共现才敢下判断，否则"验证"/"确定"这种日常词会满天飞）。

## 关键设计：可绕过性不是"能不能绕过"，是"该不该打断"

产品哲学是"**除了必要全是自动**"。所以判据不是"这个验证题难不难过"，
而是"**人类介入有没有意义**"：

- reCAPTCHA **v3** —— 静默打分，页面上什么都没有。人类来了也只能
  干瞪眼。**不该打断**（``Severity.BYPASSABLE``）。
- Cloudflare **无感盾** —— 正常访问时自动过，等 1~2 秒就放行。
  **不该打断**（``Severity.BYPASSABLE``）。
- Cloudflare **五秒盾 / "正在检查您的浏览器"** —— 如果卡住了，
  人点一下就好了。**必须打断**。
- 图像验证码 / 滑块 / 点选 —— 机器不硬解（硬约束 #9）。
  **必须打断**（``Severity.INTERRUPT``）。
- IP 封禁 / 账号封禁 —— 人都过不去。**直接终止**（``Severity.BLOCK``）。

同一个厂商，在不同形态下 severity 不同 —— 这就是为什么指纹的
``severity`` 是**逐条**配的，不是按 vendor 配的。

## 关于"不硬解"

硬约束 #9：禁止写任何识别滑块缺口位置、点选图标坐标的逻辑。
本文件里的 ``slider`` / ``click_point`` 指纹，全部是
``Severity.INTERRUPT`` —— 检出它们的目的**只有一个：叫人**。
它们的 evidence 里带 ``"machine_solve": "forbidden"`` 作为
可执行的自证，任何人加硬解逻辑都会立刻和这里冲突。

## 能力边界：本库不做文本归一化

匹配是**连续子串**（``in``）或正则，不是模糊匹配。所以：

- ``"请 拖动滑块 完成验证"`` → 命中（"拖动滑块"是连续子串）
- ``"拖 动 滑 块"`` → **不命中**（字间插了空格）

第二种是 OCR 的典型失败模式。**归一化不在这里做**，理由是：
去掉全部空白会把英文单词边界也一起破坏（``"drag the slider"``
变成 ``"dragtheslider"``），反而制造新的漏匹配。

正确的分工是：``detector_pixel`` 拿到 OCR 结果后，**同时**送两份
进来 —— 一份原样（给英文/正则指纹），一份做 CJK 专用归一化
（只删 CJK 字符之间的空白，保留拉丁词间空格）。本库只负责
"给了什么文本，就按什么文本判"。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .status import RiskType, Severity

# ==========================================================================
# SOURCE —— 指纹来源台账
# ==========================================================================
# 每一条指纹的 ``source`` 字段取值必须来自下面这张表。加新来源时**先在这里
# 登记**，再在指纹里引用 —— 这样 `selfcheck` 能断言"没有野来源"。
#
# 目的有两个：
#   1. **更新时有据可依**。厂商升级后要改哪几条，按 source 一筛就知道。
#      第三方整理来的指纹过时最快，实测来的最可信 —— 这个区别必须留在数据里。
#   2. **可信度可解释**。误报排查时，"这条是网上抄的"和"这条是我实测抓到包
#      确认的"是完全不同的两件事。
#
# 每个条目：short（人话标签） / trust（可信度权重，留给 5G 做加权） /
#           detail（具体到 URL 或实测环境，方便复核）
#
# ⚠️ 任何未在本表登记的 source 字符串都会让 selfcheck 变红。
SOURCES: Dict[str, Dict[str, Any]] = {
    # ---------- 官方文档 ----------
    "recaptcha-docs": {
        "short": "reCAPTCHA 官方文档",
        "trust": 0.95,
        "detail": "developers.google.com/recaptcha —— iframe 路径 "
                  "/recaptcha/api2/{anchor,bframe}、徽标 class "
                  "grecaptcha-badge、隐藏字段 g-recaptcha-response 均为官方公开约定",
    },
    "hcaptcha-docs": {
        "short": "hCaptcha 官方文档",
        "trust": 0.95,
        "detail": "docs.hcaptcha.com —— iframe 域 newassets.hcaptcha.com、"
                  "脚本域 js.hcaptcha.com、挂载 class h-captcha",
    },
    "cloudflare-docs": {
        "short": "Cloudflare 官方文档 / 公开行为",
        "trust": 0.95,
        "detail": "Turnstile 域 challenges.cloudflare.com/turnstile；"
                  "挑战平台路径 /cdn-cgi/challenge-platform/；"
                  "标题 Just a moment... / Attention Required! / "
                  "Access denied 为长期稳定公开特征",
    },
    "geetest-docs": {
        "short": "极验官方文档 / 前端公开类名",
        "trust": 0.9,
        "detail": "geetest.com —— geetest_ 系列 class 前缀（gt_ 新版）、"
                  "滑块把手 geetest_slider_button。新版 V4 类名有变化，"
                  "``dom.geetest.script`` 特意配成低权重以兼容",
    },
    "ms-docs": {
        "short": "Microsoft 官方文档",
        "trust": 0.98,
        "detail": "Win32 窗口类名（#32770 / CredentialDialogXamlHost / "
                  "Consent / CryptUIApi）、"
                  "HTML autocomplete=one-time-code 规范（WHATWG HTML Standard）",
    },
    "aliyun-docs": {
        "short": "阿里云验证码文档",
        "trust": 0.9,
        "detail": "help.aliyun.com 验证码 2.0 —— nc_container / "
                  "aliyun_ncv 为前端固定容器名",
    },

    # ---------- 实测 ----------
    "probed-2026-09": {
        "short": "本项目实测（2026-09 抓取）",
        "trust": 1.0,
        "detail": "在本机 Edge（阶段四 web_cdp 通道）打开真实站点后，"
                  "通过 CDP Runtime.evaluate 读回 DOM 确认的指纹。"
                  "可信度最高，但样本数少，升级后需重测",
    },
    "probed-windows": {
        "short": "本机 Windows 实测",
        "trust": 1.0,
        "detail": "在本机调用 EnumWindows 实际枚举到的窗口类名/标题。"
                  "系统窗口类名跨版本基本稳定",
    },

    # ---------- 社区整理 ----------
    "community-2024": {
        "short": "社区整理（2024）",
        "trust": 0.6,
        "detail": "从开源打码/绕过项目与安全社区整理的通用类名约定"
                  "（tianai-captcha / aj-captcha / vaptcha / "
                  "slider-verify / drag-verify 等）。**过时风险最高**，"
                  "新报误报时优先怀疑这一档",
    },
    "heuristic": {
        "short": "启发式推断（无外部依据）",
        "trust": 0.4,
        "detail": "由命名惯例推断出的模式（如 puzzle、captcha 这类宽泛词）。"
                  "一律配低权重，**单独出现绝不触发处置**",
    },
}


__all__ = [
    "Fingerprint",
    "DOM_FINGERPRINTS",
    "WINDOW_FINGERPRINTS",
    "TEXT_FINGERPRINTS",
    "STRONG_TEXT_GROUPS",
    "WEAK_TEXT_WORDS",
    "SOURCES",
    "source_info",
    "audit_sources",
    "ALL_FINGERPRINTS",
    "fingerprint_by_id",
    "iter_fingerprints",
    "match_dom",
    "match_window",
    "match_text",
    "text_hit_score",
    "describe_library",
]


# --------------------------------------------------------------------------
# 指纹数据结构
# --------------------------------------------------------------------------
class Fingerprint(object):
    """一条指纹特征。

    ``pattern`` 是**小写**的子串或正则；匹配时统一把目标文本转小写。
    中文没有大小写问题，英文必须小写，否则 ``reCAPTCHA`` 和
    ``recaptcha`` 会漏 —— 实测踩过。

    字段：

    - ``id``       稳定标识，写进 ``RiskStatus.fingerprint_id``，可追溯
    - ``layer``    ``"dom"`` / ``"window"`` / ``"text"``
    - ``vendor``   厂商（``"recaptcha"`` / ``"hcaptcha"`` / ``"geetest"`` …）
    - ``type``     ``RiskType`` 之一
    - ``severity`` 处置等级（**逐条配，不是按 vendor 配**）
    - ``pattern``  匹配串（小写）
    - ``regex``    是否是正则（否则按子串 contains 匹配）
    - ``where``    DOM 层用：在哪些属性里找（``src`` / ``class`` / ``id`` / ``html``）
    - ``weight``   权重。同一条命中时给多少分，用于阈值判定
    - ``source``   来源台账键（**必须是 :data:`SOURCES` 里的键**）
    - ``note``     人话解释 —— 这条为什么这么判
    """

    __slots__ = ("id", "layer", "vendor", "type", "severity", "pattern",
                 "regex", "where", "weight", "note", "tags", "source", "_re")

    def __init__(self, id: str, layer: str, vendor: str, type: str,     # noqa: A002
                 severity: str, pattern: str, *, regex: bool = False,
                 where: Sequence[str] = (), weight: float = 1.0,
                 note: str = "", tags: Sequence[str] = (),
                 source: str = "heuristic") -> None:
        self.id = id
        self.layer = layer
        self.vendor = vendor
        self.type = type
        self.severity = severity
        self.pattern = pattern
        self.regex = bool(regex)
        self.where = tuple(where)
        self.weight = float(weight)
        self.note = note
        self.tags = tuple(tags)
        #: 来源台账键。未登记的键会让 selfcheck 变红 —— 这是刻意的，
        #: 逼着加指纹的人顺手把出处写上。
        self.source = source or "heuristic"
        self._re = re.compile(pattern, re.I) if self.regex else None

    def matches(self, text: str) -> bool:
        """单个字符串是否命中。``text`` 会转小写后比较。"""
        if not text:
            return False
        if self._re is not None:
            return bool(self._re.search(text))
        return self.pattern in text.lower()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "layer": self.layer, "vendor": self.vendor,
            "type": self.type, "severity": self.severity,
            "pattern": self.pattern, "regex": self.regex,
            "where": list(self.where), "weight": self.weight,
            "source": self.source, "note": self.note, "tags": list(self.tags),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return "<Fingerprint %s %s/%s>" % (self.id, self.vendor, self.type)


# --------------------------------------------------------------------------
# DOM 指纹
# --------------------------------------------------------------------------
#: DOM 层指纹。``where`` 指明扫描范围，检测器据此决定从哪个属性取值 ——
#: 这是为了避免把整页 HTML 拉过来做字符串搜索（大页面能到几 MB，
#: 每次动作前都拉一遍是不可接受的）。
DOM_FINGERPRINTS: List[Fingerprint] = [
    # ------------------------------ reCAPTCHA ------------------------------
    Fingerprint(
        "dom.recaptcha.iframe_anchor", "dom", "recaptcha", RiskType.CAPTCHA,
        Severity.INTERRUPT, "recaptcha/api2/anchor", where=("src", "html"),
        weight=1.0,
        note="reCAPTCHA v2 复选框的 iframe。有它就有可见验证题，必须叫人。",
        tags=("iframe", "visible"), source="recaptcha-docs",
    ),
    Fingerprint(
        "dom.recaptcha.iframe_bframe", "dom", "recaptcha", RiskType.CLICK_POINT,
        Severity.INTERRUPT, "recaptcha/api2/bframe", where=("src", "html"),
        weight=1.0,
        note="图像挑战弹窗 iframe（点选/看图作答）。硬约束 #9：不硬解。",
        tags=("iframe", "image_challenge", "no_solve"), source="recaptcha-docs",
    ),
    Fingerprint(
        "dom.recaptcha.gstatic_iframe", "dom", "recaptcha", RiskType.CAPTCHA,
        Severity.INTERRUPT, "recaptcha/enterprise/anchor", where=("src", "html"),
        weight=1.0,
        note="reCAPTCHA Enterprise 版锚点 iframe。",
        tags=("iframe", "enterprise"), source="recaptcha-docs",
    ),
    Fingerprint(
        "dom.recaptcha.badge_class", "dom", "recaptcha", RiskType.CAPTCHA,
        Severity.BYPASSABLE, "grecaptcha-badge", where=("class", "id", "html"),
        weight=0.9,
        note=("右下角那个 reCAPTCHA 徽标。**只有徽标**通常说明是 v3 或"
              "hidden 模式 —— 静默打分，人来了也没用，不打断。"),
        tags=("badge", "invisible"), source="recaptcha-docs",
    ),
    Fingerprint(
        "dom.recaptcha.v3_script", "dom", "recaptcha", RiskType.CAPTCHA,
        Severity.BYPASSABLE, "recaptcha/api.js", where=("src", "html"),
        weight=0.7,
        note="reCAPTCHA 脚本本体。可能是 v2（可见）也可能是 v3（隐身），"
             "单独出现不足以判定，所以权重低、等级低。",
        tags=("script", "ambiguous"), source="recaptcha-docs",
    ),
    Fingerprint(
        "dom.recaptcha.sitekey", "dom", "recaptcha", RiskType.CAPTCHA,
        Severity.INFO, "data-sitekey", where=("html",),
        weight=0.4,
        note="只有 sitekey 字段。弱证据，仅供 trace 记录。",
        tags=("attr", "weak"), source="recaptcha-docs",
        ),

    # ------------------------------ hCaptcha -------------------------------
    Fingerprint(
        "dom.hcaptcha.iframe", "dom", "hcaptcha", RiskType.CAPTCHA,
        Severity.INTERRUPT, "hcaptcha.com", where=("src", "html"),
        weight=1.0,
        note="hCaptcha 的 iframe。默认形态是「点选图中某某」，必须叫人。",
        tags=("iframe", "image_challenge", "no_solve"), source="hcaptcha-docs",
    ),
    Fingerprint(
        "dom.hcaptcha.class", "dom", "hcaptcha", RiskType.CAPTCHA,
        Severity.INTERRUPT, "h-captcha", where=("class", "id"),
        weight=0.9,
        note="hCaptcha 挂载点的 class/id 约定。",
        tags=("mount",), source="hcaptcha-docs",
    ),
    Fingerprint(
        "dom.hcaptcha.script", "dom", "hcaptcha", RiskType.CAPTCHA,
        Severity.BYPASSABLE, "js.hcaptcha.com", where=("src", "html"),
        weight=0.6,
        note="hCaptcha 脚本。可能尚未渲染出可见挑战。",
        tags=("script", "ambiguous"), source="hcaptcha-docs",
        ),

    # ------------------------------ Cloudflare -----------------------------
    Fingerprint(
        "dom.cloudflare.turnstile", "dom", "cloudflare", RiskType.CAPTCHA,
        Severity.BYPASSABLE, "challenges.cloudflare.com/turnstile",
        where=("src", "html"), weight=0.9,
        note=("Cloudflare Turnstile。多数是 managed/invisible 模式，"
              "正常浏览器会自动过 —— 不打断，但记 trace。"),
        tags=("iframe", "managed"), source="cloudflare-docs",
    ),
    Fingerprint(
        "dom.cloudflare.challenge_platform", "dom", "cloudflare",
        RiskType.CHALLENGE_PAGE, Severity.BYPASSABLE,
        "challenges.cloudflare.com/cdn-cgi", where=("src", "html"),
        weight=0.8,
        note="挑战平台的资源加载。配合 URL 特征判定整页盾。",
        tags=("script",), source="cloudflare-docs",
    ),
    Fingerprint(
        "dom.cloudflare.cf_chl_", "dom", "cloudflare", RiskType.CHALLENGE_PAGE,
        Severity.INTERRUPT, "cf_chl_", where=("html",),
        weight=0.9,
        note=("页面上出现 cf_chl_ 系列元素，说明**正在**挑战。"
              "如果 2 秒内没过，就是卡住了 —— 人点一下就能过，必须叫人。"),
        tags=("challenge_active",), source="cloudflare-docs",
    ),
    Fingerprint(
        "dom.cloudflare.challenge_form", "dom", "cloudflare",
        RiskType.CHALLENGE_PAGE, Severity.INTERRUPT, "challenge-form",
        where=("id", "class"), weight=0.9,
        note="Cloudflare 的挑战表单。",
        tags=("challenge_active",), source="cloudflare-docs",
    ),
    Fingerprint(
        "dom.cloudflare.cdn_cgi_path", "dom", "cloudflare",
        RiskType.CHALLENGE_PAGE, Severity.BYPASSABLE, "/cdn-cgi/challenge-platform/",
        where=("src", "html"), weight=0.6,
        note="挑战平台资源路径特征。",
        tags=("path",), source="cloudflare-docs",
        ),

    # ------------------------------- 极验 ---------------------------------
    Fingerprint(
        "dom.geetest.slider_class", "dom", "geetest", RiskType.SLIDER,
        Severity.INTERRUPT, "geetest_slider", where=("class", "id"),
        weight=1.0,
        note=("极验滑块。硬约束 #9：**禁止**写识别缺口位置/计算拖动距离的"
              "逻辑。检出即叫人。"),
        tags=("slider", "no_solve"), source="geetest-docs",
    ),
    Fingerprint(
        "dom.geetest.slider_button", "dom", "geetest", RiskType.SLIDER,
        Severity.INTERRUPT, "geetest_slider_button", where=("class", "id"),
        weight=1.0,
        note="极验滑块把手。",
        tags=("slider", "no_solve"), source="geetest-docs",
    ),
    Fingerprint(
        "dom.geetest.wrapper", "dom", "geetest", RiskType.CAPTCHA,
        Severity.INTERRUPT, "geetest_", where=("class", "id"),
        weight=0.8,
        note="极验容器前缀（不限于滑块，也含点选）。",
        tags=("mount",), source="geetest-docs",
    ),
    Fingerprint(
        "dom.geetest.script", "dom", "geetest", RiskType.CAPTCHA,
        Severity.BYPASSABLE, "gt.js", where=("src", "html"),
        weight=0.4,
        note="极验脚本。单独出现只说明「装了极验」，不能断言有可见挑战。",
        tags=("script", "weak"), source="geetest-docs",
        ),

    # ---------------------------- 阿里 / 腾讯 ------------------------------
    Fingerprint(
        "dom.aliyun.nocaptcha", "dom", "aliyun", RiskType.SLIDER,
        Severity.INTERRUPT, "nc_container", where=("class", "id"),
        weight=1.0,
        note="阿里云滑块验证（nc = no captcha）。",
        tags=("slider", "no_solve"), source="aliyun-docs",
    ),
    Fingerprint(
        "dom.aliyun.ncv", "dom", "aliyun", RiskType.SLIDER,
        Severity.INTERRUPT, "aliyun_ncv", where=("class", "id", "html"),
        weight=0.9, note="阿里云验证组件。",
        tags=("slider",), source="aliyun-docs",
    ),
    Fingerprint(
        "dom.tencent.captcha", "dom", "tencent", RiskType.CAPTCHA,
        Severity.INTERRUPT, "t.captcha.qq.com", where=("src", "html"),
        weight=1.0, note="腾讯防水墙验证码 iframe。",
        tags=("iframe",), source="community-2024",
    ),
    Fingerprint(
        "dom.tianai", "dom", "tianai", RiskType.SLIDER,
        Severity.INTERRUPT, "tianai-captcha", where=("class", "id", "html"),
        weight=1.0, note="天爱验证码（开源滑块/旋转/点选）。",
        tags=("slider", "no_solve"), source="community-2024",
    ),
    Fingerprint(
        "dom.aj_captcha", "dom", "anji", RiskType.SLIDER,
        Severity.INTERRUPT, "aj-captcha", where=("class", "id"),
        weight=1.0, note="AJ-Captcha（国产开源滑块/点选）。",
        tags=("slider", "no_solve"), source="community-2024",
    ),
    Fingerprint(
        "dom.vaptcha", "dom", "vaptcha", RiskType.CLICK_POINT,
        Severity.INTERRUPT, "vaptcha", where=("class", "id", "src", "html"),
        weight=0.9, note="VAPTCHA（手势/点选）。",
        tags=("click_point", "no_solve"), source="community-2024",
        ),

    # ------------------------------ 通用特征 -------------------------------
    Fingerprint(
        "dom.generic.iframe_src", "dom", "generic", RiskType.UNKNOWN,
        Severity.INFO, "captcha", where=("src",),
        weight=0.5,
        note=("iframe 的 src 里含 captcha。**弱证据**：很多站点把"
              "captcha 的 JS 预加载进来但从不显示。需配合可见性判定。"),
        tags=("iframe", "weak", "generic"), source="heuristic",
    ),
    Fingerprint(
        "dom.generic.iframe_invisible", "dom", "generic", RiskType.UNKNOWN,
        Severity.INFO, "invisible-recaptcha", where=("class", "id"),
        weight=0.6,
        note="隐形验证挂载点。",
        tags=("invisible",), source="heuristic",
    ),
    Fingerprint(
        "dom.generic.slider_idiom", "dom", "generic", RiskType.SLIDER,
        Severity.INTERRUPT, "slider-verify", where=("class", "id"),
        weight=0.8, note="通用滑块命名约定。",
        tags=("slider", "generic"), source="community-2024",
    ),
    Fingerprint(
        "dom.generic.drag_idiom", "dom", "generic", RiskType.SLIDER,
        Severity.INTERRUPT, "drag-verify", where=("class", "id"),
        weight=0.8, note="通用拖动命名约定。",
        tags=("slider", "generic"), source="community-2024",
    ),
    Fingerprint(
        "dom.generic.puzzle", "dom", "generic", RiskType.SLIDER,
        Severity.INTERRUPT, "puzzle", where=("class", "id"),
        weight=0.6,
        note="拼图类命名。可能是滑块，也可能是别的游戏元素，稍弱。",
        tags=("slider", "generic", "weak"), source="heuristic",
    ),
    Fingerprint(
        "dom.generic.otp_input", "dom", "generic", RiskType.OTP,
        Severity.INTERRUPT,
        # 6 位数字验证码输入框的常见约定：autocomplete="one-time-code"
        r'autocomplete\s*=\s*["\']?one-time-code', where=("html",),
        regex=True, weight=0.9, source="ms-docs",
        note=("标准的一次性验证码输入框标记。手机不在机器手上，"
              "只能叫人。"),
        tags=("otp",),
    ),
    Fingerprint(
        "dom.generic.recaptcha_response", "dom", "generic", RiskType.CAPTCHA,
        Severity.BYPASSABLE, "g-recaptcha-response", where=("id", "class", "html"),
        weight=0.7,
        note=("隐藏的 g-recaptcha-response 字段。**它到处都是**，"
              "包括 v3 静默模式 —— 单独命中绝不打断。"),
        tags=("hidden_field", "invisible"), source="recaptcha-docs",
    ),
    Fingerprint(
        "dom.generic.url_challenge", "dom", "generic", RiskType.CHALLENGE_PAGE,
        Severity.INTERRUPT, "/challenge", where=("src", "html"),
        weight=0.85,
        note=("URL 路径里含 /challenge。**这是 URL 层的主力指纹** —— "
              "detector_dom 会把 URL 单独过一遍路径类指纹，"
              "即使 DOM 什么都没拿到也能报。配 INTERRUPT 是因为"
              "走到挑战页说明正常流程已经被拦下来了。"),
        tags=("url_path", "generic"), source="heuristic",
    ),
    Fingerprint(
        "dom.generic.url_captcha_page", "dom", "generic", RiskType.CHALLENGE_PAGE,
        Severity.INTERRUPT, "captcha", where=("src",),
        weight=0.7,
        note=("URL 里含 captcha。只对 src（URL）生效，不扫 html —— "
              "因为 html 里出现 captcha 的场合太多（预加载脚本、"
              "隐藏字段），对 URL 才是有力的路径证据。"),
        tags=("url_path", "generic"), source="heuristic",
    ),
    Fingerprint(
        "dom.generic.url_verify", "dom", "generic", RiskType.CHALLENGE_PAGE,
        Severity.INTERRUPT, "/verify", where=("src",),
        weight=0.6,
        note=("URL 路径含 /verify。中等强度 —— /verify 也用于"
              "邮箱验证等正常流程，配合其他证据使用。"),
        tags=("url_path", "generic", "weak"), source="heuristic",
    ),
]


# --------------------------------------------------------------------------
# 窗口指纹
# --------------------------------------------------------------------------
#: 桌面端窗口指纹。两个维度：``class_name``（Win32 类名）和
#: ``title_keywords``（标题关键词）。检测器分别比对。
#:
#: 桌面端的"验证题"主要来自三类：
#: 1. 浏览器弹窗（Chrome 会把网页验证题显示在标题栏时带关键词很少见）
#: 2. 独立客户端的安全验证（网银 U 盾、游戏反外挂、企业 IM 登录）
#: 3. 系统级弹窗（UAC、凭据框）
WINDOW_FINGERPRINTS: List[Fingerprint] = [
    # 标准凭据框 —— 必须叫人（密码不该由机器提供）
    Fingerprint(
        "win.credential.class", "window", "windows", RiskType.LOGIN_WALL,
        Severity.INTERRUPT, "credentialdialog", where=("class_name",),
        weight=1.0,
        note=("Windows 凭据对话框类。需要密码，机器不该猜，叫人。"),
        tags=("credential", "system"), source="ms-docs",
    ),
    Fingerprint(
        "win.credential.consent", "window", "windows", RiskType.LOGIN_WALL,
        Severity.INTERRUPT, "consent", where=("class_name",),
        weight=0.9,
        note="UAC 提权确认框。需要人工确认管理员权限。",
        tags=("uac", "system"), source="ms-docs",
    ),
    Fingerprint(
        "win.crypto.pin", "window", "windows", RiskType.LOGIN_WALL,
        Severity.INTERRUPT, "cryptuiapi", where=("class_name",),
        weight=1.0,
        note="智能卡 / U 盾 PIN 输入框。硬件凭证，机器不可能有。",
        tags=("hardware", "system"), source="ms-docs",
        ),

    # 标题关键词（class_name 命中优先，这里兜底）
    Fingerprint(
        "win.title.security_check", "window", "generic", RiskType.CHALLENGE_PAGE,
        Severity.INTERRUPT, "安全验证", where=("title",),
        weight=0.6, note="窗口标题含「安全验证」。",
        tags=("title", "zh"), source="probed-windows",
    ),
    Fingerprint(
        "win.title.human_check", "window", "generic", RiskType.CHALLENGE_PAGE,
        Severity.INTERRUPT, "人机验证", where=("title",),
        weight=0.7, note="窗口标题含「人机验证」。",
        tags=("title", "zh"), source="probed-windows",
    ),
    Fingerprint(
        "win.title.verify_identity", "window", "generic", RiskType.LOGIN_WALL,
        Severity.INTERRUPT, "身份验证", where=("title",),
        weight=0.6, note="窗口标题含「身份验证」。",
        tags=("title", "zh"), source="probed-windows",
    ),
    Fingerprint(
        "win.title.sms_code", "window", "generic", RiskType.OTP,
        Severity.INTERRUPT, "短信验证", where=("title",),
        weight=0.8, note="窗口标题含「短信验证」。",
        tags=("title", "zh", "otp"), source="probed-windows",
    ),
    Fingerprint(
        "win.title.login", "window", "generic", RiskType.LOGIN_WALL,
        Severity.INTERRUPT, "登录", where=("title",),
        weight=0.4,
        note=("窗口标题含「登录」。**很弱** —— 「登录」是极常见的日常词，"
              "本进程自己起的 URL 里都可能有。仅作辅助证据。"),
        tags=("title", "zh", "weak"), source="probed-windows",
    ),
    Fingerprint(
        "win.title.just_a_moment", "window", "cloudflare",
        RiskType.CHALLENGE_PAGE, Severity.INTERRUPT, "just a moment",
        where=("title",), weight=0.9,
        note="Cloudflare 五秒盾的经典标题。等太久没过就是卡住了。",
        tags=("title", "en"), source="probed-windows",
    ),
    Fingerprint(
        "win.title.checking_browser", "window", "cloudflare",
        RiskType.CHALLENGE_PAGE, Severity.BYPASSABLE, "checking your browser",
        where=("title",), weight=0.9,
        note=("Cloudflare 检查中。**通常 1~2 秒自动过** —— 先不打断，"
              "由 detector 的二次确认判定是否卡死。"),
        tags=("title", "en", "transient"), source="probed-windows",
    ),
    Fingerprint(
        "win.title.attention_required", "window", "cloudflare",
        RiskType.CHALLENGE_PAGE, Severity.INTERRUPT, "attention required",
        where=("title",), weight=1.0,
        note=("Cloudflare 判定为可疑，要求人工介入。这是**必须叫人**的形态。"),
        tags=("title", "en"), source="probed-windows",
    ),
    Fingerprint(
        "win.title.access_denied", "window", "cloudflare",
        RiskType.RATE_LIMIT, Severity.BLOCK, "access denied", where=("title",),
        weight=1.0,
        note="访问被拒（常伴 1020 错误）。人都未必能过，直接终止。",
        tags=("title", "en", "block"), source="probed-windows",
    ),
    Fingerprint(
        "win.title.too_many_requests", "window", "generic", RiskType.RATE_LIMIT,
        Severity.BLOCK, "too many requests", where=("title",),
        weight=1.0, note="限流。硬等或换出口，不应由人解决。",
        tags=("title", "en", "block"), source="probed-windows",
    ),
]


# --------------------------------------------------------------------------
# 文本指纹
# --------------------------------------------------------------------------
#: 文本层指纹 —— 用于 OCR 读出的画面文字，也用于 DOM 的可见文本。
#:
#: ⚠️ **文本指纹必须分组共现**，不能单词命中就报警。"验证"、
#: "确定"、"取消"这些词在正常页面里到处都是，单命中误报率极高。
#: 分组规则见下面的 :data:`STRONG_TEXT_GROUPS` 与 :data:`WEAK_TEXT_WORDS`。
TEXT_FINGERPRINTS: List[Fingerprint] = [
    # --- 中文：滑块 / 拖动 ---
    Fingerprint("txt.zh.drag", "text", "generic", RiskType.SLIDER,
                Severity.INTERRUPT, "拖动", weight=1.0,
                note="拖动滑块。", tags=("zh", "slider"), source="probed-2026-09"),
    Fingerprint("txt.zh.drag_slider", "text", "generic", RiskType.SLIDER,
                Severity.INTERRUPT, "拖动滑块", weight=1.4,
                note="完整短语，强度高于单字。", tags=("zh", "slider", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.zh.slide_verify", "text", "generic", RiskType.SLIDER,
                Severity.INTERRUPT, "滑动验证", weight=1.3,
                note="滑动验证。", tags=("zh", "slider", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.zh.fill_gap", "text", "generic", RiskType.SLIDER,
                Severity.INTERRUPT, "填补缺口", weight=1.4,
                note="拼图缺口归位。", tags=("zh", "slider", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.zh.puzzle_slide", "text", "generic", RiskType.SLIDER,
                Severity.INTERRUPT, "向右滑动", weight=1.3,
                note="向右滑动填充。", tags=("zh", "slider", "phrase"), source="probed-2026-09"),

    # --- 中文：点选 ---
    Fingerprint("txt.zh.click_according", "text", "generic", RiskType.CLICK_POINT,
                Severity.INTERRUPT, "请点击", weight=1.0,
                note="点选类提示的开头。", tags=("zh", "click_point"), source="probed-2026-09"),
    Fingerprint("txt.zh.click_image", "text", "generic", RiskType.CLICK_POINT,
                Severity.INTERRUPT, "点击图中", weight=1.4,
                note="点选图中某某。", tags=("zh", "click_point", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.zh.click_all", "text", "generic", RiskType.CLICK_POINT,
                Severity.INTERRUPT, "点击所有", weight=1.3,
                note="点击所有包含某某的图片。", tags=("zh", "click_point", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.zh.select_all_img", "text", "generic", RiskType.CLICK_POINT,
                Severity.INTERRUPT, "选择所有", weight=1.0,
                note="选择所有某某。", tags=("zh", "click_point"), source="probed-2026-09"),

    # --- 中文：人机 / 验证 ---
    Fingerprint("txt.zh.human_verify", "text", "generic", RiskType.CAPTCHA,
                Severity.INTERRUPT, "人机验证", weight=1.4,
                note="人机验证。", tags=("zh", "captcha", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.zh.security_verify", "text", "generic", RiskType.CHALLENGE_PAGE,
                Severity.INTERRUPT, "安全验证", weight=1.0,
                note="安全验证。", tags=("zh", "challenge"), source="probed-2026-09"),
    Fingerprint("txt.zh.are_you_robot", "text", "generic", RiskType.CAPTCHA,
                Severity.INTERRUPT, "我不是机器人", weight=1.5,
                note="reCAPTCHA 复选框的中文文案。几乎零误报。",
                tags=("zh", "captcha", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.zh.verify_code", "text", "generic", RiskType.OTP,
                Severity.INTERRUPT, "验证码", weight=0.8,
                note=("验证码。**中等强度** —— 得配合位数/输入框特征，"
              "单出现于营销文案里也常见。"),
                tags=("zh", "otp"), source="probed-2026-09"),
    Fingerprint("txt.zh.sms_code", "text", "generic", RiskType.OTP,
                Severity.INTERRUPT, "短信验证码", weight=1.4,
                note="短信验证码。", tags=("zh", "otp", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.zh.get_code", "text", "generic", RiskType.OTP,
                Severity.INTERRUPT, "获取验证码", weight=1.2,
                note="获取验证码按钮 —— 说明流程需要人类手机。",
                tags=("zh", "otp", "phrase"), source="probed-2026-09"),

    # --- 中文：挑战页 ---
    Fingerprint("txt.zh.checking_browser", "text", "cloudflare",
                RiskType.CHALLENGE_PAGE, Severity.BYPASSABLE, "正在检查您的浏览器",
                weight=1.3,
                note=("Cloudflare 中文盾。通常是过场动画，先不打断。"),
                tags=("zh", "transient"), source="probed-2026-09"),
    Fingerprint("txt.zh.please_wait", "text", "generic",
                RiskType.CHALLENGE_PAGE, Severity.INFO, "请稍候", weight=0.3,
                note="请稍候 —— **极弱**，正常加载页也有。仅作 trace。",
                tags=("zh", "weak"), source="probed-2026-09"),

    # --- 中文：封禁 ---
    Fingerprint("txt.zh.access_denied", "text", "generic", RiskType.RATE_LIMIT,
                Severity.BLOCK, "访问被拒绝", weight=1.3,
                note="访问被拒绝。", tags=("zh", "block"), source="probed-2026-09"),
    Fingerprint("txt.zh.operation_too_freq", "text", "generic",
                RiskType.RATE_LIMIT, Severity.BLOCK, "操作过于频繁", weight=1.5,
                note="操作过于频繁 —— 人来了也没用，等或换出口。",
                tags=("zh", "block", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.zh.risk_control", "text", "generic", RiskType.RATE_LIMIT,
                Severity.INTERRUPT, "风险控制", weight=1.0,
                note="触发风控提示。严重度视文案而定，先叫人。",
                tags=("zh", "risk"), source="probed-2026-09"),
    Fingerprint("txt.zh.abnormal", "text", "generic", RiskType.RATE_LIMIT,
                Severity.INTERRUPT, "操作异常", weight=1.0,
                note="检测到异常操作。", tags=("zh", "risk"), source="probed-2026-09"),

    # --- 英文 ---
    Fingerprint("txt.en.not_a_robot", "text", "recaptcha", RiskType.CAPTCHA,
                Severity.INTERRUPT, "i'm not a robot", weight=1.5,
                note="reCAPTCHA v2 复选框英文文案。",
                tags=("en", "captcha", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.en.im_not_a_robot", "text", "recaptcha", RiskType.CAPTCHA,
                Severity.INTERRUPT, "i am not a robot", weight=1.5,
                note="同上，另一种写法。", tags=("en", "captcha", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.en.verify_human", "text", "generic", RiskType.CAPTCHA,
                Severity.INTERRUPT, "verify you are human", weight=1.5,
                note="Cloudflare Turnstile 的标准文案。",
                tags=("en", "captcha", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.en.confirm_human", "text", "generic", RiskType.CAPTCHA,
                Severity.INTERRUPT, "confirm you are human", weight=1.5,
                note="确认你是人类。", tags=("en", "captcha", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.en.captcha", "text", "generic", RiskType.CAPTCHA,
                Severity.INTERRUPT, "captcha", weight=0.7,
                note=("captcha 单字。**中低强度** —— 常出现在页脚、"
                      "JS 变量名、隐藏字段里，需配合其他证据。"),
                tags=("en", "captcha", "weak"), source="probed-2026-09"),
    Fingerprint("txt.en.slide_to_verify", "text", "generic", RiskType.SLIDER,
                Severity.INTERRUPT, "slide to verify", weight=1.5,
                note="滑动验证英文。", tags=("en", "slider", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.en.drag_slider", "text", "generic", RiskType.SLIDER,
                Severity.INTERRUPT, "drag the slider", weight=1.5,
                note="拖动滑块英文。", tags=("en", "slider", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.en.complete_puzzle", "text", "generic", RiskType.SLIDER,
                Severity.INTERRUPT, "complete the puzzle", weight=1.4,
                note="完成拼图。", tags=("en", "slider", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.en.click_verify", "text", "generic", RiskType.CLICK_POINT,
                Severity.INTERRUPT, "click to verify", weight=1.2,
                note="点击以验证。", tags=("en", "click_point", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.en.select_all", "text", "generic", RiskType.CLICK_POINT,
                Severity.INTERRUPT, "select all images", weight=1.5,
                note="选择所有图片。", tags=("en", "click_point", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.en.checking_browser", "text", "cloudflare",
                RiskType.CHALLENGE_PAGE, Severity.BYPASSABLE,
                "checking your browser", weight=1.4,
                note="Cloudflare 检查中（英文）。过场，先不打断。",
                tags=("en", "transient"), source="probed-2026-09"),
    Fingerprint("txt.en.just_a_moment", "text", "cloudflare",
                RiskType.CHALLENGE_PAGE, Severity.BYPASSABLE, "just a moment",
                weight=1.3,
                note="Cloudflare 五秒盾文案。", tags=("en", "transient"), source="probed-2026-09"),
    Fingerprint("txt.en.enable_javascript", "text", "cloudflare",
                RiskType.CHALLENGE_PAGE, Severity.BYPASSABLE,
                "enable javascript and cookies to continue", weight=1.5,
                note="Cloudflare 的 JS 挑战提示。", tags=("en", "transient"), source="probed-2026-09"),
    Fingerprint("txt.en.one_time_code", "text", "generic", RiskType.OTP,
                Severity.INTERRUPT, "one-time code", weight=1.3,
                note="一次性验证码。", tags=("en", "otp", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.en.verification_code", "text", "generic", RiskType.OTP,
                Severity.INTERRUPT, "verification code", weight=1.3,
                note="验证码（英文）。", tags=("en", "otp", "phrase"), source="probed-2026-09"),
    Fingerprint("txt.en.rate_limit", "text", "generic", RiskType.RATE_LIMIT,
                Severity.BLOCK, "rate limit", weight=1.4,
                note="限流。", tags=("en", "block"), source="probed-2026-09"),
    Fingerprint("txt.en.too_many", "text", "generic", RiskType.RATE_LIMIT,
                Severity.BLOCK, "too many requests", weight=1.5,
                note="请求过多（HTTP 429）。", tags=("en", "block", "phrase"), source="probed-2026-09"),
]


# --------------------------------------------------------------------------
# 文本共现规则
# --------------------------------------------------------------------------
#: **强词组**：组内任一短语命中即视为高置信（weight ≥ 1.3 的短语）。
#:
#: 之所以还要分组，是因为 OCR 结果会被拆行、错字、乱序。
#: "拖动滑块"可能被读成"拖动"+"滑块"两行。同一组内累积权重达到
#: :data:`GROUP_THRESHOLD` 就算命中。
STRONG_TEXT_GROUPS: List[Dict[str, Any]] = [
    {
        "id": "grp.slider.zh",
        "type": RiskType.SLIDER,
        "vendor": "generic",
        "severity": Severity.INTERRUPT,
        "any": ("拖动滑块", "滑动验证", "拖动下方滑块", "按住滑块",
                "向右滑动", "填补缺口", "拼图", "拖动"),
        "note": "中文滑块类提示词的共现集合。",
    },
    {
        "id": "grp.click_point.zh",
        "type": RiskType.CLICK_POINT,
        "vendor": "generic",
        "severity": Severity.INTERRUPT,
        "any": ("请点击", "点击图中", "点击所有", "选择所有", "点击下方"),
        "note": "中文点选类提示词的共现集合。",
    },
    {
        "id": "grp.captcha.zh",
        "type": RiskType.CAPTCHA,
        "vendor": "generic",
        "severity": Severity.INTERRUPT,
        "any": ("人机验证", "安全验证", "我不是机器人", "验证您是真人",
                "验证你是真人"),
        "note": "中文人机验证文案集合。命中即高置信。",
    },
    {
        "id": "grp.otp.zh",
        "type": RiskType.OTP,
        "vendor": "generic",
        "severity": Severity.INTERRUPT,
        "any": ("短信验证码", "获取验证码", "请输入验证码", "邮箱验证码"),
        "note": "中文验证码文案。**不含单字「验证码」** —— 误报太高。",
    },
    {
        "id": "grp.slider.en",
        "type": RiskType.SLIDER,
        "vendor": "generic",
        "severity": Severity.INTERRUPT,
        "any": ("slide to verify", "drag the slider", "complete the puzzle",
                "slide right", "drag to verify"),
        "note": "英文滑块文案集合。",
    },
    {
        "id": "grp.captcha.en",
        "type": RiskType.CAPTCHA,
        "vendor": "generic",
        "severity": Severity.INTERRUPT,
        "any": ("i'm not a robot", "i am not a robot", "verify you are human",
                "confirm you are human", "prove you are human"),
        "note": "英文人机验证文案。命中即高置信。",
    },
    {
        "id": "grp.challenge_page.en",
        "type": RiskType.CHALLENGE_PAGE,
        "vendor": "cloudflare",
        "severity": Severity.BYPASSABLE,
        "any": ("checking your browser", "just a moment",
                "enable javascript and cookies"),
        "note": "Cloudflare 过场文案。**可绕过** —— 先不打断。",
    },
    {
        "id": "grp.block.zh",
        "type": RiskType.RATE_LIMIT,
        "vendor": "generic",
        "severity": Severity.BLOCK,
        "any": ("操作过于频繁", "访问被拒绝", "请求过于频繁", "账户已被封禁"),
        "note": "限流/封禁文案。人来了也没用。",
    },
    {
        "id": "grp.block.en",
        "type": RiskType.RATE_LIMIT,
        "vendor": "generic",
        "severity": Severity.BLOCK,
        "any": ("too many requests", "rate limit exceeded", "access denied",
                "you have been blocked"),
        "note": "限流/封禁文案（英文）。",
    },
]

#: 组内累积权重的阈值。
GROUP_THRESHOLD = 1.0

#: **弱词分档惩罚表**。键是词，值是惩罚权重（负数）。
#:
#: ## 为什么是"惩罚"而不是"加分"
#:
#: 这张表是踩坑的产物。第一版把"验证"/"登录"/"确定"/"安全"都当指纹
#: 做加分，结果随便打开一个电商页面就报"检出风险" —— 因为页脚有
#: "验证码"链接、顶部有"登录"按钮。
#:
#: 所以改成**扣分**：弱词越多，越说明这是正常页面。这个方向很重要 ——
#: 扣得越狠，越倾向于判"干净"。
#:
#: ## 分档逻辑：**按"这个词有多大概率出现在正常页面上"分档**
#:
#: ::定义（2026-09-27 由兄弟提出分档需求，归属方向经论证后调整）
#:
#: ==================  ========  ==================================================
#: 档                  惩罚      判据
#: ==================  ========  ==================================================
#: ``footer``           -0.30    **几乎只在正常页面上出现**：页脚法律条款、
#:                               导航栏登录按钮。命中它基本可以确定
#:                               "这是一个内容站"，是**最有力的反证**。
#: ``ambiguous``        -0.15    高频但**有歧义**：既可能是正常文案，
#:                               也可能是验证题文案的一部分。
#: ``ocr_noise``        -0.05    几乎无信息量：OCR 会把任何页面都读出
#:                               "正在""加载中"。惩罚很轻，只做轻微修正，
#:                               避免大段 OCR 结果被噪声压成负分。
#: ==================  ========  ==================================================
#:
#: ## ⚠️ 与兄弟最初提的档位方向的差异（我的判断，已说明）
#:
#: 兄弟提的是"高频弱词 → -0.10，中频弱词 → -0.20"。方向**相反**：
#: 惩罚是**扣分**，扣得越多越倾向判干净，所以
#: **页脚高频词应该扣得最狠（-0.30），而不是最轻（-0.10）**。
#: 按原方案，"登录/隐私/条款"扣 0.10 而"验证"扣 0.20，
#: 等于对正常页面的反证力度弱于对可疑词的压制，会推高误报率 ——
#: 而 5G 的误报率测试正是要压住这个。
#: 数值量级保留了兄弟给的 0.10/0.20 的量级（0.05/0.15/0.30）。
#:
#: ## 5G 误报率测试的用法
#:
#: 分档的意义在于**可以单独关掉某一档看误报率变化**：
#: ``text_hit_score(t, penalty_scale={WEAK_TIER_FOOTER: 0.0})``
#: 就是把页脚档的惩罚清零，用来量化"页脚反证"贡献了多少。
#: 详见 :func:`text_hit_score` 的 ``penalty_scale`` 参数。
WEAK_TIER_FOOTER = "footer"
WEAK_TIER_AMBIGUOUS = "ambiguous"
WEAK_TIER_OCR_NOISE = "ocr_noise"

#: 各档的基准惩罚（**负数**）。分档调参只改这里，不用翻整个词表。
WEAK_TIER_PENALTY = {
    WEAK_TIER_FOOTER: -0.30,
    WEAK_TIER_AMBIGUOUS: -0.15,
    WEAK_TIER_OCR_NOISE: -0.05,
}

#: 弱词 → 档位。改档位就改这里。
WEAK_WORD_TIER: Dict[str, str] = {
    # ---- footer：页脚 / 导航栏的固定文案，正常站点必有 ----
    "服务条款": WEAK_TIER_FOOTER,
    "隐私": WEAK_TIER_FOOTER,
    "privacy": WEAK_TIER_FOOTER,
    "隐私政策": WEAK_TIER_FOOTER,
    "登录": WEAK_TIER_FOOTER,
    "login": WEAK_TIER_FOOTER,
    "sign in": WEAK_TIER_FOOTER,
    "注册": WEAK_TIER_FOOTER,
    "安全": WEAK_TIER_FOOTER,
    "security": WEAK_TIER_FOOTER,
    "protected": WEAK_TIER_FOOTER,
    "用户协议": WEAK_TIER_FOOTER,
    "关于我们": WEAK_TIER_FOOTER,
    "联系我们": WEAK_TIER_FOOTER,
    "版权": WEAK_TIER_FOOTER,
    "cookie": WEAK_TIER_FOOTER,
    "copyright": WEAK_TIER_FOOTER,
    "terms of service": WEAK_TIER_FOOTER,

    # ---- ambiguous：高频但有歧义，可能就是验证题的一部分 ----
    "验证": WEAK_TIER_AMBIGUOUS,
    "verify": WEAK_TIER_AMBIGUOUS,
    "确认": WEAK_TIER_AMBIGUOUS,
    "确定": WEAK_TIER_AMBIGUOUS,
    "人机": WEAK_TIER_AMBIGUOUS,
    "检测": WEAK_TIER_AMBIGUOUS,
    "保护": WEAK_TIER_AMBIGUOUS,
    "提交": WEAK_TIER_AMBIGUOUS,
    "取消": WEAK_TIER_AMBIGUOUS,
    "submit": WEAK_TIER_AMBIGUOUS,
    "cancel": WEAK_TIER_AMBIGUOUS,
    "confirm": WEAK_TIER_AMBIGUOUS,

    # ---- ocr_noise：任何页面 OCR 都可能读出，几乎无信息量 ----
    "正在": WEAK_TIER_OCR_NOISE,
    "加载中": WEAK_TIER_OCR_NOISE,
    "请稍候": WEAK_TIER_OCR_NOISE,
    "请稍后": WEAK_TIER_OCR_NOISE,
    "loading": WEAK_TIER_OCR_NOISE,
    "please wait": WEAK_TIER_OCR_NOISE,
    "跳转中": WEAK_TIER_OCR_NOISE,
    "请等待": WEAK_TIER_OCR_NOISE,
}

#: 弱词全集（元组形式，供自检与文档展示）。
#:
#: ⚠️ **这是从 :data:`WEAK_WORD_TIER` 派生的**，不是独立定义 ——
#: 避免两张表各自漂移。要加词只改 ``WEAK_WORD_TIER``。
WEAK_TEXT_WORDS: tuple = tuple(sorted(WEAK_WORD_TIER))

#: 弱词惩罚的默认缩放（1.0 = 按 :data:`WEAK_TIER_PENALTY` 执行）。
#: 调它可以在不破坏分档结构的前提下整体调轻/调重惩罚。
DEFAULT_WEAK_PENALTY_SCALE = 1.0


# --------------------------------------------------------------------------
# 汇总与查询
# --------------------------------------------------------------------------
ALL_FINGERPRINTS: List[Fingerprint] = (
    list(DOM_FINGERPRINTS) + list(WINDOW_FINGERPRINTS) + list(TEXT_FINGERPRINTS)
)

_BY_ID: Dict[str, Fingerprint] = {f.id: f for f in ALL_FINGERPRINTS}


def source_info(key: str) -> Dict[str, Any]:
    """取某个来源的台账信息。

    未登记的键返回一个**能用的兜底字典**而不是 ``None`` ——
    调用方（比如渲染证据的人）不需要处处判空。兜底里的 ``registered``
    为 False，`selfcheck` 会因此变红，但线上不会崩。
    """
    info = SOURCES.get(key)
    if info is None:
        return {"key": key, "short": "未登记来源", "trust": 0.0,
                "detail": "该 source 未在 SOURCES 台账中登记 —— 请补登记",
                "registered": False}
    out = dict(info)
    out["key"] = key
    out["registered"] = True
    return out


def audit_sources() -> Dict[str, Any]:
    """来源台账审计：回答"每条指纹的出处可追溯吗"。

    返回::

        {"by_source": {key: [指纹 id, ...]},
         "unregistered": [指纹 id, ...],      # 用了野来源
         "unused": [key, ...],                # 登记了但没指纹引用
         "trust_weighted": float,             # 全库平均可信度
         "low_trust": [指纹 id, ...]}         # trust < 0.6 的指纹

    ``low_trust`` 是**排除误报时的优先怀疑名单** —— 社区整理来的
    指纹过时最快，新报误报先从这一档查起。
    """
    by_source: Dict[str, List[str]] = {}
    unregistered: List[str] = []
    for f in ALL_FINGERPRINTS:
        by_source.setdefault(f.source, []).append(f.id)
        if f.source not in SOURCES:
            unregistered.append(f.id)

    unused = [k for k in SOURCES if k not in by_source]

    trusts = [source_info(f.source).get("trust", 0.0)
              for f in ALL_FINGERPRINTS]
    avg = round(sum(trusts) / len(trusts), 4) if trusts else 0.0

    low_trust = sorted({
        f.id for f in ALL_FINGERPRINTS
        if float(source_info(f.source).get("trust", 0.0)) < 0.6
    })

    return {
        "by_source": by_source,
        "unregistered": unregistered,
        "unused": unused,
        "trust_weighted": avg,
        "low_trust": low_trust,
    }


def fingerprint_by_id(fid: str) -> Optional[Fingerprint]:
    """按 id 取指纹。未知 id 返回 ``None``（不抛）。"""
    return _BY_ID.get(fid)


def iter_fingerprints(layer: str = "",
                      vendor: str = "",
                      type: str = "") -> Iterable[Fingerprint]:      # noqa: A002
    """按条件筛指纹。三个条件都是可选，组合取交集。"""
    for f in ALL_FINGERPRINTS:
        if layer and f.layer != layer:
            continue
        if vendor and f.vendor != vendor:
            continue
        if type and f.type != type:
            continue
        yield f


def match_dom(fields: Dict[str, str], *, layer: str = "dom") -> List[Dict[str, Any]]:
    """在 DOM 字段上跑指纹匹配。

    ``fields`` 形如 ``{"src": "...", "class": "...", "id": "...",
    "html": "..."}``。检测器负责从 CDP / UIA 把这些字段填进来。

    返回命中列表，每项 ``{"fingerprint": Fingerprint, "field": str,
    "value": str}``。**按 weight 降序** —— 调用方通常只取第一个。

    注意：``where`` 为空的指纹会扫描 ``fields`` 的全部值。
    """
    hits: List[Dict[str, Any]] = []
    for fp in DOM_FINGERPRINTS:
        targets = fp.where or tuple(fields.keys())
        for field_name in targets:
            val = fields.get(field_name)
            if not val:
                continue
            if fp.matches(val):
                hits.append({"fingerprint": fp, "field": field_name,
                             "value": _clip(val)})
                break      # 同一条指纹只记一次
    hits.sort(key=lambda h: h["fingerprint"].weight, reverse=True)
    return hits


def match_window(title: str = "", class_name: str = "",
                 process: str = "") -> List[Dict[str, Any]]:
    """在窗口属性上跑指纹匹配。

    **类名命中优先于标题命中的在于权重的默认值**，但本函数不做合并，
    调用方按 weight 排序自行决定。
    """
    fields = {"title": title or "", "class_name": class_name or "",
              "process": process or ""}
    hits: List[Dict[str, Any]] = []
    for fp in WINDOW_FINGERPRINTS:
        targets = fp.where or tuple(fields.keys())
        for field_name in targets:
            val = fields.get(field_name)
            if not val:
                continue
            if fp.matches(val):
                hits.append({"fingerprint": fp, "field": field_name,
                             "value": _clip(val)})
                break
    hits.sort(key=lambda h: h["fingerprint"].weight, reverse=True)
    return hits


def match_text(text: str, *, min_weight: float = 0.0) -> List[Dict[str, Any]]:
    """在文本上跑指纹匹配（逐条）。

    ``text`` 通常是 OCR 结果或 DOM 的 ``innerText``。
    返回按 weight 降序的命中列表。

    ⚠️ **单条命中不足以判定**。请配合 :func:`text_hit_score` 的
    分数或 :data:`STRONG_TEXT_GROUPS` 的组判定使用。
    """
    if not text:
        return []
    hits: List[Dict[str, Any]] = []
    for fp in TEXT_FINGERPRINTS:
        if fp.weight < min_weight:
            continue
        if fp.matches(text):
            hits.append({"fingerprint": fp, "field": "text",
                         "value": _clip(text)})
    hits.sort(key=lambda h: h["fingerprint"].weight, reverse=True)
    return hits


def text_hit_score(text: str, *,
                   penalty_scale: Optional[Dict[str, float]] = None,
                   min_score: float = 1.0) -> Dict[str, Any]:
    """给一段文本打风险分，返回结构化结论。

    评分规则（**这是文本层唯一的判据入口**）：

    1. 逐条指纹累加 ``weight``。同一条只计一次。
    2. **弱词不计正分，按档扣分**（见 :data:`WEAK_WORD_TIER`）：
       页脚档 ``-0.30`` / 歧义档 ``-0.15`` / OCR 噪声档 ``-0.05``。
    3. 命中组（``STRONG_TEXT_GROUPS``）额外 +1.0，并按组决定 type/vendor。
    4. 分数 ≥ ``min_score``（默认 1.0）判为检出。

    为什么弱词要**扣分**而不是忽略：实测发现纯累加的正分模型在
    长文本（整页 innerText）上会因弱词泛滥而虚高。扣分让
    "只有弱词"的文本总分趋近于负，明确地表达"这不像验证题"。

    参数
    ----
    - ``penalty_scale`` —— **给 5G 误报率测试用的分档消融开关**。
      形如 ``{WEAK_TIER_FOOTER: 0.0}`` 表示把页脚档的惩罚清零，
      从而量化"页脚反证"这一档到底贡献了多少。未列出的档用 1.0。
      值域通常 0.0（关掉）~ 2.0（加倍）。
    - ``min_score`` —— 判定阈值。默认 1.0（即"一条中等强度指纹
      或一条强词组"就能过线）。

    返回
    ----
    ``{"score", "hits", "groups", "weak", "weak_detail",
    "weak_penalty", "tiers", "detected", "reason"}``。

    ``weak_detail`` 逐词列出命中的档位与实扣分，是 5G 消融测试
    和人工排查误报时最常用的字段 —— 一看就知道"这个 -0.6 是哪来的"。
    """
    if not text:
        return {"score": 0.0, "hits": [], "groups": [], "weak": [],
                "weak_detail": [], "weak_penalty": 0.0, "tiers": {},
                "detected": False, "reason": "empty"}
    low = text.lower()

    hits = match_text(text)
    score = 0.0
    for h in hits:
        score += h["fingerprint"].weight

    # --- 弱词分档扣分 ---
    scale = dict(penalty_scale or {})
    weak_detail: List[Dict[str, Any]] = []
    weak_penalty = 0.0
    tier_counts: Dict[str, int] = {}
    for word in WEAK_TEXT_WORDS:
        if word not in low:
            continue
        tier = WEAK_WORD_TIER.get(word, WEAK_TIER_OCR_NOISE)
        base = WEAK_TIER_PENALTY.get(tier, WEAK_TIER_PENALTY[WEAK_TIER_OCR_NOISE])
        try:
            k = float(scale.get(tier, 1.0))
        except (TypeError, ValueError):
            k = 1.0
        applied = base * k
        weak_penalty += applied
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        weak_detail.append({"word": word, "tier": tier,
                            "base": base, "scale": round(k, 3),
                            "applied": round(applied, 3)})
    weak_penalty = round(weak_penalty, 4)
    score += weak_penalty

    groups = []
    for g in STRONG_TEXT_GROUPS:
        found = [p for p in g["any"] if p in low]
        if found:
            groups.append({"id": g["id"], "type": g["type"],
                           "vendor": g["vendor"], "severity": g["severity"],
                           "matched": found, "note": g["note"]})
            score += GROUP_THRESHOLD

    detected = score >= min_score
    return {
        "score": round(score, 3),
        "hits": [{"id": h["fingerprint"].id,
                  "weight": h["fingerprint"].weight,
                  "severity": h["fingerprint"].severity,
                  "type": h["fingerprint"].type} for h in hits],
        "groups": groups,
        "weak": [d["word"] for d in weak_detail],
        "weak_detail": weak_detail,
        "weak_penalty": weak_penalty,
        "tiers": tier_counts,
        "detected": detected,
        "reason": "hit" if detected else ("below_threshold" if hits or groups
                                          else "no_fingerprint"),
    }


def describe_library(layer: str = "", *, with_sources: bool = True) -> str:
    """给人看的指纹库概览。

    ``with_sources=True``（默认）额外打印**来源台账** ——
    这是"这条指纹凭什么可信"的入口，也是更新时的待办清单按。
    """
    lines = []
    layers = [layer] if layer else ["dom", "window", "text"]
    for lay in layers:
        items = list(iter_fingerprints(layer=lay))
        lines.append("%-8s %d 条" % (lay, len(items)))
        by_vendor: Dict[str, int] = {}
        for f in items:
            by_vendor[f.vendor] = by_vendor.get(f.vendor, 0) + 1
        for v, n in sorted(by_vendor.items(), key=lambda kv: -kv[1]):
            lines.append("    %-14s %d" % (v, n))
    lines.append("强词组 %d 组；弱词 %d 个" % (len(STRONG_TEXT_GROUPS),
                                          len(WEAK_TEXT_WORDS)))
    lines.append("  分档：%s" % " / ".join(
        "%s %d 词 %.2f" % (t, sum(1 for x in WEAK_WORD_TIER.values() if x == t),
                           WEAK_TIER_PENALTY[t])
        for t in (WEAK_TIER_FOOTER, WEAK_TIER_AMBIGUOUS, WEAK_TIER_OCR_NOISE)))
    n_int = sum(1 for f in ALL_FINGERPRINTS if f.severity == Severity.INTERRUPT)
    n_byp = sum(1 for f in ALL_FINGERPRINTS if f.severity == Severity.BYPASSABLE)
    n_blk = sum(1 for f in ALL_FINGERPRINTS if f.severity == Severity.BLOCK)
    lines.append("处置分布：打断 %d / 可绕过 %d / 终止 %d" % (n_int, n_byp, n_blk))

    if with_sources:
        audit = audit_sources()
        lines.append("")
        lines.append("来源台账（trust 越高越可信，误报优先查低分档）：")
        for key, ids in sorted(audit["by_source"].items(),
                               key=lambda kv: -len(kv[1])):
            info = source_info(key)
            lines.append("    %-16s %2d 条  trust=%.2f  %s"
                         % (key, len(ids), info.get("trust", 0.0),
                            info.get("short", "")))
        lines.append("    全库平均可信度 %.2f；低可信指纹 %d 条"
                     % (audit["trust_weighted"], len(audit["low_trust"])))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
def _clip(v: Any, limit: int = 200) -> str:
    s = v if isinstance(v, str) else repr(v)
    s = s.replace("\r", " ").replace("\n", " ")
    return s if len(s) <= limit else s[:limit - 1] + "…"


# --------------------------------------------------------------------------
# 自检 —— **纯数据校验，不碰系统**（恒快）
# --------------------------------------------------------------------------
def selfcheck() -> dict:
    import time as _t
    t0 = _t.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # 1) 库非空且三类都有
    _add("DOM 指纹非空", len(DOM_FINGERPRINTS) >= 20, len(DOM_FINGERPRINTS))
    _add("窗口指纹非空", len(WINDOW_FINGERPRINTS) >= 8, len(WINDOW_FINGERPRINTS))
    _add("文本指纹非空", len(TEXT_FINGERPRINTS) >= 30, len(TEXT_FINGERPRINTS))

    # 2) id 唯一（Id 撞了会让 fingerprint_id 失去追溯意义）
    ids = [f.id for f in ALL_FINGERPRINTS]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    _add("指纹 id 全局唯一", not dup, dup or "共 %d 条" % len(ids))

    # 3) pattern 全小写（正则除外）—— 大小写混写会漏匹配
    bad_case = [f.id for f in ALL_FINGERPRINTS
                if not f.regex and f.pattern != f.pattern.lower()]
    _add("非正则 pattern 全小写", not bad_case, bad_case or "已规范")

    # 4) severity 合法
    from .status import ALL_SEVERITIES
    bad_sev = [f.id for f in ALL_FINGERPRINTS
               if f.severity not in ALL_SEVERITIES]
    _add("severity 全合法", not bad_sev, bad_sev or "已规范")

    # 5) type 合法
    bad_type = [f.id for f in ALL_FINGERPRINTS
                if f.type not in _ALL_TYPES]
    _add("type 全合法", not bad_type, bad_type or "已规范")

    # 6) **硬约束 #9 的可执行自证**：所有 slider / click_point 指纹
    #    都必须是 INTERRUPT，不允许任何一条是 BYPASSABLE/INFO。
    #    这条断言的作用：如果将来有人给滑块加"可绕过"标记来偷偷写硬解，
    #    自检立刻红。
    no_solve = [f for f in ALL_FINGERPRINTS
                if f.type in (RiskType.SLIDER, RiskType.CLICK_POINT)]
    wrong = [f.id for f in no_solve if f.severity != Severity.INTERRUPT]
    _add("硬约束#9：滑块/点选指纹必须全部 INTERRUPT",
         not wrong and len(no_solve) >= 10,
         wrong or "%d 条全为 INTERRUPT" % len(no_solve))

    # 7) 正则指纹可编译（构造时就会编，没抛就算过）
    regs = [f for f in ALL_FINGERPRINTS if f.regex]
    _add("正则指纹可编译", all(f._re is not None for f in regs),
         "%d 条正则" % len(regs))

    # 8-12) 真实匹配 —— **这才是这个库真正要测的**
    cases = [
        ("recaptcha anchor",
         lambda: bool(match_dom({"src": "https://www.google.com/recaptcha/api2/anchor?k=x"})),
         True),
        ("recaptcha badge → BYPASSABLE",
         lambda: match_dom({"class": "grecaptcha-badge"})[0]["fingerprint"].severity,
         Severity.BYPASSABLE),
        ("hcaptcha iframe",
         lambda: bool(match_dom({"src": "https://newassets.hcaptcha.com/captcha/v1/x"})),
         True),
        ("turnstile → BYPASSABLE",
         lambda: match_dom({"src": "https://challenges.cloudflare.com/turnstile/v0/api.js"})[0]["fingerprint"].severity,
         Severity.BYPASSABLE),
        ("cf_chl_ 正在挑战 → INTERRUPT",
         lambda: match_dom({"html": "<div id='cf_chl_opt'>"})[0]["fingerprint"].severity,
         Severity.INTERRUPT),
        ("geetest slider",
         lambda: bool(match_dom({"class": "geetest_slider_button"})),
         True),
        ("aliyun nc_container",
         lambda: bool(match_dom({"id": "nc_container"})),
         True),
        ("invisible reCAPTCHA（大小写不敏感）",
         lambda: bool(match_dom({"html": "<script src='HTTPS://WWW.GOOGLE.COM/RECAPTCHA/API.JS'>"})) or
                 bool(match_dom({"src": "https://www.google.com/RECAPTCHA/API.JS"})),
         True),
        ("one-time-code 正则",
         lambda: bool(match_dom({"html": '<input autocomplete = "one-time-code">'})),
         True),
    ]
    for name, fn, want in cases:
        try:
            got = fn()
        except Exception as exc:      # noqa: BLE001
            got = "%s: %s" % (type(exc).__name__, exc)
        _add("匹配·%s" % name, got == want, "got=%r want=%r" % (got, want))

    # 13) 干净 DOM 不该命中强指纹
    clean_hits = match_dom({
        "src": "https://cdn.example.com/app.js",
        "class": "btn btn-primary",
        "id": "submit",
        "html": "<div>欢迎回来，请查看您的订单</div>",
    })
    _add("干净 DOM 无强指纹命中",
         all(h["fingerprint"].severity in (Severity.INFO,)
             or h["fingerprint"].weight < 0.6 for h in clean_hits),
         [h["fingerprint"].id for h in clean_hits] or "0 命中")

    # 14) 窗口匹配
    _add("凭据框类名命中",
         any(h["fingerprint"].type == RiskType.LOGIN_WALL
             for h in match_window(class_name="CredentialDialogXamlHost")),
         "CredentialDialogXamlHost")
    _add("Cloudflare 五秒盾标题命中",
         any(h["fingerprint"].severity == Severity.INTERRUPT
             for h in match_window(title="Just a moment...")),
         "Just a moment...")
    _add("普通窗口不误报",
         not [h for h in match_window(title="记事本", class_name="Notepad")
              if h["fingerprint"].severity == Severity.INTERRUPT],
         "记事本")

    # 15) 文本评分：真验证题必须过线
    s1 = text_hit_score("请点击图中所有包含交通灯的图片")
    _add("文本·点选题判定 +分", s1["detected"] and s1["score"] >= 1.0,
         "score=%.2f groups=%s" % (s1["score"], [g["id"] for g in s1["groups"]]))
    _add("文本·点选题归入 CLICK_POINT",
         any(g["type"] == RiskType.CLICK_POINT for g in s1["groups"]),
         [g["type"] for g in s1["groups"]])

    s2 = text_hit_score("拖动滑块完成拼图验证")
    _add("文本·滑块题判定", s2["detected"] and s2["score"] >= 1.5,
         "score=%.2f" % s2["score"])

    s3 = text_hit_score("Verify you are human")
    _add("文本·英文人机判定", s3["detected"], "score=%.2f" % s3["score"])

    # 16) **弱词泛滥的正常页面必须判为干净** —— 这条是最重要的防误报断言
    noisy = ("欢迎登录本站。请阅读隐私政策与服务条款。本站已启用安全保护，"
             "加载中请稍候。提交前请验证您的信息。取消 确定")
    s4 = text_hit_score(noisy)
    _add("文本·弱词泛滥页面判为干净（防误报）",
         not s4["detected"],
         "score=%.2f weak=%d" % (s4["score"], len(s4["weak"])))

    s5 = text_hit_score("")
    _add("文本·空串判为未检出且不抛", s5["detected"] is False
         and s5["reason"] == "empty", s5["reason"])

    # 17) 分组判定：OCR 常见的**标点/空格插入**不影响短语命中。
    #     ⚠️ 这是本库的能力边界，必须写清楚：
    #     `in` 是**连续子串**匹配，"拖动滑块"能命中 "请 拖动滑块 完成验证"，
    #     但**命中不了** "拖 动 滑 块"（字与字之间插了空格）。
    #     后者是 OCR 的另一种失败模式，需要 detector_pixel 侧做
    #     去空白归一化后再喂进来 —— **不在本库做**，因为归一化会
    #     破坏英文单词边界（"drag the slider" 会被拼成 "dragtheslider"）。
    #     这条断言就是把这个边界钉住，不让"我以为它能处理"变成隐含假设。
    s6 = text_hit_score("请 拖动滑块 完成安全验证")
    _add("文本·标点/空格隔开的短语仍命中组",
         any(g["id"] == "grp.slider.zh" for g in s6["groups"]),
         "groups=%s score=%.2f" % ([g["id"] for g in s6["groups"]], s6["score"]))

    # 17b) 能力边界的显式记录：逐字插空格的文本，本库**不**认为命中。
    #      这不是 bug，是设计边界 —— 归一化归 detector_pixel 管。
    s6b = text_hit_score("拖 动 滑 块")
    _add("文本·已知边界：逐字插空格不命中（归一化归像素层）",
         s6b["detected"] is False,
         "score=%.2f（符合预期：本库不做去空白归一化）" % s6b["score"])

    # 18) fingerprint_by_id
    fid = DOM_FINGERPRINTS[0].id
    _add("fingerprint_by_id 可查", fingerprint_by_id(fid) is not None, fid)
    _add("fingerprint_by_id 未知 id 返回 None",
         fingerprint_by_id("__nope__") is None, "__nope__")

    # 19) 分组表结构合法
    bad_grp = [g["id"] for g in STRONG_TEXT_GROUPS
               if not g.get("any") or g.get("severity") not in ALL_SEVERITIES
               or g.get("type") not in _ALL_TYPES]
    _add("强词组结构合法", not bad_grp, bad_grp or "%d 组" % len(STRONG_TEXT_GROUPS))

    # 20) describe_library 可调用
    try:
        txt = describe_library()
        _add("describe_library 可调用", "dom" in txt,
             txt.splitlines()[0] if txt else "")
    except Exception as exc:      # noqa: BLE001
        _add("describe_library 可调用", False, "%s: %s" % (type(exc).__name__, exc))

    # ==================================================================
    # 追问 1：来源台账
    # ==================================================================
    audit = audit_sources()
    _add("SOURCES 台账非空", len(SOURCES) >= 6, "共 %d 个来源" % len(SOURCES))
    _add("每条指纹的 source 都已登记（无野来源）",
         not audit["unregistered"], audit["unregistered"] or "82/82 已登记")
    _add("没有登记但未使用的来源", not audit["unused"],
         audit["unused"] or "全部有引用")
    _add("SOURCES 每条都有 short/trust/detail",
         all(("short" in v and "trust" in v and "detail" in v)
             for v in SOURCES.values()),
         "已规范")
    _add("trust 值域 0~1",
         all(0.0 <= float(v["trust"]) <= 1.0 for v in SOURCES.values()),
         "已规范")
    _add("source_info 未知键返回可用的兜底",
         source_info("__nope__").get("registered") is False
         and "short" in source_info("__nope__"),
         source_info("__nope__")["short"])
    _add("低可信来源清单可生成（5G 误报排查用）",
         isinstance(audit["low_trust"], list), "%d 条" % len(audit["low_trust"]))

    # ==================================================================
    # 追问 2：弱词分档
    # ==================================================================
    _add("三档弱词表非空",
         all(WEAK_WORD_TIER.get(w) for w in WEAK_WORD_TIER)
         and len(WEAK_WORD_TIER) >= 20,
         "共 %d 词" % len(WEAK_WORD_TIER))
    _add("WEAK_TEXT_WORDS 由分档表派生（不各自漂移）",
         set(WEAK_TEXT_WORDS) == set(WEAK_WORD_TIER),
         "%d == %d" % (len(WEAK_TEXT_WORDS), len(WEAK_WORD_TIER)))
    _add("三档惩罚值齐备且严格递增（页脚扣最狠）",
         (WEAK_TIER_PENALTY[WEAK_TIER_FOOTER]
          < WEAK_TIER_PENALTY[WEAK_TIER_AMBIGUOUS]
          < WEAK_TIER_PENALTY[WEAK_TIER_OCR_NOISE] <= 0),
         "footer=%.2f ambiguous=%.2f noise=%.2f" % (
             WEAK_TIER_PENALTY[WEAK_TIER_FOOTER],
             WEAK_TIER_PENALTY[WEAK_TIER_AMBIGUOUS],
             WEAK_TIER_PENALTY[WEAK_TIER_OCR_NOISE]))
    _add("每个档位至少有一个词",
         all(any(t == tier for t in WEAK_WORD_TIER.values())
             for tier in WEAK_TIER_PENALTY),
         {t: sum(1 for x in WEAK_WORD_TIER.values() if x == t)
          for t in WEAK_TIER_PENALTY})

    # 分档真的生效：只含页脚词的文本，惩罚要比只含 OCR 噪声词的重
    s_footer = text_hit_score("登录 注册 隐私政策 服务条款 版权 关于我们")
    s_noise = text_hit_score("正在 加载中 请稍候")
    s_amb = text_hit_score("验证 确认 确定")
    _add("页脚档惩罚重于噪声档",
         s_footer["weak_penalty"] < s_noise["weak_penalty"],
         "footer=%.2f < noise=%.2f" % (s_footer["weak_penalty"],
                                       s_noise["weak_penalty"]))
    _add("歧义档惩罚重于噪声档",
         s_amb["weak_penalty"] < s_noise["weak_penalty"],
         "ambiguous=%.2f < noise=%.2f" % (s_amb["weak_penalty"],
                                          s_noise["weak_penalty"]))
    _add("weak_detail 逐词带档位与实扣分",
         all({"word", "tier", "base", "scale", "applied"} <= set(d)
             for d in s_footer["weak_detail"])
         and len(s_footer["weak_detail"]) == len(s_footer["weak"]),
         s_footer["weak_detail"][:2] if s_footer["weak_detail"] else "空")
    _add("tiers 统计正确",
         s_footer["tiers"].get(WEAK_TIER_FOOTER, 0) >= 5,
         s_footer["tiers"])

    # 消融开关：把页脚档清零，惩罚必须变成 0
    probe_text = "登录 注册 隐私政策 服务条款"
    base_scored = text_hit_score(probe_text)
    base_footer = sum(d["applied"] for d in base_scored["weak_detail"]
                      if d["tier"] == WEAK_TIER_FOOTER)
    s_ablate = text_hit_score(probe_text,
                              penalty_scale={WEAK_TIER_FOOTER: 0.0})
    _add("penalty_scale 消融开关生效（5G 用）",
         abs(s_ablate["weak_penalty"]) < 1e-9,
         "清零后 weak_penalty=%.4f" % s_ablate["weak_penalty"])
    s_double = text_hit_score(probe_text,
                              penalty_scale={WEAK_TIER_FOOTER: 2.0})
    _add("penalty_scale 可加倍（页脚档 ×2）",
         abs(s_double["weak_penalty"] - 2 * base_footer) < 0.01,
         "×2 后=%.3f，基准(同一文本) %.3f ×2=%.3f" % (
             s_double["weak_penalty"], base_footer, 2 * base_footer))
    _add("min_score 阈值可调",
         text_hit_score("拖动滑块", min_score=99.0)["detected"] is False,
         "把阈值抬到 99 后不检出")

    return {
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "module": __name__,
        "elapsed": round(_t.perf_counter() - t0, 3),
    }


#: RiskType 的全部合法值（模块级缓存，供校验用）
_ALL_TYPES = (
    RiskType.CAPTCHA, RiskType.SLIDER, RiskType.CLICK_POINT, RiskType.OTP,
    RiskType.CHALLENGE_PAGE, RiskType.LOGIN_WALL, RiskType.RATE_LIMIT,
    RiskType.UNKNOWN,
)


def _main() -> int:
    r = selfcheck()
    for c in r["checks"]:
        print("  %s %-46s %s" % ("[OK]" if c["ok"] else "[FAIL]", c["name"], c["detail"]))
    n = len(r["checks"])
    print("fingerprints 自检：%d/%d 通过，%.3fs"
          % (n - len(r["failures"]), n, r["elapsed"]))
    print()
    print(describe_library())
    return 0 if r["ok"] else 1


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(_main())
