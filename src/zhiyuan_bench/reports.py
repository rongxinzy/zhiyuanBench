"""Standalone, prompt-free HTML reports for persisted campaigns."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zhiyuan_bench.persistence import atomic_write_text


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _suite_row(suite: dict[str, Any]) -> str:
    progress = suite.get("progress")
    progress_text = "-"
    progress_element = ""
    if isinstance(progress, dict):
        completed = progress.get("completed")
        total = progress.get("total")
        if isinstance(completed, int) and isinstance(total, int) and total > 0:
            progress_text = f"{completed}/{total}"
            progress_element = f'<progress value="{completed}" max="{total}">{progress_text}</progress>'
    failure = suite.get("failure")
    failure_text = ""
    if isinstance(failure, dict):
        failure_text = _escape(failure.get("message", ""))
    phase_text = ""
    if suite.get("phase"):
        phase_text = str(suite["phase"])
        phase_progress = suite.get("phase_progress")
        if isinstance(phase_progress, dict) and isinstance(
            phase_progress.get("total"), int
        ):
            phase_text += (
                f" · {phase_progress.get('completed', 0)}/{phase_progress['total']}"
            )
    return f"""
      <tr>
        <td><span class="suite-name">{_escape(suite.get("id", ""))}</span><span class="bridge">{_escape(suite.get("bridge", ""))}</span></td>
        <td><span class="status status-{_escape(suite.get("status", "unknown"))}">{_escape(suite.get("status", "unknown"))}</span></td>
        <td><div class="progress-cell"><div class="progress-overall">{progress_element}<span>{progress_text}</span></div><span class="progress-phase">{_escape(phase_text)}</span></div></td>
        <td class="failure">{failure_text}</td>
      </tr>"""


def _report_notice(summary: dict[str, Any]) -> str:
    metadata = summary.get("report") or {}
    kind = str(metadata.get("kind", "partial"))
    mode = str(
        metadata.get(
            "mode", "solo" if len(summary.get("candidates", [])) == 1 else "comparison"
        )
    )
    notices = {
        "comparison": {
            "complete": ("完整报告", "所有测试集均已完成并通过结果验证。"),
            "completed_with_issues": (
                "异常结算报告",
                "评测流程已结束；只有标记为 succeeded 的测试集构成有效对比。",
            ),
            "interrupted": (
                "中断报告",
                "报告保留中断前的进度和错误；未完成测试集不构成有效对比。",
            ),
            "partial": (
                "部分报告",
                "评测仍在进行或尚未开始；此页面会随持久化进度更新。",
            ),
        },
        "solo": {
            "complete": ("单分支报告", "所有测试集均已完成并通过结果验证。"),
            "completed_with_issues": (
                "单分支异常结算报告",
                "评测流程已结束；只有标记为 succeeded 的测试集构成有效单分支结果。",
            ),
            "interrupted": (
                "单分支中断报告",
                "报告保留中断前的进度和错误；未完成测试集不构成有效结果。",
            ),
            "partial": (
                "单分支部分报告",
                "评测仍在进行或尚未开始；此页面会随持久化进度更新。",
            ),
        },
    }
    mode_notices = notices.get(mode, notices["comparison"])
    title, description = mode_notices.get(kind, mode_notices["partial"])
    failure = summary.get("failure")
    failure_text = ""
    if isinstance(failure, dict) and failure.get("message"):
        failure_text = f'<span class="notice-detail">{_escape(failure["message"])}</span>'
    return (
        f'<aside class="report-notice report-notice-{_escape(kind)}">'
        f"<strong>{title}</strong><span>{description}</span>{failure_text}</aside>"
    )


def render_campaign_report(summary: dict[str, Any]) -> str:
    candidates = "".join(
        f"""
        <div class="candidate">
          <span class="candidate-label">{_escape(item.get("label", ""))}</span>
          <span>{_escape(item.get("source_ref", ""))}</span>
          <code>{_escape(str(item.get("revision", ""))[:8])}</code>
        </div>"""
        for item in summary.get("candidates", [])
    )
    rows = "".join(_suite_row(suite) for suite in summary.get("suites", []))
    notice = _report_notice(summary)
    generated = datetime.now(UTC).isoformat()
    auto_refresh = (
        '  <meta http-equiv="refresh" content="3">\n'
        if summary.get("status") in {"created", "running"}
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
{auto_refresh}  <title>{_escape(summary.get("campaign_id", "Campaign report"))}</title>
  <style>
    :root {{ color-scheme: light; --background: oklch(1 0 0); --surface: oklch(1 0 0); --raised: oklch(.97 .001 106.424); --foreground: oklch(.366 .008 253); --secondary: oklch(.553 .013 58.071); --border: oklch(.923 .003 48.717); --success: oklch(.723 .219 149.579); --warning: oklch(.769 .188 70.08); --danger: oklch(.577 .245 27.325); }}
    @media (prefers-color-scheme: dark) {{ :root {{ color-scheme: dark; --background: oklch(.147 .004 49.25); --surface: oklch(.216 .006 56.043); --raised: oklch(.268 .007 34.298); --foreground: oklch(.985 .001 106.423); --secondary: oklch(.709 .01 56.259); --border: oklch(1 0 0 / 10%); --success: oklch(.723 .219 149.579); --warning: oklch(.769 .188 70.08); --danger: oklch(.704 .191 22.216); }} }}
    * {{ box-sizing: border-box; }} body {{ margin: 0; background: var(--background); color: var(--foreground); font: 14px/1.6 system-ui, "PingFang SC", "Microsoft YaHei", sans-serif; letter-spacing: 0; }}
    main {{ width: min(1120px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0 48px; }} header {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; padding-bottom: 24px; border-bottom: 1px solid var(--border); }}
    h1 {{ margin: 0 0 4px; font-size: 20px; line-height: 1.375; font-weight: 600; letter-spacing: 0; }} p {{ margin: 0; color: var(--secondary); }} code {{ padding: 2px 6px; border-radius: 8px; background: var(--raised); font: 12px/1.4 "SF Mono", Consolas, monospace; }}
    .overall {{ text-align: right; }} .overall strong {{ display: block; font-size: 16px; font-weight: 600; }} .candidates {{ display: flex; flex-wrap: wrap; gap: 8px; padding: 20px 0; }} .candidate {{ display: flex; align-items: center; gap: 8px; padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }} .candidate-label {{ color: var(--secondary); font-size: 12px; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }} table {{ width: 100%; border-collapse: collapse; background: var(--surface); }} th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: middle; }} th {{ color: var(--secondary); background: var(--raised); font-size: 12px; font-weight: 500; }} tr:last-child td {{ border-bottom: 0; }} .suite-name, .bridge {{ display: block; }} .suite-name {{ font-weight: 500; }} .bridge, .failure {{ color: var(--secondary); font-size: 12px; }}
    .status {{ font-weight: 500; }} .status-succeeded {{ color: var(--success); }} .status-failed, .status-interrupted {{ color: var(--danger); }} .status-skipped, .status-completed_with_issues, .status-cancelled {{ color: var(--warning); }} .report-notice {{ display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; margin: 20px 0; padding: 12px 14px; border: 1px solid var(--border); border-radius: 8px; background: var(--raised); }} .report-notice strong {{ font-weight: 600; }} .report-notice span {{ color: var(--secondary); }} .report-notice .notice-detail {{ grid-column: 1 / -1; font: 12px/1.6 "SF Mono", Consolas, monospace; overflow-wrap: anywhere; }} .progress-cell {{ display: grid; gap: 4px; min-width: 150px; }} .progress-overall {{ display: grid; grid-template-columns: minmax(80px, 1fr) auto; align-items: center; gap: 8px; }} .progress-phase {{ overflow: hidden; color: var(--secondary); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }} progress {{ width: 100%; height: 6px; accent-color: var(--foreground); }} footer {{ margin-top: 16px; color: var(--secondary); font-size: 12px; }}
    @media (max-width: 640px) {{ main {{ width: min(100% - 24px, 1120px); padding-top: 20px; }} header {{ flex-direction: column; }} .overall {{ text-align: left; }} th:nth-child(4), td:nth-child(4) {{ display: none; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div><h1>知远评测报告</h1><p>{_escape(summary.get("campaign_id", ""))}</p></div>
      <div class="overall"><strong>{_escape(summary.get("status", "unknown"))}</strong><span>{_escape(summary.get("completed_at") or summary.get("created_at", ""))}</span></div>
    </header>
    <section class="candidates" aria-label="Candidates">{candidates}</section>
    {notice}
    <div class="table-wrap"><table><thead><tr><th>测试集</th><th>状态</th><th>进度</th><th>记录</th></tr></thead><tbody>{rows}</tbody></table></div>
    <footer>Generated {generated}</footer>
  </main>
</body>
</html>
"""


def write_campaign_report(campaign_dir: Path, summary: dict[str, Any]) -> None:
    report_dir = campaign_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    files = {
        report_dir / "report.html": render_campaign_report(summary),
        report_dir / "summary.json": json.dumps(
            summary, ensure_ascii=True, indent=2, sort_keys=True
        )
        + "\n",
    }
    for path, content in files.items():
        atomic_write_text(path, content)
