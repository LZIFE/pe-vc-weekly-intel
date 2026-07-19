"""Shared runtime for the PE/VC, media, and ai_web intelligence system.

The module deliberately uses the Python standard library so both collectors can
run locally, in GitHub Actions, or from Codex without maintaining another lock
file. Search is routed only to Doubao. Content understanding, including PDF
summaries, is routed to Agnes and cached by content hash to reduce token usage.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable


WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = WORKSPACE_ROOT / ".cache" / "intelligence"


def load_dotenv(path: Path, *, override: bool = False) -> None:
    """Load a simple dotenv file without adding a dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override or key not in os.environ:
            os.environ[key] = value


def _json_request(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: int,
    retries: int = 3,
) -> dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                error_payload = json.loads(exc.read().decode("utf-8", errors="replace"))
                error_obj = error_payload.get("error") or error_payload.get("ResponseMetadata", {}).get("Error") or {}
                message = error_obj.get("message") or error_obj.get("Message") or str(error_obj)
                last_error = RuntimeError(message)
            except (json.JSONDecodeError, AttributeError, OSError):
                last_error = exc
            if exc.code not in {408, 409, 425, 429, 500, 502, 503, 504}:
                raise last_error
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
        if attempt + 1 < retries:
            time.sleep(min(12.0, (2 ** attempt) + random.random()))
    raise last_error or RuntimeError(f"request failed: {url}")


def _responses_stream_request(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: int,
    retries: int = 1,
) -> dict[str, Any]:
    """Read an Ark Responses SSE stream and return its completed response.

    Streaming keeps the connection active during a long web search. This avoids
    a client-side read timeout while Ark continues inference and consumes the
    account's quota in the background.
    """
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                **headers,
            },
            method="POST",
        )
        text_parts: list[str] = []
        annotations: list[dict[str, Any]] = []
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    event_type = str(event.get("type", ""))
                    if event_type == "response.completed" and isinstance(event.get("response"), dict):
                        return event["response"]
                    if event_type in {"response.failed", "error"}:
                        error = event.get("error") or (event.get("response") or {}).get("error") or event
                        if isinstance(error, dict):
                            raise RuntimeError(str(error.get("message") or error.get("code") or error))
                        raise RuntimeError(str(error))
                    if event_type == "response.output_text.delta":
                        text_parts.append(str(event.get("delta", "")))
                    annotation = event.get("annotation")
                    if isinstance(annotation, dict):
                        annotations.append(annotation)
            if text_parts or annotations:
                return {
                    "output": [{
                        "type": "message",
                        "content": [{
                            "type": "output_text",
                            "text": "".join(text_parts),
                            "annotations": annotations,
                        }],
                    }],
                }
            last_error = RuntimeError("豆包流式响应未包含 completed 或文本事件")
        except urllib.error.HTTPError as exc:
            try:
                error_payload = json.loads(exc.read().decode("utf-8", errors="replace"))
                error_obj = error_payload.get("error") or error_payload.get("ResponseMetadata", {}).get("Error") or {}
                message = error_obj.get("message") or error_obj.get("Message") or str(error_obj)
                last_error = RuntimeError(message)
            except (json.JSONDecodeError, AttributeError, OSError):
                last_error = exc
            if exc.code not in {408, 409, 425, 429, 500, 502, 503, 504}:
                raise last_error
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
        if attempt + 1 < retries:
            time.sleep(min(12.0, (2 ** attempt) + random.random()))
    raise last_error or RuntimeError(f"stream request failed: {url}")


def _cache_path(namespace: str, payload: Any) -> Path:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    root = Path(os.environ.get("INTELLIGENCE_CACHE_DIR", str(DEFAULT_CACHE_DIR)))
    return root / namespace / f"{digest}.json"


