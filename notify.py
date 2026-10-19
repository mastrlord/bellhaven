"""Optional reviewer alerts. Does nothing unless BH_REVIEW_WEBHOOK_URL is set.

Posts a Slack-compatible {"text": ...} message to an incoming webhook, so a held
item reaches whoever does the research (e.g. calling a facility to confirm closure).
"""
import requests

from config import REVIEW_WEBHOOK_URL


def send_hold_alert(proposal, reason, link=""):
    if not REVIEW_WEBHOOK_URL:
        return "not configured"
    text = (f":mag: Needs research: {proposal['title']}\n"
            f"What to check: {reason}\n"
            + (f"CRM account: {link}" if link else ""))
    try:
        requests.post(REVIEW_WEBHOOK_URL, json={"text": text}, timeout=10).raise_for_status()
        return "sent"
    except requests.RequestException as e:  # an alert failure must never block the review itself
        return f"failed: {e}"
