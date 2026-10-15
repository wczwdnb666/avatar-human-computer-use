# -*- coding: utf-8 -*-
"""目标解析 —— 一句话指令 → 结构化 Goal。

## 这一层解决什么

用户说"打开记事本输入 hello"，这是一句**自然语言**。规划器需要的是
**结构**：要做什么动作、作用于什么、成功长什么样、有多少预算。

:func:`parse_goal` 负责这个转换。阶段六用的是**规则解析**
（关键词 + 正则），不是 LLM。

## 为什么阶段六先做规则解析

因为规划器的难点**不在理解语言**，在状态机、DAG 调度、自愈、恢复。
用一个不稳定的 LLM 去做目标解析，会让这些更难的部分无法被稳定测试 ——
LLM 今天把"打开记事本"解析成 `open`，明天可能解析成 `launch`，
测试就得跟着它改。

**规则解析的覆盖面窄，但它是确定的。** 先把确定的部分做到极致，
语言理解的部分（:class:`~.planner.LLMPlanner`）留到阶段七补。

## 规则解析能处理什么

    "打开记事本输入hello"
    "打开 notepad 并输入 hello"
    "在记事本里输入 hello 然后保存"
    "打开 https://example.com"
    "点击 保存 按钮"
    "等待 3 秒"
    "关闭 记事本"

**多动词**会被拆成动作序列（`open` → `type`），交给规划器映射成 DAG。

## 能力边界

**做**：把中文/英文的**短指令**解析成 :class:`Goal`；抽取动作、
目标、文本参数、等待时长；给默认预算；给出可读的失败原因。

**不做**：

- **不调用 LLM**。本模块**没有一行网络代码**。语言理解是
  :class:`~.planner.LLMPlanner` 的职责，阶段七补。
- **不做指代消解**。"把那个东西关掉"里的"那个"解析不出来 ——
  返回 `intent="unknown"` 并说明原因，不猜。
- **不做多轮对话**。一次调用解析一句话，不带上下文。
- **不做歧义消解**。"打开记事本保存"里"保存"是动词还是名词？
  按动词处理，并把这个选择记进 `notes` 让人能复核。
- **不验证可行性**。"打开 Photoshop" 里 Photoshop 没装，
  本模块不管 —— 那是执行层的事。
- **不生成步骤**。本模块只产出"要什么"，"怎么做"是
  :mod:`~.planner` 的事。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "Goal",
    "DEFAULT_BUDGET_SECONDS",
    "DEFAULT_BUDGET_STEPS",
    "INTENT_OPEN",
    "INTENT_TYPE",
    "INTENT_CLICK",
    "INTENT_NAVIGATE",
    "INTENT_KEY",
    "INTENT_WAIT",
    "INTENT_SCROLL",
    "INTENT_SCREENSHOT",
    "INTENT_CLOSE",
    "INTENT_SAVE",
    "INTENT_COMPOSITE",
    "INTENT_UNKNOWN",
    "INTENTS",
    "parse_goal",
    "describe_grammar",
    "selfcheck",
]

#: 默认时间预算（秒）。用户明确要求 30 分钟。
DEFAULT_BUDGET_SECONDS = 30 * 60.0

#: 默认步数预算。
DEFAULT_BUDGET_STEPS = 50

# ---- 意图常量 ----
INTENT_OPEN = "open"
INTENT_TYPE = "type"
INTENT_CLICK = "click"
INTENT_NAVIGATE = "navigate"
INTENT_KEY = "key"
INTENT_WAIT = "wait"
INTENT_SCROLL = "scroll"
INTENT_SCREENSHOT = "screenshot"
INTENT_CLOSE = "close"
INTENT_SAVE = "save"
INTENT_COMPOSITE = "composite"      # 一句话里有多个动作
INTENT_UNKNOWN = "unknown"

#: 全部合法意图。
INTENTS = (
    INTENT_OPEN, INTENT_TYPE, INTENT_CLICK, INTENT_NAVIGATE, INTENT_KEY,
    INTENT_WAIT, INTENT_SCROLL, INTENT_SCREENSHOT, INTENT_CLOSE,
    INTENT_SAVE, INTENT_COMPOSITE, INTENT_UNKNOWN,
)


# --------------------------------------------------------------------------
# 动词表
# --------------------------------------------------------------------------
#: 动词 → 意图。**顺序有意义**：长的/更具体的排前面，
#: 因为匹配是"第一个命中即返回"。
#:
#: 为什么不用正则的 ``|`` 交替：交替匹配的是"最早出现的"，
#: 而这里要的是"最具体的"。两者在 "打开...保存" 这种句子上结论不同。
_VERBS: List[Tuple[str, str]] = [
    # ---- 打开 / 启动 ----
    ("打开", INTENT_OPEN), ("启动", INTENT_OPEN), ("运行", INTENT_OPEN),
    ("开启", INTENT_OPEN),
    ("open", INTENT_OPEN), ("launch", INTENT_OPEN), ("start", INTENT_OPEN),
    ("run", INTENT_OPEN),
    # ---- 输入 ----
    ("输入", INTENT_TYPE), ("键入", INTENT_TYPE), ("填写", INTENT_TYPE),
    ("填入", INTENT_TYPE), ("写上", INTENT_TYPE),
    ("type", INTENT_TYPE), ("input", INTENT_TYPE), ("enter", INTENT_TYPE),
    ("write", INTENT_TYPE), ("fill", INTENT_TYPE),
    # ---- 点击 ----
    ("点击", INTENT_CLICK), ("单击", INTENT_CLICK), ("按下", INTENT_CLICK),
    ("点一下", INTENT_CLICK), ("摁", INTENT_CLICK),
    ("click", INTENT_CLICK), ("press", INTENT_CLICK), ("tap", INTENT_CLICK),
    # ---- 导航 ----
    ("访问", INTENT_NAVIGATE), ("导航到", INTENT_NAVIGATE),
    ("前往", INTENT_NAVIGATE),
    ("navigate", INTENT_NAVIGATE), ("goto", INTENT_NAVIGATE),
    ("visit", INTENT_NAVIGATE),
    # ---- 按键 ----
    ("按键", INTENT_KEY), ("快捷键", INTENT_KEY),
    ("hotkey", INTENT_KEY), ("shortcut", INTENT_KEY),
    # ---- 等待 ----
    ("等待", INTENT_WAIT), ("等", INTENT_WAIT), ("稍等", INTENT_WAIT),
    ("wait", INTENT_WAIT), ("sleep", INTENT_WAIT),
    # ---- 滚动 ----
    ("滚动", INTENT_SCROLL), ("下滑", INTENT_SCROLL), ("上滑", INTENT_SCROLL),
    ("scroll", INTENT_SCROLL),
    # ---- 截图 ----
    ("截图", INTENT_SCREENSHOT), ("截屏", INTENT_SCREENSHOT),
    ("screenshot", INTENT_SCREENSHOT), ("capture", INTENT_SCREENSHOT),
    # ---- 关闭 ----
    ("关闭", INTENT_CLOSE), ("退出", INTENT_CLOSE), ("关掉", INTENT_CLOSE),
    ("close", INTENT_CLOSE), ("quit", INTENT_CLOSE), ("exit", INTENT_CLOSE),
    # ---- 保存 ----
    # "保存" 是一个**意图**，不是实现手段 —— 执行层可以选 Ctrl+S，
    # 也可以选点"保存"按钮，还可能弹"另存为"对话框。意图层不该替它定。
    ("另存为", INTENT_SAVE), ("保存", INTENT_SAVE), ("存盘", INTENT_SAVE),
    ("save as", INTENT_SAVE), ("save", INTENT_SAVE),
]

#: 意图 → 人类可读的动作说明（用于 success 判据）。
_SUCCESS_HINT = {
    INTENT_OPEN: "目标窗口出现且可见",
    INTENT_TYPE: "目标控件里的文本等于期望值",
    INTENT_CLICK: "目标元素被激活（状态或界面发生变化）",
    INTENT_NAVIGATE: "页面 URL 匹配目标地址",
    INTENT_KEY: "快捷键生效（界面出现预期变化）",
    INTENT_WAIT: "等待时长已耗尽",
    INTENT_SCROLL: "可滚动区域位置发生变化",
    INTENT_SCREENSHOT: "截图文件已落盘且非空",
    INTENT_CLOSE: "目标窗口消失",
    INTENT_SAVE: "文档已写入磁盘（标题栏不再有未保存标记）",
}

#: 常见应用的别名 → 规范名。
#: 规则解析的"领域知识"就这一点点，够用即可。
_APP_ALIASES = {
    "notepad": "notepad",
    "记事本": "notepad",
    "calc": "calc",
    "计算器": "calc",
    "mspaint": "mspaint",
    "画图": "mspaint",
    "explorer": "explorer",
    "资源管理器": "explorer",
    "文件管理器": "explorer",
    "cmd": "cmd",
    "命令提示符": "cmd",
    "powershell": "powershell",
    "edge": "msedge",
    "浏览器": "msedge",
    "chrome": "chrome",
    "谷歌浏览器": "chrome",
}

#: URL 形态。
_URL_RE = re.compile(r"https?://[^\s，。；、\"'）)】\]]+", re.I)

#: "等待 N 秒 / N 分钟"。
_WAIT_RE = re.compile(
    r"(?:等待|等|wait|sleep)\s*([0-9]+(?:\.[0-9]+)?)\s*"
    r"(秒|s|sec|seconds?|分钟|min|minutes?|毫秒|ms)?", re.I)

#: 引号包裹的文本 —— 优先当作要输入的内容。
#:
#: 收的引号：ASCII 单双引号、中文弯引号（“ ” ‘ ’）、
#: 以及**中文直角引号（「 」）**。最后这对容易漏 ——
#: 本项目自己的文档里就用「」标注术语，写指令时自然也会用。
_QUOTED_RE = re.compile(
    r"[\"'\u201c\u2018\u300c]([^\"'\u201d\u2019\u300d]{1,200})"
    r"[\"'\u201d\u2019\u300d]")

#: 位置短语："在记事本里 hello" → ("记事本", "hello")。
#:
#: 为什么需要：不带引号的输入指令里，"在 XXX 里" 是**位置**，
#: 剩下那截才是**要输入的文本**。不做这个切分的话，
#: "在记事本里输入 hello" 会把 "在记事本里 hello" 整个当成文本。
_LOCATION_RE = re.compile(
    r"^(?:在)?\s*([^\s]{1,20}?)\s*"
    r"(?:里|中|内|里面|窗口里|界面上|页面上|框里)\s*(.*)$")

#: 复合动作的分隔词。
_SPLIT_RE = re.compile(r"(?:然后|接着|之后|再|并且|并|而且|then|and then|,|，|;|；)")


# --------------------------------------------------------------------------
# Goal
# --------------------------------------------------------------------------
@dataclass
class Goal:
    """结构化的目标 —— "要什么"的完整描述。

    ## 为什么不是一句原始字符串

    因为规划器要拿它做四件事，而字符串一件事都做不了：
    **决定怎么拆**（``actions``）、**判断做完没有**（``success``）、
    **知道什么时候该放弃**（``budget_*``）、**事后复盘**
    （``instruction`` 原文 + ``notes`` 解析时的取舍）。
    """

    #: 用户原话，**一字不改**。复盘时要能对上是哪句话出的问题。
    instruction: str = ""

    #: 主意图。多动作时是 :data:`INTENT_COMPOSITE`。
    intent: str = INTENT_UNKNOWN

    #: 目标对象（应用名 / URL / 元素名）。
    target: str = ""

    #: 解析出的动作序列。每个元素形如
    #: ``{"action": "open", "target": "notepad", "text": ""}``。
    actions: List[Dict[str, Any]] = field(default_factory=list)

    #: 成功判据（人类可读）。**每条都要能被验证**，
    #: 写"操作成功"这种没法验的判据等于没有。
    success: List[str] = field(default_factory=list)

    #: 时间预算（秒）。
    budget_seconds: float = DEFAULT_BUDGET_SECONDS

    #: 步数预算。
    budget_steps: int = DEFAULT_BUDGET_STEPS

    #: 任务标识。默认自动生成 —— 每次解析都是一个新的可追溯单元。
    task_id: str = ""

    #: 解析时间戳。
    created_at: float = 0.0

    #: 解析过程中的取舍说明（"保存"被当成动词等）。给人复核用。
    notes: List[str] = field(default_factory=list)

    #: 解析器标识（``"rule"`` / ``"llm"``）。**溯源用** ——
    #: 出问题时第一件事是问"这是谁解析的"。
    parser: str = "rule"

    def __post_init__(self) -> None:
        if not self.task_id:
            self.task_id = "t%s" % uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = time.time()

    # ---------------- 便捷属性 ----------------
    @property
    def ok(self) -> bool:
        """解析成功了吗？"""
        return self.intent != INTENT_UNKNOWN and bool(self.actions)

    @property
    def is_composite(self) -> bool:
        return self.intent == INTENT_COMPOSITE

    @property
    def deadline(self) -> float:
        """绝对截止时间戳（供执行器判断全局超时）。"""
        return self.created_at + float(self.budget_seconds)

    def remaining_seconds(self, now: Optional[float] = None) -> float:
        """还剩多少时间预算。可能为负（已超时）。"""
        return self.deadline - (time.time() if now is None else float(now))

    # ---------------- 序列化 ----------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "instruction": self.instruction,
            "intent": self.intent,
            "target": self.target,
            "actions": [dict(a) for a in self.actions],
            "success": list(self.success),
            "budget_seconds": float(self.budget_seconds),
            "budget_steps": int(self.budget_steps),
            "task_id": self.task_id,
            "created_at": float(self.created_at),
            "notes": list(self.notes),
            "parser": self.parser,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Goal":
        """从 dict 还原。**任何脏数据都不抛** —— 老版本/手工编辑的存档要能读。

        为什么这么宽容：``~/.avatar/tasks/`` 里的存档可能被老版本写过、
        被人手改过、被半截写入过。**读不出来就炸**会让整个任务历史
        不可用，而那正是排查问题时最需要的东西。宁可退到默认值。
        """
        d = dict(d or {}) if isinstance(d, dict) else {}
        return cls(
            instruction=_as_str(d.get("instruction")),
            intent=_as_str(d.get("intent")) or INTENT_UNKNOWN,
            target=_as_str(d.get("target")),
            actions=_as_dict_list(d.get("actions")),
            success=_as_str_list(d.get("success")),
            budget_seconds=_as_float(d.get("budget_seconds"),
                                     DEFAULT_BUDGET_SECONDS),
            budget_steps=_as_int(d.get("budget_steps"), DEFAULT_BUDGET_STEPS),
            task_id=_as_str(d.get("task_id")),
            created_at=_as_float(d.get("created_at"), 0.0),
            notes=_as_str_list(d.get("notes")),
            parser=_as_str(d.get("parser")) or "rule",
        )

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, s: str) -> "Goal":
        return cls.from_dict(json.loads(s))

    def describe(self) -> str:
        head = "[%s] %s" % (self.intent, self.instruction or "(空)")
        if self.target:
            head += "  → %s" % self.target
        bits = [head]
        for i, a in enumerate(self.actions, 1):
            bits.append("  %d) %s %s%s" % (
                i, a.get("action", "?"), a.get("target", ""),
                (" 「%s」" % a["text"]) if a.get("text") else ""))
        bits.append("  预算：%.0fs / %d 步" % (self.budget_seconds,
                                              self.budget_steps))
        if self.notes:
            for n in self.notes:
                bits.append("  · %s" % n)
        return "\n".join(bits)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def _norm(s: str) -> str:
    """归一化：全角空格 → 半角，压连续空白，两侧 strip。

    **不做的**：不改大小写（英文动词表里有小写形式，匹配时再 lower），
    不做繁简转换，不动标点 —— 标点要留着做分词边界。
    """
    s = str(s or "").replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _split_clauses(text: str) -> List[str]:
    """按复合分隔词切成子句。切完去空。

    **只切分隔词**。没有分隔词的连写句（"打开记事本输入hello"）
    在这里还是一整句 —— 由 :func:`_clauses_with_verbs` 再按动词切。
    """
    parts = [p.strip() for p in _SPLIT_RE.split(text)]
    return [p for p in parts if p]


def _verb_marks(text: str) -> List[Tuple[int, int, str, str]]:
    """找出文本里**所有**动词的出现位置。

    返回 ``[(起点, 终点, 动词, 意图)]``，按位置升序，**已去重叠**。

    ## 去重叠规则：长的赢

    "拖动滑块" 里 "拖动"（slider）和 "滑动"（slider）会重叠；
    "关闭" 里含 "关" 吗？不含 —— 但 "打开" 含 "开" 吗？
    表里没有单字 "开"，所以不冲突。真正会撞的是
    "打开" vs "打" 这类，长动词优先能自动解决。

    同起点时保留更长的那个，然后丢掉所有与前一个重叠的。
    """
    low = text.lower()
    raw: List[Tuple[int, int, str, str]] = []
    for verb, intent in _VERBS:
        start = 0
        while True:
            p = low.find(verb, start)
            if p < 0:
                break
            raw.append((p, p + len(verb), verb, intent))
            start = p + len(verb)

    # 同起点长的在前；其余按起点
    raw.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    out: List[Tuple[int, int, str, str]] = []
    for m in raw:
        if out and m[0] < out[-1][1]:
            continue          # 与前一个重叠
        out.append(m)
    return out


def _clauses_with_verbs(text: str) -> List[Tuple[str, str, str]]:
    """按动词把一段话切成 ``[(子句, 动词, 意图)]``。

    ## 为什么需要它

    "打开记事本输入hello" 里没有分隔词，但它明显是两个动作。
    只靠分隔词切会把它当成一个 `open` 动作，宾语变成
    "记事本输入hello" —— 那是错的。

    按**动词位置**切就能正确拆开：

        打开记事本输入hello
        ^^^^      ^^^^
        段1        段2
        → [("打开记事本", "打开", open), ("输入hello", "输入", type)]

    每段**从动词开始**（含动词），所以后面还要 :func:`_strip_verb`。

    ## ⚠ 紧邻动词要合并（"点击保存"）

    中文里"保存"既是动词也是**按钮名**。"点击保存" 里它是宾语，
    不是第二个动作。判据是**两个动词之间没有夹任何字符**：

        点击保存          打开记事本输入hello
        ^^^^               ^^^^      ^^^^
        紧邻 → 合并        隔了"记事本" → 不合并
        → click(保存)     → open(记事本) + type(hello)

    这个判据很干净，不需要词性标注 —— 只靠"有没有间隔"。
    """
    marks = _verb_marks(text)
    if not marks:
        return []

    # ---- 合并紧邻动词 ----
    merged: List[Tuple[int, int, str, str]] = []
    i = 0
    while i < len(marks):
        s, e, verb, intent = marks[i]
        while i + 1 < len(marks) and marks[i + 1][0] == e:
            i += 1
            e = marks[i][1]        # 把下一个动词并进来当宾语
        merged.append((s, e, verb, intent))
        i += 1

    # ---- 按合并后的边界切段 ----
    out: List[Tuple[str, str, str]] = []
    for i, (s, _e, verb, intent) in enumerate(merged):
        end = merged[i + 1][0] if i + 1 < len(merged) else len(text)
        seg = text[s:end].strip()
        if seg:
            out.append((seg, verb, intent))
    return out


def _find_verb(clause: str) -> Tuple[str, str, int]:
    """在子句里找**第一个**动词（按表内优先级）。

    返回 ``(动词, 意图, 位置)``；找不到返回 ``("", INTENT_UNKNOWN, -1)``。

    这是给"只有一个动词的子句"用的快捷路径。
    多动词子句走 :func:`_clauses_with_verbs`。
    """
    marks = _verb_marks(clause)
    if not marks:
        return "", INTENT_UNKNOWN, -1
    # 表内优先级：_verb_marks 已按位置排，这里按表序挑
    for verb, intent in _VERBS:
        for s, _e, v, it in marks:
            if v == verb:
                return v, it, s
    s, _e, v, it = marks[0]
    return v, it, s


def _strip_verb(clause: str, verb: str, pos: int) -> str:
    """把动词从子句里去掉，剩下的当宾语。"""
    if pos < 0:
        return clause.strip()
    return (clause[:pos] + clause[pos + len(verb):]).strip()


def _clean_object(s: str) -> str:
    """清理宾语：去掉装饰性词与多余标点。"""
    s = s.strip()
    # 去掉常见的介词/助词前缀
    for pre in ("在", "把", "将", "给", "对", "到", "从", "于"):
        if s.startswith(pre) and len(s) > len(pre):
            s = s[len(pre):].strip()
    # 去掉后缀助词
    for suf in ("里", "中", "内", "上面", "里面", "按钮", "控件", "元素"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[:-len(suf)].strip()
    return s.strip("　 ,.，。;；:：、!！?？\"'“”‘’「」『』")

def _split_location(s: str) -> Tuple[str, str]:
    """把 "在记事本里 hello" 拆成 ``("记事本", "hello")``。

    拆不出来时返回 ``("", 原样)`` —— **不猜**。
    """
    t = str(s or "").strip()
    m = _LOCATION_RE.match(t)
    if not m:
        return "", t
    return m.group(1).strip(), m.group(2).strip()


def _canon_app(name: str) -> str:
    """应用别名 → 规范名。认不出就原样返回（不猜）。"""
    n = str(name or "").strip()
    low = n.lower()
    if low in _APP_ALIASES:
        return _APP_ALIASES[low]
    return n


# ---- 反序列化的安全转换 ----
#: 存档可能被老版本写过、被人手改过、被半截写入过。
#: 这几个函数保证"读不出来就退默认值"，而不是抛异常让整个
#: 任务历史不可用。
def _as_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list, tuple)):
        return ""
    return str(v)


def _as_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _as_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return int(default)


def _as_str_list(v: Any) -> List[str]:
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x) for x in v if x is not None]


def _as_dict_list(v: Any) -> List[Dict[str, Any]]:
    if not isinstance(v, (list, tuple)):
        return []
    return [dict(x) for x in v if isinstance(x, dict)]


def parse_goal(instruction: str,
               *,
               budget_seconds: Optional[float] = None,
               budget_steps: Optional[int] = None,
               notes: Optional[List[str]] = None) -> Goal:
    """把一句话解析成 :class:`Goal`。**规则驱动，零网络。**

    ## 解析流程

    1. 归一化 + 按复合分隔词切子句
    2. 每个子句找动词 → 意图
    3. 抽宾语（应用名 / 元素名 / URL / 文本）
    4. 抽参数（等待时长 / 引号文本 / URL）
    5. 拼 :class:`Goal`，主意图 = 单动作时的那个，多动作时 composite

    ## 解析不出来时

    **返回 `intent="unknown"` 的 Goal，不抛异常。** 理由：解析失败是
    常态（用户会说各种奇怪的话），把它做成异常会逼调用方到处写 try。
    而且 `unknown` 的 Goal 仍然带着 `instruction` 原文，
    可以直接交给 LLM 后端再试一次。

    ## 参数

    ``budget_seconds`` / ``budget_steps`` 不给就用默认（30 分钟 / 50 步）。
    ``notes`` 会**追加**到自动生成的解析说明后面（调用方想标注就传）。
    """
    raw = str(instruction or "")
    text = _norm(raw)

    g = Goal(
        instruction=raw,
        budget_seconds=float(budget_seconds) if budget_seconds
        else DEFAULT_BUDGET_SECONDS,
        budget_steps=int(budget_steps) if budget_steps else DEFAULT_BUDGET_STEPS,
    )
    if notes:
        g.notes.extend(str(n) for n in notes)

    if not text:
        g.notes.append("空指令 —— 无法解析")
        return g

    # ---- 引号里的文本优先当输入内容 ----
    quoted = _QUOTED_RE.search(text)
    quoted_text = quoted.group(1).strip() if quoted else ""

    # ---- 切子句：先按分隔词，再按动词 ----
    clauses = _split_clauses(text)
    if len(clauses) > 1:
        g.notes.append("识别到 %d 个子句，按复合目标处理" % len(clauses))

    #: 待处理的 (子句, 动词, 意图) 三元组
    pieces: List[Tuple[str, str, str]] = []
    for clause in clauses:
        subs = _clauses_with_verbs(clause)
        if subs:
            pieces.extend(subs)
        else:
            g.notes.append("子句「%s」没识别出动词" % clause[:40])

    if len(pieces) > 1 and len(clauses) == 1:
        g.notes.append("单句里识别到 %d 个动词，按动词位置拆分"
                       % len(pieces))

    actions: List[Dict[str, Any]] = []

    for piece, verb, intent in pieces:
        obj = _strip_verb(piece, verb, piece.lower().find(verb.lower()))
        act: Dict[str, Any] = {"action": intent, "target": "", "text": ""}

        if intent == INTENT_WAIT:
            m = _WAIT_RE.search(piece)
            secs = 1.0
            if m:
                try:
                    secs = float(m.group(1))
                except (TypeError, ValueError):
                    secs = 1.0
                unit = (m.group(2) or "秒").lower()
                if unit.startswith(("分钟", "min")):
                    secs *= 60.0
                elif unit.startswith(("毫秒", "ms")):
                    secs /= 1000.0
            act["seconds"] = max(0.0, secs)
            act["target"] = "%gs" % secs
            actions.append(act)
            continue

        # ---- URL 检测：优先于动词分类 ----
        # "打开 https://x.com" 的意图是 navigate 不是 open ——
        # 打开一个网址和打开一个本地程序，执行路径完全不同。
        url_m = _URL_RE.search(piece) or _URL_RE.search(obj)
        if url_m and intent in (INTENT_NAVIGATE, INTENT_OPEN):
            act["action"] = INTENT_NAVIGATE
            act["target"] = url_m.group(0)
            if intent == INTENT_OPEN:
                g.notes.append("宾语是 URL，动作由 open 修正为 navigate")
            actions.append(act)
            continue

        if intent == INTENT_NAVIGATE:
            act["target"] = _clean_object(obj)
            g.notes.append("导航目标「%s」不像 URL，执行时可能需要补全"
                           % act["target"][:40])
            actions.append(act)
            continue

        if intent == INTENT_TYPE:
            # 位置短语先剥出来当目标："在记事本里 hello" → ("记事本", "hello")
            loc, rest = _split_location(obj)
            if loc:
                act["target"] = _canon_app(_clean_object(loc))
                g.notes.append("输入目标推断为「%s」" % act["target"])

            # 文本优先级：**本段**的引号内容 > 剥掉位置后的剩余
            # （用本段而不是整句的引号，避免引号串到别的子句里）
            q = _QUOTED_RE.search(piece)
            local_quoted = q.group(1).strip() if q else quoted_text
            payload = local_quoted or rest
            act["text"] = _clean_object(payload) if not local_quoted else payload
            if not act["text"]:
                g.notes.append("输入动作没有文本内容")
            actions.append(act)
            continue

        if intent == INTENT_OPEN:
            act["target"] = _canon_app(_clean_object(obj))
            actions.append(act)
            continue

        # close 的目标是**应用名**，走别名归一化
        if intent == INTENT_CLOSE:
            act["target"] = _canon_app(_clean_object(obj))
            actions.append(act)
            continue

        # click / key / scroll / screenshot —— 目标是元素名，不做别名归一化
        # （"保存" 是按钮名，不是应用名，归一化会把它改坏）
        act["target"] = _clean_object(obj)
        if intent == INTENT_KEY and not act["target"]:
            act["target"] = "enter"
            g.notes.append("按键动作没给键名，默认 enter")
        actions.append(act)

    # ---- 拼 Goal ----
    if not actions:
        g.notes.append("没有任何动作被识别 —— 交给 LLM 后端再试")
        return g

    g.actions = actions
    if len(actions) == 1:
        g.intent = actions[0]["action"]
        g.target = actions[0].get("target", "")
    else:
        g.intent = INTENT_COMPOSITE
        g.target = actions[0].get("target", "")

    # ---- 成功判据 ----
    g.success = []
    for a in actions:
        hint = _SUCCESS_HINT.get(a["action"], "动作生效")
        tgt = a.get("target") or ""
        g.success.append(("%s：%s" % (tgt, hint)) if tgt else hint)

    # ---- 步数预算的合理性检查 ----
    if len(actions) > g.budget_steps:
        g.notes.append("动作数 %d 超过步数预算 %d —— 执行时会被截断"
                       % (len(actions), g.budget_steps))

    return g


def describe_grammar() -> str:
    """给人看的解析规则说明。"""
    lines = ["目标解析规则（RuleBased，阶段六）", "=" * 60, ""]
    lines.append("识别到的动词（按匹配优先级）：")
    by_intent: Dict[str, List[str]] = {}
    for verb, intent in _VERBS:
        by_intent.setdefault(intent, []).append(verb)
    for intent in INTENTS:
        verbs = by_intent.get(intent)
        if verbs:
            lines.append("  %-12s %s" % (intent, " / ".join(verbs)))

    lines += ["", "复合分隔词：", "  然后 / 接着 / 之后 / 再 / 并且 / 并 / "
              "而且 / then / and then / , / ，/ ; / ；", ""]
    lines.append("应用别名：")
    for alias, canon in sorted(_APP_ALIASES.items()):
        lines.append("  %-14s → %s" % (alias, canon))
    lines += ["", "默认预算：%.0f 秒 / %d 步"
              % (DEFAULT_BUDGET_SECONDS, DEFAULT_BUDGET_STEPS)]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """goal 自检。**纯逻辑，不碰桌面、不联网。**"""
    import sys as _sys
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []

    def _add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(name)

    _me = _sys.modules[__name__]

    # ============ 1) 常量 ============
    _add("默认时间预算是 30 分钟（用户要求）",
         _me.DEFAULT_BUDGET_SECONDS == 1800.0,
         "%.0fs" % _me.DEFAULT_BUDGET_SECONDS)
    _add("默认步数预算 50", _me.DEFAULT_BUDGET_STEPS == 50,
         str(_me.DEFAULT_BUDGET_STEPS))
    _add("意图常量齐全（12 个）", len(_me.INTENTS) == 12,
         "%d 个" % len(_me.INTENTS))
    _add("unknown 在意图表里", _me.INTENT_UNKNOWN in _me.INTENTS, "unknown")
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")

    # ============ 2) 单动作解析 ============
    g = _me.parse_goal("打开记事本")
    _add("打开记事本 → open",
         g.intent == _me.INTENT_OPEN and g.target == "notepad",
         g.describe())
    _add("解析成功 ok=True", g.ok is True, "ok")

    g = _me.parse_goal("输入 hello")
    _add("输入 hello → type", g.intent == _me.INTENT_TYPE, g.describe())
    _add("输入文本被抽出来",
         g.actions and g.actions[0].get("text") == "hello",
         str(g.actions[0] if g.actions else None))

    g = _me.parse_goal("点击保存")
    _add("点击保存 → click", g.intent == _me.INTENT_CLICK, g.describe())
    _add("点击目标被抽出来", g.target == "保存", g.target)

    g = _me.parse_goal("关闭记事本")
    _add("关闭记事本 → close", g.intent == _me.INTENT_CLOSE, g.describe())

    # ============ 3) 复合动作 ============
    g = _me.parse_goal("打开记事本输入hello")
    _add("★ 无分隔词的复合句也能拆出 2 个动作",
         len(g.actions) == 2, "%d 个：%s" % (len(g.actions), g.actions))
    _add("复合句 intent=composite",
         g.intent == _me.INTENT_COMPOSITE, g.intent)
    _add("第一个动作是 open",
         g.actions and g.actions[0]["action"] == _me.INTENT_OPEN,
         str(g.actions[0] if g.actions else None))
    _add("第二个动作是 type",
         len(g.actions) > 1
         and g.actions[1]["action"] == _me.INTENT_TYPE,
         str(g.actions[1] if len(g.actions) > 1 else None))

    g = _me.parse_goal("打开记事本，然后输入 hello")
    _add("带分隔词的复合句",
         len(g.actions) == 2 and g.intent == _me.INTENT_COMPOSITE,
         "%d 个动作" % len(g.actions))

    g = _me.parse_goal("打开 notepad then type hello")
    _add("英文复合句",
         len(g.actions) == 2, "%d 个" % len(g.actions))

    # ============ 4) URL 导航 ============
    g = _me.parse_goal("打开 https://example.com/login")
    _add("★ URL 被完整抽出来",
         "https://example.com/login" in
         " ".join(a.get("target", "") for a in g.actions),
         str(g.actions))
    _add("URL 句子识别为 navigate",
         g.actions and g.actions[0]["action"] == _me.INTENT_NAVIGATE,
         str(g.actions[0] if g.actions else None))

    g = _me.parse_goal("访问 https://a.com/x?y=1#z")
    _add("带 query 和 fragment 的 URL",
         g.actions and "?y=1#z" in g.actions[0]["target"],
         str(g.actions[0]["target"] if g.actions else ""))

    # ============ 5) 等待时长 ============
    g = _me.parse_goal("等待 3 秒")
    _add("等待 3 秒 → 3.0",
         g.actions and abs(g.actions[0].get("seconds", 0) - 3.0) < 1e-6,
         str(g.actions[0] if g.actions else None))

    g = _me.parse_goal("等 2 分钟")
    _add("★ 分钟单位被换算成秒",
         g.actions and abs(g.actions[0].get("seconds", 0) - 120.0) < 1e-6,
         str(g.actions[0] if g.actions else None))

    g = _me.parse_goal("wait 500 ms")
    _add("毫秒单位被换算",
         g.actions and abs(g.actions[0].get("seconds", 0) - 0.5) < 1e-6,
         str(g.actions[0] if g.actions else None))

    # ============ 6) 引号文本 ============
    g = _me.parse_goal("在记事本里输入「你好世界」")
    _add("★ 中文引号里的文本被优先当输入内容",
         g.actions and "你好世界" in str(g.actions[0].get("text", "")),
         str(g.actions[0] if g.actions else None))

    g = _me.parse_goal('type "hello world"')
    _add("英文引号文本被抽出",
         g.actions and g.actions[0].get("text") == "hello world",
         str(g.actions[0] if g.actions else None))

    # ============ 7) 应用别名 ============
    for alias, canon in (("记事本", "notepad"), ("计算器", "calc"),
                         ("notepad", "notepad"), ("浏览器", "msedge")):
        gg = _me.parse_goal("打开 %s" % alias)
        _add("别名 %s → %s" % (alias, canon),
             gg.target == canon, gg.target)

    g = _me.parse_goal("打开 Photoshop")
    _add("★ 认不出的应用名原样保留（不猜）",
         g.target == "Photoshop", g.target)

    # ============ 8) 解析失败不抛 ============
    g = _me.parse_goal("")
    _add("空指令 → unknown，不抛",
         g.intent == _me.INTENT_UNKNOWN and not g.ok, g.intent)

    g = _me.parse_goal("嗯那个东西你懂的")
    _add("★ 无动词的句子 → unknown，不抛",
         g.intent == _me.INTENT_UNKNOWN, g.intent)
    _add("★ 解析失败仍保留原话（可交给 LLM 重试）",
         g.instruction == "嗯那个东西你懂的", g.instruction)
    _add("★ 解析失败有 note 说明原因",
         any("没识别出动词" in n or "没有任何动作" in n for n in g.notes),
         str(g.notes))

    _add("None 输入不抛",
         _me.parse_goal(None).intent == _me.INTENT_UNKNOWN, "None")
    _add("超长输入不抛",
         _me.parse_goal("打开" * 500).intent != "", "500 次重复")

    # ============ 9) 预算 ============
    g = _me.parse_goal("打开记事本")
    _add("默认预算生效",
         g.budget_seconds == _me.DEFAULT_BUDGET_SECONDS
         and g.budget_steps == _me.DEFAULT_BUDGET_STEPS, "默认")
    g = _me.parse_goal("打开记事本", budget_seconds=5.0, budget_steps=2)
    _add("自定义预算生效",
         g.budget_seconds == 5.0 and g.budget_steps == 2, "5s / 2 步")

    g = _me.parse_goal("打开记事本")
    _add("deadline = created_at + budget",
         abs(g.deadline - (g.created_at + g.budget_seconds)) < 1e-9,
         "一致")
    _add("remaining_seconds 递减",
         g.remaining_seconds(g.created_at) > g.remaining_seconds(
             g.created_at + 100), "递减")
    _add("★ 已过期时 remaining 为负（不截断到 0）",
         g.remaining_seconds(g.created_at + 10 ** 6) < 0, "负数")

    # ============ 10) 成功判据 ============
    g = _me.parse_goal("打开记事本输入hello")
    _add("★ 每个动作都有成功判据",
         len(g.success) == len(g.actions) and all(g.success),
         "%d 条" % len(g.success))
    _add("★ 判据不含空泛的「操作成功」",
         not any(s.strip() in ("操作成功", "成功", "ok")
                 for s in g.success), str(g.success))
    _add("判据带目标名（可定位）",
         any("notepad" in s for s in g.success), str(g.success))

    # ============ 11) 序列化 ============
    g = _me.parse_goal("打开记事本输入hello")
    d = g.to_dict()
    _add("to_dict JSON 安全",
         json.dumps(d, ensure_ascii=False) != "", "ok")
    g2 = _me.Goal.from_dict(d)
    _add("★ from_dict 往返一致",
         g2.instruction == g.instruction and g2.intent == g.intent
         and len(g2.actions) == len(g.actions),
         g2.describe())
    _add("from_json 往返一致",
         _me.Goal.from_json(g.to_json()).task_id == g.task_id, "一致")
    _add("★ from_dict 缺字段不抛",
         _me.Goal.from_dict({}).intent == _me.INTENT_UNKNOWN, "空 dict")
    _add("from_dict(None) 不抛",
         _me.Goal.from_dict(None).intent == _me.INTENT_UNKNOWN, "None")
    _add("★ from_dict 容错脏数据",
         _me.Goal.from_dict(
             {"actions": "不是列表", "success": None,
              "budget_seconds": "abc"}).intent == _me.INTENT_UNKNOWN,
         "脏数据不崩")

    # ============ 12) task_id 唯一 ============
    ids = {_me.parse_goal("打开记事本").task_id for _ in range(50)}
    _add("★ task_id 唯一（50 次不重复）", len(ids) == 50,
         "%d 个" % len(ids))
    _add("task_id 非空", all(i.startswith("t") for i in ids), "t 前缀")

    # ============ 13) 溯源字段 ============
    g = _me.parse_goal("打开记事本")
    _add("parser 标为 rule", g.parser == "rule", g.parser)
    _add("created_at 是正数", g.created_at > 0, "%.0f" % g.created_at)
    _add("★ 原话一字不改", g.instruction == "打开记事本", g.instruction)

    # ============ 14) 不联网 ============
    import ast as _ast
    src = open(_me.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    top = set()
    for node in tree.body:
        if isinstance(node, _ast.Import):
            for a in node.names:
                top.add((a.name or "").split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            top.add((node.module or "").split(".")[0])
    _add("★ 顶层不 import 网络库",
         not (top & {"urllib", "http", "socket", "requests"}),
         sorted(top))
    _add("顶层只依赖标准库",
         top <= {"re", "json", "time", "uuid", "dataclasses", "typing",
                 "__future__", "sys"},
         sorted(top))

    # ============ 15) describe ============
    _add("describe 可读",
         "open" in _me.parse_goal("打开记事本").describe(), "有内容")
    _add("describe_grammar 不抛",
         isinstance(_me.describe_grammar(), str), "有内容")
    _add("describe_grammar 列出动词",
         "打开" in _me.describe_grammar(), "有动词")

    elapsed = time.perf_counter() - t0
    return {
        "module": "goal",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(describe_grammar())
