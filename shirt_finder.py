#!/usr/bin/env python3
"""
Shirt Finder - hunts resale sites and the wider web for one specific lost shirt,
and emails / texts when it turns up.

The shirt: an all-over sublimation-print tee from Urban Outfitters (~2004-2008).
Cream base; a rainbow arc inside an ornate red/orange scrollwork frame; a man in a
cap riding a large golden fish (goldfish/carp) against a yellow sun, with
bicycle-spoke-like lines; pastel sun rays below; small hot-air balloons and
parachutes; faint graph-paper grid; blue-grey clouds on the shoulders.

Each run:
  1. Searches eBay (keyword search + eBay's own image search) - free, every run.
  2. Once a day: Google Lens reverse-image search + a rotating Google Images
     keyword search (via SerpAPI) - covers Poshmark, Depop, Mercari, Etsy,
     Grailed, blogs, etc.
  3. Sends every listing it hasn't seen before to Claude together with the
     reference photo, and asks whether it is the same design.
  4. Strong match  -> email + text.  Possible match -> email only.
  5. Remembers everything it has looked at (state/seen.json) so it never
     re-checks or re-notifies the same listing.

Configuration is entirely through environment variables (GitHub Actions secrets);
see README.md.

Usage:
  python shirt_finder.py               # normal run
  python shirt_finder.py --dry-run     # search + judge, but print instead of notifying
  python shirt_finder.py --test-notify # just send a test email + text
  python shirt_finder.py --selftest    # offline checks, no network
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parent
REFERENCE_IMAGE = ROOT / "reference" / "shirt.jpg"
STATE_FILE = ROOT / "state" / "seen.json"

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
HTTP_TIMEOUT = 25

# --------------------------------------------------------------------------- #
# Search terms
# --------------------------------------------------------------------------- #

# eBay keyword searches - free, run every time.
EBAY_QUERIES = [
    "urban outfitters goldfish shirt",
    "urban outfitters all over print tee rainbow",
    "urban outfitters sublimation tee hot air balloon",
    "all over print t-shirt man riding goldfish",
    "goldfish rainbow hot air balloon t-shirt",
    "vintage all over print tee goldfish balloon parachute",
    "urban outfitters all over print t-shirt fish",
    "y2k sublimation tee rainbow fish balloon",
]

# eBay category IDs for search_by_image: Men's T-Shirts, Women's T-Shirts.
EBAY_IMAGE_CATEGORIES = ["15687", "63869"]

# Google Images queries (SerpAPI) - one per day, rotating.
GOOGLE_QUERIES = [
    '"urban outfitters" all over print shirt goldfish rainbow balloon',
    'all over print tee man riding goldfish rainbow hot air balloon',
    'site:poshmark.com OR site:depop.com OR site:mercari.com urban outfitters all over print tee fish rainbow',
    'site:etsy.com OR site:grailed.com OR site:ebay.com vintage urban outfitters sublimation tee goldfish',
    'vintage 2000s urban outfitters graphic tee man riding fish rainbow',
    'sublimation t-shirt goldfish rider rainbow scroll frame balloons parachutes',
]

# Words that make a listing title worth a look when Claude verification is off.
DISTINCTIVE_WORDS = [
    "goldfish", "carp", "koi", "fish", "rainbow", "balloon", "parachute",
    "all over", "all-over", "allover", "sublimat", "aop",
]

SHIRT_DESCRIPTION = (
    "An all-over sublimation-print t-shirt sold by Urban Outfitters around 2004-2008. "
    "Cream/off-white base. The centrepiece is a rainbow arc inside an ornate red-orange "
    "scrollwork (rococo) frame; inside it a man in a cap rides a large golden fish "
    "(goldfish/carp) in front of a pale yellow sun, with thin spoke-like lines radiating "
    "like a bicycle wheel. Pastel orange/pink sun rays fan downward. Small vintage "
    "hot-air balloons (one with lettering on a band) and parachutes float around it, "
    "over a faint graph-paper grid. Blue-grey and pink clouds wrap onto the sleeves. "
    "Overall look: faded, pastel, vintage-collage / Victorian-engraving style."
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def normalize_url(url: str) -> str:
    """Stable key for a listing URL: drop tracking query strings and fragments."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    m = re.search(r"/itm/(?:[^/]+/)?(\d{9,})", path)
    if "ebay." in host and m:
        return f"ebay:{m.group(1)}"
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


