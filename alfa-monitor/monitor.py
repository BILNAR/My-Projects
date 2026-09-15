import hashlib
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from pypdf import PdfReader

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "state.json"

SOURCES = [
    "https://alfabank.ru/actions/rules/",
    "https://alfabank.ru/sme/quick/docstariffs/",
]

ALLOWED_HOSTS = {
    "alfabank.ru",
    "www.alfabank.ru",
    "alfabank.st",
    "alfabank.servicecdn.ru",
    "alfachannels.servicecdn.ru",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
}

PDF_RE = re.compile(r'https?://[^\s"\'<>\\]+?\.pdf(?:\?[^\s"\'<>\\]*)?', re.I)
DATE_RE = re.compile(r"(?<!\d)(0?[1-9]|[12]\d|3[01])[./-](0?[1-9]|1[0-2])[./-](20\d{2}|\d{2})(?!\d)")


def load_state():
    if not STATE_FILE.exists():
        return {"initialized": False, "documents": {}}
    with STATE_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def is_allowed(url):
    try:
        return urlparse(url).hostname in ALLOWED_HOSTS
    except Exception:
        return False


def clean_url(url):
    return (
        url.replace("\\u002F", "/")
        .replace("\\/", "/")
        .replace("&amp;", "&")
        .strip()
    )


def fetch(url, timeout=45):
    if not is_allowed(url):
        raise ValueError(f"Host not allowed: {url}")
    return requests.get(url, headers=HEADERS, timeout=timeout, verify=False, allow_redirects=True)


def discover_from_page(page_url):
    r = fetch(page_url)
    print(f"SOURCE {page_url} -> HTTP {r.status_code}, {len(r.content)} bytes")
    print(f"FINAL URL: {r.url}")
    print(f"CONTENT-TYPE: {r.headers.get('Content-Type', '')}")
    print(f"SERVER: {r.headers.get('Server', '')}")
    r.raise_for_status()

    found = {}
    soup = BeautifulSoup(r.text, "html.parser")

    for a in soup.find_all("a", href=True):
        absolute = clean_url(urljoin(page_url, a["href"]))
        if ".pdf" in absolute.lower() and is_allowed(absolute):
            found[absolute] = {
                "title": " ".join(a.stripped_strings).strip(),
                "source": page_url,
            }

    raw = clean_url(r.text)
    for match in PDF_RE.findall(raw):
        match = clean_url(match)
        if is_allowed(match):
            found.setdefault(match, {"title": "", "source": page_url})

    if not found:
        preview = re.sub(r"\s+", " ", r.text).strip()[:1800]
        print("NO PDF ON SOURCE. BODY PREVIEW:")
        print(preview)

    return found


def pdf_text(data, max_pages=6):
    try:
        reader = PdfReader(io.BytesIO(data))
        chunks = []
        for page in reader.pages[:max_pages]:
            chunks.append(page.extract_text() or "")
        text = "\n".join(chunks)
        return re.sub(r"\s+", " ", text).strip()
    except Exception as e:
        print(f"PDF TEXT ERROR: {e}")
        return ""


def extract_dates(text):
    out = []
    for d, m, y in DATE_RE.findall(text):
        y = int(y)
        if y < 100:
            y += 2000
        value = f"{int(d):02d}.{int(m):02d}.{y:04d}"
        if value not in out:
            out.append(value)
    return out[:12]


def inspect_pdf(url, meta):
    r = fetch(url, timeout=60)
    r.raise_for_status()
    data = r.content
    text = pdf_text(data)

    return {
        "url": url,
        "title": meta.get("title", ""),
        "source": meta.get("source", ""),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "etag": r.headers.get("ETag", ""),
        "last_modified": r.headers.get("Last-Modified", ""),
        "content_type": r.headers.get("Content-Type", ""),
        "dates": extract_dates(text),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def relay_send(text):
    relay_url = os.environ.get("ALFA_RELAY_URL", "").strip()
    relay_key = os.environ.get("ALFA_RELAY_KEY", "").strip()
    if not relay_url or not relay_key:
        print("Telegram relay secrets are not configured; skipping notification")
        return

    r = requests.post(
        relay_url,
        headers={"X-Relay-Key": relay_key},
        json={"text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    print("RELAY", r.status_code, r.text[:300])
    r.raise_for_status()


def format_alert(kind, doc):
    icon = "🆕" if kind == "new" else "♻️"
    title = doc.get("title") or Path(urlparse(doc["url"]).path).name
    dates = ", ".join(doc.get("dates") or []) or "не извлечены"
    lm = doc.get("last_modified") or "нет"

    return (
        f"{icon} Alfa Monitor: {'новый PDF' if kind == 'new' else 'PDF изменён'}\n\n"
        f"{title}\n"
        f"Даты в документе: {dates}\n"
        f"Last-Modified: {lm}\n"
        f"Размер: {doc.get('size', 0)} байт\n\n"
        f"{doc['url']}"
    )


def main():
    state = load_state()
    previous = state.get("documents", {})

    discovered = {}
    for source in SOURCES:
        try:
            discovered.update(discover_from_page(source))
        except Exception as e:
            print(f"SOURCE ERROR {source}: {type(e).__name__}: {e}")

    print(f"DISCOVERED PDF: {len(discovered)}")
    if not discovered:
        raise RuntimeError("No PDFs discovered. Refusing to overwrite state with an empty scan.")

    current = {}
    events = []

    for idx, (url, meta) in enumerate(sorted(discovered.items()), 1):
        print(f"[{idx}/{len(discovered)}] {url}")
        try:
            doc = inspect_pdf(url, meta)
            current[url] = doc
        except Exception as e:
            print(f"PDF ERROR {url}: {type(e).__name__}: {e}")
            if url in previous:
                current[url] = previous[url]
            continue

        if not state.get("initialized", False):
            continue

        old = previous.get(url)
        if old is None:
            events.append(("new", doc))
        elif old.get("sha256") != doc.get("sha256"):
            events.append(("changed", doc))

    if not state.get("initialized", False):
        print(f"BASELINE: saved {len(current)} documents; no Telegram notifications")
    else:
        print(f"EVENTS: {len(events)}")
        for kind, doc in events:
            relay_send(format_alert(kind, doc))

    save_state({
        "initialized": True,
        "last_scan_utc": datetime.now(timezone.utc).isoformat(),
        "documents": current,
    })


if __name__ == "__main__":
    main()
