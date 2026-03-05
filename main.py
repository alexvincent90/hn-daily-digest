"""
HN Daily Digest — main.py
Fetches top Hacker News stories, summarizes with Claude, sends via Resend.
Run daily via GitHub Actions cron.
"""

import os
import json
import logging
import requests
from datetime import datetime, timezone
import anthropic
import resend

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config (all from environment variables / GitHub Secrets) ──────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
RESEND_API_KEY      = os.environ["RESEND_API_KEY"]
FROM_EMAIL          = os.environ.get("FROM_EMAIL", "digest@yourdomain.com")
FROM_NAME           = os.environ.get("FROM_NAME",  "HN Daily Digest")
TOP_N               = int(os.environ.get("TOP_N", "10"))


# ── 1. Fetch top HN stories ────────────────────────────────────────────────────
def fetch_top_stories(n: int = 10) -> list[dict]:
    """Pull top stories from HN Algolia API (no auth needed)."""
    url = "https://hn.algolia.com/api/v1/search"
    params = {"tags": "front_page", "hitsPerPage": n}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    hits = r.json().get("hits", [])
    stories = []
    for h in hits:
        stories.append({
            "id":       h["objectID"],
            "title":    h.get("title", "(no title)"),
            "url":      h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
            "hn_url":   f"https://news.ycombinator.com/item?id={h['objectID']}",
            "points":   h.get("points", 0),
            "comments": h.get("num_comments", 0),
        })
    log.info("Fetched %d stories from HN", len(stories))
    return stories


# ── 2. Summarize with Claude ───────────────────────────────────────────────────
def summarize_stories(stories: list[dict]) -> list[dict]:
    """Ask Claude for a one-liner on each story. Batched in one call."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt_lines = "\n".join(
        f"{i+1}. Title: {s['title']}\n   URL: {s['url']}" for i, s in enumerate(stories)
    )
    system = (
        "You are writing a daily tech newsletter for senior engineers and builders. "
        "For each story, write exactly ONE punchy sentence (max 20 words) that captures "
        "why it's interesting or surprising. Be direct and opinionated. No hype. "
        "Return ONLY a JSON array of strings, one per story, in the same order."
    )
    user = f"Summarize these {len(stories)} HN stories:\n\n{prompt_lines}"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",   # cheap + fast for summaries
        max_tokens=600,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = response.content[0].text.strip()

    # Strip markdown fences if Claude wraps in ```json
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    summaries = json.loads(raw)
    for s, summary in zip(stories, summaries):
        s["summary"] = summary
    log.info("Summaries generated")
    return stories


# ── 3. Build HTML email ────────────────────────────────────────────────────────
def build_email(stories: list[dict], date_str: str) -> tuple[str, str]:
    """Returns (subject, html_body)."""
    subject = f"🔥 HN Digest — {date_str}: {stories[0]['title'][:50]}…"

    items_html = ""
    for i, s in enumerate(stories, 1):
        items_html += f"""
        <div style="margin-bottom:28px;padding-bottom:20px;border-bottom:1px solid #f4f4f4">
          <div style="font-size:11px;color:#aaa;margin-bottom:5px;letter-spacing:.4px;text-transform:uppercase">
            #{i} &nbsp;·&nbsp; ▲ {s['points']:,} pts &nbsp;·&nbsp; 💬 {s['comments']:,}
          </div>
          <div style="font-size:17px;font-weight:700;line-height:1.3;margin-bottom:6px">
            <a href="{s['url']}" style="color:#1a1a1a;text-decoration:none">{s['title']}</a>
          </div>
          <div style="font-size:14px;color:#555;line-height:1.5;margin-bottom:7px">{s.get('summary', '')}</div>
          <a href="{s['hn_url']}" style="font-size:12px;color:#ff6600;font-weight:600;text-decoration:none">
            Read discussion →
          </a>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="background:#fff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;padding:30px 20px;color:#1a1a1a">

  <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:30px">
    <tr>
      <td>
        <span style="font-size:22px;font-weight:800;color:#ff6600">🔥 HN Daily Digest</span>
        <div style="font-size:13px;color:#888;margin-top:3px">{date_str} &nbsp;·&nbsp; Top {len(stories)} from Hacker News</div>
      </td>
    </tr>
  </table>

  {items_html}

  <hr style="border:none;border-top:1px solid #eee;margin:30px 0">
  <p style="font-size:11px;color:#bbb;text-align:center;line-height:1.6">
    You're receiving this because you subscribed to HN Daily Digest.<br>
    <a href="{{{{unsubscribe_url}}}}" style="color:#bbb">Unsubscribe</a> &nbsp;·&nbsp; Sent via Resend
  </p>
</body>
</html>"""
    return subject, html


# ── 4. Fetch all subscribers from Resend Audience ─────────────────────────────
def get_audience_id() -> str:
    """Auto-fetch the first Resend audience ID — no env var needed."""
    r = requests.get(
        "https://api.resend.com/audiences",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        timeout=10,
    )
    r.raise_for_status()
    audiences = r.json().get("data", [])
    if not audiences:
        raise ValueError("No Resend audiences found. Create one at resend.com/audiences.")
    audience_id = audiences[0]["id"]
    log.info("Using audience: %s (%s)", audiences[0].get("name", "unnamed"), audience_id)
    return audience_id

def get_subscribers() -> list[str]:
    """Pull confirmed contacts from Resend Audience."""
    resend.api_key = RESEND_API_KEY
    audience_id = get_audience_id()
    contacts = resend.Contacts.list(audience_id=audience_id)
    emails = [c["email"] for c in contacts.get("data", []) if not c.get("unsubscribed", False)]
    log.info("Found %d active subscribers", len(emails))
    return emails


# ── 5. Send email ──────────────────────────────────────────────────────────────
def send_digest(subject: str, html: str, subscribers: list[str]) -> None:
    resend.api_key = RESEND_API_KEY

    if not subscribers:
        log.warning("No subscribers — sending test to FROM_EMAIL")
        subscribers = [FROM_EMAIL]

    # Resend batch send (max 100 per call)
    BATCH_SIZE = 100
    for i in range(0, len(subscribers), BATCH_SIZE):
        batch = subscribers[i:i + BATCH_SIZE]
        params = resend.Emails.SendParams(
            from_=f"{FROM_NAME} <{FROM_EMAIL}>",
            to=batch,
            subject=subject,
            html=html,
        )
        result = resend.Emails.send(params)
        log.info("Batch %d: sent to %d recipients — id=%s", i // BATCH_SIZE + 1, len(batch), result.get("id"))


# ── Entrypoint ─────────────────────────────────────────────────────────────────
def main():
    date_str = datetime.now(timezone.utc).strftime("%B %-d, %Y")
    log.info("Starting HN Daily Digest for %s", date_str)

    try:
        stories = fetch_top_stories(TOP_N)
        stories = summarize_stories(stories)
        subject, html = build_email(stories, date_str)
        subscribers = get_subscribers()
        send_digest(subject, html, subscribers)
        log.info("Done ✓")
    except Exception as e:
        log.exception("Fatal error: %s", e)
        raise   # fail the GitHub Action so you get an email alert


if __name__ == "__main__":
    main()