def _read_cache(path: Path, ttl_seconds: int) -> dict[str, Any] | None:
    if not path.exists() or time.time() - path.stat().st_mtime > ttl_seconds:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def agnes_chat(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 2048,
    cache_namespace: str = "agnes-chat",
    cache_ttl_seconds: int = 7 * 24 * 3600,
) -> dict[str, Any]:
    """Call the OpenAI-compatible Agnes Chat Completions endpoint.

    Prompts are capped before transport and identical requests are cached. The
    cache key includes the model and prompt but never the API key.
    """
    api_key = os.environ.get("AGNES_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 AGNES_API_KEY")
    base_url = os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").rstrip("/")
    model = os.environ.get("AGNES_MODEL", "agnes-2.0-flash")
    max_prompt_chars = max(2_000, int(os.environ.get("AGNES_MAX_PROMPT_CHARS", "48000")))

    compact_messages: list[dict[str, Any]] = []
    remaining = max_prompt_chars
    for message in reversed(messages):
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        content = content[:remaining]
        remaining -= len(content)
        compact_messages.append({"role": message.get("role", "user"), "content": content})
        if remaining <= 0:
            break
    compact_messages.reverse()

    body = {
        "model": model,
        "messages": compact_messages,
        "temperature": temperature,
        "max_tokens": min(max_tokens, int(os.environ.get("AGNES_MAX_OUTPUT_TOKENS", "4096"))),
        "stream": False,
    }
    cache_key = {"v": 1, "body": body}
    cache_file = _cache_path(cache_namespace, cache_key)
    cached = _read_cache(cache_file, cache_ttl_seconds)
    if cached is not None:
        cached.setdefault("_system", {})["cache_hit"] = True
        return cached

    result = _json_request(
        f"{base_url}/chat/completions",
        body,
        {"Authorization": f"Bearer {api_key}"},
        timeout=int(os.environ.get("AGNES_TIMEOUT", os.environ.get("AI_HTTP_TIMEOUT", "300"))),
        retries=int(os.environ.get("AGNES_RETRIES", "3")),
    )
    if not isinstance(result.get("choices"), list):
        raise RuntimeError("Agnes 返回中缺少 choices")
    result.setdefault("_system", {})["cache_hit"] = False
    _write_cache(cache_file, result)
    return result


def doubao_search(
    query: str,
    *,
    start_date: str,
    end_date: str,
    count: int = 20,
    cache_ttl_seconds: int = 12 * 3600,
) -> list[dict[str, Any]]:
    """Use Doubao only for web search and return a normalized source list.

    Ark model API keys (``ark-...``) use the official Responses API with the
    built-in ``web_search`` tool. Legacy Harness keys can still use the native
    structured search endpoint. No provider other than Doubao is used here.
    """
    api_key = os.environ.get("ARK_SEARCH_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 ARK_SEARCH_API_KEY（豆包纯搜索 API 密钥）")
    mode = os.environ.get("ARK_SEARCH_MODE", "responses" if api_key.startswith("ark-") else "harness").lower()
    if mode == "responses":
        return _doubao_responses_search(
            api_key,
            query,
            start_date=start_date,
            end_date=end_date,
            count=count,
            cache_ttl_seconds=cache_ttl_seconds,
        )
    base_url = os.environ.get("ARK_SEARCH_BASE_URL", "https://open.feedcoopapi.com").rstrip("/")
    body = {
        "Query": query[:100],
        "SearchType": "web",
        "Count": max(1, min(count, 50)),
        "NeedSummary": True,
        "TimeRange": f"{start_date}..{end_date}",
        "Filter": {
            "NeedContent": False,
            "NeedUrl": True,
            "AuthInfoLevel": int(os.environ.get("DOUBAO_SEARCH_AUTH_LEVEL", "0")),
        },
    }
    cache_file = _cache_path("doubao-search", {"v": 1, "body": body})
    cached = _read_cache(cache_file, cache_ttl_seconds)
    if cached is not None:
        rows = cached.get("rows", [])
        return rows if isinstance(rows, list) else []

    response = _json_request(
        f"{base_url}/search_api/web_search",
        body,
        {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "X-Traffic-Tag": "intelligence_system",
        },
        timeout=int(os.environ.get("DOUBAO_SEARCH_TIMEOUT", "60")),
        retries=int(os.environ.get("DOUBAO_SEARCH_MAX_RETRIES", "3")),
    )
    metadata = response.get("ResponseMetadata") or {}
    error = metadata.get("Error") or response.get("error") or {}
    if error:
        raise RuntimeError(error.get("Message") or error.get("message") or str(error))
    rows = (response.get("Result") or {}).get("WebResults")
    if not isinstance(rows, list):
        raise RuntimeError("豆包搜索返回中缺少 Result.WebResults")
    clean_rows = [row for row in rows if isinstance(row, dict)]
    _write_cache(cache_file, {"rows": clean_rows})
    return clean_rows


