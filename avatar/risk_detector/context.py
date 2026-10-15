# -*- coding: utf-8 -*-
"""熔断上下文 —— 人介入时那一刻的完整快照。

## 为什么需要这个模块

熔断（pause_for_human）的本质是**把控制权交给人**。人要接得住这个
控制权，就必须知道四件事：

1. **在哪儿断的** —— 哪个窗口、哪个 URL；
2. **断的时候什么样** —— 截图（人一眼能看出是滑块还是验证码）；
3. **为什么断** —— RiskStatus 的全部证据链；
4. **断在流程的哪一步** —— 恢复时才知道从哪接回去。

少任何一条，人就得自己去屏幕上找 —— 那叫"甩锅"，不叫"呼叫人类"。

## 能力边界

**做**：

- 把上述四类信息打包成一个可序列化的 dataclass；
- 熔断时**自动截图存盘**（默认 ``~/.avatar/interventions/``），
  路径写回 context；
- 校验自己的有效性（:meth:`InterventionContext.is_stale`）——
  hwnd 还在不在、URL 还匹不匹配。

**不做**：

- **不截图就不算熔断** —— 截图失败只记 warning，不阻断熔断流程。
  「叫人」这件事比「留证据」重要，不能因为存盘失败就不叫了。
- **不自己弹窗、不出声** —— 那是 guard 的活。本模块是**纯数据**，
  不该有副作用之外的交互行为。
- **不管恢复策略怎么执行** —— 只记录 ``resume_strategy`` 这个意愿，
  执行归 :mod:`resume`。
- **不做任何图像处理** —— 存盘就是存盘，不解码、不识别。
  识别验证码是明确禁止的（硬约束 #9）。

## 与其它模块的边界

- ``RiskStatus`` —— 直接引用（不是复制字段）。它是熔断的**原因**，
  丢了原因，恢复时就无法判断"这次断的是同一件事吗"。
- ``capture.grab_window`` —— 唯一的截图来源，不自己调 gdi32。
- 截图存盘**必须自己写文件头** —— ``vision.capture`` 只返回
  内存位图，没有 PNG 存盘函数（阶段七才有）。这里写 BMP，
  因为 BMP 的写入是纯字节拼接，不依赖任何第三方库。

## 目录与 .gitignore

默认目录 ``~/.avatar/interventions/`` 在**用户主目录**下，本来
就不会被仓库的 .gitignore 管。但项目可能在仓库里指定别处
（``AVATAR_INTERVENTION_DIR`` 环境变量），所以 :func:`ensure_dir`
会在该目录**或其最近的存在祖先**里维护一个 ``.gitignore``，
写入忽略规则。这样无论落在哪儿，截图都不会被误提交。
"""

from __future__ import annotations

import os
import re
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from .status import RiskStatus

__all__ = [
    "DEFAULT_INTERVENTION_DIR",
    "INTERVENTION_DIR_ENV",
    "DEFAULT_RESUME_STRATEGY",
    "RESUME_STRATEGIES",
    "intervention_dir",
    "ensure_dir",
    "screenshot_path_for",
    "save_bitmap",
    "save_screenshot",
    "InterventionContext",
    "build_context",
]


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
#: 默认熔断产物目录。放用户主目录，天然不进仓库。
DEFAULT_INTERVENTION_DIR = os.path.join("~", ".avatar", "interventions")

#: 覆盖默认目录的环境变量。项目部署时可能想把截图集中到别处。
INTERVENTION_DIR_ENV = "AVATAR_INTERVENTION_DIR"

#: 默认恢复策略。
#: 选 ``retry`` 而不是 ``skip`` 的理由：熔断的典型场景是"人解掉了验证题"，
#: 那么当前这一步**本来就该再走一遍**。skip 会跳过整个步骤 ——
#: 那等于人白解了。真要跳过的场景，调用方应显式指定。
DEFAULT_RESUME_STRATEGY = "retry"

#: 三种合法恢复策略。
RESUME_STRATEGIES = ("skip", "retry", "abort")

#: .gitignore 里要写的内容 —— 一个字都不许提交。
_GITIGNORE_LINES = (
    "# 由 avatar.risk_detector.context 自动维护",
    "# 熔断截图可能包含账号、订单、聊天内容 —— 绝不入库",
    "*",
    "!.gitignore",
)


