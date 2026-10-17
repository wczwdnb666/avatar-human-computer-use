# v0.1.0 发布清单

> 这份清单是**给人照着做**的，不是自动化脚本。
> 每一步都写了"为什么"和"怎么算成功"。
>
> ⚠ **本机没装 Git for Windows** —— `git` 只存在于 WorkBuddy 自带的
> PortableGit 里（`/mingw64/bin/git`，版本 `2.55.0.windows.3`）。
> 所以下面的 git 命令**要么在 WorkBuddy 里让我执行**，
> 要么你先自己装一个 [Git for Windows](https://git-scm.com/download/win)。

---

## 0. 占位符复查（已全部替换，此节留作复核）

真实仓库地址**已经全量落实**，来源是 `pyproject.toml` 的
`[project.urls]` —— 以那里为唯一真值，不在别处另设常量：

```
https://github.com/wczwdnb666/avatar-human-computer-use
```

已替换的位置：

| 文件 | 原占位符 |
|---|---|
| `CHANGELOG.md` | 底部两条版本链接 |
| `CONTRIBUTING.md` | clone 示例 |
| `SECURITY.md` | 漏洞上报入口 |
| `README.md` | 顶部 CI badge（2 处）、clone 命令 |
| `.github/ISSUE_TEMPLATE/config.yml` | 安全公告链接 |

复查一条命令：

```bash
cd C:\github\avatar-human-computer-use
grep -rn "OWNER/REPO\|YOUR_ORG\|clone <repo>" \
     --include=*.md --include=*.yml --include=*.toml .
```

**必须无输出。**

---

## 1. 发布前本地验证（必须全绿）

```bash
cd C:\github\avatar-human-computer-use

# 1.1 零依赖（AST 扫 import，与 CI 同款逻辑）
python -c "
import ast, pathlib, sys
BANNED = {'requests','httpx','aiohttp','openai','anthropic','litellm',
          'PIL','cv2','numpy','pytesseract','paddleocr','easyocr',
          'win32com','comtypes','pywin32','clr','pydantic','anyio'}
bad = []
for p in list(pathlib.Path('avatar').rglob('*.py')):
    for n in ast.walk(ast.parse(p.read_text(encoding='utf-8'))):
        names = []
        if isinstance(n, ast.Import):
            names = [(a.name or '').split('.')[0] for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.level == 0:
            names = [(n.module or '').split('.')[0]]
        bad += ['%s: %s' % (p, nm) for nm in names if nm in BANNED]
print('✗ ' + '\n✗ '.join(bad[:20]) if bad else '✓ 零第三方依赖')
sys.exit(1 if bad else 0)
"

# 1.2 模块自检 + 硬约束
python scripts/selfcheck.py

# 1.3 全量测试（约 5 分钟，会动鼠标）
python -m pytest tests/ -q


```

**通过判据**

| 命令 | 判据 |
|---|---|
| 1.1 | 打印 `✓ 零第三方依赖`，退出码 0 |
| 1.2 | 退出码 0，没有 `✗`，总时长 < 30 秒 |
| 1.3 | `0 failed`（skip 允许） |
| 1.4 | 4 个模块全过（state / menu / ball / bridge） |
| 1.5 | `0 failed` |

---

## 2. 初始化仓库并提交

```bash
cd C:\github\avatar-human-computer-use

# 2.1 初始化（默认分支 main）
git init -b main

# 2.2 确认 .gitignore 生效
git status --short | head -40
#    ✗ 不该出现：.workbuddy-ai/  __pycache__/  .pytest_cache/
#                demo_report.md  logs/  *.log

# 2.3 全量加入
git add -A

# 2.4 数一下要提交多少文件（心里有个数）
git status --short | wc -l

# 2.5 提交
git commit -m "chore: initial commit for v0.1.0"
```

⚠ **提交前必须配好身份**，否则 git 直接拒绝：

```bash
git config user.name  "你的名字"
git config user.email "你的邮箱"
```

---

## 3. 打 tag

```bash
# 3.1 确认版本号一致
grep -n '^version' pyproject.toml            # version = "0.1.0"

# 3.2 打**附注** tag
git tag -a v0.1.0 -m "Avatar v0.1.0 —— 首个版本：九个阶段全部交付"

# 3.3 验证
git tag -n1
git describe --tags                          # v0.1.0
```

**为什么必须用 `-a`（附注 tag）**

轻量 tag 没有 tagger 和日期信息，GitHub Release 会把它标成
`untagged`，`release.yml` 里的版本校验与 CHANGELOG 提取也可能拿不到预期内容。

---

## 4. 推到 GitHub

**先在 GitHub 网页上建一个空仓库。**
不要勾 README / .gitignore / LICENSE —— 本地已经有了，勾了会冲突。

```bash
# 4.1 加远端
git remote add origin https://github.com/wczwdnb666/avatar-human-computer-use.git

# 4.2 推主分支
git push -u origin main

# 4.3 推 tag（★ 必须单独推，-u 不会带上 tag）
git push origin v0.1.0
```

### 关于凭证

**不要把长期 token 写进 remote URL。** 两种安全做法：

**做法 A（推荐）**：装 GitHub CLI，走浏览器授权

```bash
gh auth login                                 # 选 HTTPS → 浏览器登录
git push -u origin main
git push origin v0.1.0
```

**做法 B**：临时 token，**用完立刻删**

```bash
# 先去 GitHub → Settings → Developer settings → Personal access tokens
#   → Fine-grained tokens，建一个：
#     权限只要 Contents: Read and write
#     有效期设 1 天
#     只授权给这一个仓库

git remote set-url origin https://YOUR_TOKEN@github.com/wczwdnb666/avatar-human-computer-use.git
git push -u origin main
git push origin v0.1.0

# ★★ 推完立刻改回不含 token 的 URL
git remote set-url origin https://github.com/wczwdnb666/avatar-human-computer-use.git

# ★★ 然后去 GitHub 网页把那个 token 删掉
```

⚠ **`git remote -v` 会明文显示 token。**
推完务必执行上面两条 `set-url` —— token 泄露等于仓库写权限泄露。

---

## 5. 创建 Release

推 tag 之后 `release.yml` 会**自动触发**。

### 方式 A：让 CI 自动建（推荐）

去看 **Actions** 页，确认 `Release` 那次 run 是绿的。
绿了就去 **Releases** 页看结果。

`release.yml` 做三件事：

1. 校验 tag 与 `pyproject.toml` 的版本号一致
2. 从 `CHANGELOG.md` 提取 `## [v0.1.0]` 一节的正文
3. 创建 Release

### 方式 B：手动建

1. 打开 `https://github.com/wczwdnb666/avatar-human-computer-use/releases/new`
2. **Choose a tag** → 选 `v0.1.0`
3. **Release title** → `v0.1.0 · 首个版本`
4. **Describe this release** → 从 `CHANGELOG.md` 的 `## [v0.1.0]` 一节整段粘贴
5. 勾 **Set as the latest release**
6. **不要**勾 "This is a pre-release"
7. 点 **Publish release**

---

## 6. 发布后验证

### 6.1 仓库页自检（浏览器）

| 检查 | 期望 |
|---|---|
| README 顶部 CI badge | **绿色 passing**（首次约 3–5 分钟） |
| Releases | 有 `v0.1.0`，标着 **Latest** |
| Actions | `CI` 与 `Release` 两个 workflow 都绿 |
| Issues → New issue | 只有两个模板，**没有** "Blank issue" |
| Security → Advisories | "Report a vulnerability" 按钮可用 |

### 6.2 加 GitHub Topics

仓库首页右上角 **⚙（齿轮）** → **Topics**，逐个加这 20 个：

```
computer-use  desktop-automation  windows-automation  ui-automation
rpa  ai-agent  llm-agent  mcp  model-context-protocol  ctypes
win32  winrt  zero-dependency  human-like  accessibility  ocr
cdp  python  agent  automation
```

### 6.3 clone 到临时目录验证（★ 最硬的一道）

```bash
cd /tmp
rm -rf avatar_verify
git clone https://github.com/wczwdnb666/avatar-human-computer-use.git avatar_verify
cd avatar_verify

# 版本号对
grep -n '^version' pyproject.toml                 # 0.1.0
git describe --tags                               # v0.1.0

# 零依赖跑得起来（★ 全程没有任何 pip install）
python scripts/selfcheck.py
python -m pytest tests/ -q

```

**全绿 = 发布成功。**

### 6.4 常见失败与处置

| 现象 | 原因 | 处置 |
|---|---|---|
| CI badge 红 | 某个 Python 版本不兼容 | 看 Actions 日志定位版本 |
| CI 报"发现第三方依赖" | 有人加了 import | 删掉，或用纯标准库重写 |
| `release.yml` 报版本不一致 | tag 与 pyproject 版本号不符 | 删 tag 重打：`git tag -d v0.1.0` |
| clone 后 `selfcheck` 红 | 有文件漏了 `git add` | 补提交后重打 tag |
| `git push` 要求输入密码 | GitHub 已停用密码认证 | 改用 token 或 `gh auth login` |

---

## 附：本次发布的一条特殊说明

**v0.1.0 修掉了一个确定性失败，而不是偶发失败。**

`tests/test_phase7_vision_chain.py::TestLayerGuards::test_llm_is_placeholder`
原先断言 `"占位" in reason` —— 那是阶段七的文案（第五层当时只有接口占位）。
阶段八把它真正实现后，文案变成"视觉大模型未启用：视觉后端未启用
（需要 enabled=True）"，`"占位"` 两个字没了，这条就**每次都挂**。

当时的判断是"跨测试污染导致的偶发失败"，**那个判断是错的** ——
单独跑也挂。已核实。测试与模块 docstring 都已按新契约更新。

详见 `tests/conftest.py` 与 `avatar/vision/chain.py` 里的说明。
