# 从 Win32 到 MCP，零依赖手搓的技术选择

> 讲一个工程决策：**为什么不装那些"标准"的库。**

---

## 先看这个项目装了什么

```toml
# pyproject.toml
dependencies = []
```

**运行期一个第三方包都没有。**

> 注意口径：`pip list` 在任何人机器上都不会是空的（系统里总有别的东西）。
> 真正该看的是**项目自己声明的依赖** —— 那个是空的。
>
> 测试要 `pytest`，那是**开发期**依赖，CI 里显式
> `pip install pytest`，不藏着。把测试框架也算进"零依赖"是自欺欺人。

但它能做这些事：

| 能力 | 通常需要装 | 本项目怎么做 |
|---|---|---|
| COM 调用 | `pywin32` | 纯 `ctypes` |
| UI 自动化 | `uiautomation` / `pywinauto` | 手写 UIA COM 接口 |
| 键鼠模拟 | `pynput` / `pyautogui` | `ctypes` 调 `SendInput` |
| 截图 | `Pillow` / `mss` | GDI `BitBlt` + 手写 BMP/PNG |
| OCR | `pytesseract` / `paddleocr` | 手搓 WinRT `Windows.Media.Ocr` |
| 图像编码 | `Pillow` | GDI+ 编 JPEG + `zlib` 手写 PNG |
| 调大模型 | `openai` / `anthropic` | `urllib.request` |
| MCP Server | 官方 `mcp` SDK | 手写 JSON-RPC over stdio |

---

## 为什么要这么干

### 理由一：依赖是**长期的维护负担**

每个依赖都在给你签一份**无限期合同**：

- 它的大版本升级可能破坏 API（`openai` 从 0.x 到 1.x 就是重写）
- 它的依赖会冲突（`httpx` 版本打架是常见问题）
- 它可能停止维护
- 它可能带漏洞（供应链攻击）

**而 HTTP 协议十年没变过。** `urllib` 也不会有 breaking change。

### 理由二：依赖**遮住了问题**

用 `pyautogui` 的时候，你不知道它内部用的是
`SetCursorPos` 还是 `SendInput` —— 而这两者**行为完全不同**：

- `SetCursorPos` —— 直接改光标位置，很多游戏和风控**能检测到**
- `SendInput` —— 走输入队列，是**真实的输入事件**

**本项目必须用 `SendInput`**（要真人化、要过风控）。
如果用 `pyautogui`，这个关键选择就被藏起来了。

### 理由三：**错误处理需要看到底层**

```python
# 用库的时候
result = pyautogui.click(x, y)
# 失败了，你怎么知道是"坐标越界"还是"窗口没激活"还是"权限不足"？
```

```python
# 手写的时候
hr = SendInput(1, byref(inp), sizeof(inp))
if hr == 0:
    err = GetLastError()
    # err = 5 → ERROR_ACCESS_DENIED（需要更高权限）
    # err = 87 → ERROR_INVALID_PARAMETER（结构体写错了）
```

**本项目的核心是"失败要能解释"** —— 那必须看到底层的错误码。

### 理由四：这个项目的**核心价值就在这些底层细节里**

如果把 COM / WinRT / GDI+ 都换成库，那这个项目就变成
"一个用现成轮子拼的 Agent" —— 没有任何技术壁垒。

**它的价值恰恰在于**：

- `TryCreateFromUserProfileLanguages` 在 vtable `[10]` 不是 `[9]`
- `GlobalLock` 的 restype 不声明 `c_void_p` 会**截断 64 位指针**
- `EncoderParameterValueTypeLong` 是 `4` 不是 `1`
- MCP 用**换行分隔 JSON** 而不是 LSP 的 `Content-Length`

**这些知识只有手搓才能得到。**

---

## 手搓的代价（诚实版）

### 代价一：代码量大