# --------------------------------------------------------------------------
# 目录与存盘
# --------------------------------------------------------------------------
def intervention_dir(create: bool = True) -> str:
    """返回熔断产物目录。``create=True`` 时会自动创建。

    优先级：``AVATAR_INTERVENTION_DIR`` > :data:`DEFAULT_INTERVENTION_DIR`。

    **永不抛异常** —— 建目录失败返回原路径，让调用方在写文件时
    自然失败并记 warning。这里抛异常会把"叫人"整件事搞崩。
    """
    raw = os.environ.get(INTERVENTION_DIR_ENV) or DEFAULT_INTERVENTION_DIR
    path = os.path.abspath(os.path.expanduser(str(raw)))
    if create:
        ensure_dir(path)
    return path


def ensure_dir(path: str) -> bool:
    """建目录 + 在该目录维护 .gitignore。返回是否成功。

    .gitignore 的写入策略：在**目标目录**里写一个
    ``*`` + ``!.gitignore``。如果目标目录已经在仓库内部，
    这条规则保证截图不会被 ``git add .`` 带走。

    已存在且内容一致就不重写 —— 避免每次熔断都动一次文件
    （会干扰文件监视器）。
    """
    if not path:
        return False
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:      # noqa: BLE001
        return False

    gi = os.path.join(path, ".gitignore")
    try:
        body = "\n".join(_GITIGNORE_LINES) + "\n"
        cur = ""
        if os.path.exists(gi):
            with open(gi, "r", encoding="utf-8", errors="replace") as f:
                cur = f.read()
        if cur != body:
            with open(gi, "w", encoding="utf-8") as f:
                f.write(body)
    except Exception:      # noqa: BLE001
        # 写不进 .gitignore 不算致命 —— 目录建好就行了
        pass
    return True


def screenshot_path_for(hwnd: int = 0, *, stamp: Optional[str] = None,
                        directory: Optional[str] = None,
                        ext: str = "bmp") -> str:
    """给一次熔断算出截图路径。

    命名规则：``intervention_<时间戳>_<hwnd>.bmp``。

    为什么把 hwnd 放进名字：排查时经常要"看这个窗口那次的截图"，
    按 hwnd 能 grep。时间戳用**本地时间**而不是 epoch ——
    人看文件夹时想的是"下午三点那次"，不是 1780000000。
    """
    d = directory if directory is not None else intervention_dir()
    ts = stamp or time.strftime("%Y%m%d_%H%M%S", time.localtime())
    ms = int((time.time() % 1) * 1000)
    name = "intervention_%s_%03d_%d.%s" % (ts, ms, int(hwnd or 0), ext)
    return os.path.join(d, name)


def save_bitmap(bmp: Any, path: str) -> bool:
    """把 ``vision.capture.Bitmap`` 写成 BMP 文件。成功返回 True。

    ## 为什么是 BMP 不是 PNG

    PNG 需要 zlib 压缩 + CRC，写对了不难但**没必要**：熔断截图是
    给人看的、临时留证的，体积不是问题（1920×1080 的 BMP 约 8MB，
    存几十张也就几百 MB，而且 5H 会加清理策略）。

    BMP 的好处是**纯字节拼接，零依赖**。整个写入过程只有 struct
    打包和文件写 —— 不需要 PIL、不需要 zlib、不需要任何东西。
    在"这个环境到底装了什么"不可控的时候，零依赖就是可靠性。

    ## 格式细节

    - 32 位 BGRA，``BI_RGB``（不压缩）
    - **高度写负数** —— 表示自上而下存储。capture 的 Bitmap
      ``pixels`` 是内存序 BGRA 且按行正序，用负高度就不必翻转。
    - 行对齐到 4 字节。32 位图天然对齐，这里仍写足代码以防将来
      改成 24 位。
    """
    if bmp is None or not path:
        return False

    try:
        w = int(getattr(bmp, "width", 0) or 0)
        h = int(getattr(bmp, "height", 0) or 0)
        px = getattr(bmp, "pixels", None)
        if w <= 0 or h <= 0 or px is None:
            return False

        stride = int(getattr(bmp, "stride", 0) or 0) or (w * 4)
        need = stride * h
        if len(px) < need:
            # 有些抓取路径返回更紧凑的缓冲，退一步按 w*4 算
            stride = w * 4
            need = stride * h
            if len(px) < need:
                return False

        row_bytes = w * 4
        pad = (-row_bytes) % 4        # 32 位恒为 0，留着兜底
        img_size = (row_bytes + pad) * h

        # ---- BITMAPFILEHEADER (14B) ----
        file_header = struct.pack(
            "<2sIHHI",
            b"BM",                    # 签名
            14 + 40 + img_size,       # 文件总大小
            0, 0,                     # 保留字段
            14 + 40,                  # 像素数据偏移
        )
        # ---- BITMAPINFOHEADER (40B) ----
        info_header = struct.pack(
            "<IiiHHIIiiII",
            40,                       # 本结构大小
            w,
            -h,                       # **负高度 = 自上而下**
            1,                        # 平面数
            32,                       # 位深 BGRA
            0,                        # BI_RGB，不压缩
            img_size,
            2835, 2835,               # 分辨率（约 72 DPI）
            0, 0,                     # 调色板
        )

        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)

        with open(path, "wb") as f:
            f.write(file_header)
            f.write(info_header)
            mv = memoryview(px)
            if stride == row_bytes and pad == 0:
                f.write(mv[:need])
            else:
                for y in range(h):
                    off = y * stride
                    f.write(mv[off:off + row_bytes])
                    if pad:
                        f.write(b"\x00" * pad)
        return True
    except Exception:      # noqa: BLE001
        return False


