"""
N-Labs Fellowship — GitHub PR → Notion Tracker
Flask endpoint that receives GitHub PR webhooks and syncs to Notion.
"""

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nlabs")

app = Flask(__name__)

WEBHOOK_SECRET    = os.environ.get("NLABS_WEBHOOK_SECRET", "")
NOTION_TOKEN      = os.environ.get("NOTION_TOKEN", "")
FELLOW_TRACKER_DB = os.environ.get("NOTION_FELLOW_TRACKER_DB_ID", "")

NOTION_VERSION = "2022-06-28"
NOTION_BASE    = "https://api.notion.com/v1"

def notion_headers():
    return {
        "Authorization":  f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type":   "application/json",
    }


# ─── signature verification ───────────────────────────────────────────────────

def verify_signature(body: bytes, signature_header: str) -> bool:
    """HMAC-SHA256 verification — same algorithm GitHub uses."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


# ─── Notion helpers ───────────────────────────────────────────────────────────

def find_fellow_by_github(github_username: str) -> dict | None:
    """Query Fellow Tracker for a record whose 'GitHub username' matches."""
    payload = {
        "filter": {
            "property": "GitHub username",
            "rich_text": {"equals": github_username},
        }
    }
    r = requests.post(
        f"{NOTION_BASE}/databases/{FELLOW_TRACKER_DB}/query",
        headers=notion_headers(),
        json=payload,
        timeout=10,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def get_current_counts(page: dict) -> tuple[int, int]:
    """Extract current PRs submitted and approved from a Notion page."""
    props = page.get("properties", {})
    def num(k):
        return props.get(k, {}).get("number") or 0
    return num("PRs submitted"), num("PRs approved")


def update_fellow_tracker(
    page_id:   str,
    submitted: int,
    approved:  int,
    event:     dict,
) -> dict:
    """Patch the Fellow Tracker page with updated PR counts and a note."""
    properties = {
        "PRs submitted": {"number": submitted},
        "PRs approved":  {"number": approved},
        "Status":        {"select": {"name": "Active"}},
        "Red flag":      {"select": {"name": "None"}},
    }
    r = requests.patch(
        f"{NOTION_BASE}/pages/{page_id}",
        headers=notion_headers(),
        json={"properties": properties},
        timeout=10,
    )
    r.raise_for_status()

    note = (
        f"[{event['event_time']}] {event['label']} — "
        f"PR #{event['pr_number']}: '{event['pr_title']}' "
        f"({event['additions']}+ / {event['deletions']}-) → {event['pr_url']}"
    )
    _append_note(page_id, note)
    return r.json()


def _append_note(page_id: str, text: str) -> None:
    """Append a timestamped bullet to the page body."""
    requests.patch(
        f"{NOTION_BASE}/blocks/{page_id}/children",
        headers=notion_headers(),
        json={
            "children": [{
                "object": "block",
                "type":   "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": text}}]
                },
            }]
        },
        timeout=10,
    )


def log_unknown_fellow(event: dict) -> None:
    log.warning("Unknown GitHub user '%s' — PR #%d not synced",
                event["author"], event["pr_number"])


# ─── main webhook endpoint ────────────────────────────────────────────────────

@app.route("/webhook/github-pr", methods=["POST"])
def github_pr_webhook():
    body = request.get_data()
    sig  = request.headers.get("X-Hub-Signature-256", "")

    if not verify_signature(body, sig):
        return jsonify({"error": "Invalid or missing webhook signature"}), 401

    if request.headers.get("X-GitHub-Event") != "pull_request":
        return jsonify({"ok": True, "skipped": "not a pull_request event"})

    try:
        event = json.loads(body)
    except Exception as exc:
        return jsonify({"error": f"Invalid JSON: {exc}"}), 422

    required = ["status","label","pr_number","pr_title","pr_url",
                "author","repo","branch","base_branch","merged",
                "commits","additions","deletions","event_time"]
    for field in required:
        if field not in event:
            return jsonify({"error": f"Missing field: {field}"}), 422

    log.info("PR #%d '%s' — status=%s — author=%s",
             event["pr_number"], event["pr_title"],
             event["status"], event["author"])

    fellow = find_fellow_by_github(event["author"])

    if fellow is None:
        log_unknown_fellow(event)
        return jsonify({
            "ok":     True,
            "synced": False,
            "reason": f"GitHub user '{event['author']}' not found in Fellow Tracker",
            "pr":     event["pr_number"],
        })

    page_id = fellow["id"]
    current_submitted, current_approved = get_current_counts(fellow)

    status = event["status"]
    if status == "submitted":
        new_submitted = current_submitted + 1
        new_approved  = current_approved
    elif status == "approved":
        new_submitted = current_submitted
        new_approved  = current_approved + 1
    else:  # rejected / updated
        new_submitted = current_submitted
        new_approved  = current_approved

    update_fellow_tracker(page_id, new_submitted, new_approved, event)

    approval_rate = round(new_approved / new_submitted * 100) if new_submitted else 0

    log.info("Synced: fellow='%s' | submitted=%d→%d | approved=%d→%d | rate=%d%%",
             event["author"],
             current_submitted, new_submitted,
             current_approved,  new_approved,
             approval_rate)

    return jsonify({
        "ok":            True,
        "synced":        True,
        "fellow_page":   page_id,
        "pr":            event["pr_number"],
        "status":        status,
        "prs_submitted": new_submitted,
        "prs_approved":  new_approved,
        "approval_rate": approval_rate,
    })


# ─── health check ─────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "status":  "ok",
        "service": "nlabs-pr-tracker",
        "time":    datetime.now(timezone.utc).isoformat(),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
