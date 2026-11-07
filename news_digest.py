#!/usr/bin/env python3
"""Merge the existing papers.py report with a Chinese news brief.

Flow:
1. Back up seen.json.
2. Run the existing papers.py with SERVERCHAN_SENDKEY removed, so it only
   generates reports/ and updates seen.json (no WeChat push).
3. Read today's paper report, fetch display-industry news, summarize with
   DeepSeek, and send one combined email + Feishu notification.
4. If the combined delivery fails, restore seen.json and keep news pending
   so tomorrow's run retries.

This wrapper deliberately leaves papers.py unchanged.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import markdown

import brief


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "news_config.yaml"
PAPERS_SCRIPT = ROOT / "papers.py"
PAPERS_SEEN = ROOT / "seen.json"
REPORTS_DIR = ROOT / "reports"
PAPERS_TIMEOUT_SECONDS = 1200


def log(message: str) -> None:
    print(message, flush=True)


def run_papers() -> tuple[bool, str]:
    if not PAPERS_SCRIPT.exists():
        return False, f"找不到 {PAPERS_SCRIPT.name}"

    env = os.environ.copy()
    # papers.py uses the presence of this key to decide whether to push.
    # Removing it makes the script generate reports and update seen.json
    # without sending a separate WeChat message.
    env.pop("SERVERCHAN_SENDKEY", None)

    try:
        proc = subprocess.run(
            [sys.executable, "-u", str(PAPERS_SCRIPT)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=PAPERS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"papers.py 超时（>{PAPERS_TIMEOUT_SECONDS}s）"
    except Exception as exc:  # noqa: BLE001
        return False, f"papers.py 启动失败：{brief.sanitize_error(exc)}"

    if proc.stdout:
        print(proc.stdout, flush=True)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, flush=True)
    if proc.returncode != 0:
        return False, f"papers.py 退出码 {proc.returncode}"
    return True, ""


def read_paper_report(date_str: str) -> tuple[str, int, str]:
    path = REPORTS_DIR / f"display_papers_daily_{date_str}.md"
    if not path.exists():
        return "", 0, f"papers.py 没有生成 {path.name}"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return "", 0, f"读取论文报告失败：{brief.sanitize_error(exc)}"

    match = re.search(r"新增论文[:：]\s*(\d+)", text)
    count = int(match.group(1)) if match else 0
    return text, count, ""


def restore_seen(backup: bytes | None) -> None:
    try:
        if backup is None:
            if PAPERS_SEEN.exists():
                PAPERS_SEEN.unlink()
        else:
            PAPERS_SEEN.write_bytes(backup)
        log("[papers] seen.json restored for retry")
    except OSError as exc:
        log(f"[warn] restore seen.json failed: {brief.sanitize_error(exc)}")


def paper_section_html(report_md: str, report_error: str) -> str:
    if report_error:
        body = (
            '<div class="warn">论文部分本次采集失败：'
            f"{html.escape(report_error)}</div>"
        )
    elif report_md:
        # Drop the report's own H1 so it does not duplicate the email hero.
        body_md = re.sub(r"^\s*#\s+.*\n", "", report_md, count=1)
        body = markdown.markdown(body_md, extensions=["extra", "sane_lists"])
    else:
        body = '<div class="warn">今日没有论文报告。</div>'

    return (
        "<style>"
        ".paper-report h1{font-size:19px;line-height:1.4;margin:14px 0 8px;color:#0f172a;}"
        ".paper-report h2{font-size:16px;line-height:1.45;margin:16px 0 6px;color:#111827;}"
        ".paper-report h3{font-size:15px;margin:14px 0 6px;color:#111827;}"
        ".paper-report p,.paper-report li{font-size:13px;line-height:1.7;color:#374151;}"
        ".paper-report ul{padding-left:20px;margin:6px 0;}"
        ".paper-report a{color:#1d4ed8;text-decoration:none;}"
        ".paper-report hr{border:none;border-top:1px solid #e5e7eb;margin:14px 0;}"
        "</style>"
        '<div class="card paper-report">'
        '<div class="section" style="margin-top:0;">论文日报</div>'
        f"{body}</div>"
    )


def inject_paper_section(news_html: str, section: str) -> str:
    # Put the paper report after the overview and before the news cards.
    news_marker = '<div class="section">行业新闻</div>'
    if news_marker in news_html:
        return news_html.replace(news_marker, section + news_marker, 1)

    # No news today: put it before the footer.
    footer_marker = '<div class="footer">'
    if footer_marker in news_html:
        return news_html.replace(footer_marker, section + footer_marker, 1)

    return section + news_html


def main() -> int:
    cfg = brief.load_config(CONFIG_PATH)
    brief.USER_AGENT = str(cfg.get("user_agent", brief.USER_AGENT))

    tz_name = str(cfg.get("timezone", "Asia/Shanghai"))
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    date_str = now.strftime("%Y-%m-%d")
    since = now.astimezone(timezone.utc) - timedelta(
        hours=int(cfg.get("lookback_hours", 36))
    )

    seen_backup = PAPERS_SEEN.read_bytes() if PAPERS_SEEN.exists() else None

    log(f"[papers] running {PAPERS_SCRIPT.name}")
    paper_ok, paper_error = run_papers()
    report_md = ""
    paper_count = 0

    if paper_ok:
        report_md, paper_count, report_error = read_paper_report(date_str)
        if report_error:
            paper_ok = False
            paper_error = report_error
        else:
            log(f"[papers] report ok: {paper_count} new paper(s)")

    if not paper_ok:
        restore_seen(seen_backup)

    errors: list[str] = []
    if paper_error:
        errors.append(f"papers.py: {paper_error}")

    log("[news] collecting")
    fetched = brief.collect(cfg, since, errors)
    log(f"[news] {len(fetched)} raw item(s)")

    state_path = brief.resolve_path(str(cfg.get("state_path", "state/news_seen.json")))
    pending_path = brief.pending_path_for(state_path)
    state = brief.load_state(state_path)
    pending = brief.load_pending(pending_path)
    if pending:
        log(f"[news] retrying {len(pending)} pending item(s)")

    fresh = brief.dedupe(
        [
            item
            for item in (pending + fetched)
            if item.id not in state.get("seen", {})
        ]
    )
    selected = brief.select_items(fresh, cfg)
    log(f"[news] fresh={len(fresh)} selected={len(selected)}")

    raw_digest = None
    if selected:
        llm_cfg = cfg.get("llm", {})
        if not isinstance(llm_cfg, dict):
            llm_cfg = {}
        raw_digest = brief.call_llm(llm_cfg, selected, errors)

    if raw_digest is not None:
        digest = brief.normalize_digest(raw_digest, selected)
    elif selected:
        digest = brief.fallback_digest(selected)
    else:
        digest = brief.build_empty_digest(errors)

    render_errors = [
        error for error in errors if not error.startswith("papers.py:")
    ]
    news_html = brief.render_html(
        date_str, digest, selected, render_errors, paper_count=paper_count
    )
    news_plain = brief.render_plain(
        date_str, digest, selected, paper_count=paper_count
    )
    news_markdown = brief.render_markdown(
        date_str, digest, selected, render_errors, paper_count=paper_count
    )

    paper_html = paper_section_html(report_md, paper_error)
    combined_html = inject_paper_section(news_html, paper_html)

    combined_parts = [
        f"显示行业日报 | {date_str}",
        f"今日 {paper_count} 篇论文 · {len(digest.get('news', []))} 条新闻",
        "",
    ]
    if report_md:
        combined_parts.extend(["【论文日报】", "", report_md.strip(), ""])
    elif paper_error:
        combined_parts.extend(["【论文日报】", f"采集失败：{paper_error}", ""])
    combined_parts.extend(["【行业新闻】", "", news_plain.strip(), ""])
    combined_plain = "\n".join(combined_parts)

    combined_markdown = "\n\n".join(
        part
        for part in [
            f"# 显示行业日报 | {date_str}",
            f"> 今日 {paper_count} 篇论文 · {len(digest.get('news', []))} 条新闻",
            report_md.strip() if report_md else "",
            news_markdown.strip(),
        ]
        if part
    )

    archive_cfg = dict(cfg)
    archive_cfg["archive_dir"] = str(cfg.get("archive_dir", "news"))
    brief.write_archive(
        archive_cfg,
        date_str,
        digest,
        selected,
        combined_markdown,
        combined_html,
        combined_plain,
        errors,
    )

    subject = (
        f"显示行业日报 | {date_str} | {paper_count} 篇论文 · "
        f"{len(digest.get('news', []))} 条新闻"
    )

    email_cfg = cfg.get("email", {})
    email_enabled = isinstance(email_cfg, dict) and email_cfg.get("enabled", True)
    email_ok = False
    if email_enabled:
        email_ok = brief.send_email(
            email_cfg, subject, combined_plain, combined_html
        )

    push_cfg = cfg.get("push", {})
    feishu_ok = False
    if isinstance(push_cfg, dict):
        webhook_env = str(push_cfg.get("feishu_webhook_env", "")).strip()
        if webhook_env and os.environ.get(webhook_env):
            feishu_ok = brief.send_feishu(
                push_cfg,
                date_str,
                digest,
                selected,
                paper_count=paper_count,
            )

    delivered = email_ok if email_enabled else feishu_ok
    log(f"[deliver] email={email_ok} feishu={feishu_ok} delivered={delivered}")

    if delivered:
        for item in selected:
            state.setdefault("seen", {})[item.id] = date_str
        state["last_run"] = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        state["last_error"] = None
        brief.save_state(state_path, state)
        brief.clear_pending(pending_path)
        log("[done] combined brief delivered")
        if not paper_ok:
            log("[warn] paper section failed; seen.json was restored for retry")
        return 0

    # Delivery failed: restore paper seen so the paper section is retried,
    # and keep news items pending.
    if paper_ok:
        restore_seen(seen_backup)
    retry_items = brief.dedupe(pending + fresh)
    if retry_items:
        brief.save_pending(pending_path, retry_items)
        log(f"[news] saved {min(len(retry_items), 50)} item(s) for retry")
    log("[error] combined brief delivery failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