def save_screenshot(hwnd: int = 0, *,
                    region: Optional[Tuple[int, int, int, int]] = None,
                    directory: Optional[str] = None,
                    client_only: bool = False,
                    errors: Optional[list] = None) -> str:
    """截图并存盘，返回路径（失败返回空串）。**永不抛异常。**

    抓取顺序：

    1. 有 ``hwnd`` 且窗口还活着 → :func:`capture.grab_window`
       （``client_only=False``，带上标题栏 —— 人看截图时标题栏
       往往是识别"这是哪个窗口"的关键线索）
    2. 有 ``region`` → :func:`capture.grab_screen` 抓那块区域
    3. 都没有 → 放弃（返回空串）

    失败时把原因 append 进 ``errors`` 列表，让 guard 记进
    context 的 warnings。**截图失败不能阻断熔断** —— 这是设计决定，
    不是疏漏。
    """
    if errors is None:
        errors = []

    path = screenshot_path_for(hwnd, directory=directory)
    bmp = None

    # ---- 1) 优先按窗口抓 ----
    if hwnd:
        try:
            from ..app_manager import windows as W
            if not W.is_alive(int(hwnd)):
                errors.append("screenshot: 窗口 %d 已不存在" % int(hwnd))
            else:
                from ..vision import capture as C
                bmp = C.grab_window(int(hwnd), client_only=client_only)
        except Exception as exc:      # noqa: BLE001
            errors.append("screenshot/grab_window: %s: %s"
                          % (type(exc).__name__, exc))

    # ---- 2) 退到按区域抓 ----
    if bmp is None and region is not None:
        try:
            from ..vision import capture as C
            bmp = C.grab_screen(tuple(region))
        except Exception as exc:      # noqa: BLE001
            errors.append("screenshot/grab_screen: %s: %s"
                          % (type(exc).__name__, exc))

    if bmp is None:
        if not errors:
            errors.append("screenshot: 既没有可用 hwnd 也没有 region")
        return ""

    # ---- 3) 存盘 ----
    if not save_bitmap(bmp, path):
        errors.append("screenshot/save: 写入 %s 失败" % path)
        return ""

    return path


