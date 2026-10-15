# Web / Electron CDP 控制层

> 阶段四的前半。让"浏览器里的东西"和"桌面上的东西"一样能被精确操作。

## 为什么手搓 WebSocket

手搓之前权衡过 `websocket-client` 和 `websockets`。都否决了：

1. **它们都不可用。** `websockets` 是 `asyncio` 的，而整个 avatar 是同步
   `ctypes` 架构 —— 为了连一个调试口把主线程改成事件循环，代价荒谬。
   `websocket-client` 倒是同步的，但它把 URL 解析、连接、代理、SSL、
   重连全包了 —— 我们要用的只是其中 `ws://127.0.0.1:9222` 这一段。
2. **依赖成本。** 阶段一到三零第三方依赖（只有 stdlib + ctypes）。
   为了一个本地回环连接破例，会让安装说明从"有 Python 就行"变成
   "先 pip install 一堆东西"。
3. **出问题时看不见。** CDP 的失败模式很有特点：握手成功但第一条命令
   超时、事件和响应混在一条 socket 上、`sessionId` 丢了导致命令发到
   浏览器级。这些都需要**看得到字节**。自己写的 300 行里每一行都能打断点。

代价是多了一个自实现的 RFC 6455 子集。换来的是零依赖 + 每个字节可见。

## 分层

```
web_cdp/websocket.py    RFC 6455：握手、帧编解码、消息重组
web_cdp/session.py      CDP JSON-RPC：id 匹配、事件分发、会话路由
web_cdp/launcher.py     找浏览器 / 挑端口 / 启动 / 探测
web_cdp/navigate.py     导航与等待加载
web_cdp/dom.py          DOM 查询与属性读取
web_cdp/interact.py     点击 / 输入 / 滚动 / 按键
web_cdp/network.py      请求监听 / 响应体抓取
web_cdp/screenshot.py   截图（含整页、元素）
web_cdp/tab.py          标签页管理与 target 选择
web_cdp/electron.py     Electron 识别 / 端口扫描 / 附加
web_cdp/fallback.py     降级决策（该不该用 CDP）
```

依赖单向：`tab/dom/interact/network/screenshot → session → websocket`。
`launcher` 只依赖 stdlib（`subprocess` + `http.client`）。
`electron` 依赖 `session` 和 `fallback`；`fallback` 是纯决策，不依赖任何网络。

## 一、RFC 6455 里真正会咬人的几处

### 客户端帧必须掩码，服务端帧必须不掩码

RFC 规定得很死：客户端 → 服务端**必须**带掩码；服务端 → 客户端**必须不带**。
写反了症状是服务端直接断开（很多实现连错误都不回）。

我们的 `encode_frame(..., mask=True)` 默认掩码。而 `FrameDecoder` 对
**带掩码的服务端帧会解掩码而不是拒绝** —— 这是刻意的宽容：有些中间层
（代理、mock）会违反规定，硬拒绝会让排查变成"为什么连不上"而不是
"对端不守规矩"。宽容但有记录，必要时能查。

### 长度三档

| payload 长度 | 长度字段 |
|---|---|
| 0–125 | 7 位直接编码 |
| 126–65535 | 7 位为 126，后跟 2 字节大端 |
| ≥65536 | 7 位为 127，后跟 8 字节大端 |

**126 和 127 是标记值，不是长度本身。** 把 126 当成长度 126 处理，
症状是 130 字节的消息能过、126 字节的消息解析错位。

### 控制帧不可分片，payload ≤125

`close` / `ping` / `pong` 必须 `FIN=1` 且 payload 不超过 125 字节。
超了必须报错，不能"尽力而为"地发出去 —— 对端会直接判定协议违规。

### 分片重组与控制帧穿插

一条消息可以拆成 `TEXT(fin=0)` + `CONTINUATION(fin=0)` +
`CONTINUATION(fin=1)`。控制帧可以**插在分片中间**：

```
TEXT fin=0 "part1"
PING "ping!"          ← 控制帧穿插，必须立即处理
CONTINUATION fin=1 "part2"
```

`FrameDecoder.messages()` 会先吐 ping，再吐重组后的 `"part1part2"`。
写成"攒够再一起吐"就会把 ping 压到后面，导致心跳应答延迟。

### `wss://` 被明确拒绝

CDP 的调试端口**永远是明文 `ws://`**。这不是实现偷懒 —— Chrome 刻意
不在非 localhost 上开调试端口，就是要求你保证只有本机能连。

