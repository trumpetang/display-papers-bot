#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
显示面板行业论文每日监控 + Server酱微信推送
数据源: OpenAlex API (https://api.openalex.org)
覆盖: SID 2025 全部参与机构(见 institutions.json)
排除: SID Digest / ICDT / IMID 会议论文
去重: seen.json 记录已推送论文 ID, 跨次执行增量推送
运行环境: Python 3.8+ 纯标准库, 无第三方依赖
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
INSTITUTIONS_FILE = os.path.join(BASE, "institutions.json")
SEEN_FILE = os.path.join(BASE, "seen.json")
REPORTS_DIR = os.path.join(BASE, "reports")
CONTACT_EMAIL = "papers-bot@example.com"  # OpenAlex polite pool 标识

# ---------------- 排除规则(会议) ----------------
EXCLUDED_VENUE_PATTERNS = [
    "SID Symposium Digest",
    "SID Symposium",
    "International Conference on Display Technology",  # ICDT
    "ICDT",
    "International Meeting on Information Display",   # IMID
    "IMID",
]

# ---------------- 期刊分级 ----------------
TOP_JOURNALS = [
    "Nature", "Science", "Nature Communications", "Nature Photonics",
    "Nature Materials", "Nature Electronics", "Science Advances",
    "Advanced Materials", "Advanced Optical Materials",
]
DISPLAY_CORE_JOURNALS = [
    "Journal of the SID", "JSID", "Journal of Information Display",
    "Displays", "Organic Electronics", "IEEE Transactions on Electron Devices",
    "IEEE Electron Device Letters", "IEEE Journal of the Electron Devices Society",
    "Optics Express", "Optics Letters", "Applied Physics Letters",
    "APL Photonics", "ACS Applied Electronic Materials", "ACS Photonics",
    "Scientific Reports", "液晶与显示", "发光学报", "光学学报", "光子学报",
]

# ---------------- 技术方向分类(12大类) ----------------
TECH_CATEGORIES = [
    ("OLED",       ["oled", "phosphorescent", "tadf", "thermally activated delayed fluorescence", "叠层", "stacked organic", "flexible oled", "foldable oled"]),
    ("Micro-LED",  ["micro-led", "micro led", "microled", "mass transfer", "巨量转移", "full-color micro"]),
    ("Mini-LED",   ["mini-led", "mini led", "miniled", "local dimming", "halo"]),
    ("LCD",        ["lcd", "liquid crystal display", "ltps", "low-temperature polycrystalline", "igzo backplane", "lcd backlight"]),
    ("QLED/QD",    ["qled", "quantum dot", "qd-oled", "qd-led", "perovskite quantum"]),
    ("PeLED",      ["perovskite led", "perovskite light-emitting", "peled", "perovskite emissive"]),
    ("氧化物TFT",  ["igzo", "oxide tft", "oxide semiconductor thin film", "a-igzo", "flexible tft"]),
    ("显示光学",   ["holographic", "light field", "waveguide", "augmented reality", "virtual reality", "ar display", "vr display", "near-eye"]),
    ("AI/检测",    ["defect detection", "demura", "mura", "image quality optimization", "deep learning display", "machine learning inspection"]),
    ("触控/交互",  ["touch sensor", "in-cell touch", "foldable touch"]),
    ("封装/模组",  ["thin film encapsulation", "tfe", "oca", "optical clear adhesive", "3d lamination", "lamination"]),
    ("材料/设备",  ["evaporation", "蒸镀", "photolithography", "cvd", "pvd", "sputtering", "annealing"]),
]


def http_get_json(url, max_retries=5):
    """GET 请求, 429/5xx 退避重试(5/10/20/40/60s), 坚持完成"""
    delays = [5, 10, 20, 40, 60]
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"display-papers-bot ({CONTACT_EMAIL})"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < max_retries - 1:
                wait = delays[min(attempt, len(delays) - 1)]
                print(f"  [warn] HTTP {e.code}, {wait}s 后重试 ({attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            print(f"  [error] HTTP {e.code}: {url[:120]}")
            return None
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(delays[min(attempt, len(delays) - 1)])
                continue
            print(f"  [error] {e}: {url[:120]}")
            return None
    return None


def http_post_form(url, params):
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def rebuild_abstract(inv):
    """从 OpenAlex abstract_inverted_index 重建摘要文本"""
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def query_batch(org_expr, from_date, page=1):
    """一批机构关键词(| 或查询) + 显示主题词双重过滤, 返回 results 列表"""
    topics = ('display|OLED|AMOLED|microLED|"micro-LED"|"Mini-LED"|LCD|"liquid crystal display"|'
              'TFT|"quantum dot"|QLED|electroluminescen|waveguide|"head-mounted"|holograph')
    flt = (f'raw_affiliation_strings.search:{org_expr},'
           f'title_and_abstract.search:{topics},'
           f'from_publication_date:{from_date}')
    url = ("https://api.openalex.org/works?filter=" + urllib.parse.quote(flt)
           + f"&sort=publication_date:desc&per-page=100&page={page}&mailto={CONTACT_EMAIL}")
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if api_key:
        url += f"&api_key={urllib.parse.quote(api_key)}"
    data = http_get_json(url)
    if not isinstance(data, dict):  # 容错: 异常响应(list/None)一律视为失败
        if data is not None:
            print(f"  [error] 非预期响应格式: {str(data)[:120]}")
        return None
    return data


def venue_of(work):
    src = (work.get("primary_location") or {}).get("source") or {}
    return (src.get("display_name") or "").strip()


def is_excluded(work):
    venue = venue_of(work)
    return any(p.lower() in venue.lower() for p in EXCLUDED_VENUE_PATTERNS)


def classify(title, abstract):
    text = (title + " " + abstract).lower()
    best, best_hits = "其他/跨领域", 0
    for cat, kws in TECH_CATEGORIES:
        hits = sum(1 for k in kws if k in text)
        if hits > best_hits:
            best, best_hits = cat, hits
    return best


def rate(work, cat, org_categories):
    venue = venue_of(work)
    if any(t.lower() in venue.lower() for t in TOP_JOURNALS):
        return "⭐顶刊"
    if any(t.lower() in venue.lower() for t in DISPLAY_CORE_JOURNALS):
        return "★显示核心"
    if org_categories & {"materials_components", "equipment", "driver_ic_electronics"}:
        return "📌行业关注"
    return "一般"


def main():
    now = datetime.now(timezone.utc)
    today_cn = (now + timedelta(hours=8)).date()
    from_date = (today_cn - timedelta(days=3)).isoformat()  # 近3天窗口, 配合 seen.json 去重防漏

    with open(INSTITUTIONS_FILE, encoding="utf-8") as f:
        inst = json.load(f)
    org_to_cat = {}
    for cat, kws in inst.items():
        for k in kws:
            org_to_cat[k] = cat

    seen = set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen = set(json.load(f).get("ids", []))
    print(f"已推送论文数(去重库): {len(seen)}, 检索窗口: {from_date} ~ {today_cn}")

    papers = {}       # wid -> paper dict
    org_cats = {}     # wid -> set(inst categories)
    failed_batches = 0

    def collect(results):
        for w in results:
            wid = w["id"].rsplit("/", 1)[-1]
            if wid in seen or is_excluded(w):
                continue
            if wid not in papers:
                title = (w.get("title") or w.get("display_name") or "(无标题)").strip()
                authors = [a["author"]["display_name"] for a in (w.get("authorships") or [])[:3] if a.get("author")]
                abstract = rebuild_abstract(w.get("abstract_inverted_index"))
                papers[wid] = {
                    "id": wid,
                    "title": title,
                    "authors": authors,
                    "journal": venue_of(w) or "(预印本/未知)",
                    "pubdate": w.get("publication_date") or "",
                    "doi": w.get("doi") or "",
                    "abstract": abstract[:300],
                }
                org_cats[wid] = set()
            # 按论文自身机构串归属类别
            affil_text = " ".join(a.get("raw_affiliation_string", "") for a in (w.get("authorships") or []))
            affil_text = affil_text.lower()
            for kw, cat in org_to_cat.items():
                if kw.lower() in affil_text:
                    org_cats[wid].add(cat)

    # 机构关键词打包成批量或查询, 控制单批 12 个以内, 避免 URL 过长
    all_kws = list(org_to_cat.keys())
    for i in range(0, len(all_kws), 12):
        batch = all_kws[i:i + 12]
        org_expr = "|".join(f'"{k}"' for k in batch)
        page, got = 1, 0
        while True:
            data = query_batch(org_expr, from_date, page)
            if data is None:
                failed_batches += 1
                break
            count = data.get("meta", {}).get("count", 0)
            results = data.get("results", [])
            collect(results)
            got += len(results)
            print(f"  批次 {i // 12 + 1}: 命中 {count} 篇 (取回 {got})", flush=True)
            if got >= count or page >= 3 or not results:
                break
            page += 1
            time.sleep(1.0)
        time.sleep(1.2)  # 温和限速

    for wid, p in papers.items():
        p["category"] = classify(p["title"], p["abstract"])
        p["rating"] = rate(p, p["category"], org_cats[wid] or {"未匹配"})
        p["orgs"] = sorted(org_cats[wid])

    order = {"⭐顶刊": 0, "★显示核心": 1, "📌行业关注": 2, "一般": 3}
    lst = sorted(papers.values(), key=lambda p: (order[p["rating"]], p.get("pubdate", ""), p["title"]))

    # ---------- 生成本地完整日报 ----------
    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_path = os.path.join(REPORTS_DIR, f"display_papers_daily_{today_cn.isoformat()}.md")
    lines = [f"# 显示面板论文日报 {today_cn.isoformat()}", "",
             f"- 检索机构关键词: {len(org_to_cat)} 个 | 时间窗口: {from_date} ~ {today_cn} | 新增论文: {len(lst)} 篇"]
    if failed_batches:
        lines.append(f"- ⚠️ 检索失败批次 {failed_batches} 个(限流), 建议检查执行日志")
    lines.append("")
    if not lst:
        lines.append("**今日无新增论文** ✅")
    for p in lst:
        title_line = f"## {p['rating']} [{p['title']}]({p['doi']})" if p["doi"] else f"## {p['rating']} {p['title']}"
        lines += [title_line,
                  f"- 作者: {', '.join(p['authors']) or '-'}",
                  f"- 期刊: {p['journal']} | 发表: {p['pubdate']}",
                  f"- 技术分类: {p['category']} | 机构类别: {', '.join(p['orgs'])}"]
        if p["abstract"]:
            lines.append(f"- 摘要: {p['abstract']}…")
        lines.append("")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"日报已保存: {report_path}")

    # ---------- 组装微信推送内容(Server酱 Markdown) ----------
    counts = {}
    for p in lst:
        counts[p["rating"]] = counts.get(p["rating"], 0) + 1
    if lst:
        title = f"显示论文日报 {today_cn.month}/{today_cn.day}: 新增{len(lst)}篇"
        parts = [f"**{today_cn.isoformat()} 显示面板论文日报**", "", "### 分级统计", "", "| 评级 | 数量 |", "|:--|:--|"]
        for r in ["⭐顶刊", "★显示核心", "📌行业关注", "一般"]:
            if r in counts:
                parts.append(f"| {r} | {counts[r]} |")
        parts += ["", "### 重点论文(标题可点击跳转)"]
        for p in [x for x in lst if x["rating"] in ("⭐顶刊", "★显示核心", "📌行业关注")][:15]:
            title_text = p["title"][:80]
            if p["doi"]:
                parts.append(f"- {p['rating']} [{title_text}]({p['doi']})")
            else:
                parts.append(f"- {p['rating']} **{title_text}**")
            parts.append(f"  {p['journal']} · {p['pubdate']} · {p['category']}")
        parts += ["", f"共 {len(lst)} 篇, 完整日报见仓库 reports/ 目录."]
    else:
        title = f"显示论文日报 {today_cn.month}/{today_cn.day}: 无新增"
        parts = [f"**{today_cn.isoformat()}** 无新增论文 ✅", f"时间窗口 {from_date} ~ {today_cn}, 覆盖 {len(org_to_cat)} 个机构关键词."]
    desp = "\n".join(parts)

    # ---------- Server酱推送 ----------
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if not sendkey:  # 本地测试兜底: 读本地 key 文件
        for kp in [os.path.expanduser("~/.workbuddy/.serverchan_key"), os.path.join(BASE, ".sendkey")]:
            if os.path.exists(kp):
                with open(kp, encoding="utf-8") as f:
                    sendkey = f.read().strip()
                break
    push_status = "未推送(无SendKey)"
    if sendkey:
        try:
            resp = http_post_form(f"https://sctapi.ftqq.com/{sendkey}.send",
                                  {"title": title[:32], "desp": desp})
            push_status = "推送成功" if resp.get("code") == 0 else f"推送失败: {resp}"
        except Exception as e:
            push_status = f"推送异常: {e}"
    print(f"微信推送: {push_status}")

    # ---------- 回写去重库(仅推送成功或无SendKey本地模式时记为已处理) ----------
    if push_status in ("推送成功", "未推送(无SendKey)"):
        seen.update(papers.keys())
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"ids": sorted(seen), "updated": now.isoformat()}, f, ensure_ascii=False, indent=1)
        print(f"去重库已更新: {len(seen)} 条")

    print(f"执行摘要: 检索 {len(all_kws)} 个关键词(分 {len(all_kws) // 12 + 1} 批) | 新增 {len(lst)} 篇 | 失败批次 {failed_batches} 个 | {push_status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