def _extract_json_rows(text: str) -> list[dict[str, Any]]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    candidates = [cleaned]
    match = re.search(r"\[[\s\S]*\]", cleaned)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            value = value.get("items") or value.get("results") or []
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _doubao_responses_search(
    api_key: str,
    query: str,
    *,
    start_date: str,
    end_date: str,
    count: int,
    cache_ttl_seconds: int,
) -> list[dict[str, Any]]:
    base_url = os.environ.get("ARK_RESPONSES_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    model = os.environ.get("ARK_SEARCH_MODEL", "doubao-seed-2-1-pro-260628")
    requested_count = max(1, min(count, 20))
    prompt = (
        f"只使用 web_search 搜索 {start_date} 至 {end_date} 的以下主题：{query[:2000]}。"
        f"最多返回 {requested_count} 条互不重复、可核验的原始信源。"
        "只输出 JSON 数组，不要分析、不要 markdown。每项字段固定为："
        "title,url,source,published,summary。published 尽量使用 ISO-8601；"
        "url 必须是原始网页，不能是搜索页或虚构链接。"
    )
    use_stream = os.environ.get("DOUBAO_SEARCH_STREAM", "1") != "0"
    body = {
        "model": model,
        "stream": use_stream,
        "tools": [{"type": "web_search", "max_keyword": int(os.environ.get("ARK_SEARCH_MAX_KEYWORD", "2"))}],
        "input": [{
            "role": "user",
            "content": [{"type": "input_text", "text": prompt}],
        }],
    }
    cache_file = _cache_path("doubao-responses-search", {"v": 1, "body": body})
    cached = _read_cache(cache_file, cache_ttl_seconds)
    if cached is not None:
        rows = cached.get("rows", [])
        return rows if isinstance(rows, list) else []
    request = _responses_stream_request if use_stream else _json_request
    response = request(
        f"{base_url}/responses",
        body,
        {"Authorization": f"Bearer {api_key}"},
        timeout=int(os.environ.get("DOUBAO_SEARCH_TIMEOUT", "300")),
        retries=int(os.environ.get("DOUBAO_SEARCH_MAX_RETRIES", "1")),
    )
    text_parts: list[str] = []
    annotations: list[dict[str, Any]] = []
    for output in response.get("output", []):
        if not isinstance(output, dict) or output.get("type") != "message":
            continue
        for content in output.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text":
                text_parts.append(str(content.get("text", "")))
                annotations.extend(a for a in content.get("annotations", []) if isinstance(a, dict))
    raw_rows = _extract_json_rows("\n".join(text_parts))
    if not raw_rows and annotations:
        raw_rows = [{
            "title": row.get("title", ""),
            "url": row.get("url", ""),
            "source": row.get("title", ""),
            "published": "",
            "summary": "",
        } for row in annotations if row.get("url")]
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    grounded_urls = {str(row.get("url", "")).strip() for row in annotations if row.get("url")}
    for row in raw_rows:
        url = str(row.get("url") or row.get("Url") or "").strip()
        if not url or url in seen or (grounded_urls and url not in grounded_urls):
            continue
        seen.add(url)
        normalized.append({
            "Title": str(row.get("title") or row.get("Title") or "").strip(),
            "Url": url,
            "SiteName": str(row.get("source") or row.get("SiteName") or "").strip(),
            "PublishTime": str(row.get("published") or row.get("PublishTime") or "").strip(),
            "Summary": str(row.get("summary") or row.get("Summary") or "").strip(),
        })
        if len(normalized) >= requested_count:
            break
    _write_cache(cache_file, {"rows": normalized})
    return normalized


def read_pdf_with_agnes(url: str, *, context: str = "") -> str:
    """Download a PDF, extract its text, then let Agnes read and summarize it.

    PDF bytes never enter prompts blindly: text extraction and a character cap
    keep requests predictable. The final model result is cached by PDF content.
    """
    if not url or ".pdf" not in url.lower():
        return ""
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=int(os.environ.get("PDF_DOWNLOAD_TIMEOUT", "30"))) as response:
        data = response.read(int(os.environ.get("PDF_MAX_BYTES", str(25 * 1024 * 1024))) + 1)
    if len(data) > int(os.environ.get("PDF_MAX_BYTES", str(25 * 1024 * 1024))):
        raise RuntimeError("PDF 超过 PDF_MAX_BYTES 限制")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("读取 PDF 需要安装 pypdf") from exc

    reader = PdfReader(io.BytesIO(data))
    page_limit = max(1, int(os.environ.get("PDF_MAX_PAGES", "40")))
    text_parts = [(page.extract_text() or "") for page in reader.pages[:page_limit]]
    raw_text = re.sub(r"\s+", " ", "\n".join(text_parts)).strip()
    if not raw_text:
        return ""
    raw_text = raw_text[: int(os.environ.get("PDF_MAX_TEXT_CHARS", "36000"))]
    prompt = (
        "阅读以下公告 PDF 的提取文本，输出一段 120-220 字中文事实摘要。"
        "只保留关键决策、金额、日期、人名、财务指标和结论；删除地址、电话、"
        "公告编号、董事会保证套话与表格字段噪声。不要猜测，不要输出 markdown。\n"
        f"上下文：{context[:300]}\nPDF文本：{raw_text}"
    )
    result = agnes_chat(
        [{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=700,
        cache_namespace=f"agnes-pdf-{hashlib.sha256(data).hexdigest()[:16]}",
        cache_ttl_seconds=30 * 24 * 3600,
    )
    return str(result["choices"][0]["message"].get("content", "")).strip()


def load_crawler_rows(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    """Load reusable crawler drops from JSON or JSONL files/directories."""
    files: list[Path] = []
    for value in paths:
        path = Path(value).expanduser()
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*") if p.suffix.lower() in {".json", ".jsonl"}))
        elif path.exists():
            files.append(path)
    rows: list[dict[str, Any]] = []
    for path in files:
        try:
            if path.suffix.lower() == ".jsonl":
                values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            else:
                value = json.loads(path.read_text(encoding="utf-8"))
                values = value if isinstance(value, list) else value.get("items", [])
            rows.extend(row for row in values if isinstance(row, dict))
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    return rows


def fetch_ai_web_package(channel: str, *, days: int = 90) -> dict[str, Any]:
    """Download and validate a versioned intelligence package from AI_web.

    This reads already-collected cloud data and never triggers an RSS crawl.
    """
    if channel not in {"media", "private-equity"}:
        raise ValueError("channel must be media or private-equity")
    base_url = os.environ.get("AI_WEB_BASE_URL", "https://aiweb-roan.vercel.app").rstrip("/")
    query = urllib.parse.urlencode({"channel": channel, "days": max(1, min(days, 120))})
    headers = {"Accept": "application/json", "User-Agent": "AI-web automation consumer/1.0"}
    secret = os.environ.get("AI_WEB_CRON_SECRET", "").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    request = urllib.request.Request(f"{base_url}/api/intelligence/export?{query}", headers=headers)
    with urllib.request.urlopen(request, timeout=int(os.environ.get("AI_WEB_TIMEOUT", "60"))) as response:
        package = json.loads(response.read().decode("utf-8"))
    if not isinstance(package, dict) or package.get("schemaVersion") != 1:
        raise RuntimeError("AI_web 数据包版本无效")
    if package.get("channel") != channel or not isinstance(package.get("items"), list):
        raise RuntimeError("AI_web 数据包契约不完整")
    if int(package.get("count", -1)) != len(package["items"]):
        raise RuntimeError("AI_web 数据包 count 与 items 不一致")
    return package


def _source_host(value: str) -> str:
    try:
        return (urllib.parse.urlparse(value).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _verified_feed_url(article_url: str) -> str:
    """Return a verified direct RSS/Atom URL when one is cheaply discoverable."""
    if os.environ.get("AI_WEB_DISCOVER_DIRECT_RSS", "1") == "0":
        return ""
    parsed = urllib.parse.urlparse(article_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    origin = f"{parsed.scheme}://{parsed.netloc}/"
    candidates: list[str] = []
    try:
        request = urllib.request.Request(origin, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
        with urllib.request.urlopen(request, timeout=int(os.environ.get("SOURCE_DISCOVERY_TIMEOUT", "6"))) as response:
            content = response.read(512_000).decode("utf-8", errors="ignore")
        for match in re.finditer(
            r'<link[^>]+(?:type=["\']application/(?:rss|atom)\+xml["\']|rel=["\'][^"\']*alternate[^"\']*["\'])[^>]+>',
            content,
            flags=re.I,
        ):
            href = re.search(r'href=["\']([^"\']+)', match.group(0), flags=re.I)
            if href:
                candidates.append(urllib.parse.urljoin(origin, href.group(1)))
    except Exception:
        pass
    candidates.extend(urllib.parse.urljoin(origin, suffix) for suffix in ("feed", "rss.xml", "atom.xml"))
    for candidate in list(dict.fromkeys(candidates))[:6]:
        try:
            request = urllib.request.Request(candidate, headers={"User-Agent": "AI-web source discovery/1.0"})
            with urllib.request.urlopen(request, timeout=int(os.environ.get("SOURCE_DISCOVERY_TIMEOUT", "6"))) as response:
                data = response.read(1_000_000)
            tag = ET.fromstring(data).tag.lower()
            if tag.endswith("rss") or tag.endswith("feed") or tag.endswith("rdf"):
                return candidate
        except Exception:
            continue
    return ""


def discover_source_candidates(
    channel: str,
    rows: Iterable[dict[str, Any]],
    package: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compare Doubao result hosts with AI_web's known source inventory."""
    known_hosts = {
        str(source.get("host", "")).lower().removeprefix("www.")
        for source in package.get("sourceInventory", [])
        if isinstance(source, dict)
    }
    ignored_hosts = {"google.com", "baidu.com", "bing.com", "news.google.com"}
    limit = max(0, int(os.environ.get("SOURCE_CANDIDATE_LIMIT", "10")))
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        url = str(row.get("url") or row.get("Url") or "").strip()
        host = _source_host(url)
        if not host or host in known_hosts or host in ignored_hosts or host in seen:
            continue
        seen.add(host)
        direct_feed_url = _verified_feed_url(url) if len(candidates) < limit else ""
        candidates.append({
            "channel": channel,
            "sourceName": str(row.get("source") or row.get("SiteName") or host).strip()[:100],
            "articleUrl": url,
            "directFeedUrl": direct_feed_url or None,
            "rsshubRouteHint": None if direct_feed_url else f"为 {host} 评估或新建 RSSHub 路由",
            "evidenceTitle": str(row.get("title") or row.get("Title") or "").strip()[:180],
            "discoveredAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        if len(candidates) >= limit:
            break
    return candidates


def save_source_candidates(path: str | Path, candidates: list[dict[str, Any]]) -> None:
    destination = Path(path)
    existing: list[dict[str, Any]] = []
    try:
        value = json.loads(destination.read_text(encoding="utf-8"))
        existing = value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        pass
    merged: dict[str, dict[str, Any]] = {}
    for candidate in [*existing, *candidates]:
        host = _source_host(str(candidate.get("articleUrl", "")))
        channel = str(candidate.get("channel", ""))
        if host and channel:
            merged[f"{channel}\0{host}"] = candidate
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    tmp.write_text(json.dumps(list(merged.values()), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(destination)


def submit_source_candidates(candidates: list[dict[str, Any]]) -> tuple[int, str]:
    """Submit source discoveries to AI_web's cloud review queue."""
    if not candidates:
        return 0, ""
    secret = os.environ.get("AI_WEB_CRON_SECRET", "").strip()
    if not secret:
        return 0, "AI_WEB_CRON_SECRET 未配置；候选源仅保存到本地文件"
    base_url = os.environ.get("AI_WEB_BASE_URL", "https://aiweb-roan.vercel.app").rstrip("/")
    request = urllib.request.Request(
        f"{base_url}/api/intelligence/source-candidates",
        data=json.dumps({"candidates": candidates}, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "User-Agent": "AI-web automation source feedback/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=int(os.environ.get("AI_WEB_TIMEOUT", "60"))) as response:
            value = json.loads(response.read().decode("utf-8"))
        return int(value.get("accepted", 0)), ""
    except Exception as exc:
        return 0, f"AI_web 候选源反馈失败：{exc}"