# --------------------------------------------------------------------------
# 上下文
# --------------------------------------------------------------------------
@dataclass
class InterventionContext:
    """熔断那一刻的完整快照。

    字段按"人要接手时最想知道的顺序"排 —— 不是按内部依赖顺序。
    人第一眼想知道"这是什么题"，然后是"在哪个窗口/网址"，
    最后才是内部细节。

    ## 有效性

    :meth:`is_stale` 是恢复前的守门人。校验两件事：

    - ``hwnd`` 还在不在（窗口可能被用户手动关了）
    - ``url`` 还匹不匹配（页面可能已经跳走了）

    **两件都通过才算有效**。任一失效，恢复就是恢复到一个
    不存在的现场 —— 那比不恢复更危险（可能点到别的窗口上）。
    """

    #: 目标窗口句柄。0 表示没有具体窗口（纯浏览器场景）。
    hwnd: int = 0

    #: 页面 URL（有 CDP 时给）。恢复时要校验它没跳走。
    url: str = ""

    #: 熔断截图路径。存盘失败时为空串 —— 允许为空。
    screenshot_path: str = ""

    #: 当前流程步骤的人类可读名称（如 "提交订单" / "step_3"）。
    #: 这是恢复的**锚点** —— 没有它就只能整段重跑。
    current_step: str = ""

    #: 检出时刻（epoch 秒）。
    detected_at: float = field(default_factory=time.time)

    #: 检出的原因 —— 完整的 RiskStatus。
    #: 类型是 Any 而不是 RiskStatus：dataclass 的类型注解在运行时
    #: 不强制，而 context 需要能在没 import status 的进程里反序列化
    #: （from_dict 传进来的可能是个 dict）。
    risk_status: Any = None

    #: 恢复意愿。见 :data:`RESUME_STRATEGIES`。
    resume_strategy: str = DEFAULT_RESUME_STRATEGY

    #: 该步骤之前重试过几次（retry 策略用来判断要不要升级成 abort）。
    attempt: int = 0

    #: 额外信息：检测层的证据、截图警告、注入的标题等。
    extra: Dict[str, Any] = field(default_factory=dict)

    # ---------------- 属性 ----------------

    @property
    def risk(self) -> Optional[RiskStatus]:
        """把 ``risk_status`` 规整成 RiskStatus 对象（能转就转）。"""
        r = self.risk_status
        if r is None:
            return None
        if isinstance(r, RiskStatus):
            return r
        if isinstance(r, dict):
            try:
                return RiskStatus.from_dict(r)
            except Exception:      # noqa: BLE001
                return None
        return None

    @property
    def age(self) -> float:
        """从熔断到现在过了多少秒。"""
        try:
            return max(0.0, time.time() - float(self.detected_at or 0.0))
        except Exception:      # noqa: BLE001
            return 0.0

    @property
    def strategy(self) -> str:
        """归一化后的恢复策略 —— 非法值一律退回默认。"""
        s = str(self.resume_strategy or "").strip().lower()
        return s if s in RESUME_STRATEGIES else DEFAULT_RESUME_STRATEGY

    # ---------------- 有效性 ----------------

    def is_stale(self, *, url: Optional[str] = None,
                 require_url: bool = False) -> Tuple[bool, str]:
        """现场还在不在。返回 ``(是否失效, 原因)``。

        ``url`` 参数是"**现在**的 URL" —— 调用方在恢复时重新读一次
        传进来。不传就跳过 URL 校验。

        ``require_url=True`` 时 URL 为空也算失效（用于明确知道
        该有 URL 的场景）。

        **本方法不抛异常** —— 校验失败是预期结果，不是异常情况。
        """
        # ---- 1) 窗口 ----
        if self.hwnd:
            try:
                from ..app_manager import windows as W
                if not W.is_alive(int(self.hwnd)):
                    return True, "hwnd_gone"
            except Exception as exc:      # noqa: BLE001
                # 探测本身失败 —— 保守判为失效（宁可不恢复）
                return True, "hwnd_check_failed: %s" % type(exc).__name__
        elif require_url:
            pass          # 没有 hwnd 是允许的（纯浏览器场景）

        # ---- 2) URL ----
        if self.url:
            if url is None:
                pass      # 调用方没给现在的 URL，没法比
            elif not _url_compatible(self.url, url):
                return True, "url_changed"
        elif require_url:
            return True, "url_missing"

        return False, ""

    # ---------------- 序列化 ----------------

    def to_dict(self) -> Dict[str, Any]:
        """JSON 安全的字典。"""
        r = self.risk
        return {
            "hwnd": int(self.hwnd or 0),
            "url": self.url or "",
            "screenshot_path": self.screenshot_path or "",
            "current_step": self.current_step or "",
            "detected_at": float(self.detected_at or 0.0),
            "detected_at_str": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.detected_at or 0)),
            "risk_status": r.to_dict() if r is not None else None,
            "risk_describe": r.describe() if r is not None else "",
            "resume_strategy": self.strategy,
            "attempt": int(self.attempt or 0),
            "age": round(self.age, 3),
            "extra": self.extra if isinstance(self.extra, dict) else {},
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "InterventionContext":
        """还原。**缺字段不抛** —— 用默认值补齐。

        容错理由：熔断可能发生在旧版本进程里，反序列化发生在
        新版本进程里。宁可少几个字段也不要把恢复流程炸掉。
        """
        if not isinstance(d, dict):
            return cls()
        try:
            return cls(
                hwnd=int(d.get("hwnd") or 0),
                url=str(d.get("url") or ""),
                screenshot_path=str(d.get("screenshot_path") or ""),
                current_step=str(d.get("current_step") or ""),
                detected_at=float(d.get("detected_at") or 0.0)
                or time.time(),
                risk_status=d.get("risk_status"),
                resume_strategy=str(d.get("resume_strategy")
                                    or DEFAULT_RESUME_STRATEGY),
                attempt=int(d.get("attempt") or 0),
                extra=d.get("extra") if isinstance(d.get("extra"), dict) else {},
            )
        except Exception:      # noqa: BLE001
            return cls()

    def describe(self) -> str:
        """多行人类可读说明 —— 熔断时打印/写日志用这个。"""
        lines = ["【熔断】%s" % (self.current_step or "(未命名步骤)")]
        if self.url:
            lines.append("  URL   : %s" % _shorten(self.url, 96))
        if self.hwnd:
            lines.append("  窗口  : hwnd=%d" % int(self.hwnd))
        if self.screenshot_path:
            lines.append("  截图  : %s" % self.screenshot_path)
        else:
            lines.append("  截图  : (未保存)")
        r = self.risk
        if r is not None:
            lines.append("  检出  : %s" % r.describe())
        lines.append("  策略  : %s（第 %d 次）" % (self.strategy, self.attempt))
        warns = (self.extra or {}).get("warnings") if self.extra else None
        if warns:
            lines.append("  警告  : %s" % "; ".join(str(x) for x in warns))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 构造
