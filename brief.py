#!/usr/bin/env python3
"""Display Daily Brief.

Collect display-industry papers and news, summarize them in Chinese with
DeepSeek, then deliver an HTML email and a short Feishu notification.

The script is intentionally dependency-light so it can run on a fresh
GitHub Actions Ubuntu runner.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.yaml"
USER_AGENT = "DisplayDailyBrief/1.0"
HTTP_TIMEOUT = 25
MAX_ABSTRACT_CHARS = 900


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Item:
    id: str
    kind: str  # "paper" | "news"
    title: str
    abstract: str
    url: str
    source: str
    published: str  # ISO 8601 UTC, e.g. 2026-11-03T08:00:00Z
    authors: list[str] = field(default_factory=list)
    score: int = 0


# ---------------------------------------------------------------------------
# Config and small helpers
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    print(message, flush=True)


def load_config(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")

    def expand(value: Any) -> Any:
        if isinstance(value, str):
            return os.path.expandvars(value)
        if isinstance(value, list):
            return [expand(v) for v in value]
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        return value

    return expand(raw)


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def env_value(name: str, required: bool = False) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise RuntimeError(f"missing environment variable: {name}")
    return value


SECRET_QUERY_RE = re.compile(
    r"(?i)\b(api_key|apikey|access_token|token|key)=([^&\s]+)"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+")
FEISHU_WEBHOOK_RE = re.compile(
    r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9_\-]+"
)


def sanitize_error(value: Any) -> str:
    text = str(value)
    text = SECRET_QUERY_RE.sub(r"\1=***", text)
    text = BEARER_RE.sub("Bearer ***", text)
    text = FEISHU_WEBHOOK_RE.sub("https://open.feishu.cn/.../***", text)
    return text


def record_error(source: str, exc: Exception, errors: list[str]) -> None:
    message = sanitize_error(exc)
    errors.append(f"{source}: {message}")
    log(f"[warn] {source}: {message}")


def http_get(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = HTTP_TIMEOUT,
    retries: int = 2,
) -> requests.Response:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "application/atom+xml, application/rss+xml, application/xml, "
            "application/json, text/xml, */*"
        ),
    }
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(
                url, params=params, headers=headers, timeout=timeout
            )
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001 - we re-raise below
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (2**attempt))
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"request failed: {url}")


TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\u00a0", " ")
    return WHITESPACE_RE.sub(" ", text).strip()


def struct_to_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime(*value[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def date_parts_to_dt(value: Any) -> datetime | None:
    if not isinstance(value, list) or not value:
        return None
    parts = value[0]
    if not isinstance(parts, list) or not parts:
        return None
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return datetime(year, month, day, tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def to_iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def normalize_url(value: str) -> str:
    if not value:
        return ""
    parts = urlsplit(value.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k.lower() not in {"gclid", "fbclid", "ref", "source"}
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), "")
    )


def item_id(
    *, doi: str = "", arxiv_id: str = "", url: str = "", title: str = ""
) -> str:
    if doi:
        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip(), flags=re.I)
        return "doi:" + doi.lower()
    if arxiv_id:
        return "arxiv:" + re.sub(r"v\d+$", "", arxiv_id)
    if url:
        return "url:" + hashlib.sha1(
            normalize_url(url).encode("utf-8")
        ).hexdigest()[:16]
    return "title:" + hashlib.sha1(title.casefold().encode("utf-8")).hexdigest()[:16]


def relevance_score(title: str, abstract: str, keywords: Iterable[str]) -> int:
    title_l = title.casefold()
    text_l = f"{title} {abstract}".casefold()
    score = 0
    for keyword in keywords:
        needle = str(keyword).casefold().strip()
        if not needle:
            continue
        if needle in title_l:
            score += 3
        elif needle in text_l:
            score += 1
    return score


def is_excluded(title: str, abstract: str, exclude: Iterable[str]) -> bool:
    text = f"{title} {abstract}".casefold()
    return any(str(word).casefold() in text for word in exclude if str(word).strip())


def reconstruct_abstract(index: Any) -> str:
    if not isinstance(index, dict) or not index:
        return ""
    pairs: list[tuple[int, str]] = []
    for word, positions in index.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int):
                pairs.append((position, str(word)))
    pairs.sort(key=lambda pair: pair[0])
    return " ".join(word for _, word in pairs)


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def arxiv_query_term(term: str) -> str:
    cleaned = term.strip()
    if not cleaned:
        return ""
    if re.search(r"[\s\-]", cleaned):
        return f'abs:"{cleaned}"'
    return f"abs:{cleaned}"


def fetch_arxiv(
    cfg: dict[str, Any],
    since: datetime,
    keywords: list[str],
    min_score: int,
    exclude: list[str],
) -> list[Item]:
    categories = [str(c) for c in cfg.get("categories", []) if c]
    terms = [str(k) for k in cfg.get("keywords", keywords) if k]

    query_parts = []
    if categories:
        query_parts.append("(" + " OR ".join(f"cat:{c}" for c in categories) + ")")
    term_query = " OR ".join(
        value for value in (arxiv_query_term(term) for term in terms) if value
    )
    if term_query:
        query_parts.append(f"({term_query})")
    if not query_parts:
        return []
    query = " AND ".join(query_parts)

    response = http_get(
        "https://export.arxiv.org/api/query",
        params={
            "search_query": query,
            "start": 0,
            "max_results": int(cfg.get("max_results", 80)),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        },
        timeout=40,
    )
    parsed = feedparser.parse(response.content)
    items: list[Item] = []

    for entry in parsed.entries:
        published = struct_to_dt(
            entry.get("published_parsed") or entry.get("updated_parsed")
        )
        if not published or published < since:
            continue

        title = clean_text(entry.get("title"))
        abstract = clean_text(entry.get("summary"))
        if not title or is_excluded(title, abstract, exclude):
            continue

        score = relevance_score(title, abstract, keywords)
        if score < min_score:
            continue

        raw_id = str(entry.get("id", ""))
        arxiv_id = raw_id.rsplit("/", 1)[-1] if raw_id else ""
        arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
        url = str(entry.get("link") or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""))
        if not url:
            continue

        raw_authors = entry.get("authors")
        if not isinstance(raw_authors, list):
            raw_authors = []
        authors = [
            clean_text(author.get("name"))
            for author in raw_authors
            if isinstance(author, dict) and clean_text(author.get("name"))
        ]
        items.append(
            Item(
                id=item_id(arxiv_id=arxiv_id, url=url, title=title),
                kind="paper",
                title=title,
                abstract=abstract,
                url=url,
                source="arXiv",
                published=to_iso(published),
                authors=authors[:8],
                score=score,
            )
        )

    return items


def fetch_openalex(
    cfg: dict[str, Any],
    since: datetime,
    keywords: list[str],
    min_score: int,
    exclude: list[str],
    mailto: str,
) -> list[Item]:
    params: dict[str, Any] = {
        "search": cfg.get(
            "search", "OLED display microLED liquid crystal quantum dot"
        ),
        "filter": f"from_publication_date:{since.date().isoformat()}",
        "sort": "publication_date:desc",
        "per-page": int(cfg.get("per_page", 50)),
    }
    if mailto:
        params["mailto"] = mailto

    api_key_name = str(cfg.get("api_key_env", "")).strip()
    if api_key_name:
        api_key = env_value(api_key_name)
        if api_key:
            params["api_key"] = api_key

    response = http_get("https://api.openalex.org/works", params=params, timeout=40)
    payload = response.json()
    results = payload.get("results", []) if isinstance(payload, dict) else []
    items: list[Item] = []

    for row in results:
        if not isinstance(row, dict):
            continue
        published_date = str(row.get("publication_date") or "")
        published = parse_iso(published_date + "T00:00:00Z") if published_date else None
        if not published or published.date() < since.date():
            continue

        title = clean_text(row.get("title") or row.get("display_name"))
        abstract = reconstruct_abstract(row.get("abstract_inverted_index"))
        if not title or is_excluded(title, abstract, exclude):
            continue

        score = relevance_score(title, abstract, keywords)
        if score < min_score:
            continue

        doi = str(row.get("doi") or "")
        url = doi or str(row.get("id") or "")
        if not url:
            continue

        primary = row.get("primary_location")
        source_obj = primary.get("source") if isinstance(primary, dict) else {}
        if not isinstance(source_obj, dict):
            source_obj = {}
        source = clean_text(source_obj.get("display_name")) or "OpenAlex"

        authors = []
        authorships = row.get("authorships")
        if not isinstance(authorships, list):
            authorships = []
        for authorship in authorships[:8]:
            if not isinstance(authorship, dict):
                continue
            author = authorship.get("author") or {}
            if not isinstance(author, dict):
                continue
            name = clean_text(author.get("display_name"))
            if name:
                authors.append(name)

        items.append(
            Item(
                id=item_id(doi=doi, url=url, title=title),
                kind="paper",
                title=title,
                abstract=abstract,
                url=url,
                source=source,
                published=to_iso(published),
                authors=authors,
                score=score,
            )
        )

    return items


def fetch_crossref(
    cfg: dict[str, Any],
    since: datetime,
    keywords: list[str],
    min_score: int,
    exclude: list[str],
    mailto: str,
) -> list[Item]:
    items: list[Item] = []
    rows = int(cfg.get("rows_per_journal", 12))
    journals = cfg.get("journals", [])
    if not isinstance(journals, list):
        return items

    for journal in journals:
        if not isinstance(journal, dict):
            continue
        issn = str(journal.get("issn", "")).strip()
        name = clean_text(journal.get("name"))
        if not issn:
            continue

        params = {
            "filter": f"from-created-date:{since.date().isoformat()}",
            "rows": rows,
            "sort": "created",
            "order": "desc",
        }
        if mailto:
            params["mailto"] = mailto

        try:
            response = http_get(
                f"https://api.crossref.org/journals/{quote(issn)}/works",
                params=params,
                timeout=40,
            )
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - one bad journal should not stop all
            log(f"[warn] Crossref {name or issn}: {sanitize_error(exc)}")
            continue

        message = payload.get("message", {}) if isinstance(payload, dict) else {}
        records = message.get("items", []) if isinstance(message, dict) else []

        for record in records:
            if not isinstance(record, dict):
                continue

            created = record.get("created", {}) if isinstance(record.get("created"), dict) else {}
            published = parse_iso(created.get("date-time")) or date_parts_to_dt(
                created.get("date-parts")
            )
            if not published or published < since:
                continue

            title_list = record.get("title") or []
            title = clean_text(title_list[0]) if isinstance(title_list, list) and title_list else ""
            abstract = clean_text(record.get("abstract"))
            if not title or is_excluded(title, abstract, exclude):
                continue

            score = relevance_score(title, abstract, keywords)
            if score < min_score:
                continue

            doi = str(record.get("DOI") or "")
            url = str(record.get("URL") or (f"https://doi.org/{doi}" if doi else ""))
            if not url:
                continue

            container = record.get("container-title") or []
            source = (
                clean_text(container[0])
                if isinstance(container, list) and container
                else ""
            )
            source = source or name or "Crossref"

            authors = []
            raw_authors = record.get("author")
            if not isinstance(raw_authors, list):
                raw_authors = []
            for author in raw_authors[:8]:
                if not isinstance(author, dict):
                    continue
                given = clean_text(author.get("given"))
                family = clean_text(author.get("family"))
                full_name = " ".join(part for part in (given, family) if part)
                if full_name:
                    authors.append(full_name)

            items.append(
                Item(
                    id=item_id(doi=doi, url=url, title=title),
                    kind="paper",
                    title=title,
                    abstract=abstract,
                    url=url,
                    source=source,
                    published=to_iso(published),
                    authors=authors,
                    score=score,
                )
            )

        time.sleep(0.25)

    return items


def fetch_rss_feed(
    feed_cfg: dict[str, Any],
    since: datetime,
    keywords: list[str],
    min_score: int,
    exclude: list[str],
) -> list[Item]:
    name = clean_text(feed_cfg.get("name")) or "RSS"
    url = str(feed_cfg.get("url", "")).strip()
    if not url:
        return []

    response = http_get(url, timeout=30)
    parsed = feedparser.parse(response.content)
    keep_all = bool(feed_cfg.get("keep_all", False))
    items: list[Item] = []

    for entry in parsed.entries:
        published = struct_to_dt(
            entry.get("published_parsed") or entry.get("updated_parsed")
        )
        if not published or published < since:
            continue

        title = clean_text(entry.get("title"))
        abstract = clean_text(
            entry.get("summary") or entry.get("description") or ""
        )[:1500]
        link = urljoin(url, str(entry.get("link") or "").strip())
        if not title or not link or is_excluded(title, abstract, exclude):
            continue

        score = relevance_score(title, abstract, keywords)
        if not keep_all and score < min_score:
            continue
        if keep_all:
            # Vertical sources are relevant by definition; give them a small
            # base score so they are not crowded out by broad feeds.
            score = max(score, min_score)

        items.append(
            Item(
                id=item_id(url=link, title=title),
                kind=str(feed_cfg.get("kind", "news")),
                title=title,
                abstract=abstract,
                url=link,
                source=name,
                published=to_iso(published),
                score=score,
            )
        )

    return items


def fetch_google_news(
    query: str,
    since: datetime,
    keywords: list[str],
    min_score: int,
    exclude: list[str],
    hl: str,
    gl: str,
    ceid: str,
) -> list[Item]:
    response = http_get(
        "https://news.google.com/rss/search",
        params={"q": query, "hl": hl, "gl": gl, "ceid": ceid},
        timeout=30,
    )
    parsed = feedparser.parse(response.content)
    items: list[Item] = []

    for entry in parsed.entries:
        published = struct_to_dt(
            entry.get("published_parsed") or entry.get("updated_parsed")
        )
        if not published or published < since:
            continue

        title = clean_text(entry.get("title"))
        source_obj = entry.get("source")
        source = ""
        if hasattr(source_obj, "get"):
            source = clean_text(source_obj.get("title"))
        if source:
            title = re.sub(
                rf"\s+-\s+{re.escape(source)}\s*$", "", title
            ).strip()

        abstract = clean_text(entry.get("summary") or "")
        link = urljoin(
            "https://news.google.com", str(entry.get("link") or "").strip()
        )
        if not title or not link or is_excluded(title, abstract, exclude):
            continue

        score = relevance_score(title, abstract, keywords)
        if score < min_score:
            continue

        items.append(
            Item(
                id=item_id(url=link, title=title),
                kind="news",
                title=title,
                abstract=abstract,
                url=link,
                source=source or "Google News",
                published=to_iso(published),
                score=score,
            )
        )

    return items


def collect(cfg: dict[str, Any], since: datetime, errors: list[str]) -> list[Item]:
    keyword_cfg = cfg.get("keywords", {})
    if not isinstance(keyword_cfg, dict):
        keyword_cfg = {}
    paper_keywords = [
        str(k) for k in keyword_cfg.get("papers", []) if str(k).strip()
    ]
    news_keywords = [
        str(k) for k in keyword_cfg.get("news", []) if str(k).strip()
    ]
    exclude = [str(k) for k in keyword_cfg.get("exclude", []) if str(k).strip()]
    min_paper_score = int(cfg.get("min_paper_score", 3))
    min_news_score = int(cfg.get("min_news_score", 2))
    mailto = str(cfg.get("mailto", "")).strip()

    items: list[Item] = []

    arxiv_cfg = cfg.get("arxiv", {})
    if isinstance(arxiv_cfg, dict) and arxiv_cfg.get("enabled", True):
        try:
            found = fetch_arxiv(
                arxiv_cfg, since, paper_keywords, min_paper_score, exclude
            )
            items.extend(found)
            log(f"[ok] arXiv: {len(found)}")
        except Exception as exc:  # noqa: BLE001
            record_error("arXiv", exc, errors)

    openalex_cfg = cfg.get("openalex", {})
    if isinstance(openalex_cfg, dict) and openalex_cfg.get("enabled", True):
        try:
            found = fetch_openalex(
                openalex_cfg,
                since,
                paper_keywords,
                min_paper_score,
                exclude,
                mailto,
            )
            items.extend(found)
            log(f"[ok] OpenAlex: {len(found)}")
        except Exception as exc:  # noqa: BLE001
            record_error("OpenAlex", exc, errors)

    crossref_cfg = cfg.get("crossref", {})
    if isinstance(crossref_cfg, dict) and crossref_cfg.get("enabled", True):
        try:
            found = fetch_crossref(
                crossref_cfg,
                since,
                paper_keywords,
                min_paper_score,
                exclude,
                mailto,
            )
            items.extend(found)
            log(f"[ok] Crossref: {len(found)}")
        except Exception as exc:  # noqa: BLE001
            record_error("Crossref", exc, errors)

    rss_cfg = cfg.get("rss", [])
    if isinstance(rss_cfg, list):
        for feed_cfg in rss_cfg:
            if not isinstance(feed_cfg, dict) or not feed_cfg.get("enabled", True):
                continue
            try:
                found = fetch_rss_feed(
                    feed_cfg, since, news_keywords, min_news_score, exclude
                )
                items.extend(found)
                log(f"[ok] {feed_cfg.get('name', 'RSS')}: {len(found)}")
            except Exception as exc:  # noqa: BLE001
                record_error(f"RSS {feed_cfg.get('name', '?')}", exc, errors)

    google_cfg = cfg.get("google_news", {})
    if isinstance(google_cfg, dict) and google_cfg.get("enabled", False):
        hl = str(google_cfg.get("hl", "en-US"))
        gl = str(google_cfg.get("gl", "US"))
        ceid = str(google_cfg.get("ceid", "US:en"))
        queries = google_cfg.get("queries", [])
        if not isinstance(queries, list):
            queries = []
        for query in queries:
            if not str(query).strip():
                continue
            try:
                found = fetch_google_news(
                    str(query),
                    since,
                    news_keywords,
                    min_news_score,
                    exclude,
                    hl,
                    gl,
                    ceid,
                )
                items.extend(found)
                log(f"[ok] Google News ({query}): {len(found)}")
            except Exception as exc:  # noqa: BLE001
                record_error(f"Google News ({query})", exc, errors)

    return items


# ---------------------------------------------------------------------------
# Ranking, dedupe, selection
# ---------------------------------------------------------------------------


def dedupe(items: list[Item]) -> list[Item]:
    ordered = sorted(items, key=lambda item: (item.score, item.published), reverse=True)
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    result: list[Item] = []

    for item in ordered:
        if item.id in seen_ids:
            continue
        fingerprint = re.sub(
            r"[^0-9a-z\u4e00-\u9fff\uac00-\ud7af]+", "", item.title.casefold()
        )
        if len(fingerprint) >= 30 and fingerprint in seen_titles:
            continue
        seen_ids.add(item.id)
        if len(fingerprint) >= 30:
            seen_titles.add(fingerprint)
        result.append(item)

    return result


def pick(
    items: list[Item], limit: int, max_per_source: int
) -> list[Item]:
    if limit <= 0:
        return []
    ordered = sorted(items, key=lambda item: (item.score, item.published), reverse=True)
    picked: list[Item] = []
    counts: dict[str, int] = {}

    for item in ordered:
        if counts.get(item.source, 0) >= max_per_source:
            continue
        picked.append(item)
        counts[item.source] = counts.get(item.source, 0) + 1
        if len(picked) >= limit:
            return picked

    if len(picked) < limit:
        picked_ids = {item.id for item in picked}
        for item in ordered:
            if item.id in picked_ids:
                continue
            picked.append(item)
            if len(picked) >= limit:
                break

    return picked


def select_items(items: list[Item], cfg: dict[str, Any]) -> list[Item]:
    max_papers = int(cfg.get("max_papers", 7))
    max_news = int(cfg.get("max_news", 10))
    fallback_cap = int(cfg.get("max_per_source", 2))
    paper_cap = int(cfg.get("max_per_source_papers", fallback_cap))
    news_cap = int(cfg.get("max_per_source_news", fallback_cap))
    papers = [item for item in items if item.kind == "paper"]
    news = [item for item in items if item.kind == "news"]
    return pick(papers, max_papers, paper_cap) + pick(news, max_news, news_cap)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "seen": {}, "last_run": None, "last_error": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "seen": {}, "last_run": None, "last_error": None}
    if not isinstance(data, dict):
        return {"version": 1, "seen": {}, "last_run": None, "last_error": None}
    data.setdefault("version", 1)
    if not isinstance(data.get("seen"), dict):
        data["seen"] = {}
    return data


def save_state(path: Path, state: dict[str, Any], retention_days: int = 120) -> None:
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=retention_days)).isoformat()
    seen = {
        str(key): str(value)
        for key, value in state.get("seen", {}).items()
        if str(value) >= cutoff
    }
    state["seen"] = seen
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def pending_path_for(state_path: Path) -> Path:
    return state_path.with_name("pending.json")


def load_pending(path: Path) -> list[Item]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    items: list[Item] = []
    for row in rows[:50]:
        if not isinstance(row, dict):
            continue
        try:
            raw_authors = row.get("authors") or []
            authors = (
                [str(a) for a in raw_authors]
                if isinstance(raw_authors, list)
                else []
            )
            items.append(
                Item(
                    id=str(row["id"]),
                    kind=str(row["kind"]),
                    title=str(row["title"]),
                    abstract=str(row.get("abstract", "")),
                    url=str(row["url"]),
                    source=str(row.get("source", "")),
                    published=str(row["published"]),
                    authors=authors,
                    score=int(row.get("score") or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return items


def save_pending(path: Path, items: list[Item]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "items": [asdict(item) for item in items[:50]],
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def clear_pending(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"[warn] could not remove pending state: {sanitize_error(exc)}")


# ---------------------------------------------------------------------------
# LLM summary
# ---------------------------------------------------------------------------


def build_prompt(items: list[Item]) -> str:
    payload = [
        {
            "id": item.id,
            "kind": item.kind,
            "title": item.title[:400],
            "abstract": item.abstract[:MAX_ABSTRACT_CHARS],
            "source": item.source,
            "published": item.published[:10],
        }
        for item in items
    ]
    return f"""你是显示行业研究助理。请把下面提供的论文/新闻元数据整理成一份中文日报。

