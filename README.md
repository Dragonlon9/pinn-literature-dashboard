# PINN 文献追踪看板

一个单用户、本地运行的文献追踪看板：从 arXiv 拉取最近 X 天的英文论文，按 PINN/PDE 规则筛选，用 DeepSeek 提取三项中文字段和英文代码证据，存入 SQLite，并用 Streamlit 展示。

- 只处理英文标题和摘要；不下载 PDF，不自动访问 GitHub。
- 代码是否开源只依据摘要中作者的公开证据；提取失败/待提取显示“提取失败/待提取”，**绝不显示为 No**。
- 外部标题、摘要、API 响应一律按不可信数据处理；模型输出必须通过 JSON/schema/证据子串校验。
- 真实密钥只从环境变量读取；`.env`、数据库、日志和缓存不提交到 GitHub。

## 1. 环境要求

- Windows 10/11
- Python 3.10+（本项目在本机 Python 3.14.5 上实际运行，见 `progress.md`）
- 能访问 arXiv API 和 `https://api.deepseek.com`
- 一个 DeepSeek API 密钥

## 2. 从零开始（PowerShell）

在仓库根目录打开 PowerShell（路径含空格或中文时请保留引号）：

```powershell
cd "C:\path\to\your_project_root"

# 1) 创建虚拟环境
python -m venv .venv

# 2) 激活；若执行策略阻止 Activate.ps1，可跳过激活，后面直接使用 .\.venv\Scripts\python.exe
.\.venv\Scripts\Activate.ps1

# 3) 安装依赖
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果 `Activate.ps1` 被 PowerShell 执行策略阻止，**不要修改系统执行策略**，直接使用解释器全路径：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 3. 配置 DeepSeek 密钥

密钥只放在当前 PowerShell 进程的环境变量中，程序不会自动读取 `.env`：

```powershell
$env:DEEPSEEK_API_KEY = "<your-key>"

# 只检查是否存在，不打印密钥内容
if ($env:DEEPSEEK_API_KEY) { "DEEPSEEK_API_KEY 已配置" } else { "DEEPSEEK_API_KEY 未配置" }
```

可选配置：

```powershell
$env:DEEPSEEK_MODEL = "deepseek-flash"          # 默认；如确认 deepseek-chat 可用可覆盖
$env:PINN_DB_PATH = "data\pinn.db"              # 默认
$env:PINN_TIMEZONE = "Asia/Shanghai"            # 默认
$env:PINN_SCHEDULER_ENABLED = "1"               # 默认开启应用内 08:00 调度
```

不要把密钥写进 `README.md`、`.env.example`、任务计划程序参数、截图或 Git 提交。`sk-your-key-here` 只是占位符。

## 4. 首次抓取

```powershell
.\.venv\Scripts\python.exe update.py --days 7
```

成功时会打印一行 JSON，例如：

```json
{"status":"success","run_id":1,"result_count":12,"extraction_failures":0,"busy":false,"truncated":false,"error_code":null,"error_message":null,"model":"deepseek-flash"}
```

判断标准：

- `status` 为 `success`：检索成功且全部提取有效。
- `status` 为 `empty`：检索成功但范围内 0 篇，也是成功；可扩大 `--days`。
- `status` 为 `partial`：检索成功但至少 1 篇提取失败；失败项在数据库中为 `NULL`，不会变成 No。
- `status` 为 `failed`：arXiv 失败/超时/解析失败，旧批次保留；查看 `logs/app.log` 的脱敏错误。
- 命令退出码：`0` 成功/空结果，`2` 部分成功，`1` 失败/忙。

可选：用临时数据库做一次隔离演练，不污染 `data/pinn.db`：

```powershell
.\.venv\Scripts\python.exe update.py --days 7 --db .\data\smoke-test.db
```

## 5. 启动看板

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

浏览器打开 Streamlit 输出的地址（默认 `http://localhost:8501`）。页面只读 SQLite；只有点击“应用并刷新”或后台 08:00 调度才会请求外部 API。

看板包含：

- 时间窗口、最后成功更新时间、本次结果数（筛选前）、当前显示数（筛选后）
- 标题、v1 提交日期、arXiv 原文链接、中文贡献、数据集/方法、代码状态、英文证据、可展开英文摘要
- 关键词 + 开源状态（全部/Yes/No）组合筛选；空结果、更新失败、提取失败分别显示
- 提取失败项的“重试失败项”按钮（不重新检索 arXiv）

## 6. 每日 08:00 调度（应用内，主方案）

应用运行时，后台守护线程每天 08:00（Asia/Shanghai）调用同一个 `Pipeline.refresh(7, "scheduled")`；关闭期间不后台运行，错过则下次启动补跑一次。调度失败也会记录当天已尝试，避免无限重试。

应用内调度不需要额外配置。若不想启用，设置：

```powershell
$env:PINN_SCHEDULER_ENABLED = "0"
```

## 7. Windows 任务计划程序（可选，第二方案）

任务计划程序可以在看板关闭时运行独立更新命令。它与应用内调度**不是必须二选一**：两者同时配置时，跨进程文件锁会让其中一个返回 `busy`，不会并发调用 arXiv/DeepSeek。若只想保留任务计划程序，可设置 `PINN_SCHEDULER_ENABLED=0`。

### 7.1 先确认解释器和工作目录

```powershell
$Root = (Get-Location).Path
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$Root
$Python
```

