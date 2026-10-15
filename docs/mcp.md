# MCP Server 使用指南

> 让 Claude Desktop / Cursor 直接调用 Avatar。
> 这份文档讲的是**协议细节、接入步骤、排错**。

最后更新：阶段七 7E / 7I（2026-10-07）

---

## 1. 一句话启动

```bash
python -m avatar.mcp
```

它阻塞在 stdin 上，等客户端发请求。

---

## 2. ⚠ 先说一个最容易搞错的事实

**MCP 用的是「换行分隔的 JSON」，不是 LSP 的 `Content-Length`。**

两种都叫"JSON-RPC over stdio"，但线格式不同：

```
LSP 风格（**不是 MCP**）:
    Content-Length: 87\r\n
    \r\n
    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}

MCP 风格（**是这个**）:
    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n
```

**按 LSP 风格写出来的 server，Claude Desktop 连不上** ——
它发的是裸 JSON 行，而你的 server 在等 `Content-Length`，
**双方都在等对方**，握手永远不完成。

本模块**两种都收**（读取时自动识别），但**写出时用 MCP 风格**。

---

## 3. 支持的请求

| 方法 | 说明 |
|---|---|
| `initialize` | 握手，返回协议版本与能力 |
| `notifications/initialized` | 客户端就绪（**通知，不回响应**） |
| `tools/list` | 列出工具及其 JSON Schema |
| `tools/call` | 调用工具 |
| `ping` | 心跳 |

### 协议版本不匹配时不假装

客户端报的版本我们不认识时，**如实返回我们的版本**，
由客户端决定要不要继续 —— 不假装支持。

---

## 4. 12 个工具

| 工具 | 说明 | 长任务 |
|---|---|---|
| `launch_app` | 启动桌面应用，返回 hwnd | |
| `list_windows` | 列出窗口（可按标题筛） | |
| `mouse_click` | 点击（屏幕或客户区坐标） | |
| `type_text` | 输入文本（真人化节奏） | |
| `uia_find` | UIA 语义查找（最快） | |
| `vision_locate` | **五层降级查找** | |
| `cdp_execute_js` | 浏览器里执行 JS | |
| `detect` | 风控检测（**只检测不解题**） | |
| `pause_for_human` | **熔断**，等人处理 | ⏳ |
| `execute_autonomous_task` | **一句话目标 → 执行** | ⏳ |
| `tasks_list` | 历史任务 | |
| `avatar_status` | 各组件自检 | |

**粒度原则**：粗一点，不要细。

反面例子：把"移动鼠标""按下左键""松开左键"拆成三个工具 ——
那是在让模型**手写底层操作**，它一定会写错顺序。

判断标准：**"这个工具名能不能直接回答用户想干什么"**。

---

## 5. ⚠ 硬约束 8：stdout 只能出 JSON-RPC

**所有日志、进度、异常一律走 stderr。**

理由：MCP 客户端把 stdout 的**每一行**都当成协议消息解析。
往里打一个 `print("启动成功")` 就会让客户端收到一条非法消息，
轻则报错，重则**直接断开连接**。

这个 bug 极难查，因为它看起来"只是多打了一行字"。

**本项目的 MCP 模块里没有任何 `print()`** —— 连自检输出都走 stderr。
这条被自检机械检查：

```
[OK] ⑧ MCP server 里**没有 print**（stdout 只出 JSON-RPC） 干净
```

---

## 6. 接入 Claude Desktop

### 6.1 找到配置

```
%APPDATA%\Claude\claude_desktop_config.json
```

没有就新建。

### 6.2 合并配置

```json
{
  "mcpServers": {
    "avatar": {
      "command": "python",
      "args": ["-m", "avatar.mcp"],
      "cwd": "C:/github/avatar-human-computer-use",
      "env": {
        "PYTHONPATH": "C:/github/avatar-human-computer-use",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

⚠ **`cwd` 和 `PYTHONPATH` 都要改成你机器上的实际路径。**

三个环境变量的作用：

| 变量 | 为什么需要 |
|---|---|
| `PYTHONPATH` | 让 `python -m avatar.mcp` 找得到包 |
| `PYTHONIOENCODING=utf-8` | Windows 默认 GBK，中文会乱码 |
| `PYTHONUNBUFFERED=1` | **不加这个握手会卡住** —— 输出被缓冲，客户端等不到响应 |

### 6.3 重启

**完全退出** Claude Desktop（不是关窗口，要在托盘图标上退出），再启动。

对话输入框旁边会出现一个**工具图标**。

---

## 7. 排错

### 工具图标不出现

1. **检查 JSON 语法** —— 用 `python -m json.tool` 验证
2. **路径里的反斜杠**要写成 `/` 或 `\\`
3. 看日志：`%APPDATA%\Claude\logs\mcp-server-avatar.log`

### 连接失败

```bash
cd C:/github/avatar-human-computer-use
python -m avatar.mcp -v
```

它会阻塞等输入（Ctrl+C 退出）。
**只要没立刻报错，就说明能启动。**

### 先验证工具注册

```bash
python -m avatar mcp test
```

**预期**：

```
MCP 工具注册检查
==================================================================
服务标识：avatar-human-computer-use
协议版本：2024-11-05
工具数量：12

  ✓ avatar_status              schema 完整
  ...
协议自测：
  ✓ initialize 握手
  ✓ tools/list 返回 12 个工具

结果：全部通过
```

### 手动跑一次协议

```bash
printf '%s\n%s\n%s\n' \
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}' \
'{"jsonrpc":"2.0","method":"notifications/initialized"}' \
'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_windows","arguments":{"limit":3}}}' \
| python -m avatar mcp
```

---

## 8. 错误处理策略

**工具的业务失败**用 `isError`，**不是** JSON-RPC error：

```json
{"jsonrpc":"2.0","id":2,"result":{
  "content":[{"type":"text","text":"✗ 缺少必填参数：x、y"}],
  "isError":true}}
```

**为什么区分**：

- `isError` = "工具跑了但没成功" → **模型能看到并调整**
- JSON-RPC error = "协议层出了问题" → 客户端直接报错给用户

用错的话，模型看不到"缺少参数"这种可自愈的信息。

### 永不崩

**任何异常都不能让 server 崩。** 理由：客户端通常只在启动时
拉起一次 server，崩了之后它不会自动重启 ——
用户看到的是"AI 突然不能用工具了"，而没有任何提示。

所以 `handle()` 外面包了一层兜底：无论发生什么，
都返回一个 error 响应，然后继续服务。

---

## 9. 安全提醒

MCP server 一旦接上，**Claude 就能操作你的电脑**。

- 只在信任的客户端上启用
- 不需要时把配置注释掉
- `execute_autonomous_task` 会真的执行动作，风控闸会拦验证题，
  但它**不会拦"删除文件"这种操作**（那是另一个层面的问题）

---

## 10. 相关命令

```bash
python -m avatar mcp serve       # 启动（stdio）
python -m avatar mcp serve -v    # 带日志（stderr）
python -m avatar mcp test        # 验证注册
python -m avatar.mcp --selftest  # 模块自检（103 项）
python -m avatar.mcp --list      # 工具清单
python -m pytest tests/test_phase7_mcp.py -q
```