所以 `ws_url_to_host_port` 对 `wss://` / `https://` **主动抛错**，
而不是"不支持所以失败"。这是安全特性，`scripts/selfcheck.py` 里
有专门的检查项守着它。

`http://` 反而被**容忍**（按 ws 处理）：`/json/version` 返回的
端点有时写成 `http://`，宽容一点省一次排查。

## 二、CDP 会话：一条 socket 上的多路复用

### 扁平会话（flatten）

浏览器级 WebSocket 只有一条。页面级命令靠 `sessionId` 字段区分：

```json
// 浏览器级：没有 sessionId
{"id": 1, "method": "Browser.getVersion"}

// 页面级：带 sessionId
{"id": 2, "method": "Runtime.evaluate", "sessionId": "sess-abc",
 "params": {"expression": "1+1"}}
```

**踩过的坑**：`_PageFacade.call` 里用 `kw.setdefault("session_id", sid)`，
但 `CdpSession.evaluate` 内部显式传了 `session_id=None` ——
`setdefault` 看到 key 已存在就不动，于是 sessionId 丢了，
命令被当浏览器级方法，报 `'Runtime.evaluate' wasn't found`。

这条报错极具误导性（明明浏览器支持这个方法）。
修法是 `if kw.get("session_id") is None: kw["session_id"] = sid`。

### 事件与响应混流

同一条 socket 上既有命令响应（带 `id`）也有事件（带 `method`、无 `id`）。
分发必须严格按 `id` 匹配，**不能按到达顺序**。

`tests/test_phase4_cdp.py` 里的 mock 会**在每次响应前先推一条事件** ——
就是为了让任何"按顺序取"的实现立刻暴露。

事件可以按方法名订阅（`subscribe("Page.loadEventFired")`），
`drain_events()` 取走已到达的。

### 错误类型刻意分成两类

```
CdpError    协议错（-32601 方法不存在 / -32000 执行失败）
CdpTimeout  超时（继承 TimeoutError_）
```

**它们不是父子关系。** 这是刻意的：
超时是"暂时性、可重试"的，协议错是"确定性失败"，重试没意义。
混成一个类型，上层就写不出 `except CdpTimeout: retry` 这样干净的策略。

## 三、启动浏览器：最坑的一种失败

```python
port = LB.pick_free_port()                  # 必须先挑空闲端口
proc, info = LB.launch_browser(
    exe, port=port, headless=True,
    url="about:blank",
    user_data_dir=profile,                  # ⚠ 必须独立 profile
    wait_ready=True, ready_timeout=30.0)
```

### 为什么要独立 `--user-data-dir`

撞上用户默认 profile 时，Chrome 会**静默地把请求转交给已运行的实例，
然后自己退出**。调试端口不会生效，而 `launch_browser` 看起来是成功的。

这个失败模式极其恶劣：进程起来了、超时没超、端口探测也可能因为
连到了那个已有实例而看似正常 —— 但你操作的是**别人的浏览器**。

所以：`pick_free_port()` 挑空闲端口 + 独立的 `user_data_dir`，
两条一起才安全。

### 本机没有 Chrome 怎么办

`find_browser(prefer="any")` 会依次找 Chrome 和 Edge。
默认的 `prefer="chrome"` **只找 Chrome** —— 本机只有 Edge 时会报
"找不到浏览器"，很容易误判成"环境不支持"。

`pick_free_port()` 的签名是 `pick_free_port(lo, hi)`（两个位置参数），
不是 `pick_free_port(range_tuple)`。默认范围 `DEBUG_PORT_RANGE = (9222, 9242)`。

## 四、Electron：重点不是"能不能控"，而是"该不该控"

绝大多数商业 Electron（钉钉、飞书、Discord…）**默认不开调试端口**。
硬去连只会拿到一串连接超时，然后误判成"工具不好用"。

正确做法是先判定，判定不通过就**明确降级到 UIA**。

### 三重判据

| 判据 | 成本 | 结果标记 |
|---|---|---|
| 进程名在白名单里 | 极低 | `kind="electron"` |
| pid 有监听端口且端口是 CDP | 中 | `kind="electron_cdp"`（**最强**） |
| 都不是 | — | 返回 `None`，降级 UIA |

第二条最强，因为**只有 Chromium/Electron 才暴露 CDP** ——
这个反证比维护一份永远追不上的进程名白名单可靠得多。

### 端口探测只认一个指纹

```python
def probe_port(port) -> Optional[dict]:
    """判据是响应体里有 webSocketDebuggerUrl，不是状态码。"""
```

