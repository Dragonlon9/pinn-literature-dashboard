"""Streamlit 看板：只读 SQLite + 显式刷新触发。

页面顶层不调用 arXiv/DeepSeek；所有外部请求都通过 Pipeline.refresh，
并由 session_state 守卫 + 进程内锁 + 跨进程文件锁防重入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlparse

import streamlit as st

from src.config import Settings
from src.logging_setup import setup_logging
from src.models import PaperView, RefreshResult, from_iso
from src.pipeline import Pipeline
from src.repository import Repository
from src.runner import Runtime, build_runtime
from src.scheduler import Scheduler

st.set_page_config(
    page_title="PINN 文献追踪看板",
    page_icon="📚",
    layout="wide",
)


@dataclass
class _Resources:
    settings: Settings
    repo: Repository
    pipeline: Pipeline
    scheduler: Scheduler


@st.cache_resource(show_spinner=False)
def get_resources() -> _Resources:
    settings = Settings.from_env()
    setup_logging(settings.log_dir)
    runtime: Runtime = build_runtime(settings, start_scheduler=True)
    return _Resources(
        settings=runtime.settings,
        repo=runtime.repo,
        pipeline=runtime.pipeline,
        scheduler=runtime.scheduler,
    )


def _escape_md(text: str) -> str:
    value = re.sub(r"([\\`*_\[\]<>])", r"\\\1", text or "")
    return value.replace("\n", " ")


def _format_local(value: str | datetime | None, settings: Settings) -> str:
    if value is None:
        return "尚无"
    if isinstance(value, str):
        parsed = from_iso(value)
    else:
        parsed = value
    if parsed is None:
        return "尚无"
    return parsed.astimezone(settings.tz).strftime("%Y-%m-%d %H:%M")


def _result_label(result: RefreshResult) -> str:
    mapping = {
        "success": "更新成功",
        "empty": "检索成功，范围内 0 篇",
        "partial": "部分成功",
        "failed": "更新失败",
        "busy": "已有更新任务",
        "invalid_days": "天数非法",
    }
    return mapping.get(result.status, result.status)


def _show_result(result: RefreshResult) -> None:
    label = _result_label(result)
    if result.status == "success":
        st.success(
            f"{label}：本次结果 {result.result_count} 篇；提取失败 "
            f"{result.extraction_failures} 篇。"
        )
    elif result.status == "empty":
        st.info(f"{label}。可扩大“最近 X 天”后重试。")
    elif result.status == "partial":
        st.warning(
            f"{label}：本次结果 {result.result_count} 篇，其中 "
            f"{result.extraction_failures} 篇提取失败；失败项不会显示为 No。"
        )
    elif result.status == "busy":
        st.warning(result.error_message or "已有更新任务正在运行。")
    elif result.status == "invalid_days":
        st.error(result.error_message or "天数必须是 1–30 的整数。")
    else:
        st.error(
            f"{label}：{result.error_code or 'unknown'} "
            f"{result.error_message or ''}".strip()
        )


def _extract_urls(text: str) -> list[str]:
    found = re.findall(r"https?://[^\s<>\"')\]]+", text or "")
    result: list[str] = []
    for raw in found:
        url = raw.rstrip(".,;:")
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            result.append(url)
    return list(dict.fromkeys(result))


def _safe_url(url: str) -> str:
    return url.replace(")", "%29").replace("]", "%5D").replace(" ", "%20")


def _render_status_banner(latest_run, current_run) -> None:
    if latest_run is None:
        return
    if latest_run.status == "failed":
        message = (
            f"检索失败：{latest_run.error_code or 'unknown'} "
            f"{latest_run.error_message or ''}"
        ).strip()
        if current_run is not None:
            message += (
                " 已保留上一次可用批次（可能含提取失败项）；"
                "最后成功更新时间不变。"
            )
        st.error(message)
    elif latest_run.status == "partial":
        st.warning(
            "部分成功：检索成功，但至少 1 篇提取失败。"
            "“最后成功更新时间”未推进，“最后成功检索时间”已更新。"
        )
    elif latest_run.status == "empty":
        st.info("检索成功，范围内 0 篇；可扩大“最近 X 天”后重试。")
    elif latest_run.status == "success":
        st.success("检索成功，全部提取有效。")


def _render_sidebar(resources: _Resources) -> None:
    settings = resources.settings
    st.sidebar.header("运行状态")
    st.sidebar.caption(
        "DEEPSEEK_API_KEY：" + ("已配置" if settings.has_api_key else "未配置")
    )
    st.sidebar.caption(f"模型：{settings.deepseek_model}")
    st.sidebar.caption(f"数据库：{settings.db_path}")
    scheduler_status = resources.scheduler.status()
    st.sidebar.caption(
        "调度器：" + ("运行中" if scheduler_status["running"] else "已关闭")
    )
    st.sidebar.caption(
        "每日 08:00（Asia/Shanghai）自动刷新；关闭期间不后台运行，"
        "下次启动补跑。"
    )
    st.sidebar.caption(
        f"最近一次调度尝试：{scheduler_status['last_scheduled_date'] or '尚无'}"
    )
    if st.sidebar.button("刷新状态"):
        st.rerun()
    st.sidebar.caption(
        "任务计划程序为可选入口；与看板同时运行由跨进程文件锁避免并发。"
    )


def _render_paper(row: PaperView, settings: Settings) -> None:
    with st.container(border=True):
        st.markdown(f"**{_escape_md(row.title)}**")
        local_date = row.first_submitted_at.astimezone(settings.tz).strftime(
            "%Y-%m-%d"
        )
        st.caption(
            f"v1 提交：{local_date}（Asia/Shanghai）｜ arXiv:{row.arxiv_id}"
        )
        st.markdown(
            f"[arXiv 原文](https://arxiv.org/abs/{row.arxiv_id})"
        )

        left, right = st.columns([3, 2])
        with left:
            st.markdown("**核心贡献**")
            st.text(row.contribution_zh or "—")
            st.markdown("**使用的数据集或方法**")
            st.text(row.dataset_or_method_zh or "—")
        with right:
            st.markdown("**代码开源状态**")
            if row.extraction_status == "success":
                if row.code_open_source == "Yes":
                    st.success("Yes｜摘要声明代码公开")
                elif row.code_open_source == "No":
                    st.info("No｜摘要未见公开证据")
                    if row.code_evidence_en:
                        st.caption("证据不足，供人工复核。")
                else:
                    st.warning("提取结果异常：缺少 Yes/No")
            elif row.extraction_status == "failed":
                st.error(
                    f"提取失败（{row.extraction_error or 'unknown'}）；"
                    "不会显示为 No。"
                )
            else:
                st.warning("待提取")

        if row.code_evidence_en:
            with st.expander("代码判断证据（英文原句）"):
                st.text(row.code_evidence_en)
                urls = _extract_urls(row.code_evidence_en)
                if urls:
                    st.markdown(
                        "[代码链接（摘要声明，未验证）]"
                        f"({_safe_url(urls[0])})"
                    )
                    st.caption("系统不访问、不验证该仓库。")
        else:
            st.caption("代码判断证据：无")

        with st.expander("英文摘要"):
            st.text(row.abstract)


def main() -> None:
    resources = get_resources()
    settings = resources.settings
    repo = resources.repo

    _render_sidebar(resources)

    st.title("PINN 文献追踪看板")
    st.caption(
        "arXiv 英文标题/摘要 → PINN/PDE 相关性筛选 → DeepSeek 结构化提取 → "
        "SQLite 缓存；界面不主动访问外部 API。"
    )

    current_id = repo.current_run_id()
    current_run = repo.get_run(current_id)
    latest_run = repo.latest_run()

    keyword = st.session_state.get("keyword", "")
    code_label = st.session_state.get("code_filter_label", "全部")
    code_filter = {"全部": "all", "Yes": "yes", "No": "no"}.get(
        code_label, "all"
    )

    rows = repo.batch_rows(current_id, keyword, code_filter)
    display_count = len(rows)

    if current_run is not None:
        days = current_run.requested_days
        window_start = current_run.window_start_utc
        window_end = current_run.window_end_utc
        result_count_text = str(current_run.result_count)
    else:
        days = 7
        now_utc = datetime.now(timezone.utc)
        window_start = now_utc - timedelta(days=days)
        window_end = now_utc
        if latest_run is not None and latest_run.status == "failed":
            result_count_text = "未更新"
        else:
            result_count_text = "尚无"

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("时间窗口", f"最近 {days} 天")
    col2.metric("本次结果数（筛选前）", result_count_text)
    col3.metric("当前显示数（筛选后）", str(display_count))
    col4.metric(
        "最后成功更新",
        _format_local(repo.last_update_success_at(), settings),
    )

    st.caption(
        "检索窗口："
        f"{_format_local(window_start, settings)} 至 "
        f"{_format_local(window_end, settings)}（Asia/Shanghai）"
    )
    last_retrieval = repo.last_retrieval_success_at()
    if last_retrieval:
        st.caption(f"最后成功检索：{_format_local(last_retrieval, settings)}")

    if current_run is not None and current_run.truncated:
        st.warning("达到 500 篇上限，结果可能被截断。")

    _render_status_banner(latest_run, current_run)

    previous = st.session_state.pop("last_refresh_result", None)
    if previous is not None:
        _show_result(previous)
    previous_error = st.session_state.pop("last_refresh_error", None)
    if previous_error:
        st.error(previous_error)

    with st.form("refresh_form"):
        input_days = st.number_input(
            "最近 X 天",
            min_value=1,
            max_value=30,
            value=int(days),
            step=1,
            help="窗口为 [now−X 天, now]，按 v1 首次提交时间过滤。",
        )
        submitted = st.form_submit_button(
            "应用并刷新",
            type="primary",
            disabled=(
                st.session_state.get("refresh_in_progress", False)
                or not settings.has_api_key
            ),
        )

    if not settings.has_api_key:
        st.error(
            "未检测到 DEEPSEEK_API_KEY 环境变量；为避免把提取失败写成 No，"
            "刷新按钮已禁用。请先在 PowerShell 设置密钥并重启看板。"
        )

    if submitted:
        if st.session_state.get("refresh_in_progress", False):
            st.warning("已有刷新任务在进行中。")
        else:
            st.session_state["refresh_in_progress"] = True
            try:
                with st.spinner("正在检索 arXiv 并调用 DeepSeek，请稍候…"):
                    result = resources.pipeline.refresh(
                        int(input_days), "manual"
                    )
                st.session_state["last_refresh_result"] = result
            except Exception as exc:
                st.session_state["last_refresh_error"] = (
                    f"刷新失败：{type(exc).__name__}（详情见 logs/app.log）"
                )
            finally:
                st.session_state["refresh_in_progress"] = False
            st.rerun()

    if current_run is not None and current_run.extraction_failures > 0:
        if st.button(
            "重试失败项（不重新检索 arXiv）",
            disabled=(
                st.session_state.get("refresh_in_progress", False)
                or not settings.has_api_key
            ),
        ):
            st.session_state["refresh_in_progress"] = True
            try:
                with st.spinner("正在重试失败项…"):
                    result = resources.pipeline.retry_failed(current_run.id)
                st.session_state["last_refresh_result"] = result
            except Exception as exc:
                st.session_state["last_refresh_error"] = (
                    f"重试失败：{type(exc).__name__}（详情见 logs/app.log）"
                )
            finally:
                st.session_state["refresh_in_progress"] = False
            st.rerun()

    st.divider()
    st.subheader("筛选")
    st.text_input(
        "关键词（匹配英文标题、英文摘要和中文提取）",
        key="keyword",
        placeholder="例如 inverse、Burgers、开源",
    )
    st.selectbox(
        "开源状态",
        options=["全部", "Yes", "No"],
        key="code_filter_label",
    )
    st.caption(
        "关键词与开源状态取交集；提取失败/待提取只在“全部”中出现，"
        "不会被筛成 No。"
    )

    st.subheader(f"论文列表（当前显示 {display_count} 篇）")

    if current_run is None:
        if latest_run is not None and latest_run.status == "failed":
            st.info("尚无可用旧数据；请修复检索问题后重试。")
        else:
            st.info(
                "尚无缓存。首次启动会在后台补跑一次 7 天检索；"
                "也可以点击“应用并刷新”。"
            )
        return

    if current_run.result_count == 0:
        st.info("检索成功，范围内 0 篇。可扩大“最近 X 天”后重试。")
        return

    if display_count == 0:
        st.info(
            "当前筛选无匹配。请清空关键词，或把开源状态切回“全部”。"
        )
        return

    for row in rows:
        _render_paper(row, settings)


main()