| 功能 | 用库 | 手搓 |
|---|---|---|
| UIA 查找 | 5 行 | ~800 行 |
| WinRT OCR | 20 行 | ~1400 行 |
| MCP Server | 50 行 | ~1200 行 |

**总计：约 15000 行 vs 约 500 行。**

### 代价二：要处理大量 ABI 细节

```python
# 结构体尺寸必须精确
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", c_uint32), ("biWidth", c_int32),
        ("biHeight", c_int32), ("biPlanes", c_uint16),
        ("biBitCount", c_uint16), ("biCompression", c_uint32),
        ...
    ]
# 40 字节 —— 多一个字段少一个字段都会让 API 返回错误
```

**而且这些错误通常不报错，只是"结果不对"。**

### 代价三：踩坑成本高

本项目踩过的坑（都写进了注释）：

1. **`WindowsCreateStringReference` 不复制字符串** ——
   传 Python `str` 会因为临时缓冲区被回收而变成野指针
2. **`IBufferByteAccess` 的 IID 与 `IBuffer` 只差最后一位** ——
   QI 错了不报错，会在 `GetIids` 上按 `Buffer(byte**)` 调 → **直接崩**
3. **`_NoiseBmp` 用 LCG 低字节不是噪声** ——
   周期太短，测出"PNG 比 JPEG 小得多"的假结论

**每一个都是几小时。**

---

## 什么时候**该**用库

**零依赖不是教条。** 以下情况该用库：

| 情况 | 理由 |
|---|---|
| **纯算法密集型** | 手写快速傅里叶变换没意义 |
| **安全关键** | 手写加密算法是**危险的** |
| **协议极复杂** | 比如完整实现 HTTP/2 或 QUIC |
| **开发时间 > 维护成本** | 一次性脚本 |

**判断标准**：这个库解决的是"**繁琐**"还是"**困难**"？

- **繁琐**（调 API、拼结构体）→ **手搓**
- **困难**（算法、密码学）→ **用库**

---

## 一个具体的对比：为什么 MCP 不用官方 SDK

官方 `mcp` Python SDK 会带这些依赖：

```
mcp
├── anyio
├── httpx
├── pydantic
├── starlette        （如果用 HTTP transport）
└── sse-starlette
```

**为了发一个 JSON-RPC over stdio 的请求，装 5 个包。**

而手写需要多少代码？

```python
def encode(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8") + b"\n"

def decode(line: str) -> dict:
    return json.loads(line)
```

**两行。**

剩下的就是消息路由 —— 那本来就要写（业务逻辑）。

---

## 怎么验证"真的零依赖"

CI 里有一段 AST 扫描：

```python
BANNED = {
    'requests','httpx','aiohttp','openai','anthropic','litellm',
    'PIL','cv2','numpy','pytesseract','paddleocr','easyocr',
    'win32com','comtypes','pywin32','clr','pydantic','anyio',
}
for p in pathlib.Path('avatar').rglob('*.py'):
    tree = ast.parse(p.read_text(encoding='utf-8'))
    for n in ast.walk(tree):
        # 收集 import 名，检查是否在 BANNED 里
```

**哪天有人加了依赖，CI 立刻红。**

---

## 小结

| 维度 | 用库 | 手搓 |
|---|---|---|
| 上手速度 | 快 | 慢 |
| 代码量 | 少 | 多（30 倍） |
| 长期维护 | 跟别人的节奏 | 自己的节奏 |
| 可解释性 | 黑盒 | 完全可控 |
| 技术壁垒 | 无 | **有** |
| 调试难度 | 低 | **高** |

**这个项目选"手搓"不是因为"优雅"，是因为：**

1. 它的**核心价值**就在这些底层细节里
2. 它需要**精确的错误归因**（"失败要能解释"）
3. 它要**长期不坏**（依赖会烂，协议不会）

**但这不是通用建议。** 如果你在做业务系统，
装 `requests` 是**正确**的选择 —— 别为了"优雅"重造轮子。

**判断标准是**：这个轮子是不是你的**核心竞争力**？
