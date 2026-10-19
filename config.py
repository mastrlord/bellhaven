"""Shared settings. The API token is read from the environment, never hard-coded."""
import os

BASE_URL = os.environ.get("BH_BASE_URL", "https://analyst-assessment-production.up.railway.app")
API_BASE = f"{BASE_URL}/api/v1"
API_TOKEN = os.environ.get("BH_API_TOKEN", "")

# Optional: Slack/Teams-style incoming webhook. When set, "Hold for research" posts an alert there.
REVIEW_WEBHOOK_URL = os.environ.get("BH_REVIEW_WEBHOOK_URL", "")

DATA_DIR =os.environ.get("BH_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
