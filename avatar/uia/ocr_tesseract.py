# -*- coding: utf-8 -*-
"""OCR 备选引擎 —— 外挂 Tesseract，用 subprocess 调 `tesseract.exe`。

## 这一层解决什么

WinRT OCR（``uia/ocr_fallback.py``）在本机实测**位图通路不通**：
`SoftwareBitmap.CreateCopyFromBuffer` 恒返回 `E_INVALIDARG`。
引擎能建、语言包也在，但图喂不进去 —— 五层降级链的 OCR 层
实际不可用。

本模块是那条被堵死的路的**旁路**：不碰 WinRT，把 GDI 位图存成
临时 BMP 文件，调外部 `tesseract.exe` 识别，解析它的 TSV 输出。

```
GDI 抓屏（vision.capture.Bitmap）
    ↓  BGRA 像素（内存序，DIB 自下而上）
BMP 临时文件（纯字节拼接，零依赖）
    ↓  subprocess: tesseract in.bmp stdout -l chi_sim+eng tsv
TSV 文本（level/page/block/par/line/word/left/top/w/h/conf/text）
    ↓  只取 level=5（词级），conf 归一化到 0~1
[OcrWord]（坐标相对传入图像，与 WinRT 路径口径一致）
```

## 为什么是 subprocess 而不是 Python 绑定

- **零依赖**。不引 `pytesseract`、不引 `tesserocr`。Tesseract 是
  **可选**的：没装就是不可用，主功能（UIA 通道）完全不受影响。
- 进程隔离。Tesseract 崩了不会带走主进程 —— subprocess 崩掉只是
  一次识别失败，抛 :class:`~avatar.uia.ocr_fallback.OcrUnavailable`。
- 一条命令的事。写 BMP + 调一次 + 解析 TSV，比调 C API 绑定简单得多。

## ⚠ Tesseract 是可选依赖

- 没装：``is_available()`` 返回 False，上层自动跳过 OCR 层
  （走 UIA 或视觉链的其他层）。
- 装了但缺 `chi_sim` 语言包：``is_available()`` 仍为 True（exe 能跑），
  但识别中文会失败 —— ``languages()`` 能看出装了哪些语言。
- **subprocess 一律带超时**（默认 10s）。Tesseract 在大图上可能跑很久，
  没超时就是把自己的命交给外部进程。

## 能力边界

**做**：

- 探测 `tesseract.exe` 是否可用（PATH + 常见安装路径 + 真跑
  `--version` 验证，三层都过才算可用）
- 列出已安装的识别语言（`--list-langs`）
- 把 ``vision.capture.Bitmap``（或同构对象）写成临时 BMP
- 识别整图或指定区域（先裁剪再存盘，省 IO 也提准）
- 解析 TSV：只取词级（level=5），带坐标与置信度
- 语言映射：中文名 / BCP-47 风格 → Tesseract 语言码
  （默认 ``chi_sim+eng``，中英混排）

**不做**：

- **不安装 Tesseract**。缺了就报不可用，附安装指引。装不装是
  用户的选择。
- **不做图像预处理**。不二值化、不去噪、不放大。图不清楚就是不清楚 ——
  预处理是"让 OCR 猜得更准"，而猜出来的坐标拿去点击是危险的。
  （与 WinRT 路径同一口径。）
- **不做版面分析**。Tesseract 给的是"块 → 段 → 行 → 词"，本模块
  只透出词级结果，不合并段落、不重排阅读顺序。
- **不缓存识别结果**。界面变了结果就该变，缓存反而是错的。
- **不硬解验证题**（硬约束 #9 全项目生效）。识别出"拖动滑块"四个字
  的目的是**叫人**，不是去算缺口在哪。
- **不引第三方 Python 库**。`pytesseract` 之类的包装一律不引 ——
  那是把"可选依赖"偷偷变成"必需依赖"。
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# ⚠ 顶部只 import 标准库 + 本包的类型定义。
#   `ocr_fallback` 里的 OcrWord / OcrUnavailable 是**数据契约**，
#   顶部 import 它们不触发 WinRT 初始化（那些是函数内的）。
#   `vision.capture`（桌面抓屏）绝不在这里 import —— 那是调用方的
#   事，本模块只吃"位图对象"。
from .ocr_fallback import OcrUnavailable, OcrWord

__all__ = [
    "CAPABILITY",
    "DEFAULT_TIMEOUT",
    "DEFAULT_LANGUAGE",
    "LANGUAGE_MAP",
    "COMMON_PATHS",
    "exe_path",
    "version",
    "is_available",
    "unavailable_reason",
    "languages",
    "recognize",
    "describe_environment",
    "selfcheck",
]

#: 本模块在降级链里的名字（与 ocr_fallback 的 CAPABILITY 相同 ——
#: 它是同一层能力的另一个实现）
CAPABILITY = "ocr"

#: 单次识别的 subprocess 超时（秒）。
#: ⚠ 必须带超时 —— 大图上 Tesseract 能跑几十秒，
#:   没超时就是把自己的命交给外部进程。
DEFAULT_TIMEOUT = 10.0

#: 探测（``--version``）的超时。比识别短得多 —— 它只是确认 exe 能跑。
DEFAULT_PROBE_TIMEOUT = 5.0

#: 默认语言。中英混排是最常见的界面场景。
#: ⚠ 需要用户装 ``chi_sim.traineddata``；只有 eng 是安装包自带的。
DEFAULT_LANGUAGE = "chi_sim+eng"

#: 可读语言名 → Tesseract 语言码。
#: 注意 Tesseract 的码与 BCP-47 不同（``chi_sim`` 不是 ``zh``）。
LANGUAGE_MAP = {
    "中文": "chi_sim+eng",
    "简体中文": "chi_sim+eng",
    "简体": "chi_sim",
    "繁體中文": "chi_tra",
    "繁体": "chi_tra",
    "英语": "eng",
    "英文": "eng",
    "日语": "jpn",
    "韩语": "kor",
    "中英": "chi_sim+eng",
}

#: Windows 上 Tesseract 的常见安装位置（PATH 找不到时按序试）。
#: 官方安装包默认装这里；装到别处的用户自己加 PATH。
COMMON_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    os.path.expandvars(r"%USERPROFILE%\scoop\shims\tesseract.exe"),
)


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------
#: 探测结果缓存。``done`` = 探测过了（结果可能是"不可用"）。
_PROBE: Dict[str, Any] = {"done": False, "ok": False, "exe": "",
                          "version": "", "reason": "", "langs": []}
_PROBE_LOCK = threading.Lock()


def _resolve_exe() -> str:
    """找 tesseract.exe。返回空串表示找不到。

    顺序：PATH → 常见安装路径。**不验证能不能跑**（那一步留给
    :func:`_probe`，探测一次就够）。
    """
    found = shutil.which("tesseract")
    if found:
        return found
    for p in COMMON_PATHS:
        try:
            if p and os.path.isfile(p):
                return p
        except OSError:
            continue
    return ""


def _run_version(exe: str) -> str:
    """跑 ``tesseract --version``。返回版本行，失败返回空串。

    ⚠ Tesseract 把版本信息打到 **stderr**，不是 stdout。
    """
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True, timeout=DEFAULT_PROBE_TIMEOUT,
            encoding="utf-8", errors="replace")
        blob = (proc.stdout or "") + (proc.stderr or "")
        for line in blob.splitlines():
            line = line.strip()
            if line.lower().startswith("tesseract"):
                return line
        return blob.strip().splitlines()[0] if blob.strip() else ""
    except Exception:      # noqa: BLE001
        return ""


def _run_langs(exe: str) -> List[str]:
    """跑 ``tesseract --list-langs``。失败返回空列表。"""
    try:
        proc = subprocess.run(
            [exe, "--list-langs"],
            capture_output=True, timeout=DEFAULT_PROBE_TIMEOUT,
            encoding="utf-8", errors="replace")
        out = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line or line.lower().startswith("list of"):
                continue
            out.append(line)
        return out
    except Exception:      # noqa: BLE001
        return []


def _probe(force: bool = False) -> Dict[str, Any]:
    """探测 Tesseract 可用性。**结果缓存**（探测要跑一次子进程）。"""
    with _PROBE_LOCK:
        if _PROBE["done"] and not force:
            return dict(_PROBE)

        res: Dict[str, Any] = {"done": True, "ok": False, "exe": "",
                               "version": "", "reason": "", "langs": []}
        exe = _resolve_exe()
        if not exe:
            res["reason"] = (
                "PATH 和常见安装路径里都找不到 tesseract.exe。"
                "装了的话把它加进 PATH，或看 docs/ocr.md 的安装指引。")
        else:
            ver = _run_version(exe)
            if not ver:
                res["reason"] = (
                    "找到 %s 但 `tesseract --version` 跑不起来"
                    "（可能缺 VC++ 运行库）。" % exe)
            else:
                res["ok"] = True
                res["exe"] = exe
                res["version"] = ver
                res["langs"] = _run_langs(exe)

        _PROBE.update(res)
        return dict(_PROBE)


def exe_path() -> str:
    """tesseract.exe 的路径。不可用返回空串。"""
    return _probe()["exe"]


def version() -> str:
    """版本行（如 ``tesseract 5.3.3``）。不可用返回空串。"""
    return _probe()["version"]


def is_available() -> bool:
    """Tesseract 现在能用吗？**真的去探测，不假设。**"""
    return bool(_probe()["ok"])


def unavailable_reason() -> str:
    """不可用的原因（一句话）。可用时返回空串。"""
    return str(_probe()["reason"] or "")


def languages() -> List[str]:
    """已安装的 Tesseract 语言码（如 ``["chi_sim", "eng"]``）。

    不可用返回空列表（不抛）。
    """
    return list(_probe()["langs"])


# --------------------------------------------------------------------------
# 位图 → BMP 文件
# --------------------------------------------------------------------------
def _to_bmp_bytes(bmp: Any) -> bytes:
    """把位图对象写成 32 位 BMP 字节流。

    ## 为什么是 BMP 不是 PNG

    PNG 要 zlib + CRC，写对了不难但没必要：这是给 Tesseract 吃的
    临时文件，体积不是问题（1920x1080 约 8MB，用完就删）。
    BMP 是**纯字节拼接**，整个写入只有 struct 打包 —— 零依赖。
    项目在 ``risk_detector/context.py`` 里已经用同一招写熔断截图。

    ## 格式细节

    - 32 位 BGRA，``BI_RGB``（不压缩）
    - **高度写负数** —— 表示自上而下。capture 的 Bitmap 是内存序
      BGRA、按行正序（第 0 行 = 图的最上面，尽管 DIB 惯例是自下而上
      —— 本项目 capture 的 ``pixels`` 已按行正序排好），
      用负高度就不必翻转。
    """
    w = int(getattr(bmp, "width", 0) or 0)
    h = int(getattr(bmp, "height", 0) or 0)
    px = getattr(bmp, "pixels", None)
    if w <= 0 or h <= 0 or px is None:
        raise OcrUnavailable("位图尺寸或像素异常：%dx%d" % (w, h))

    stride = int(getattr(bmp, "stride", 0) or 0) or (w * 4)
    row_bytes = w * 4
    pad = (-row_bytes) % 4          # 32 位恒为 0，留着兜底
    img_size = (row_bytes + pad) * h

    file_header = struct.pack(
        "<2sIHHI",
        b"BM",
        14 + 40 + img_size,
        0, 0,
        14 + 40)
    info_header = struct.pack(
        "<IiiHHIIiiII",
        40, w, -h,                   # **负高度 = 自上而下**
        1, 32,                       # 平面数 / 位深 BGRA
        0,                           # BI_RGB，不压缩
        img_size,
        2835, 2835,
        0, 0)

    mv = memoryview(px)
    if len(mv) < stride * h:
        # 紧凑缓冲退一步按 w*4 算
        stride = w * 4
        if len(mv) < stride * h:
            raise OcrUnavailable(
                "像素数据不足：需要 %d 字节，只有 %d" % (stride * h, len(mv)))

    out = bytearray()
    out += file_header
    out += info_header
    if stride == row_bytes and pad == 0:
        out += mv[:stride * h]
    else:
        for y in range(h):
            off = y * stride
            out += mv[off:off + row_bytes]
            if pad:
                out += b"\x00" * pad
    return bytes(out)


def _crop(bmp: Any, region: Optional[Tuple[int, int, int, int]]) -> Any:
    """裁剪位图。**没有裁剪方法就原样返回**（不报错）。

    ``region`` 是 ``(left, top, right, bottom)``，相对传入图像。
    """
    if not region:
        return bmp
    fn = getattr(bmp, "region", None)
    if callable(fn):
        try:
            l, t, r, b = region
            return fn(int(l), int(t), max(1, int(r - l)), max(1, int(b - t)))
        except Exception:      # noqa: BLE001
            return bmp
    return bmp


# --------------------------------------------------------------------------
# TSV 解析
# --------------------------------------------------------------------------
#: TSV 的列（``tesseract ... tsv`` 的输出）。
#: 只有 level=5 是**词级**；1=页 2=块 3=段 4=行 5=词。
TSV_LEVEL_WORD = 5


def _parse_tsv(text: str) -> List[OcrWord]:
    """解析 Tesseract 的 TSV 输出 → ``[OcrWord]``。

    ## TSV 的列

    ``level page_num block_num par_num line_num word_num left top
    width height conf text``（12 列，制表符分隔）

    ## 三个坑

    1. **conf 是 0~100**，不是 0~1 —— 要除以 100。
    2. **conf = -1** 出现在非词级行（level != 5），直接跳过。
    3. **text 可能为空**（纯图形块），也跳过。
    4. ``line`` 用 ``(block, par, line)`` 三元组映射成递增序号 ——
       直接拿 line_num 会在跨块时重复。
    """
    words: List[OcrWord] = []
    if not text:
        return words

    line_map: Dict[Tuple[int, int, int], int] = {}
    next_line = 0

    for row in text.splitlines()[1:]:        # 第一行是表头
        if not row.strip():
            continue
        cols = row.split("\t")
        if len(cols) < 12:
            continue
        try:
            level = int(cols[0])
            left = int(cols[6])
            top = int(cols[7])
            width = int(cols[8])
            height = int(cols[9])
            conf = float(cols[10])
        except (ValueError, IndexError):
            continue

        if level != TSV_LEVEL_WORD:
            continue
        word_text = cols[11]
        if not word_text.strip():
            continue

        # (block, par, line) → 递增行号
        try:
            key = (int(cols[2]), int(cols[3]), int(cols[4]))
        except ValueError:
            key = (-1, -1, next_line)
        if key not in line_map:
            line_map[key] = next_line
            next_line += 1

        words.append(OcrWord(
            text=word_text,
            rect=(left, top, left + width, top + height),
            line=line_map[key],
            confidence=max(0.0, conf / 100.0),
            engine="tesseract"))
    return words


# --------------------------------------------------------------------------
# 识别入口
# --------------------------------------------------------------------------
def _map_language(lang: str) -> str:
    """语言名 → Tesseract 码。认不出来的原样透出（Tesseract 会报错，
    那时错误信息里有它支持的语言列表，比这里瞎猜有用）。"""
    key = str(lang or "").strip()
    if not key:
        return DEFAULT_LANGUAGE
    return LANGUAGE_MAP.get(key, key)


def recognize(bitmap: Any, *,
              region: Optional[Tuple[int, int, int, int]] = None,
              language: str = "",
              timeout: float = DEFAULT_TIMEOUT) -> List[OcrWord]:
    """识别一张位图。返回**相对该图**的词列表。

    ## 参数

    - ``bitmap`` —— ``vision.capture.Bitmap``（或有
      ``width`` / ``height`` / ``stride`` / ``pixels`` / ``region`` 的对象）
    - ``region`` —— 先裁剪再识别，``(left, top, right, bottom)``
      相对传入图像。
    - ``language`` —— 中文名（"中文"/"英文"）或 Tesseract 码
      （``chi_sim+eng``）。留空用默认 ``chi_sim+eng``。
    - ``timeout`` —— subprocess 超时（秒）。**默认 10s。**

    ## 不可用时

    抛 :class:`~avatar.uia.ocr_fallback.OcrUnavailable`，消息带原因。
    上层捕获它沿降级链往下走。
    """
    if bitmap is None:
        raise OcrUnavailable("没有位图可识别")

    probe = _probe()
    if not probe["ok"]:
        raise OcrUnavailable(
            probe["reason"] or "Tesseract 不可用",
            hint="安装 Tesseract 并把它加进 PATH，见 docs/ocr.md")

    bmp = _crop(bitmap, region)
    lang = _map_language(language)

    tmp_dir = tempfile.mkdtemp(prefix="avatar_tess_")
    img_path = os.path.join(tmp_dir, "ocr_in.bmp")
    try:
        data = _to_bmp_bytes(bmp)
        with open(img_path, "wb") as f:
            f.write(data)

        # stdout 作为输出名 —— TSV 直接进管道，不用管临时输出文件
        cmd = [probe["exe"], img_path, "stdout", "-l", lang, "tsv"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True,
                timeout=max(0.5, float(timeout)),
                encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            raise OcrUnavailable(
                "Tesseract 识别超时（%.1fs）。图太大就传 region 限定范围。"
                % float(timeout), method="tesseract")
        except OSError as exc:
            raise OcrUnavailable("调不起 tesseract：%s" % exc,
                                 method="tesseract")

        # 语言包缺失是最常见的失败：stderr 里有明确提示
        err = (proc.stderr or "").strip()
        if proc.returncode != 0 and not (proc.stdout or "").strip():
            raise OcrUnavailable(
                "Tesseract 退出码 %d：%s"
                % (proc.returncode, err.splitlines()[-1][:120] if err else "(无输出)"),
                method="tesseract",
                hint=("语言包 %r 没装 —— `tesseract --list-langs` 看已装的，"
                      "缺 chi_sim 要单独下 traineddata" % lang))

        return _parse_tsv(proc.stdout or "")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# 环境描述
# --------------------------------------------------------------------------
def describe_environment() -> Dict[str, Any]:
    """Tesseract 环境快照。**永不抛** —— 诊断函数不该因为诊断失败而失败。"""
    try:
        probe = _probe()
        return {
            "capability": CAPABILITY,
            "engine": "tesseract",
            "available": probe["ok"],
            "exe": probe["exe"],
            "version": probe["version"],
            "languages": probe["langs"],
            "reason": probe["reason"],
            "default_language": DEFAULT_LANGUAGE,
            "backend": "tesseract (subprocess, optional)",
        }
    except Exception as exc:      # noqa: BLE001
        return {"capability": CAPABILITY, "available": False,
                "engine": "tesseract", "exe": "", "version": "",
                "languages": [], "reason": "%s: %s" % (type(exc).__name__, exc),
                "default_language": DEFAULT_LANGUAGE, "backend": ""}


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selfcheck(quick: bool = True) -> dict:
    """Tesseract 引擎自检。

    ## quick 档（默认，< 1s）

    纯逻辑 + 一次 ``--version`` 探测：契约、BMP 头正确性、
    TSV 解析（合成数据）、语言映射、超时常量、可用性自洽。

    ## full 档

    追加**真跑一次**识别（合成空白图 —— 没装就是 SKIP，
    装了则验证 subprocess 通路真的通）。这一项随机器负载浮动，
    所以归 heavy。
    """
    t0 = time.perf_counter()
    checks: List[Dict[str, Any]] = []
    failures: List[str] = []
    skipped_heavy: List[str] = []

    def _add(name, ok, detail="", skip=False):
        """``skip=True`` → 本机环境缺这个资源，标 SKIP，不计入失败。"""
        checks.append({"name": name, "ok": bool(ok),
                       "skipped": bool(skip), "detail": detail})
        if skip:
            pass
        elif not ok:
            failures.append(name)

    import sys as _sys
    _me = _sys.modules[__name__]

    # ============ 1) 契约 ============
    _add("模块 docstring 含「能力边界」",
         "能力边界" in (_me.__doc__ or ""), "有")
    _add("OcrWord 复用 ocr_fallback 的契约",
         hasattr(_me.OcrWord, "to_dict")
         and hasattr(_me.OcrWord, "center"), "同一结构")
    _add("OcrUnavailable 可沿降级链传播",
         issubclass(_me.OcrUnavailable, Exception), "是异常")

    # ============ 2) 常量与语言映射 ============
    _add("★ 默认超时不超 10 秒（外部进程的命门）",
         0 < DEFAULT_TIMEOUT <= 10.0, "%.1fs" % DEFAULT_TIMEOUT)
    _add("默认语言是中英混排",
         DEFAULT_LANGUAGE == "chi_sim+eng", DEFAULT_LANGUAGE)
    _add("★ 语言映射覆盖中文与英文",
         LANGUAGE_MAP.get("中文") == "chi_sim+eng"
         and LANGUAGE_MAP.get("英文") == "eng", "中英都有")
    _add("语言映射函数认默认",
         _map_language("") == DEFAULT_LANGUAGE, "留空→默认")
    _add("语言映射认中文名",
         _map_language("中文") == "chi_sim+eng", "中文→chi_sim+eng")
    _add("语言映射透出原样码",
         _map_language("chi_sim+eng") == "chi_sim+eng", "已是码就原样")

    # ============ 3) BMP 写入（纯字节拼接）============
    class _Bmp:
        def __init__(self, w, h):
            self.width, self.height = w, h
            self.stride = w * 4
            self.pixels = bytes(
                (y * 10 + x) % 256 for y in range(h) for x in range(w * 4))

    b = _Bmp(3, 2)
    try:
        blob = _to_bmp_bytes(b)
        sig = blob[:2]
        declared = struct.unpack("<I", blob[18:22])[0]
        height_field = struct.unpack("<i", blob[22:26])[0]
        bit_count = struct.unpack("<H", blob[28:30])[0]
        off = struct.unpack("<I", blob[10:14])[0]
        _add("★ BMP 签名是 BM", sig == b"BM", repr(sig))
        _add("★ BMP 宽高写对（3x2）", declared == 3, "w=%d" % declared)
        _add("★ BMP 高度为负 = 自上而下（免翻转）", height_field == -2,
             "h=%d" % height_field)
        _add("BMP 位深 32（BGRA）", bit_count == 32, "%d" % bit_count)
        _add("BMP 像素偏移正确（14+40）", off == 54, "%d" % off)
        _add("BMP 文件大小正确（54 + 24）",
             len(blob) == 54 + 3 * 2 * 4, "%d 字节" % len(blob))
        _add("BMP 像素内容与原图一致",
             blob[54:] == bytes(b.pixels), "逐字节相同")
    except Exception as exc:      # noqa: BLE001
        _add("BMP 写入", False, "%s: %s" % (type(exc).__name__, exc))

    _add("★ 非法尺寸抛 OcrUnavailable",
         _raise_type(lambda: _to_bmp_bytes(_Bmp(0, 0)))
         == "OcrUnavailable", "抛了")

    # ============ 4) TSV 解析（合成数据，不跑子进程）============
    # 真实 TSV 的列：level page block par line word left top w h conf text
    # fixture 故意覆盖四种要过滤/区分的行：
    #   · 页级(level=1)与行级(level=4) —— 只取词级
    #   · 块2 的行号与块1 撞车 —— 验证行号按 (block,par,line) 映射
    #   · conf=-1 的行 —— 非词级特征
    #   · 空文本行 —— 纯图形块
    _TSV_FIXTURE = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num"
        "\tleft\ttop\twidth\theight\tconf\ttext\n"
        "1\t1\t0\t0\t0\t0\t0\t0\t100\t20\t-1\t\n"           # 页级 → 过滤
        "4\t1\t1\t1\t1\t0\t10\t20\t60\t18\t-1\t\n"           # 行级 → 过滤
        "5\t1\t1\t1\t1\t1\t10\t20\t30\t18\t95.5\t确定\n"      # 块1·行1
        "5\t1\t1\t1\t1\t2\t45\t20\t30\t18\t88.0\t取消\n"      # 同行
        "5\t1\t2\t1\t1\t1\t10\t50\t40\t18\t91.0\t下一步\n"    # 块2·行1（与块1行号撞车）
        "5\t1\t3\t1\t1\t1\t10\t80\t40\t18\t77.0\t\n"          # 空文本 → 过滤
        "5\t1\t1\t1\t2\t1\t10\t100\t50\t18\t91.0\t保存\n"     # 块1·行2
    )
    try:
        ws = _parse_tsv(_TSV_FIXTURE)
        _add("★ TSV 只取词级（level=5）", len(ws) == 4,
             "4 个词（过滤了页/行级/空文本）"
             if len(ws) == 4 else "得到 %d 个" % len(ws))
        _add("★ 坐标换算成 (left, top, right, bottom)",
             ws[0].rect == (10, 20, 40, 38), str(ws[0].rect))
        _add("★ 置信度归一化到 0~1",
             abs(ws[0].confidence - 0.955) < 0.001, "%.3f" % ws[0].confidence)
        _add("conf=-1 的行被跳过（非词级）",
             all(w.confidence >= 0 for w in ws), "无负值")
        _add("★ 跨块的行号不重复（块2 行1 ≠ 块1 行1）",
             len({w.line for w in ws}) == 3
             and [w.line for w in ws] == [0, 0, 1, 2],
             "行号 %s" % [w.line for w in ws])
        _add("engine 标记为 tesseract",
             all(w.engine == "tesseract" for w in ws), "tesseract")
        _add("空输入返回空列表", _parse_tsv("") == [], "空")
        _add("表头单独一行也不炸",
             _parse_tsv("level\tpage_num\tx") == [], "无数据")
    except Exception as exc:      # noqa: BLE001
        _add("TSV 解析", False, "%s: %s" % (type(exc).__name__, exc))

    # ============ 5) 可用性探测 ============
    _add("is_available 永不抛", is_available() in (True, False), "bool")
    _add("unavailable_reason 永不抛",
         isinstance(unavailable_reason(), str), "str")
    avail = is_available()
    _add("★ 可用性有明确结论（不静默）",
         avail or bool(unavailable_reason()),
         "可用：%s" % version() if avail else "不可用，有 reason")
    _add("languages 永不抛（不可用时返回空）",
         isinstance(languages(), list), "list")

    # ============ 6) 不引第三方 ============
    # ⚠ 查**顶层 import**，不查文本 —— 文本搜索会命中本文件里
    #   "不引 pytesseract"这类说明文字（这个坑已经踩过五次）
    import ast as _ast
    src = open(_me.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    top_mods = set()
    for node in tree.body:
        if isinstance(node, _ast.Import):
            for a in node.names:
                top_mods.add((a.name or "").split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            top_mods.add((node.module or "").split(".")[0])
    _add("★ 顶层不 import OCR 第三方库",
         not (top_mods & {"pytesseract", "tesserocr", "easyocr",
                          "paddleocr", "cv2", "PIL"}),
         "零第三方，只用 subprocess")
    _add("★ 顶层不 import 桌面抓屏（延迟 import）",
         not (top_mods & {"capture", "layout", "actions"}),
         "只吃位图对象")

    # ============ 7) full 档：真跑一次 ============
    if quick:
        _add("（full）真跑一次识别（合成图）", False,
             "subprocess 真跑，quick 档跳过", skip=True)
        skipped_heavy.append("（full）真跑一次识别（合成图）")
    elif not avail:
        _add("（full）真跑一次识别（合成图）", False,
             "本机没装 Tesseract → 跳过", skip=True)
        skipped_heavy.append("（full）真跑一次识别（合成图）")
    else:
        try:
            blank = _Bmp(16, 16)
            got = recognize(blank, timeout=DEFAULT_TIMEOUT)
            _add("（full）真跑一次识别（合成图）", isinstance(got, list),
                 "返回 %d 个词（空白图预期 0）" % len(got))
        except OcrUnavailable as exc:
            _add("（full）真跑一次识别（合成图）", False, str(exc)[:80])
        except Exception as exc:      # noqa: BLE001
            _add("（full）真跑一次识别（合成图）", False,
                 "%s: %s" % (type(exc).__name__, exc))

    elapsed = time.perf_counter() - t0
    return {
        "module": "ocr_tesseract",
        "quick": bool(quick),
        "checks": checks,
        "failures": failures,
        "skipped": [c["name"] for c in checks if c.get("skipped")],
        "skipped_heavy": len(skipped_heavy),
        "ok": not failures,
        "elapsed": round(elapsed, 4),
    }


def _raise_type(fn) -> str:
    try:
        fn()
    except BaseException as exc:      # noqa: BLE001
        return type(exc).__name__
    return ""


if __name__ == "__main__":      # pragma: no cover
    import json as _json
    r = selfcheck()
    print(_json.dumps(
        {"ok": r["ok"], "passed": len(r["checks"]) - len(r["failures"]),
         "total": len(r["checks"]), "elapsed": r["elapsed"],
         "failures": r["failures"]},
        ensure_ascii=False, indent=2))
    print()
    print(_json.dumps(describe_environment(), ensure_ascii=False, indent=2))