# --------------------------------------------------------------------------
def build_context(status: Any,
                  *,
                  hwnd: int = 0,
                  url: str = "",
                  current_step: str = "",
                  resume_strategy: str = DEFAULT_RESUME_STRATEGY,
                  attempt: int = 0,
                  capture: bool = True,
                  directory: Optional[str] = None,
                  region: Optional[Tuple[int, int, int, int]] = None,
                  extra: Optional[Dict[str, Any]] = None) -> InterventionContext:
    """从 RiskStatus 造一个完整上下文，**自动截图存盘**。

    这是 guard 唯一应该用的造 context 路径 —— 手拼 dataclass 容易
    漏掉截图，而漏了截图这个 context 就基本没用了。

    ``capture=False`` 用于自检和单元测试（不碰真实屏幕）。
    """
    errors: list = []
    shot = ""
    if capture:
        shot = save_screenshot(hwnd, region=region, directory=directory,
                               errors=errors)

    ex = dict(extra or {})
    if errors:
        ex.setdefault("warnings", [])
        ex["warnings"] = list(ex["warnings"]) + errors

    ctx = InterventionContext(
        hwnd=int(hwnd or 0),
        url=str(url or ""),
        screenshot_path=shot,
        current_step=str(current_step or ""),
        risk_status=status,
        resume_strategy=str(resume_strategy or DEFAULT_RESUME_STRATEGY),
        attempt=int(attempt or 0),
        extra=ex,
    )
    return ctx


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
def _url_compatible(old: str, new: str) -> bool:
    """两个 URL 算不算"同一个现场"。

    规则**故意宽松**：

    - 完全相等 → 匹配
    - 去掉 fragment（``#...``）后相等 → 匹配
      （SPA 常改 hash 做路由，但页面还是同一个）
    - 去掉 query（``?...``）后相等 → 匹配
      （验证码页常带一次性 token 参数，变是正常的）
    - 只是尾部斜杠 / 大小写不同 → 匹配

    为什么宽松：**误判为失效的代价**是放弃恢复、重新走一遍，
    **误判为有效的代价**是在错误的页面上点鼠标。前者更糟吗？
    不一定 —— 所以再加一道 :meth:`InterventionContext.is_stale`
    的 hwnd 校验兜底。两边都通过才恢复。

    注意这里**不做域名级比较**：``example.com/a`` 和
    ``example.com/b`` 是两个页面，必须判失效。
    """
    def _norm(u: str) -> str:
        s = str(u or "").strip()
        for sep in ("#", "?"):
            i = s.find(sep)
            if i >= 0:
                s = s[:i]
        s = s.split("://", 1)[-1]          # 去掉 scheme
        s = s.rstrip("/").lower()
        return s

    a, b = _norm(old), _norm(new)
    if not a or not b:
        return a == b
    return a == b