硬性要求：
1. 只能使用我提供的信息，不得编造事实、数字、机构、结论或引用。
2. 标题翻译要准确；OLED、MicroLED、TFT、LTPO、TADF、QD-OLED 等缩写保留英文。
3. 论文条目输出：
   - id：原样返回
   - title_zh：中文标题
   - summary_zh：2-3 句，说明“做了什么、关键结果、用了什么方法”
   - why：1 句，说明对显示行业的意义
   - tags：2-4 个中文标签
4. 新闻条目输出：
   - id：原样返回
   - title_zh：中文标题
   - summary_zh：2 句，说明“发生了什么、关键数字/主体”
   - impact：1 句，说明对产业链的影响
   - tags：2-4 个中文标签
5. 如果摘要信息不足，summary_zh 写“摘要信息有限，请见原文”，不要猜测。
6. overview：2-3 句今日总体观察，只能基于给定条目。
7. trends：3 条，必须是给定条目中反复出现的主题，不要引入新事实。
8. 只输出 JSON，不要 Markdown 代码块，不要额外解释。

输出格式：
{{
  "overview": "...",
  "papers": [
    {{"id": "...", "title_zh": "...", "summary_zh": "...", "why": "...", "tags": ["..."]}}
  ],
  "news": [
    {{"id": "...", "title_zh": "...", "summary_zh": "...", "impact": "...", "tags": ["..."]}}
  ],
  "trends": ["...", "...", "..."]
}}