- **程序或脚本**：填 `$Python` 的完整路径，例如
  `C:\path\to\your_project_root\.venv\Scripts\python.exe`
- **添加参数**：`update.py --days 7 --trigger scheduled`
- **起始于（工作目录）**：填仓库根目录 `$Root`，例如
  `C:\path\to\your_project_root`
- 不要把 `DEEPSEEK_API_KEY` 写进“添加参数”。

### 7.2 图形界面步骤

1. 打开“任务计划程序” → “创建任务”（不是“创建基本任务”）。
2. “常规”：使用当前 Windows 用户；按需选择“只在用户登录时运行”或“无论用户是否登录都运行”。
3. “触发器” → 新建 → 每天 → 08:00。
4. “操作” → 新建 → 启动程序：
   - 程序或脚本：上一步的 `$Python` 全路径
   - 添加参数：`update.py --days 7 --trigger scheduled`
   - 起始于：仓库根目录
5. “条件”：取消“仅在使用计算机的交流电源时才启动”等不需要的限制。
6. 保存后右键任务 → “运行”，检查“上次运行结果”和 `logs/app.log`。

### 7.3 命令行注册（可选）

```powershell
$Root = (Get-Location).Path
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$Action = New-ScheduledTaskAction `
  -Execute $Python `
  -Argument "update.py --days 7 --trigger scheduled" `
  -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Daily -At "08:00"
Register-ScheduledTask `
  -TaskName "PINN-Literature-Update" `
  -Action $Action `
  -Trigger $Trigger `
  -Description "PINN 文献追踪看板每日刷新（可选）"
```

测试运行：

```powershell
Start-ScheduledTask -TaskName "PINN-Literature-Update"
Get-ScheduledTaskInfo -TaskName "PINN-Literature-Update"
```

### 7.4 任务计划程序的密钥注意事项

任务计划程序不会自动继承你当前 PowerShell 窗口里的 `$env:DEEPSEEK_API_KEY`。可选做法：

- 使用“只在用户登录时运行”，并在同一用户会话中设置用户级环境变量后重新登录；或
- 在“系统属性 → 环境变量”中为当前用户添加 `DEEPSEEK_API_KEY`，然后重新启动任务计划程序/重新登录；或
- 确认任务以同一用户账户运行，且该账户能读取到该环境变量。

无论哪种方式，都不要把密钥写进任务参数、脚本或仓库。设置后先用一个临时任务或手动运行 `update.py` 验证，不要假设任务一定继承了环境变量。

### 7.5 时区

任务计划程序的 08:00 使用 **Windows 系统本地时区**。如果系统时区不是 Asia/Shanghai，请把触发器改成对应的本地时间，或调整系统时区。应用内调度固定按 `PINN_TIMEZONE`（默认 Asia/Shanghai）判断。

## 8. 运行测试（离线）

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试特点：

- 全部使用 `tests/fixtures/` 中明确标注为虚构的模拟数据（arXiv ID 以 `9999.` 开头）。
- 使用 pytest 临时目录中的 SQLite，不读取、不写入 `data/pinn.db`。
- 不访问 arXiv/DeepSeek，不需要真实密钥。
- 覆盖：去重、日期边界、非法 JSON 重试成功、持续失败、arXiv 故障保留旧数据、失败记录不被筛成 No、调度 08:00/补跑等。

真实 API 联调（需要网络和密钥）不属于默认离线测试；运行前请确认会产生真实 API 费用。当前仓库不声称真实联调已通过，状态见 `progress.md`。

## 9. 常见问题

**Q: 页面提示“未检测到 DEEPSEEK_API_KEY”，刷新按钮禁用。**
A: 在启动 Streamlit 的同一个 PowerShell 窗口设置 `$env:DEEPSEEK_API_KEY`，然后重启 `streamlit run app.py`。程序不会读取 `.env`。

**Q: `status=partial` 但有些论文没有中文贡献。**
A: 这是提取失败，不是 No。页面会显示“提取失败”；可点“重试失败项”，或检查 `logs/app.log` 中的错误码（不含密钥）。

**Q: 检索失败后数据变少了吗？**
A: 不会。arXiv 失败时 `current_run_id` 不切换，旧批次和最后成功更新时间保留；页面显示“检索失败”。

**Q: 为什么 v1 日期在窗口外但论文最近更新过？**
A: 时间窗口按 v1 `published`（首次提交）过滤，不按 `updated`；这是为了与“最近 X 天的新论文”口径一致。

**Q: 如何清空本地数据重新开始？**
A: 停止看板，删除 `data\pinn.db`、`data\pinn.db-wal`、`data\pinn.db-shm` 和 `data\pinn.db.lock`。不要删除源码或定稿文档。

## 10. 提交到 GitHub 前检查

```powershell
git status --short
git check-ignore -v data\pinn.db .env .venv 2>$null
```

确认以下内容**没有**出现在待提交列表中：

- `.venv/`
- `data/` 下的数据库、WAL/SHM、锁文件
- `logs/`
- `.env`
- `.streamlit/secrets.toml`
- 任何真实密钥

首次提交：

```powershell
git init
git add .
git commit -m "PINN literature dashboard MVP"
git branch -M main
git remote add origin https://github.com/<your-account>/<your-repo>.git
git push -u origin main
```

`AGENTS.md`、`研究说明.md`、`技术设计文档.md`、`progress.md` 必须保留在仓库根目录。
