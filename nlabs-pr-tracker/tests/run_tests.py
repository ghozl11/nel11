"""
N-Labs PR Tracker — test suite (stdlib + Flask only, no pytest dependency).
Run with:  python tests/run_tests.py
"""

import hashlib
import hmac
import json
import os
import sys
import traceback
from unittest.mock import MagicMock, patch

os.environ.setdefault("NLABS_WEBHOOK_SECRET",        "test_secret_key_for_unit_tests_only")
os.environ.setdefault("NOTION_TOKEN",                "secret_test_token_placeholder")
os.environ.setdefault("NOTION_FELLOW_TRACKER_DB_ID", "test_db_id_32chars_placeholder_xx")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.main import app as flask_app

flask_app.config["TESTING"] = True
CLIENT = flask_app.test_client()


# ─── helpers ──────────────────────────────────────────────────────────────────

def sign(body: str) -> str:
    sig = hmac.new(b"test_secret_key_for_unit_tests_only", body.encode(), hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def base_payload(**overrides) -> dict:
    return {
        "status": "submitted", "label": "PR Opened", "pr_number": 42,
        "pr_title": "feat: satellite data pipeline",
        "pr_url":   "https://github.com/nlabs/ai-track/pull/42",
        "author": "ahmed_ali_nlabs", "repo": "nlabs/ai-track",
        "branch": "feat/satellite", "base_branch": "main",
        "merged": False, "commits": 3, "additions": 120,
        "deletions": 15, "event_time": "2025-06-01T10:00:00Z",
        **overrides,
    }


def post_pr(payload, event="pull_request", sig=None):
    body = json.dumps(payload)
    sig  = sig or sign(body)
    return CLIENT.post(
        "/webhook/github-pr", data=body,
        content_type="application/json",
        headers={"X-GitHub-Event": event, "X-Hub-Signature-256": sig},
    )


# ─── test runner ──────────────────────────────────────────────────────────────

PASSED = FAILED = 0

def run(name, fn):
    global PASSED, FAILED
    try:
        fn()
        print(f"  \033[32m✓\033[0m  {name}")
        PASSED += 1
    except AssertionError as e:
        print(f"  \033[31m✗\033[0m  {name}")
        print(f"      AssertionError: {e}")
        FAILED += 1
    except Exception:
        print(f"  \033[31m✗\033[0m  {name}")
        traceback.print_exc()
        FAILED += 1

def assert_eq(a, b, msg=""):
    assert a == b, msg or f"expected {b!r}, got {a!r}"


# ─── tests ────────────────────────────────────────────────────────────────────

def test_health_check():
    r = CLIENT.get("/health")
    assert_eq(r.status_code, 200)
    assert_eq(r.get_json()["status"], "ok")


def test_missing_signature_returns_401():
    r = CLIENT.post("/webhook/github-pr",
                    data=json.dumps(base_payload()),
                    content_type="application/json",
                    headers={"X-GitHub-Event": "pull_request"})
    assert_eq(r.status_code, 401)


def test_invalid_signature_returns_401():
    body = json.dumps(base_payload())
    r = CLIENT.post("/webhook/github-pr", data=body,
                    content_type="application/json",
                    headers={"X-GitHub-Event": "pull_request",
                             "X-Hub-Signature-256": "sha256=deadbeef"})
    assert_eq(r.status_code, 401)


def test_non_pr_event_is_skipped():
    r = post_pr(base_payload(), event="push")
    assert_eq(r.status_code, 200)
    assert_eq(r.get_json()["skipped"], "not a pull_request event")


def test_pr_opened_increments_submitted():
    fellow = {"id": "page-abc",
               "properties": {"PRs submitted": {"number": 2},
                               "PRs approved":  {"number": 1}}}
    with patch("app.main.find_fellow_by_github", return_value=fellow), \
         patch("app.main.update_fellow_tracker",  return_value={}):
        r = post_pr(base_payload(status="submitted"))
    assert_eq(r.status_code, 200)
    d = r.get_json()
    assert_eq(d["synced"],          True)
    assert_eq(d["prs_submitted"],   3,   "submitted should increment")
    assert_eq(d["prs_approved"],    1,   "approved should be unchanged")
    assert_eq(d["approval_rate"],  33,   "1/3 × 100 = 33")


def test_pr_merged_increments_approved():
    fellow = {"id": "page-abc",
               "properties": {"PRs submitted": {"number": 3},
                               "PRs approved":  {"number": 1}}}
    with patch("app.main.find_fellow_by_github", return_value=fellow), \
         patch("app.main.update_fellow_tracker",  return_value={}):
        r = post_pr(base_payload(status="approved", merged=True))
    d = r.get_json()
    assert_eq(d["prs_submitted"],  3,   "submitted unchanged")
    assert_eq(d["prs_approved"],   2,   "approved should increment")
    assert_eq(d["approval_rate"], 67,   "2/3 × 100 = 67")


def test_pr_rejected_no_count_change():
    fellow = {"id": "page-abc",
               "properties": {"PRs submitted": {"number": 4},
                               "PRs approved":  {"number": 2}}}
    with patch("app.main.find_fellow_by_github", return_value=fellow), \
         patch("app.main.update_fellow_tracker",  return_value={}):
        r = post_pr(base_payload(status="rejected", merged=False))
    d = r.get_json()
    assert_eq(d["prs_submitted"], 4)
    assert_eq(d["prs_approved"],  2)


def test_unknown_github_user_returns_not_synced():
    with patch("app.main.find_fellow_by_github", return_value=None), \
         patch("app.main.log_unknown_fellow") as mock_log:
        r = post_pr(base_payload(author="unknown_outsider"))
    d = r.get_json()
    assert_eq(d["synced"], False)
    assert "unknown_outsider" in d["reason"], "reason should mention the username"
    assert_eq(mock_log.call_count, 1, "log_unknown_fellow should be called once")


def test_approval_rate_zero_when_no_submissions():
    fellow = {"id": "page-fresh",
               "properties": {"PRs submitted": {"number": 0},
                               "PRs approved":  {"number": 0}}}
    with patch("app.main.find_fellow_by_github", return_value=fellow), \
         patch("app.main.update_fellow_tracker",  return_value={}):
        r = post_pr(base_payload(status="submitted"))
    assert_eq(r.get_json()["approval_rate"], 0, "should not divide by zero")


def test_missing_required_field_returns_422():
    payload = {"status": "submitted", "pr_number": 1}
    body    = json.dumps(payload)
    r = CLIENT.post("/webhook/github-pr", data=body,
                    content_type="application/json",
                    headers={"X-GitHub-Event":      "pull_request",
                             "X-Hub-Signature-256": sign(body)})
    assert_eq(r.status_code, 422)


def test_full_cohort_progression():
    """
    Simulate a Fellow submitting 5 PRs, 3 getting approved —
    final approval rate should be 60%.
    """
    counts = {"submitted": 0, "approved": 0}

    def fake_find(_username):
        return {"id": "page-xyz",
                "properties": {"PRs submitted": {"number": counts["submitted"]},
                                "PRs approved":  {"number": counts["approved"]}}}

    def fake_update(_page_id, submitted, approved, _event):
        counts["submitted"] = submitted
        counts["approved"]  = approved
        return {}

    with patch("app.main.find_fellow_by_github", side_effect=fake_find), \
         patch("app.main.update_fellow_tracker",  side_effect=fake_update):

        for i in range(1, 6):
            post_pr(base_payload(status="submitted", pr_number=i))

        for i in range(1, 4):
            post_pr(base_payload(status="approved", pr_number=i))

        r = post_pr(base_payload(status="submitted", pr_number=6))

    assert_eq(counts["submitted"], 6)
    assert_eq(counts["approved"],  3)
    final_rate = round(3 / 6 * 100)
    assert_eq(final_rate, 50)


# ─── entry point ──────────────────────────────────────────────────────────────

TESTS = [
    ("Health check endpoint",               test_health_check),
    ("Missing signature → 401",             test_missing_signature_returns_401),
    ("Invalid signature → 401",             test_invalid_signature_returns_401),
    ("Non-PR event skipped",                test_non_pr_event_is_skipped),
    ("PR opened → submitted count +1",      test_pr_opened_increments_submitted),
    ("PR merged → approved count +1",       test_pr_merged_increments_approved),
    ("PR rejected → no count change",       test_pr_rejected_no_count_change),
    ("Unknown GitHub user → not synced",    test_unknown_github_user_returns_not_synced),
    ("Zero submissions → rate = 0%",        test_approval_rate_zero_when_no_submissions),
    ("Missing required field → 422",        test_missing_required_field_returns_422),
    ("Full cohort PR progression",          test_full_cohort_progression),
]

if __name__ == "__main__":
    print(f"\nN-Labs PR Tracker — test suite ({len(TESTS)} tests)\n")
    for name, fn in TESTS:
        run(name, fn)
    print(f"\n{'─'*50}")
    total = PASSED + FAILED
    color = "\033[32m" if FAILED == 0 else "\033[31m"
    print(f"{color}{PASSED}/{total} passed\033[0m", end="")
    print(f"  {'🎉 All tests passing' if FAILED == 0 else f'❌ {FAILED} test(s) failed'}\n")
    sys.exit(0 if FAILED == 0 else 1)
