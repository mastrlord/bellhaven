# Bellhaven CRM Sync

Keeps CRM accounts for Bellhaven Senior Living in line with the communities listed on Bellhaven's website. It scrapes the website, matches each location to the CRM, and queues proposed changes. Nothing is written to the CRM until a reviewer approves the change in a local review app.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
$env:BH_API_TOKEN = "<your token>"    # macOS/Linux: export BH_API_TOKEN=...
```

## Run

```powershell
python pipeline.py    # scrape + match + queue proposals (read-only against the CRM)
python app.py         # review app at http://127.0.0.1:5000
```

The review app also has a "Run pipeline now" button.

## How it fits together

| File | Role |
|---|---|
| `scraper.py` | Crawls the paginated `/communities` directory and the homepage (new locations can be announced there before they reach the directory), then reads each detail page. |
| `crm_client.py` | Thin API client with pagination (accounts and contacts). |
| `normalize.py` | Address, name and care-type normalization. |
| `matcher.py` | Matching rules and proposal building, including the CHOW SOP. No writes. |
| `store.py` | SQLite store of proposals and decisions; this is what makes re-runs safe. |
| `executor.py` | Applies an approved proposal; re-checks live data first. |
| `app.py` | Review UI: evidence, side-by-side comparison, approve / reject / hold / reopen. |
| `notify.py` | Optional team-channel alert when an item is held for research. |
| `.github/workflows/daily-sync.yml` | Daily schedule. |

## Matching approach

1. **Address first.** Same street number and normalized street name ("Boulevard" = "Blvd", "Northwest" = "NW"), plus the same zip **or** the same city/state. Requiring the street but allowing either zip or city means a zip typo still matches and gets corrected.
2. **Name fallback, narrowly.** Only when the CRM street is unusable (PO Box or blank), match on same city/state plus a strong name similarity (≥ 0.80).
3. **Never link on name alone.** Near-misses are shown as evidence but not linked. Examples: an "Amberly Manor" in Colorado under another company, or a "Union Square Senior Living" in the same city at a different address.
4. **Duplicates.** When several CRM accounts share one facility address, one survivor is kept. Preference order: already under Bellhaven, then has billing history, then active, then closest to the website record. The rest are marked `duplicate_of_account` = survivor and set Inactive. If a losing copy has open AR, it is set to Needs Review instead, so billing is not hidden.
5. **Contacts as a second, independent check.** Each website page lists an administrator and a phone number. These are compared to CRM contacts:
   - If the contact sits on the matched account, the card says the match is confirmed. This is true for 23 items, including renamed accounts like Riverbend Manor → Bellhaven of Chagrin Falls.
   - If the contact sits on a *different* account, the card shows a warning to check before approving.
   - New-location cards state that no contact matches, which rules out an existing account under another name and address.
   - Duplicate cards list any contacts on the copy being retired.
   - Contacts never decide a match, since names like "Chris Pike" aren't unique enough.
6. **Formatting-only differences are ignored**, so the queue only holds real changes. The website is treated as the source of truth for name, address, and care type.

## Proposal types

| Type | When | Action |
|---|---|---|
| UPDATE | Matched under Bellhaven, real field difference | Patch the fields |
| REPARENT | Matched under another parent; not (revenue > 0 AND AR > 0) | Patch `parent_id` (plus any field fixes) |
| CHOW | Matched under another parent; revenue > 0 AND AR > 0 | Leave the old account untouched, create a new account under Bellhaven, set old `chow_current_account` = new id |
| DUPLICATE | Extra account at the same facility address | `duplicate_of_account` + Inactive + note |
| NEW | Website location with no CRM match | Create under Bellhaven |
| CHOW_OUT | Under Bellhaven, not on website, and another company's account exists at the same address (sold) with revenue AND AR | Leave old untouched, point `chow_current_account` to the existing new-owner account rather than creating a duplicate |
| NOT_ON_SITE | Under Bellhaven, not on website, no evidence of a new owner | Needs Review + note; no re-parent or inactivation without confirmation |

## Review actions

| Button | Effect |
|---|---|
| Approve & write to CRM | Runs the proposal's API operations after re-checking live data |
| Reject | Records the decision; never re-proposed unless the underlying data changes |
| Hold for research | Requires a note on what to check. Writes nothing to the CRM and moves the item to the Held tab. Optionally alerts a team channel |
| Reopen | Sends a rejected, held, or failed item back to pending. Approved items can't be reopened, since their change is already in the CRM |

Every decision is kept in an audit trail, shown as "Decision history" on each card.

To turn on Hold alerts, set `BH_REVIEW_WEBHOOK_URL` to a Slack or Teams incoming webhook. It's off by default, and a failed alert never blocks the review.

## Re-run safety

- A proposal's id is a hash of what it would change (notes excluded, since they carry dates).
- Ids that already exist in the store are skipped, so approved, rejected and failed items are never re-queued.
- Approved changes land in the CRM, so the next run sees them as already correct.
- Pending proposals the latest run no longer produces are marked **stale**, so nobody approves a change built on outdated data.
- On approval, the executor re-reads the live account. It refuses a direct re-parent if the account now has revenue and AR, and refuses to set a CHOW or duplicate link that already exists.
- Tested: after simulating approval of every proposal, a second run produces zero proposals and confirms all 35 locations.

## Schedule

GitHub Actions (`.github/workflows/daily-sync.yml`) runs daily at 11:00 UTC. Equivalent cron entry:

```
0 6 * * * cd /opt/bellhaven && BH_API_TOKEN=... .venv/bin/python pipeline.py >> logs/sync.log 2>&1
```

## Deliberate choices

- **Phone numbers are not synced.** They differ for most matched accounts, the brief scopes the fields to name, address and care offerings, and syncing them would flood the queue. New accounts do take the website phone.
- **Multiple care offerings.** The CRM holds one `care_type`. If the current value matches any website offering it is kept; otherwise the first listed offering is used. Offerings are recorded in the note on new accounts.
- **Accounts no longer on the website** are flagged Needs Review rather than inactivated, unless there is direct evidence of a sale (another owner's account at the same address).
