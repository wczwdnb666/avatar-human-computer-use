# 案例 7 · Claude Desktop 通过 MCP 调用 Avatar

**演示什么**：让 Claude 直接操作你的电脑。
这是阶段七的最终形态 —— 外部 AI 客户端把 avatar 当工具用。

## 第一步：验证工具注册

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
  ✓ cdp_execute_js             schema 完整
  ✓ detect                     schema 完整
  ✓ execute_autonomous_task    schema 完整
  ...
协议自测：
  ✓ initialize 握手
  ✓ tools/list 返回 12 个工具

结果：全部通过
```

## 第二步：手动跑一次协议

```bash
python -m avatar mcp --list      # 看工具清单
```

或者模拟一次完整会话：

```bash
printf '%s\n%s\n%s\n' \
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}' \
'{"jsonrpc":"2.0","method":"notifications/initialized"}' \
'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_windows","arguments":{"limit":3}}}' \
| python -m avatar mcp
```

**预期**：三条 JSON-RPC 响应（通知那条不回）。

## 第三步：接进 Claude Desktop

1. 打开配置目录：

```
%APPDATA%\Claude\claude_desktop_config.json
```

没有就新建。

2. 把 `mcp_config.json` 里的内容合并进去：

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

3. **完全退出** Claude Desktop（不是关窗口，要在托盘图标上退出），再启动。

4. 对话输入框旁边会出现一个**工具图标**。

## 第四步：试试

在 Claude 里说：

> 看看我电脑上现在开着哪些窗口

Claude 会调 `list_windows`，返回窗口列表。

> 打开记事本，输入"你好"

Claude 会调 `execute_autonomous_task`，走完整链路。

> 屏幕上有没有验证码？

Claude 会调 `detect`。

## 排错

**工具图标不出现**：
1. 检查 JSON 语法（逗号、引号）—— 用 `python -m json.tool` 验证
2. 路径里的反斜杠要写成 `/` 或 `\\`
3. 看 Claude 的日志：`%APPDATA%\Claude\logs\mcp-server-avatar.log`

**连接失败**：
```bash
# 手动跑一次，看 stderr 有没有报错
cd C:/github/avatar-human-computer-use
python -m avatar.mcp -v
```
（它会阻塞等输入，Ctrl+C 退出。**只要没立刻报错，就说明能启动**。）

**⚠ 一个最常见的坑**：
如果你的代码里有任何 `print()` 输出到 stdout，
客户端会收到非法消息然后断开。**本项目的 MCP 模块里没有任何 print** ——
所有日志走 stderr。这是硬约束 8。

## 安全提醒

MCP server 一旦接上，**Claude 就能操作你的电脑**。

- 只在信任的客户端上启用
- 不需要时把配置注释掉
- `execute_autonomous_task` 会真的执行动作，风控闸会拦验证题，
  但它**不会拦"删除文件"这种操作**（那是另一个层面的问题）
