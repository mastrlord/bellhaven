"""Daily pipeline: scrape website -> pull CRM -> match -> queue proposals for review.

Read-only against the CRM. Writes happen only when a reviewer approves in app.py.
Safe to re-run: proposals that were already decided are never re-queued.
"""
import json
import os

import store
from config import DATA_DIR
from crm_client import CRM
from matcher import Matcher
from scraper import scrape


def main():
    locations = scrape()
    crm = CRM()
    accounts = crm.list_accounts()
    contacts = crm.list_contacts()
    for name, rows in (("website_locations", locations), ("crm_accounts", accounts), ("crm_contacts", contacts)):
        with open(os.path.join(DATA_DIR, f"{name}.json"), "w") as f:
            json.dump(rows, f, indent=2)

    proposals, confirmed = Matcher(locations, accounts, contacts).run()
    stats = store.record_run(proposals, confirmed, len(locations), len(accounts))

    print(f"Website locations: {len(locations)} | CRM accounts: {len(accounts)} | CRM contacts: {len(contacts)}")
    warned = [p["title"] for p in proposals if p.get("warnings")] + \
             [c["location"] for c in confirmed if c.get("warnings")]
    if warned:
        print(f"Contact warnings on {len(warned)} item(s): {', '.join(warned)}")
    print(f"Confirmed matches, no change needed: {len(confirmed)}")
    print(f"Proposals this run: {len(proposals)} -> new: {stats['new']}, "
          f"already decided (skipped): {stats['skipped_decided']}, pending marked stale: {stats['stale']}")


if __name__ == "__main__":
    main()
