# Progress

## 2026-11-01 需求复核与实现启动

- **依据**：`AGENTS.md`、`研究说明.md` v1.0、`技术设计文档.md` v1.0；用户确认“开始实现，按推荐默认”。
- **确认的默认决策**：
  1. `DEEPSEEK_MODEL` 可配置，默认 `deepseek-flash`；`deepseek-chat` 仅作为可覆盖候选，未联调前不写成已验证。
  2. 空库首次启动在后台补跑一次 7 天 `startup_catchup`；页面先显示“尚无”。
  3. `No` 且 `code_evidence_en` 非空时，前端追加“证据不足，供人工复核”。
  4. 应用内 08:00（Asia/Shanghai）调度为主；独立 `update.py` 与 Windows 任务计划程序为可选入口；新增跨进程文件锁，保证 F2 的“同一时刻只允许一个更新任务”。
  5. 时间窗按 v1 `published` 过滤，不按 `updated`。
- **未改动**：`AGENTS.md`、`研究说明.md`、`技术设计文档.md` 保持定稿原文。

## 已完成（代码）

- `src/config.py`：环境变量配置、密钥 `repr` 脱敏、UTC/Asia-Shanghai 时区。
- `src/models.py`、`src/db.py`、`src/repository.py`：SQLite schema（papers/extractions/runs/run_papers/exclusions/app_meta）、WAL/foreign_keys、run 状态机、搜索/筛选、失败字段为 `NULL`、30 分钟 stale run 恢复。
- `src/arxiv_client.py`：arXiv 查询、`submittedDate` 窗口、分页终止、Atom 解析、canonical ID、错误 feed 识别、3 次有限重试/429 `Retry-After`。
- `src/relevance.py`：PINN 家族词 + PDE 词、ODE-only/综述/非英文/缩写不明/神经算子排除，排除原因与命中词。
- `src/extractor.py` + `prompts/`：DeepSeek Chat Completions、结构化 Prompt、JSON 对象/恰好四键/类型/枚举/中文长度/证据子串校验、单篇最多 2 次、401/402 停止后续调用、错误脱敏。
- `src/locking.py`：Windows `msvcrt` / POSIX `fcntl` 跨进程锁。
- `src/pipeline.py`：刷新锁、run 生命周期、相关性/去重/时间过滤、缓存复用、逐篇失败隔离、partial/success/empty/failed、失败项重试。
- `src/scheduler.py`：每日 08:00、启动补跑、busy 也记当天、后台线程不调用 `st.*`。
- `src/runner.py`、`update.py`：共享 `Pipeline.refresh` 的独立更新命令；任务计划程序可调用；退出码 0/2/1。
- `app.py`：Streamlit 看板、状态卡、表单刷新、关键词 + 开源状态交集筛选、空结果/失败/提取失败、证据/摘要展开、失败项重试。
- `README.md`、`requirements.txt`、`.gitignore`、`.env.example`、`pytest.ini`、`data/.gitkeep`。
- `tests/`：79 个离线测试，fixture 全部以 `9999.*` 虚构 ID 和 `FICTIONAL` 标注，使用临时 SQLite，不访问网络。

## 实际运行的命令与结果

> 以下只记录实际执行过的命令；“未运行/待我本地验证”的项不会写成通过。

### 环境

```text
Python 3.14.5 (C:\Python314\python.exe)
venv: .venv
实际安装版本：streamlit 1.66.1、requests 2.34.2、tzdata 2026.6、pytest 8.4.2
```

- `python -m venv .venv` 在**当前 DSH Windows 沙箱**内首次失败：`ensurepip` 创建 0o700 临时目录后无写权限（错误链包含 `PermissionError` / `[sandbox: file access denied]`）。
- 处理：仅在本机环境用 `--without-pip --system-site-packages` 重建 venv，并用一个临时 `sitecustomize.py` 让 `os.mkdir` 忽略 0o700；依赖安装成功。
- 这是**本机沙箱特例**，不是普通 Windows 用户需要执行的步骤；README 保留标准 `python -m venv .venv` + `pip install`。测试本身在 `tests/conftest.py` 里做了同样的临时目录兼容，因此 `pytest` 可直接运行。

### 编译与测试

```powershell
.\.venv\Scripts\python.exe -m compileall -q app.py update.py src tests
# 结果：exit 0（首次有 update.py docstring 的 SyntaxWarning，已改为 raw docstring 后重跑）

.\.venv\Scripts\python.exe -m pytest -q --disable-warnings
# 结果：79 passed in 2.71s（含新增的 2 个 AppTest 回归）
```