class Candidate:
    def __init__(self, source: str, title: str, url: str, image_url: str | None, price: str | None = None):
        self.source = source
        self.title = (title or "").strip()
        self.url = url
        self.image_url = image_url
        self.price = price
        self.key = normalize_url(url)

    def __repr__(self) -> str:
        return f"<{self.source}: {self.title[:60]!r} {self.url}>"


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": {}, "matches": [], "last_lens_date": None, "last_google_date": None, "google_query_index": 0}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


def ebay_token() -> str | None:
    client_id, client_secret = env("EBAY_CLIENT_ID"), env("EBAY_CLIENT_SECRET")
    if not (client_id and client_secret):
        log("eBay: EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set - skipping eBay.")
        return None
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _ebay_items(payload: dict, source: str) -> list[Candidate]:
    out = []
    for it in payload.get("itemSummaries", []) or []:
        url = it.get("itemWebUrl")
        if not url:
            continue
        img = (it.get("image") or {}).get("imageUrl")
        price = it.get("price") or {}
        price_s = f"{price.get('value')} {price.get('currency')}" if price.get("value") else None
        out.append(Candidate(source, it.get("title", ""), url, img, price_s))
    return out


def search_ebay() -> list[Candidate]:
    try:
        token = ebay_token()
    except Exception as e:  # noqa: BLE001
        log(f"eBay auth failed: {e}")
        return []
    if not token:
        return []
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
    found: list[Candidate] = []

    for q in EBAY_QUERIES:
        try:
            r = requests.get(
                "https://api.ebay.com/buy/browse/v1/item_summary/search",
                headers=headers, params={"q": q, "limit": 50}, timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            items = _ebay_items(r.json(), "eBay")
            log(f"eBay '{q}': {len(items)} results")
            found += items
        except Exception as e:  # noqa: BLE001
            log(f"eBay search '{q}' failed: {e}")

    # eBay's visual search with our reference photo.
    image_b64 = base64.b64encode(REFERENCE_IMAGE.read_bytes()).decode()
    for cat in EBAY_IMAGE_CATEGORIES:
        try:
            r = requests.post(
                "https://api.ebay.com/buy/browse/v1/item_summary/search_by_image",
                headers={**headers, "Content-Type": "application/json"},
                params={"category_ids": cat, "limit": 50},
                json={"image": image_b64}, timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            items = _ebay_items(r.json(), "eBay image search")
            log(f"eBay image search (category {cat}): {len(items)} results")
            found += items
        except Exception as e:  # noqa: BLE001
            log(f"eBay image search (category {cat}) failed: {e}")
    return found


def lens_image_url() -> str | None:
    explicit = env("LENS_IMAGE_URL")
    if explicit:
        return explicit
    repo = env("GITHUB_REPOSITORY")
    branch = env("GITHUB_REF_NAME", "main")
    if repo:
        return f"https://raw.githubusercontent.com/{repo}/{branch}/reference/shirt.jpg"
    return None


def serpapi(params: dict) -> dict:
    r = requests.get(
        "https://serpapi.com/search.json",
        params={**params, "api_key": env("SERPAPI_KEY")}, timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data


def search_google_lens(state: dict, today: str, force: bool) -> list[Candidate]:
    if not env("SERPAPI_KEY"):
        log("Google Lens: SERPAPI_KEY not set - skipping.")
        return []
    if state.get("last_lens_date") == today and not force:
        log("Google Lens: already ran today.")
        return []
    url = lens_image_url()
    if not url:
        log("Google Lens: no public image URL (set LENS_IMAGE_URL) - skipping.")
        return []
    try:
        data = serpapi({"engine": "google_lens", "url": url, "country": "us", "hl": "en"})
    except Exception as e:  # noqa: BLE001
        log(f"Google Lens failed: {e}")
        return []
    state["last_lens_date"] = today
    out = []
    for m in data.get("visual_matches", []) or []:
        if m.get("link"):
            price = (m.get("price") or {}).get("value") if isinstance(m.get("price"), dict) else None
            out.append(Candidate(f"Google Lens ({m.get('source', 'web')})", m.get("title", ""),
                                 m["link"], m.get("image") or m.get("thumbnail"), price))
    log(f"Google Lens: {len(out)} visual matches")
    return out


def search_google_images(state: dict, today: str, force: bool) -> list[Candidate]:
    if not env("SERPAPI_KEY"):
        log("Google Images: SERPAPI_KEY not set - skipping.")
        return []
    if state.get("last_google_date") == today and not force:
        log("Google Images: already ran today.")
        return []
    idx = state.get("google_query_index", 0) % len(GOOGLE_QUERIES)
    q = GOOGLE_QUERIES[idx]
    try:
        data = serpapi({"engine": "google_images", "q": q, "gl": "us", "hl": "en"})
    except Exception as e:  # noqa: BLE001
        log(f"Google Images '{q}' failed: {e}")
        return []
    state["last_google_date"] = today
    state["google_query_index"] = idx + 1
    out = []
    for m in data.get("images_results", []) or []:
        if m.get("link"):
            out.append(Candidate(f"Google Images ({m.get('source', 'web')})", m.get("title", ""),
                                 m["link"], m.get("original") or m.get("thumbnail")))
    log(f"Google Images '{q}': {len(out)} results")
    return out


# --------------------------------------------------------------------------- #
# Judging
# --------------------------------------------------------------------------- #


def sniff_media_type(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def fetch_image(url: str | None) -> tuple[str, str] | None:
    """Download an image -> (media_type, base64) or None."""
    if not url:
        return None
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.content
    except Exception:  # noqa: BLE001
        return None
    media = sniff_media_type(data)
    if not media or len(data) > 4_500_000:
        return None
    return media, base64.b64encode(data).decode()


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["same_design", "possible", "no"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "verdict", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

JUDGE_SYSTEM = (
    "You help someone find a specific shirt they lost years ago. The first image is a "
    "photo of that exact shirt being worn. Description: " + SHIRT_DESCRIPTION + "\n\n"
    "You will then see numbered candidate listings (a photo and/or a title). For each "
    "candidate decide:\n"
    "- same_design: clearly the same printed artwork (same fish rider in the rainbow "
    "scroll frame, same composition) - any size, colour fade, or condition.\n"
    "- possible: shares several distinctive elements but the photo is too small, "
    "partial, or unclear to be sure; or there is no photo but the title describes this "
    "artwork very specifically.\n"
    "- no: a different shirt, even if it is a similar style.\n"
    "Be strict: generic rainbow, fish, or hot-air-balloon shirts are 'no'. Give a short "
    "reason for each."
)


def judge_with_claude(batch: list[Candidate], ref_b64: str) -> dict[int, tuple[str, str]]:
    import anthropic  # imported lazily so --selftest works without the package

    client = anthropic.Anthropic()
    content: list[dict] = [
        {"type": "text", "text": "Reference photo of the lost shirt:"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": ref_b64}},
    ]
    for i, c in enumerate(batch):
        content.append({"type": "text", "text": f"Candidate {i}: title={c.title!r} source={c.source}"})
        img = fetch_image(c.image_url)
        if img:
            content.append({"type": "image", "source": {"type": "base64", "media_type": img[0], "data": img[1]}})
        else:
            content.append({"type": "text", "text": f"(Candidate {i} has no usable photo - judge on the title only.)"})
    content.append({"type": "text", "text": f"Return a verdict for every candidate id 0-{len(batch) - 1}."})

    response = client.beta.messages.create(
        model=env("CLAUDE_MODEL", "claude-opus-5-5"),
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=JUDGE_SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined this batch: {getattr(response, 'stop_details', None)}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude response was cut off (max_tokens)")
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    return {r["id"]: (r["verdict"], r["reason"]) for r in data["results"] if 0 <= r["id"] < len(batch)}


def judge_by_keywords(c: Candidate) -> tuple[str, str]:
    """Fallback when no ANTHROPIC_API_KEY: crude title scoring, never claims a sure match."""
    t = c.title.lower()
    hits = sorted({w for w in DISTINCTIVE_WORDS if w in t})
    fishy = any(w in t for w in ("goldfish", "carp", "koi", "fish"))
    if fishy and len(hits) >= 3:
        return "possible", f"title mentions {', '.join(hits)}"
    if c.source.startswith("Google Lens") or c.source == "eBay image search":
        if len(hits) >= 2:
            return "possible", f"visual-search hit; title mentions {', '.join(hits)}"
    return "no", "not enough distinctive words in title"


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #


def send_email(to_addr: str, subject: str, body: str) -> None:
    sender, password = env("GMAIL_ADDRESS"), env("GMAIL_APP_PASSWORD")
    if not (sender and password):
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(sender, password)
        s.send_message(msg)


def notify(matches: list[tuple[Candidate, str, str]], dry_run: bool) -> None:
    strong = [m for m in matches if m[1] == "same_design"]
    subject = (f"FOUND IT? {len(strong)} likely match(es) for your shirt" if strong
               else f"{len(matches)} possible match(es) for your shirt")
    lines = []
    for c, verdict, reason in matches:
        tag = "LIKELY MATCH" if verdict == "same_design" else "possible"
        lines.append(f"[{tag}] {c.title or '(no title)'}\n  {c.url}\n  "
                     f"Found via: {c.source}" + (f" | Price: {c.price}" if c.price else "") +
                     f"\n  Why: {reason}\n")
    body = "Shirt Finder spotted:\n\n" + "\n".join(lines) + "\n- Shirt Finder"

    sms = None
    if strong:
        c = strong[0][0]
        sms = f"Shirt Finder: likely match! {c.url}" + (f" (+{len(strong) - 1} more, see email)" if len(strong) > 1 else "")

    if dry_run:
        log(f"[dry run] EMAIL '{subject}':\n{body}")
        if sms:
            log(f"[dry run] TEXT: {sms}")
        return

    email_to, sms_to = env("NOTIFY_EMAIL"), env("SMS_GATEWAY_ADDRESS")
    if email_to:
        send_email(email_to, subject, body)
        log(f"Emailed {len(matches)} match(es).")
    if sms and sms_to:
        send_email(sms_to, "", sms[:300])
        log("Sent text message.")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def run(dry_run: bool, force_daily: bool) -> int:
    state = load_state()
    today = dt.date.today().isoformat()

    candidates = search_ebay()
    candidates += search_google_lens(state, today, force_daily)
    candidates += search_google_images(state, today, force_daily)

    # De-duplicate and drop anything already judged.
    fresh: dict[str, Candidate] = {}
    for c in candidates:
        if c.key not in state["seen"] and c.key not in fresh:
            fresh[c.key] = c
    new = list(fresh.values())
    log(f"{len(candidates)} results total, {len(new)} new to check.")

    max_judge = int(env("MAX_CHECKS_PER_RUN", "80"))
    if len(new) > max_judge:
        log(f"Checking the first {max_judge}; the rest wait for the next run.")
        new = new[:max_judge]

    use_claude = bool(env("ANTHROPIC_API_KEY"))
    if not use_claude:
        log("ANTHROPIC_API_KEY not set - using keyword matching only (much less accurate).")
    ref_b64 = base64.b64encode(REFERENCE_IMAGE.read_bytes()).decode()

    hits: list[tuple[Candidate, str, str]] = []
    batch_size = 8
    for start in range(0, len(new), batch_size):
        batch = new[start:start + batch_size]
        if use_claude:
            try:
                verdicts = judge_with_claude(batch, ref_b64)
            except Exception as e:  # noqa: BLE001
                log(f"Claude check failed for a batch, will retry next run: {e}")
                continue  # not marked seen -> retried next run
        else:
            verdicts = {i: judge_by_keywords(c) for i, c in enumerate(batch)}
        for i, c in enumerate(batch):
            if i not in verdicts:
                continue  # retry next run
            verdict, reason = verdicts[i]
            state["seen"][c.key] = {"v": verdict, "d": today, "t": c.title[:120]}
            if verdict != "no":
                hits.append((c, verdict, reason))
                log(f"  {verdict.upper()}: {c} - {reason}")
        time.sleep(1)

    exit_code = 0
    if hits:
        try:
            notify(hits, dry_run)
            state["matches"] += [{"url": c.url, "title": c.title, "verdict": v, "reason": r, "date": today}
                                 for c, v, r in hits]
        except Exception as e:  # noqa: BLE001
            # Forget these so they're re-found and re-sent next run instead of being lost.
            log(f"NOTIFICATION FAILED - will retry next run: {e}")
            for c, _, _ in hits:
                state["seen"].pop(c.key, None)
            exit_code = 1  # makes the GitHub run show red, so the failure is visible
    else:
        log("No matches this run. Still looking.")
    # Daily heartbeat: keeps a commit landing at least once a day, so GitHub
    # doesn't auto-disable the schedule after 60 days without repo activity.
    state["last_run_date"] = today
    if not dry_run:
        save_state(state)
    return exit_code


def selftest() -> int:
    assert REFERENCE_IMAGE.exists(), "reference/shirt.jpg missing"
    assert sniff_media_type(REFERENCE_IMAGE.read_bytes()) == "image/jpeg"
    assert normalize_url("https://www.ebay.com/itm/Vintage-Tee/123456789012?hash=x") == "ebay:123456789012"
    assert normalize_url("https://www.ebay.com/itm/123456789012") == "ebay:123456789012"
    assert normalize_url("https://poshmark.com/listing/abc/?utm=1#x") == "https://poshmark.com/listing/abc"
    c1 = Candidate("eBay", "Vintage Urban Outfitters all over print goldfish rainbow balloon tee", "https://ebay.com/itm/111111111", None)
    c2 = Candidate("eBay", "Nike running shirt", "https://ebay.com/itm/222222222", None)
    assert judge_by_keywords(c1)[0] == "possible"
    assert judge_by_keywords(c2)[0] == "no"
    notify([(c1, "same_design", "selftest"), (c2, "possible", "selftest")], dry_run=True)
    json.dumps(VERDICT_SCHEMA)
    log("Selftest passed.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print notifications instead of sending")
    p.add_argument("--test-notify", action="store_true", help="send a test email and text, then exit")
    p.add_argument("--force-daily", action="store_true", help="run the once-a-day Google searches even if already run today")
    p.add_argument("--selftest", action="store_true", help="offline sanity checks")
    a = p.parse_args()

    if a.selftest:
        return selftest()
    if a.test_notify:
        if env("NOTIFY_EMAIL"):
            send_email(env("NOTIFY_EMAIL"), "Shirt Finder test", "This is a test. Shirt Finder can email you.")
            log("Test email sent.")
        if env("SMS_GATEWAY_ADDRESS"):
            send_email(env("SMS_GATEWAY_ADDRESS"), "", "Shirt Finder test: texts work.")
            log("Test text sent.")
        return 0
    return run(a.dry_run, a.force_daily)


if __name__ == "__main__":
    sys.exit(main())