待处理数据：
{json.dumps(payload, ensure_ascii=False)}
"""


def parse_json_content(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("LLM response is not a JSON object")
    return value


def call_llm(
    cfg: dict[str, Any], items: list[Item], errors: list[str]
) -> dict[str, Any] | None:
    if not items:
        return None
    if not cfg.get("enabled", True):
        return None

    api_key_name = str(cfg.get("api_key_env", "DEEPSEEK_API_KEY"))
    api_key = env_value(api_key_name)
    if not api_key:
        log(f"[warn] {api_key_name} is not set; using fallback summary")
        return None

    base_url = str(cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
    model = str(cfg.get("model", "deepseek-chat"))
    max_items = int(cfg.get("max_items_per_call", 24))
    timeout = int(cfg.get("timeout_seconds", 120))
    prompt = build_prompt(items[:max_items])

    body: dict[str, Any] = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 6000,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
    }

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            return parse_json_content(content)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == 0:
                time.sleep(2)

    message = sanitize_error(last_error)
    errors.append(f"LLM: {message}")
    log(f"[warn] LLM failed: {message}")
    return None


def normalize_digest(
    raw: dict[str, Any] | None, selected: list[Item]
) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}

    def index_entries(key: str) -> dict[str, dict[str, Any]]:
        entries = raw.get(key, [])
        if not isinstance(entries, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id"):
                result[str(entry["id"])] = entry
        return result

    paper_entries = index_entries("papers")
    news_entries = index_entries("news")

    def make_entry(item: Item, source: dict[str, Any], kind: str) -> dict[str, Any]:
        title_zh = clean_text(source.get("title_zh")) or item.title
        summary = clean_text(source.get("summary_zh"))
        if not summary:
            summary = (
                item.abstract[:280]
                if item.abstract
                else "摘要信息有限，请见原文。"
            )
        why = clean_text(source.get("why") or source.get("impact") or "")
        tags: list[str] = []
        raw_tags = source.get("tags", [])
        if isinstance(raw_tags, list):
            for tag in raw_tags:
                cleaned = clean_text(tag)
                if cleaned and cleaned not in tags:
                    tags.append(cleaned)
        return {
            "id": item.id,
            "kind": kind,
            "title_zh": title_zh,
            "summary_zh": summary,
            "why": why,
            "tags": tags[:4],
        }

    papers = [
        make_entry(item, paper_entries.get(item.id, {}), "paper")
        for item in selected
        if item.kind == "paper"
    ]
    news = [
        make_entry(item, news_entries.get(item.id, {}), "news")
        for item in selected
        if item.kind == "news"
    ]

    overview = clean_text(raw.get("overview")) or "今日简报已生成。"
    trends: list[str] = []
    raw_trends = raw.get("trends", [])
    if isinstance(raw_trends, list):
        for trend in raw_trends:
            cleaned = clean_text(trend)
            if cleaned:
                trends.append(cleaned)

    return {
        "overview": overview,
        "papers": papers,
        "news": news,
        "trends": trends[:3],
    }


def fallback_digest(
    selected: list[Item], reason: str = "摘要服务暂不可用"
) -> dict[str, Any]:
    papers = []
    news = []
    for item in selected:
        entry = {
            "id": item.id,
            "kind": item.kind,
            "title_zh": item.title,
            "summary_zh": (
                item.abstract[:280]
                if item.abstract
                else "摘要信息有限，请见原文。"
            ),
            "why": "",
            "tags": [],
        }
        if item.kind == "paper":
            papers.append(entry)
        else:
            news.append(entry)
    return {
        "overview": f"今日自动简报（{reason}，以下保留原文标题、摘要与链接）。",
        "papers": papers,
        "news": news,
        "trends": [],
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def entry_map(digest: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    entries = digest.get(key, [])
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id"):
                result[str(entry["id"])] = entry
    return result


def safe_url(value: str) -> str:
    url = (value or "").strip()
    if url.startswith(("http://", "https://")):
        return html.escape(url, quote=True)
    return "#"


def tag_html(tags: Any) -> str:
    if not isinstance(tags, list):
        return ""
    parts = []
    for tag in tags:
        cleaned = clean_text(tag)
        if cleaned:
            parts.append(
                '<span style="display:inline-block;background:#eef2ff;color:#3730a3;'
                'border-radius:999px;padding:2px 8px;font-size:11px;margin:4px 6px 0 0;">'
                f"{html.escape(cleaned)}</span>"
            )
    return "".join(parts)


def render_markdown(
    date_str: str,
    digest: dict[str, Any],
    selected: list[Item],
    errors: list[str],
    paper_count: int | None = None,
) -> str:
    items_by_id = {item.id: item for item in selected}
    papers = [e for e in digest.get("papers", []) if isinstance(e, dict)]
    news = [e for e in digest.get("news", []) if isinstance(e, dict)]
    displayed_papers = paper_count if paper_count is not None else len(papers)

    lines = [
        f"# 显示行业日报 | {date_str}",
        "",
        f"> 今日 {displayed_papers} 篇论文 · {len(news)} 条新闻",
        "",
        "## 今日速览",
        "",
        str(digest.get("overview", "")).strip() or "今日简报已生成。",
        "",
    ]

    if papers:
        lines.extend(["## 论文精选", ""])
        for index, entry in enumerate(papers, 1):
            item = items_by_id.get(str(entry.get("id")))
            if not item:
                continue
            lines.extend(
                [
                    f"### {index}. {entry.get('title_zh', item.title)}",
                    "",
                    f"- 原文：{item.title}",
                    f"- 来源：{item.source} · {item.published[:10]}",
                ]
            )
            if item.authors:
                lines.append(f"- 作者：{', '.join(item.authors[:6])}")
            lines.append(f"- 摘要：{entry.get('summary_zh', '')}")
            if entry.get("why"):
                lines.append(f"- 为什么重要：{entry['why']}")
            if entry.get("tags"):
                lines.append(
                    "- 标签：" + "、".join(str(t) for t in entry["tags"])
                )
            lines.extend([f"- 链接：{item.url}", ""])

    if news:
        lines.extend(["## 行业新闻", ""])
        for index, entry in enumerate(news, 1):
            item = items_by_id.get(str(entry.get("id")))
            if not item:
                continue
            lines.extend(
                [
                    f"### {index}. {entry.get('title_zh', item.title)}",
                    "",
                    f"- 来源：{item.source} · {item.published[:10]}",
                    f"- 摘要：{entry.get('summary_zh', '')}",
                ]
            )
            if entry.get("why"):
                lines.append(f"- 产业链影响：{entry['why']}")
            if entry.get("tags"):
                lines.append(
                    "- 标签：" + "、".join(str(t) for t in entry["tags"])
                )
            lines.extend([f"- 链接：{item.url}", ""])

    trends = digest.get("trends", [])
    if isinstance(trends, list) and trends:
        lines.extend(["## 趋势观察", ""])
        for trend in trends:
            lines.append(f"- {trend}")
        lines.append("")

    if errors:
        lines.extend(["## 采集提示", ""])
        for error in errors:
            lines.append(f"- {error}")
        lines.append("")

    lines.extend(
        [
            "---",
            "",
            "由 GitHub Actions 自动生成。摘要由 DeepSeek 生成，请以原文为准。",
            "",
        ]
    )
    return "\n".join(lines)


def render_html(
    date_str: str,
    digest: dict[str, Any],
    selected: list[Item],
    errors: list[str],
    paper_count: int | None = None,
) -> str:
    items_by_id = {item.id: item for item in selected}
    papers = [e for e in digest.get("papers", []) if isinstance(e, dict)]
    news = [e for e in digest.get("news", []) if isinstance(e, dict)]
    displayed_papers = paper_count if paper_count is not None else len(papers)
    overview = html.escape(str(digest.get("overview", "")).strip())

    css = """
    body { margin:0; padding:0; background:#f3f4f6; color:#111827;
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
    .wrap { max-width:640px; margin:0 auto; padding:20px 14px 36px; }
    .hero { background:#0f172a; color:#ffffff; border-radius:16px; padding:26px 22px; }
    .hero .kicker { font-size:11px; letter-spacing:2px; color:#94a3b8; }
    .hero h1 { margin:8px 0 6px; font-size:26px; line-height:1.25; }
    .hero .meta { font-size:13px; color:#cbd5e1; }
    .card { background:#ffffff; border:1px solid #e5e7eb; border-radius:12px;
      padding:16px 18px; margin:12px 0; }
    .section { margin:24px 0 8px; font-size:18px; font-weight:700; color:#0f172a; }
    .source { font-size:12px; color:#6b7280; }
    .original { font-size:12px; color:#9ca3af; margin-top:4px; line-height:1.45; }
    .title { margin:7px 0 8px; font-size:17px; line-height:1.45; color:#111827; }
    .summary { margin:8px 0; font-size:14px; line-height:1.7; color:#374151; }
    .why { margin:8px 0; font-size:13px; line-height:1.65; color:#4b5563; }
    .link { display:inline-block; margin-top:10px; font-size:13px; color:#1d4ed8;
      text-decoration:none; font-weight:600; }
    .footer { margin-top:24px; font-size:11px; line-height:1.6; color:#9ca3af; }
    .warn { background:#fffbeb; border:1px solid #fde68a; color:#92400e;
      border-radius:10px; padding:12px 14px; font-size:12px; line-height:1.6; margin:12px 0; }
    @media (max-width:480px) {
      .hero h1 { font-size:22px; }
      .card { padding:14px; }
    }
    """

    parts: list[str] = [
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        "<title>显示行业日报</title>",
        f"<style>{css}</style></head><body><div class=\"wrap\">",
        "<div class=\"hero\">",
        "<div class=\"kicker\">DISPLAY DAILY BRIEF</div>",
        "<h1>显示行业日报</h1>",
        f"<div class=\"meta\">{html.escape(date_str)} · {displayed_papers} 篇论文 · {len(news)} 条新闻</div>",
        "</div>",
    ]

    if overview:
        parts.append(
            '<div class="card"><div class="source">今日速览</div>'
            f'<div class="summary">{overview}</div></div>'
        )

    def add_card(entry: dict[str, Any], kind: str) -> None:
        item = items_by_id.get(str(entry.get("id")))
        if not item:
            return
        label = "为什么重要" if kind == "paper" else "产业链影响"
        why = clean_text(entry.get("why"))
        tags = tag_html(entry.get("tags"))
        original = html.escape(item.title)
        parts.append('<div class="card">')
        parts.append(
            f'<div class="source">{html.escape(item.source)} · {html.escape(item.published[:10])}</div>'
        )
        parts.append(f'<div class="title">{html.escape(str(entry.get("title_zh", item.title)))}</div>')
        parts.append(f'<div class="original">原文：{original}</div>')
        parts.append(
            f'<div class="summary">{html.escape(str(entry.get("summary_zh", "")))}</div>'
        )
        if why:
            parts.append(
                f'<div class="why"><strong>{label}：</strong>{html.escape(why)}</div>'
            )
        if tags:
            parts.append(f"<div>{tags}</div>")
        parts.append(
            f'<a class="link" href="{safe_url(item.url)}" target="_blank" rel="noopener">阅读原文 →</a>'
        )
        parts.append("</div>")

    if papers:
        parts.append('<div class="section">论文精选</div>')
        for entry in papers:
            add_card(entry, "paper")

    if news:
        parts.append('<div class="section">行业新闻</div>')
        for entry in news:
            add_card(entry, "news")

    trends = digest.get("trends", [])
    if isinstance(trends, list) and trends:
        parts.append('<div class="section">趋势观察</div><div class="card"><ul style="margin:6px 0 0 18px;padding:0;">')
        for trend in trends:
            parts.append(
                f'<li style="margin:8px 0;font-size:14px;line-height:1.65;color:#374151;">{html.escape(str(trend))}</li>'
            )
        parts.append("</ul></div>")

    if errors:
        parts.append('<div class="section">采集提示</div><div class="warn">')
        for error in errors:
            parts.append(f"<div>• {html.escape(str(error))}</div>")
        parts.append("</div>")

    parts.append(
        '<div class="footer">由 GitHub Actions 自动生成。摘要由 DeepSeek 生成，'
        "请以原文为准。历史简报见仓库 archive/ 目录。</div>"
    )
    parts.append("</div></body></html>")
    return "".join(parts)


def render_plain(
    date_str: str,
    digest: dict[str, Any],
    selected: list[Item],
    paper_count: int | None = None,
) -> str:
    items_by_id = {item.id: item for item in selected}
    displayed_papers = (
        paper_count
        if paper_count is not None
        else len(digest.get("papers", []))
    )
    lines = [
        f"显示行业日报 | {date_str}",
        f"今日 {displayed_papers} 篇论文 · {len(digest.get('news', []))} 条新闻",
        "",
        str(digest.get("overview", "")).strip(),
        "",
    ]
    for key, heading in (("papers", "论文精选"), ("news", "行业新闻")):
        entries = digest.get(key, [])
        if not isinstance(entries, list) or not entries:
            continue
        lines.extend([heading, ""])
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                continue
            item = items_by_id.get(str(entry.get("id")))
            if not item:
                continue
            lines.append(f"{index}. {entry.get('title_zh', item.title)}")
            lines.append(f"   {item.source} · {item.published[:10]}")
            lines.append(f"   {entry.get('summary_zh', '')}")
            lines.append(f"   {item.url}")
            lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def send_email(
    cfg: dict[str, Any],
    subject: str,
    plain: str,
    html_body: str,
) -> bool:
    if not cfg.get("enabled", True):
        return False

    username = env_value(str(cfg.get("username_env", "SMTP_USER")))
    password = env_value(str(cfg.get("password_env", "SMTP_PASS")))
    to_raw = env_value(str(cfg.get("to_env", "BRIEF_TO")))
    if not username or not password or not to_raw:
        log("[warn] email credentials/to are incomplete; skipping email")
        return False

    recipients = [
        part.strip()
        for part in re.split(r"[,;，]", to_raw)
        if part.strip()
    ]
    if not recipients:
        log("[warn] BRIEF_TO has no recipient; skipping email")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(
        (str(cfg.get("from_name", "显示行业日报")), username)
    )
    message["To"] = ", ".join(recipients)
    message.set_content(plain)
    message.add_alternative(html_body, subtype="html")

    host = str(cfg.get("smtp_host", "smtp.qq.com"))
    port = int(cfg.get("smtp_port", 465))
    use_ssl = bool(cfg.get("use_ssl", True))
    context = ssl.create_default_context()
    server: smtplib.SMTP | None = None

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
            server.ehlo()
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
        server.login(username, password)
        server.send_message(message)
        log(f"[ok] email sent to {', '.join(recipients)}")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] email failed: {exc}")
        return False
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass


def send_feishu(
    cfg: dict[str, Any],
    date_str: str,
    digest: dict[str, Any],
    selected: list[Item],
    paper_count: int | None = None,
) -> bool:
    env_name = str(cfg.get("feishu_webhook_env", "")).strip()
    if not env_name:
        return False
    webhook = env_value(env_name)
    if not webhook:
        return False

    papers = digest.get("papers", [])
    news = digest.get("news", [])
    total_papers = (
        paper_count
        if paper_count is not None
        else (len(papers) if isinstance(papers, list) else 0)
    )
    titles: list[str] = []
    for entry in (papers if isinstance(papers, list) else [])[:3]:
        if isinstance(entry, dict):
            titles.append(str(entry.get("title_zh", "")))
    for entry in (news if isinstance(news, list) else [])[:3]:
        if isinstance(entry, dict):
            titles.append(str(entry.get("title_zh", "")))

    lines = [
        f"显示行业日报 {date_str}",
        f"今日 {total_papers} 篇论文 / "
        f"{len(news) if isinstance(news, list) else 0} 条新闻",
        "",
    ]
    lines.extend(f"• {title}" for title in titles if title)
    if not selected:
        lines.append("今日没有发现新的重要更新。")
    lines.extend(["", "完整版见邮件。"])
    text = "\n".join(lines)

    try:
        response = requests.post(
            webhook,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("code") not in (0, None):
            raise RuntimeError(f"Feishu error: {payload}")
        log("[ok] Feishu notification sent")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] Feishu failed: {sanitize_error(exc)}")
        return False


def deliver(
    cfg: dict[str, Any],
    date_str: str,
    digest: dict[str, Any],
    selected: list[Item],
    html_body: str,
    plain: str,
) -> tuple[bool, list[tuple[str, bool]]]:
    results: list[tuple[str, bool]] = []
    papers = digest.get("papers", [])
    news = digest.get("news", [])
    subject = (
        f"显示行业日报 | {date_str} | "
        f"{len(papers) if isinstance(papers, list) else 0} 篇论文 · "
        f"{len(news) if isinstance(news, list) else 0} 条新闻"
    )

    email_cfg = cfg.get("email", {})
    email_enabled = isinstance(email_cfg, dict) and email_cfg.get("enabled", True)
    email_ok = False
    if email_enabled:
        email_ok = send_email(email_cfg, subject, plain, html_body)
        results.append(("email", email_ok))

    push_cfg = cfg.get("push", {})
    feishu_ok = False
    if isinstance(push_cfg, dict):
        webhook_env = str(push_cfg.get("feishu_webhook_env", "")).strip()
        if webhook_env and os.environ.get(webhook_env):
            feishu_ok = send_feishu(push_cfg, date_str, digest, selected)
            results.append(("feishu", feishu_ok))

    # Email is the primary channel: if it is enabled, require it to succeed
    # before marking items as seen. Feishu is a supplementary alert.
    if email_enabled:
        return email_ok, results
    return feishu_ok, results


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def write_archive(
    cfg: dict[str, Any],
    date_str: str,
    digest: dict[str, Any],
    selected: list[Item],
    markdown: str,
    html_body: str,
    plain: str,
    errors: list[str],
) -> None:
    archive_dir = resolve_path(str(cfg.get("archive_dir", "archive")))
    archive_dir.mkdir(parents=True, exist_ok=True)

    (archive_dir / f"{date_str}.md").write_text(markdown, encoding="utf-8")
    (archive_dir / f"{date_str}.html").write_text(html_body, encoding="utf-8")
    payload = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "digest": digest,
        "items": [asdict(item) for item in selected],
        "errors": errors,
        "plain": plain,
    }
    (archive_dir / f"{date_str}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_empty_digest(errors: list[str]) -> dict[str, Any]:
    if errors:
        overview = (
            "今日采集出现异常，暂时没有生成新的简报。"
            "请检查 GitHub Actions 日志中的“采集提示”。"
        )
    else:
        overview = "今日没有发现新的显示行业论文或新闻。"
    return {"overview": overview, "papers": [], "news": [], "trends": []}


def main(argv: list[str] | None = None) -> int:
    global USER_AGENT

    parser = argparse.ArgumentParser(description="Display industry daily brief")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="path to config.yaml",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and render, but do not send or update state",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="skip the LLM call and use the fallback summary",
    )
    parser.add_argument(
        "--date",
        help="override the brief date, e.g. 2026-11-04",
    )
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config))
    USER_AGENT = str(cfg.get("user_agent", USER_AGENT))

    tz_name = str(cfg.get("timezone", "Asia/Shanghai"))
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    if args.date:
        now = datetime.strptime(args.date, "%Y-%m-%d").replace(
            hour=8, minute=0, tzinfo=tz
        )
    date_str = now.strftime("%Y-%m-%d")

    lookback_hours = int(cfg.get("lookback_hours", 36))
    since = now.astimezone(timezone.utc) - timedelta(hours=lookback_hours)

    errors: list[str] = []
    log(f"[start] {date_str} since={to_iso(since)}")
    fetched = collect(cfg, since, errors)
    log(f"[collect] {len(fetched)} raw items")

    state_path = resolve_path(str(cfg.get("state_path", "state/seen.json")))
    pending_path = pending_path_for(state_path)
    pending = load_pending(pending_path)
    if pending:
        log(f"[pending] retrying {len(pending)} item(s) from a previous failed delivery")
    items = pending + fetched

    state = load_state(state_path)
    fresh = dedupe(
        [item for item in items if item.id not in state.get("seen", {})]
    )
    selected = select_items(fresh, cfg)
    log(f"[select] {len(fresh)} fresh, {len(selected)} selected")

    raw_digest: dict[str, Any] | None = None
    if selected and not args.no_llm:
        llm_cfg = cfg.get("llm", {})
        if not isinstance(llm_cfg, dict):
            llm_cfg = {}
        raw_digest = call_llm(llm_cfg, selected, errors)
    if raw_digest is not None:
        digest = normalize_digest(raw_digest, selected)
    elif selected:
        digest = fallback_digest(selected)
    else:
        digest = build_empty_digest(errors)

    markdown = render_markdown(date_str, digest, selected, errors)
    html_body = render_html(date_str, digest, selected, errors)
    plain = render_plain(date_str, digest, selected)
    write_archive(
        cfg, date_str, digest, selected, markdown, html_body, plain, errors
    )

    if args.dry_run:
        print(markdown)
        log(
            f"[dry-run] raw={len(items)} fresh={len(fresh)} "
            f"selected={len(selected)} errors={len(errors)}"
        )
        return 0

    ok, results = deliver(cfg, date_str, digest, selected, html_body, plain)
    log(f"[deliver] {results}")

    if ok:
        for item in selected:
            state.setdefault("seen", {})[item.id] = date_str
        state["last_run"] = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        state["last_error"] = None
        save_state(state_path, state)
        clear_pending(pending_path)
        log(f"[done] state saved to {state_path}")
        return 0

    state["last_error"] = "; ".join(errors + ["all delivery channels failed"])
    save_state(state_path, state)
    retry_items = dedupe(pending + fresh)
    if retry_items:
        save_pending(pending_path, retry_items)
        log(f"[pending] saved {min(len(retry_items), 50)} item(s) for retry")
    log("[error] all delivery channels failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