### 独立更新命令（离线）

```powershell
.\.venv\Scripts\python.exe update.py --days 0 --db .\.tmp\cli-smoke.db
# stdout: {"status": "invalid_days", ... "error_code": "invalid_days", ...}
# exit=1；没有请求 arXiv。
```

### arXiv 真实检索（无密钥）

```powershell
# 仅调用 ArxivClient.fetch，7 天窗口；未调用 DeepSeek，未入库
# 结果：OK total 4 papers 4 attempts 1 truncated False
# ID：2610.38269、2610.38064、2610.37473、2610.34345
```

随后用独立命令对真实 arXiv 执行了一次完整抓取（没有配置 `DEEPSEEK_API_KEY`）：

```powershell
.\.venv\Scripts\python.exe update.py --days 7
# stdout: {"status":"partial","run_id":1,"result_count":4,
#          "extraction_failures":4,"busy":false,"truncated":false,
#          "error_code":null,"error_message":null,"model":"deepseek-flash"}
# exit=2
# 4 篇提取均为 failed / error_code=missing_api_key / code_open_source=NULL
```

说明：这是真实 arXiv 抓取 + 真实数据库写入 + 缺密钥失败路径，**不是**真实 DeepSeek 联调；未生成任何真实中文提取结果。

### Streamlit 服务冒烟

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py --server.headless true --server.port 8511 ...
# HTTP GET http://127.0.0.1:8511/
# 结果：status=200，length=6467，内容包含 streamlit；随后终止进程。
```

## 仓库状态

- 已执行 `git init` 与 `git add .`：46 个文件已暂存；**未提交**，因为本机没有配置 `git user.name` / `git user.email`。
- 已执行 `git check-ignore -v data\pinn.db data\pinn.db-wal logs\app.log .env .venv`：均被 `.gitignore` 覆盖（exit 0）。
- 已确认暂存列表不含 `.venv/`、`.tmp/`、`data/*.db`、`logs/`、`.env` 或真实密钥。

## 离线测试覆盖（实际通过）

| 要求 | 测试 |
|---|---|
| 去重 | `tests/test_pipeline.py::test_normal_run_and_exclusions`（`9999.00001` v2/v3 只保留一条） |
| 日期边界 | `tests/test_pipeline.py::test_date_boundary_includes_start_and_end`（start/end 精确包含，窗口外排除） |
| 非法 JSON 后重试成功 | `tests/test_extractor.py::test_bad_json_then_retry_success` |
| 持续失败 | `tests/test_extractor.py::test_persistent_bad_json_fails`、`test_pipeline.py::test_partial_invalid_json_then_retry_success` |
| API 故障保留旧数据 | `tests/test_pipeline.py::test_arxiv_failure_preserves_previous_batch`、`tests/test_repository.py::test_finalize_success_then_failed_does_not_switch` |
| 失败记录不会被筛成 No | `tests/test_repository.py::test_search_filter_intersection_and_failed_not_no`、`test_pipeline.py::test_partial_invalid_json_then_retry_success` |
| 空结果 | `tests/test_pipeline.py::test_empty_run_is_success_not_failure` |
| 缓存复用/不重复调用 | `tests/test_pipeline.py::test_cache_reuse_does_not_call_deepseek_again` |
| 重复刷新 busy | `tests/test_pipeline.py::test_busy_second_refresh_is_rejected` |
| 401 停止后续调用 | `tests/test_pipeline.py::test_auth_error_stops_remaining_calls`、`test_extractor.py::test_401_stops_and_does_not_leak_key` |
| 提示注入不执行 | `tests/test_extractor.py::test_prompt_injection_is_only_data` |
| 每日 08:00/启动补跑 | `tests/test_scheduler.py` 全组 |
| 看板默认头/卡片/筛选/空态 | `tests/test_app_smoke.py` 全组 |

## AC 自查（对照《研究说明.md》，按实际证据）

| AC | 证据 | 状态 |
|---|---|---|
| AC-01 默认 7 天/最后成功“尚无”/X=0、31 拒绝 | AppTest 默认头；pipeline 非法天数测试 | 离线通过；手工首次打开待本地验证 |
| AC-02 正常检索与详情 | pipeline 正常 run；AppTest 卡片含标题/日期/链接/字段；真实 arXiv 取得 4 篇 | 检索通过；真实 DeepSeek 未运行 |
| AC-03 范围筛选 | relevance 全组 + pipeline exclusions | 离线通过 |
| AC-04 字段真实性 | Prompt 规则 + 证据校验测试 | **待验证**：无真实中文提取，无法抽查语义 |
| AC-05 代码口径 | extractor Yes/No/证据测试；AppTest 精确文案 | 离线通过；真实 Yes 原句待验证 |
| AC-06 搜索+筛选 | repository 交集测试；AppTest keyword + Yes：2→1 | 通过（AppTest） |
| AC-07 重复刷新/去重/缓存 | busy/duplicate/cache 测试；真实两次刷新：4 篇、0 重复 | 通过（含真实 arXiv） |
| AC-08 空结果 | pipeline empty 测试；AppTest 空 run 页面 | 通过（AppTest） |
| AC-09 学术 API 不可用 | 离线测试 + 真实连接失败注入：旧批次保留 | 通过（含真实故障注入） |
| AC-10 非法 JSON/部分成功 | extractor 重试、pipeline partial + retry | 离线通过；真实 API 待验证 |
| AC-11 提示注入 | `test_prompt_injection_is_only_data` | 离线通过 |
| AC-12 每日更新/补跑 | scheduler 假时钟测试 | **待验证**：无任务计划程序，真实 08:00 未运行 |

**验收结论**：离线自动化覆盖 AC-01–AC-12 的代码路径；真实 arXiv 检索、去重、故障保留、页面筛选/空态/失败态有实际证据。真实 DeepSeek 字段质量、真实 08:00、GitHub 发布和页面截图仍未通过验收。

## 2026-11-01 验收复核（仅实际证据）

| 验收项 | 实际证据 | 结论 | 最小修复或验证步骤 |
|---|---|---|---|
| 窗口内真实 PINN 论文 | 真实 arXiv 7 天：`total=4, papers=4, attempts=1, truncated=False`；4 个 ID：2610.38269、2610.38064、2610.37473、2610.34345 | 通过 | 换领域时复核查询召回；保持 429/超时重试 |
| 重复刷新不重复 | 同一 DB 连续两次真实 refresh：`papers_total=4`、`extractions_total=4`、`duplicate_papers=[]`、每个 run 的 `run_papers=4` | 通过 | 无 |
| 中文贡献/方法可回溯 | 真实 4 篇均 `failed/missing_api_key`，无真实中文结果；AppTest 中文来自人工 fixture，不能证明模型语义正确 | **待验证** | 配 key 后跑 `update.py --days 7`；抽 3 篇对照 title+abstract，记录每句贡献依据 |
| Yes 有本文代码公开原句 | 真实 DB 无 Yes；fixture Yes 是人工构造；单元测试验证 Yes 空/不匹配会失败 | **待验证** | 30 天窗口找真实 Yes；在 abstract 中原样搜索 evidence；核对作者公开本文实现的陈述 |
| 失败不标 No | 真实 DB 4 条 failed 的 `code_open_source=NULL`；AppTest failed 卡片显示“提取失败…不会显示为 No”；No 筛选不含 failed | 通过 | 无 |
| 搜索与 Yes/No 组合 | AppTest：关键词 `Inverse` → 2；叠加 Yes → 1；清空/No/全部由测试覆盖 | 通过（AppTest，无截图） | 本地补截图；检查长标题/中文关键词布局 |
| 零结果 vs API 故障 | 空 run AppTest：`errors=[]`，info“检索成功，范围内 0 篇”；API 故障 AppTest：error“检索失败…”，仍显示 3 张旧卡 | 通过（AppTest，无截图） | 本地分别截取空结果、失败页；不要只留终端输出 |
| 非法 JSON 有限重试 | `test_bad_json_then_retry_success` 通过（2 次调用后成功）；`test_persistent_bad_json_fails` 2 次后 failed | 通过（模拟 HTTP） | 真实 key 后看 `attempts`/日志；真实 API 下不要强行制造非法 JSON |
| 单篇失败不影响其他 | `test_partial_invalid_json_then_retry_success`；UI demo 2 success + 1 failed 仍显示 3 卡 | 通过（fixture + AppTest） | 真实 key 后确认 partial 页面其他卡片仍可读 |
| 检索故障旧数据仍可浏览 | 真实注入 `ARXIV_API_URL=http://127.0.0.1:1`：新增 run failed=`arxiv_connection_error`；`current_run_id=2` 不变；`last_retrieval` 不变；`papers=4`；`run_papers` 仅 run 1/2 | 通过（真实故障注入） | 无 |
| 每日 08:00 | `Get-ScheduledTask PINN-Literature-Update` → `NO_TASK`；真实 DB `last_scheduled_date=None`；仅假时钟测试 | **待验证/未配置** | 按 README §7 注册任务并 `Start-ScheduledTask`；查 `last_scheduled_date` 和 run trigger；或临时把 `PINN_SCHEDULED_HOUR/MINUTE` 设为 1 分钟后重启 Streamlit |
| GitHub 仓库 | 本地 46 文件已暂存；`git log` 无提交；`git remote -v` 为空；分支 master | **失败（未发布）** | 配置 `git user.name/email`；`git commit`；`git remote add origin <repo>`；`git push -u origin main`；核对 GitHub 文件树与 `.env`/数据库排除 |
| 页面截图 | 仓库无 png/jpg；Edge headless 被本机沙箱拒绝（`OpenProcess ... 0x5`）；只有 AppTest 文本证据 | **待验证** | 本地浏览器打开 Streamlit，分别截默认/空结果/API 故障/筛选页；附 URL 与截图 |
| 真实 DeepSeek | 未配置密钥；真实 run 全部 `missing_api_key`；无中文/Yes 证据 | **待验证** | 设置密钥后跑 `update.py --days 7`，回传脱敏 JSON + 3 篇字段对照 |
| 跨进程刷新锁 | 两个 Python 进程：holder `True`，contender `False`；holder 退出后 contender `True` | 通过 | 无 |
| 作业要求文件结构 | `AGENTS.md`、`研究说明.md`、`技术设计文档.md`、`progress.md`、`app.py`、`requirements.txt`、`src/` 均存在；79 个离线测试通过 | 通过（本地） | 仅剩 GitHub 发布 |

## 已证实缺陷与最小修复

1. **已修复（UI 文案）**：API 故障横幅在 `current_run` 为 `partial` 时误写“已保留上次成功批次”。已改为“已保留上一次可用批次（可能含提取失败项）”，并新增 2 个 AppTest 回归；当前 **79 passed**。
2. **未修复（证据缺口，不是代码缺陷）**：没有真实 DeepSeek 中文结果，无法判定 AC-04/AC-05 的真实语义质量；需要密钥和人工抽查。
3. **未修复（交付缺口）**：没有注册 Windows 任务计划程序，也没有真实 08:00 运行记录；只有假时钟测试。
4. **未修复（交付缺口）**：本地 Git 无提交、无远程，GitHub 上还没有仓库。
5. **未修复（证据缺口）**：没有页面截图；本机 Edge 截图被 DSH 沙箱阻断，AppTest 不能替代视觉截图。

## 已知问题与待办

1. **真实 DeepSeek 未联调**：没有可用密钥；真实模型名/JSON Output 行为/429 头/费用均未验证。命令：
   ```powershell
   $env:DEEPSEEK_API_KEY = "<your-key>"
   .\.venv\Scripts\python.exe update.py --days 7
   ```
   预期：`status=success` 或 `empty`；若 `partial`，检查失败项错误码与 `logs/app.log`。
2. **真实 08:00 未运行**：需在本地注册任务计划程序或用临时 1 分钟配置；日志中应看到 scheduler 记录，页面最后成功更新时间推进。
3. **模型名**：默认 `deepseek-flash`；若官方只接受 `deepseek-chat` 或反之，用 `DEEPSEEK_MODEL` 覆盖，不修改代码。
4. **任务计划程序**：README 已给出程序/参数/工作目录；其 08:00 用 Windows 本地时区，且不一定继承当前 PowerShell 的 `$env:DEEPSEEK_API_KEY`，需按 README 7.4 验证。
5. **DSH 沙箱临时目录权限**：本机 venv 安装绕行已记录；不是仓库运行逻辑的一部分。普通 Windows 用户按 README 标准命令即可。
6. **真实论文数据未提交**：`.tmp/arxiv-smoke.db`、`.tmp/ui-demo/` 是本次真实验证/演示临时库，已被 `.gitignore` 覆盖；GitHub 交付只含代码、fixtures 和文档。

## 下一步

1. 本地按 README 创建 venv、安装依赖、设置密钥。
2. 先跑 `pytest -q`（预期 **79 passed**）。
3. 再跑 `update.py --days 7`，记录脱敏 JSON。
4. 启动 Streamlit，截取默认、空结果、API 故障、关键词+Yes 四张页面图。
5. 配好任务计划程序，执行一次并查 `last_scheduled_date`/run trigger。
6. 配置 git 身份，提交并推送到 GitHub；回传脱敏证据后再判定作业完成。