任何 HTTP 服务都会回 200。只看状态码会把 Electron 自己的本地 API
误判成调试端口，然后握手失败，白白浪费排查时间。

### 命令行解析：`--remote-debugging-pipe` 是另一个坑

```python
F.has_debug_flag("app.exe --remote-debugging-pipe")  # True
F.parse_debug_port(cmd)                              # None
```

用户看到 `--remote-debugging-pipe` 会以为"调试开了呀"，
但本库只实现了 TCP 模式。所以降级理由必须是专门的
`pipe_mode_unsupported`，而不是笼统的 `no_debug_port` ——
否则会让人去查"为什么端口没开"，方向全错。

### 附加失败返回 None，不抛异常

```python
app = E.attach_electron(pid=..., port=...)   # 拿不到 CDP → None
if app is None:
    ...degrade to UIA...
```

**附加失败是预期内的常态**。如果抛异常，上层就得用 try/except
包住每次调用 —— 那是很糟的接口设计。

## 五、降级决策的 `reason` 字段

`FallbackDecision` 的每次降级都必须带可读的 `reason`：

| reason | 含义 |
|---|---|
| `process_never_cdp` | 进程在黑名单里（explorer / svchost…），不做端口探测 |
| `no_process_info` | 没给进程信息也没给端口，不盲扫 |
| `no_debug_port` | 浏览器/Electron 但没带 `--remote-debugging-port` |
| `pipe_mode_unsupported` | 用的是管道模式，本库不支持 |
| `unknown_process_no_port` | 未知进程且无端口 |
| `port_not_cdp` | 端口开着但不是 CDP（可能连错了进程） |
| `cdp_available` | 探测通过 |
| `cdp_assumed` | 跳过探测（`probe=False`），**可用性未经验证** |

最后两条的区别很重要：不能把"猜的"和"验过的"写成同一个结果。
`cdp_assumed` 时 `decision.cdp_ok is False`。

### 黑名单的意义

`never_cdp` 名单（explorer / svchost / dwm / conhost…）省掉无谓的
端口扫描。没有它就得对每个窗口都扫一遍 —— 慢，而且可能误连到
别的进程的调试端口上。

**黑名单与两个白名单不能有交集**，否则判定结果取决于检查顺序，
非常脆。`scripts/selfcheck.py` 里有断言守着。

## 六、CLI

```bash
# 环境体检：浏览器路径、端口占用、Electron 窗口
python -m avatar.cli web-opts

# 无头跑一段 JS（手搓 CDP 端到端）
python -m avatar.cli web-eval --headless --js "1+1"

# 复用已有调试实例
python -m avatar.cli web-eval --port 9222 --js "document.title"

# 附加到 Electron
python -m avatar.cli electron-windows
python -m avatar.cli electron-attach --port 9222
```

`--json` 给机器读；退出码见 `SKILL.md`。

## 七、自检与测试

```bash
python -m avatar.web_cdp                                  # 12 项
python scripts/selfcheck.py --only phase4                 # 阶段四硬约束 12 项
python -m pytest tests/test_phase4_websocket.py -q        # 57 项
python -m pytest tests/test_phase4_cdp.py -q              # 21 项
python -m pytest tests/test_phase4_electron.py -q         # 51 项
```

测试分三层：

1. **纯计算** —— 握手 key / 帧编解码，不发网络。
2. **手搓服务端** —— 自己写一个最小 WebSocket 服务端和 CDP mock。
   用它验证 id 匹配、会话路由、超时、事件分发。
   **不能用 monkeypatch 代替** —— session 层的风险全在协议交互上，
   函数替身验证不了。
3. **真浏览器** —— 有 Edge/Chrome 就跑端到端，没有就 `skipif`。
   **skip 不是失败**，CI 机器上通常没浏览器。

### 写 mock 时踩的两个坑

**粘包**：mock 服务端用 `recv(65536)` 收，80000 字节的帧会被 TCP
切成两段。没做跨 recv 缓冲的话，大负载测试会收到 65522 字节 ——
看起来像"客户端发了截断的数据"，实际是**测试服务端自己没拼包**。

**关闭竞态**：测试结束时关掉客户端，mock 线程可能还在回最后一条包，
`sendall` 抛 `WinError 10038`。这只是关连接的正常竞态，
不吞掉的话 pytest 会报 `PytestUnhandledThreadExceptionWarning`，
把真正的失败淹掉。mock 里所有写操作都该走一个 `_safe_send` 包一层。