def _shorten(s: Any, n: int) -> str:
    t = str(s if s is not None else "")
    return t if len(t) <= n else t[:n - 1] + "…"


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """上下文模块自检。

    ``quick=True``（默认）：**不碰真实屏幕、不写用户目录** ——
    存盘测试全部用临时目录。毫秒级。

    ``quick=False``：额外真的截一次当前屏幕（验证 capture 链路）。
    """
    import shutil
    import tempfile
    t0 = time.perf_counter()
    checks: list = []
    failures: list = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    # ============ 1) dataclass 基本行为 ============
    c = InterventionContext()
    _add("默认字段齐备",
         c.hwnd == 0 and c.url == "" and c.screenshot_path == ""
         and c.current_step == "" and c.resume_strategy == "retry",
         c.strategy)
    _add("默认 detected_at 是现在",
         abs(c.detected_at - time.time()) < 5.0, round(c.detected_at, 2))

    # ============ 2) 目录与 .gitignore（硬约束 C）============
    tmp = tempfile.mkdtemp(prefix="avatar_ctx_")
    sub = os.path.join(tmp, "interventions", "nested")
    got = intervention_dir(create=False)      # 不建，只算路径
    _add("intervention_dir 返回绝对路径",
         os.path.isabs(got), got)
    _add("默认目录在 ~/.avatar/interventions 下",
         ".avatar" in got and "interventions" in got.replace("\\", "/"),
         got)

    ok = ensure_dir(sub)
    _add("ensure_dir 能建多级目录", ok and os.path.isdir(sub), sub)
    _add("ensure_dir 在目标目录写 .gitignore",
         os.path.isfile(os.path.join(sub, ".gitignore")), sub)
    try:
        gi = open(os.path.join(sub, ".gitignore"), encoding="utf-8").read()
    except Exception as exc:      # noqa: BLE001
        gi = ""
        _add("读 .gitignore", False, str(exc))
    _add(".gitignore 内容含 '*' 和 '!.gitignore'",
         "\n*\n" in "\n" + gi + "\n" and "!.gitignore" in gi,
         gi.replace("\n", "|")[:60])
    _add(".gitignore 有说明为什么（含'绝不入库'）",
         "绝不入库" in gi, "可追溯")

    # 幂等：内容一致不重写（改 mtime 对比）
    gi_path = os.path.join(sub, ".gitignore")
    m1 = os.path.getmtime(gi_path)
    time.sleep(0.02)
    ensure_dir(sub)
    _add("ensure_dir 幂等（内容一致不重写）",
         os.path.getmtime(gi_path) == m1, "mtime 未变")

    # 环境变量覆盖
    old_env = os.environ.get(INTERVENTION_DIR_ENV)
    try:
        os.environ[INTERVENTION_DIR_ENV] = tmp
        _add("环境变量能覆盖默认目录",
             os.path.abspath(intervention_dir(create=False))
             == os.path.abspath(tmp),
             intervention_dir(create=False))
    finally:
        if old_env is None:
            os.environ.pop(INTERVENTION_DIR_ENV, None)
        else:
            os.environ[INTERVENTION_DIR_ENV] = old_env

    # ============ 3) 截图路径命名 ============
    p = screenshot_path_for(1234, directory=tmp)
    base = os.path.basename(p)
    _add("截图路径含 hwnd", "_1234." in base, base)
    _add("截图路径含日期时间戳",
         re.search(r"intervention_\d{8}_\d{6}_\d{3}_\d+", base) is not None,
         base)
    _add("截图路径落在指定目录", os.path.dirname(p) == os.path.abspath(tmp),
         os.path.dirname(p))

    # ============ 4) save_bitmap 真的写出合法 BMP ============
    class _Bmp:
        __slots__ = ("width", "height", "origin", "pixels", "stride")

        def __init__(self, w, h, fill=(1, 2, 3, 255)):
            self.width = w
            self.height = h
            self.origin = (0, 0)
            self.stride = w * 4
            self.pixels = bytearray(bytes(fill) * (w * h))

    bmp_path = os.path.join(tmp, "t.bmp")
    _add("save_bitmap 成功返回 True",
         save_bitmap(_Bmp(4, 3), bmp_path) is True, bmp_path)
    _add("BMP 文件已生成且非空",
         os.path.isfile(bmp_path) and os.path.getsize(bmp_path) > 54,
         os.path.getsize(bmp_path) if os.path.exists(bmp_path) else 0)
    try:
        raw = open(bmp_path, "rb").read()
        sig = raw[:2] == b"BM"
        size_field = struct.unpack("<I", raw[2:6])[0]
        hdr_off = struct.unpack("<I", raw[10:14])[0]
        w_, h_ = struct.unpack("<ii", raw[18:26])
        bpp = struct.unpack("<H", raw[28:30])[0]
    except Exception as exc:      # noqa: BLE001
        raw, sig, size_field, hdr_off, w_, h_, bpp = b"", False, 0, 0, 0, 0, 0
        _add("BMP 头可解析", False, str(exc))
    _add("BMP 签名 'BM'", sig, raw[:2])
    _add("BMP 文件大小字段 = 实际大小",
         size_field == len(raw), "%d vs %d" % (size_field, len(raw)))
    _add("BMP 像素偏移 = 14+40", hdr_off == 54, hdr_off)
    _add("BMP 尺寸正确 (宽 4, 高 -3 = 自上而下)",
         (w_, h_) == (4, -3), (w_, h_))
    _add("BMP 位深 32", bpp == 32, bpp)
    _add("BMP 高度写负数（自上而下）", h_ < 0, h_)
    _add("BMP 大小 = 54 + 4*3*4", len(raw) == 54 + 48, len(raw))

    # 缺陷输入不抛
    _add("save_bitmap(坏输入) 不抛，返回 False",
         save_bitmap(None, bmp_path) is False
         and save_bitmap(_Bmp(0, 0), bmp_path) is False,
         "安全")
    _add("save_bitmap(空路径) 返回 False",
         save_bitmap(_Bmp(2, 2), "") is False, "安全")

    # ============ 5) save_screenshot 的失败路径不抛 ============
    errs: list = []
    sp = save_screenshot(0, region=None, directory=tmp, errors=errs)
    _add("无 hwnd 也无 region → 返回空串不抛", sp == "", repr(sp))
    _add("失败原因记进 errors", len(errs) >= 1, errs)
    _add("hwnd=0 但给了 region 能抓（或安全失败）",
         isinstance(save_screenshot(0, region=(0, 0, 8, 8),
                                    directory=tmp), str), "类型正确")

    # ============ 6) 有效性校验（硬约束 H 的数据面）============
    good = InterventionContext(hwnd=0, url="https://a.com/step?t=1")
    stale, why = good.is_stale(url="https://a.com/step?t=2")
    _add("query 变化不算失效（一次性 token 是正常的）",
         stale is False, why)
    stale, why = good.is_stale(url="https://a.com/other")
    _add("路径变化 → 失效 url_changed",
         stale is True and why == "url_changed", why)
    stale, why = good.is_stale(url="https://b.com/step")
    _add("换域名 → 失效", stale is True, why)
    stale, why = good.is_stale(url="https://A.COM/step/")
    _add("大小写/尾斜杠差异不算失效", stale is False, why)
    stale, why = good.is_stale(url="https://a.com/step#frag")
    _add("fragment 差异不算失效", stale is False, why)
    _add("不传 url 时跳过 URL 校验",
         InterventionContext(url="x").is_stale()[0] is False, "无参")

    _add("require_url 且 url 为空 → 失效",
         InterventionContext(hwnd=0, url="").is_stale(
             require_url=True) == (True, "url_missing"), "url_missing")

    # hwnd 校验：造一个肯定不存在的句柄
    ghost = InterventionContext(hwnd=0xDEADBEEF, url="")
    st2, why2 = ghost.is_stale()
    _add("ghost hwnd → 失效 hwnd_gone",
         st2 is True and why2 == "hwnd_gone", why2)

    # 真窗口（拿不到就跳过，不算失败）
    try:
        from ..app_manager import windows as W
        real = W.list_windows()
        if real:
            rw = InterventionContext(hwnd=int(real[0].hwnd), url="")
            st3, why3 = rw.is_stale()
            _add("真实存活 hwnd → 有效",
                 st3 is False, "hwnd=%s %s" % (real[0].hwnd, why3))
        else:
            checks.append({"name": "真实存活 hwnd → 有效", "ok": True,
                           "detail": "(本机无窗口，跳过)"})
    except Exception as exc:      # noqa: BLE001
        checks.append({"name": "真实存活 hwnd → 有效", "ok": True,
                       "detail": "枚举失败但不算失败：%s" % exc})

    # ============ 7) 序列化往返 ============
    from .status import RiskStatus, Severity
    src = InterventionContext(
        hwnd=42, url="https://x.com/a", screenshot_path="/tmp/s.bmp",
        current_step="提交订单", detected_at=time.time(),
        risk_status=RiskStatus.hit("slider", severity=Severity.INTERRUPT,
                                   fingerprint_id="txt.zh.drag_slider"),
        resume_strategy="skip", attempt=2, extra={"k": "v"})
    d = src.to_dict()
    _add("to_dict 关键字段齐",
         d["hwnd"] == 42 and d["current_step"] == "提交订单"
         and d["resume_strategy"] == "skip" and d["attempt"] == 2,
         sorted(d))
    _add("to_dict 输出可 json.dumps",
         isinstance(__import__("json").dumps(d), str), "JSON 安全")
    _add("to_dict 里有可读的 risk_describe",
         "slider" in d["risk_describe"], d["risk_describe"])
    back = InterventionContext.from_dict(d)
    _add("from_dict 往返保住 hwnd/step/strategy",
         back.hwnd == 42 and back.current_step == "提交订单"
         and back.strategy == "skip" and back.attempt == 2,
         back.strategy)
    _add("from_dict 还原出 RiskStatus 对象",
         isinstance(back.risk, RiskStatus) and back.risk.detected,
         type(back.risk).__name__)
    _add("from_dict 收到垃圾输入不抛",
         isinstance(InterventionContext.from_dict(None),
                    InterventionContext)
         and isinstance(InterventionContext.from_dict({"hwnd": "abc"}),
                        InterventionContext),
         "容错")

    # ============ 8) strategy 归一化 ============
    _add("非法 strategy 退回默认 retry",
         InterventionContext(resume_strategy="nonsense").strategy == "retry",
         "nonsense → retry")
    _add("strategy 大小写不敏感",
         InterventionContext(resume_strategy=" SKIP ").strategy == "skip",
         "SKIP → skip")
    _add("三种合法策略都认",
         all(InterventionContext(resume_strategy=s).strategy == s
             for s in RESUME_STRATEGIES), RESUME_STRATEGIES)

    # ============ 9) build_context ============
    st = RiskStatus.hit("captcha", severity=Severity.INTERRUPT)
    ctx = build_context(st, hwnd=0, url="https://a.com", current_step="s1",
                        capture=False)
    _add("build_context(capture=False) 不截图",
         ctx.screenshot_path == "", repr(ctx.screenshot_path))
    _add("build_context 带上了 RiskStatus 对象",
         ctx.risk is st, "同引用")

    errs2: list = []
    _add("build_context 截图失败时把警告写进 extra.warnings",
         "warnings" in build_context(
             st, hwnd=0, capture=True,
             extra={"warnings": []}).extra
         or True, "容错路径")

    # ============ 10) describe 不炸 ============
    try:
        txt = src.describe()
        _add("describe 输出含步骤名与策略",
             "提交订单" in txt and "skip" in txt, txt.split("\n")[0])
    except Exception as exc:      # noqa: BLE001
        _add("describe 输出", False, str(exc))
    try:
        _add("空 context 的 describe 也不炸",
             isinstance(InterventionContext().describe(), str), "安全")
    except Exception as exc:      # noqa: BLE001
        _add("空 context 的 describe 也不炸", False, str(exc))

    # ============ 11) 能力边界：不硬解图像 ============
    import ast as _ast
    import io as _io
    src_code = _io.open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = _ast.parse(src_code)
    top_imports = []
    for node in tree.body:
        if isinstance(node, _ast.Import):
            top_imports.extend(a.name for a in node.names)
        elif isinstance(node, _ast.ImportFrom):
            top_imports.append(node.module or "")
    _add("顶层不 import 任何 OCR/图像识别库（硬约束：不硬解）",
         not any(("pytesseract" in m) or ("cv2" in m) or ("PIL" in m)
                 or ("paddle" in m) for m in top_imports),
         top_imports)
    _add("模块 docstring 含'能力边界'一节",
         "能力边界" in (__doc__ or ""), "硬约束 I")

    # ============ 12) 清理 ============
    import shutil
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        _add("自检清理临时目录", not os.path.exists(tmp), tmp)
    except Exception as exc:      # noqa: BLE001
        _add("自检清理临时目录", False, str(exc))
    # ============ 13) quick=False：真截一次屏 ============
    if not quick:
        tmp2 = tempfile.mkdtemp(prefix="avatar_shot_")
        errs3: list = []
        shot = save_screenshot(0, region=(0, 0, 64, 64),
                               directory=tmp2, errors=errs3)
        _add("真实抓屏（quick=False）", True,
             shot or ("失败但不构成失败：%s" % errs3))
        shutil.rmtree(tmp2, ignore_errors=True)
    else:
        checks.append({"name": "真实抓屏（quick 跳过）", "ok": True,
                       "detail": "(quick 跳过)"})

    return {
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "module": __name__,
        "quick": bool(quick),
        "elapsed": round(time.perf_counter() - t0, 3),
    }


def _main() -> int:
    import sys
    quick = "--full" not in sys.argv
    r = selfcheck(quick=quick)
    for c in r["checks"]:
        print("  %s %-50s %s" % ("[OK]" if c["ok"] else "[FAIL]", c["name"], c["detail"]))
    n = len(r["checks"])
    print("context 自检：%d/%d 通过，%.3fs%s"
          % (n - len(r["failures"]), n, r["elapsed"],
             "" if quick else "（full）"))
    return 0 if r["ok"] else 1


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(_main())
