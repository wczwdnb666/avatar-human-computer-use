# 演示案例

七个能直接跑的案例，从"打开记事本"到"Claude 通过 MCP 调用"。

**每个案例都是可执行的命令 + 预期输出**，不是伪代码。

## 跑之前

```bash
cd C:/github/avatar-human-computer-use
python -m avatar selfcheck        # 先确认基础能力正常
```

## 七个案例

| # | 案例 | 演示什么 | 命令 |
|---|---|---|---|
| 1 | [打开记事本输入 hello](01_open_app.md) | 一句话 → DAG → 执行 | `avatar run --goal ...` |
| 2 | [浏览器搜索并提取](02_browser_search.md) | CDP 直连，不走鼠标 | `avatar web-eval` |
| 3 | [跨端填表](03_form_fill.md) | 网页抓数据 → 桌面输入 | 组合 |
| 4 | [真人化对比](04_human_like_demo.md) | 轨迹与节奏可视化 | `avatar entropy` |
| 5 | [LLM 自动拆解](05_planner_task.md) | 自然语言 → 计划 | `avatar llm plan` |
| 6 | [风控拦截](06_risk_intercept.md) | 熔断 + F9 恢复 | `avatar pause-test` |
| 7 | [Claude 通过 MCP 调用](07_mcp_claude.md) | 外部客户端接入 | `avatar mcp serve` |

## 一键跑全部

```bash
python examples/demo_recorder.py            # 跑一遍并生成报告
python examples/demo_recorder.py --with-gui # 带界面动作（会真的动鼠标）
```
