"""Match website locations to CRM accounts and produce reviewable proposals.

Nothing here writes to the CRM. Output is a list of proposals, each with:
  - a category (match_needs_fix, no_crm_account, not_on_website, duplicate)
  - human-readable evidence
  - the exact API operations that would run if a reviewer approves it

Matching rules, in order:
  1. Address match: same street number + normalized street name, and the same
     zip OR the same city/state (so a zip typo still matches and gets fixed).
  2. Fallback for unusable CRM addresses (PO Box / blank): same city/state and
     a strong name match (>= 0.80).
  3. Anything else is a new location. Near-misses (same name elsewhere, or same
     city at a different address) are shown as evidence but never auto-linked.

When several CRM accounts match one location, one survivor is kept and the
rest are proposed as duplicates of it.
"""
import hashlib
import json
import re
from datetime import date
from difflib import SequenceMatcher

from normalize import (is_po_box, map_care, name_similarity, norm_name,
                       norm_street, street_number)

BELLHAVEN_PARENT_NAME = "Bellhaven Senior Living (Parent Account)"
NAME_FALLBACK_THRESHOLD = 0.80
NEAR_MISS_NAME_THRESHOLD = 0.75


def is_parent_account(a):
    return "(Parent Account)" in (a.get("name") or "")


def has_billing_history(a):
    """SOP: revenue history AND outstanding AR > 0 means the old account must be preserved."""
    return (a.get("lifetime_revenue") or 0) > 0 and (a.get("outstanding_ar") or 0) > 0


def proposal_id(kind, key, ops):
    """Stable id from what the proposal would change. Notes are excluded because they
    carry the run date, which would otherwise make every daily run look like a new proposal."""
    stable = [{**op, "fields": {k: v for k, v in op.get("fields", {}).items() if k != "note"}} for op in ops]
    raw = json.dumps([kind, key, stable], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def digits(s):
    return re.sub(r"\D", "", s or "")


def money(v):
    return f"${v:,.0f}"


class Matcher:
    def __init__(self, locations, accounts, contacts=None, run_date=None):
        self.locations = locations
        self.accounts = accounts
        self.contacts = contacts or []
        self.by_id = {a["account_id"]: a for a in accounts}
        self.run_date = run_date or date.today().isoformat()
        parents = [a for a in accounts if a["name"] == BELLHAVEN_PARENT_NAME]
        if not parents:
            raise SystemExit("Bellhaven parent account not found in CRM")
        self.bh_parent = parents[0]["account_id"]
        # Facilities only. Accounts already marked as a duplicate were decided earlier.
        self.facilities = [a for a in accounts if not is_parent_account(a)]

    # ---------- helpers ----------
    def parent_name(self, a):
        p = self.by_id.get(a.get("parent_id") or "")
        return p["name"].replace(" (Parent Account)", "") if p else "(no parent)"

    def describe(self, a):
        return (f"{a['name']} [{a['account_id']}] at {a['billing_street']}, {a['billing_city']}, "
                f"{a['billing_state']} {a['billing_zip']} - parent: {self.parent_name(a)}, "
                f"status: {a['status']}, revenue {money(a['lifetime_revenue'])}, AR {money(a['outstanding_ar'])}")

    def addr_match(self, loc, a):
        if not a["billing_street"] or is_po_box(a["billing_street"]):
            return False
        if street_number(a["billing_street"]) != street_number(loc["street"]):
            return False
        if norm_street(a["billing_street"]) != norm_street(loc["street"]):
            return False
        same_zip = a["billing_zip"] == loc["zip"]
        same_city = (a["billing_city"].lower() == loc["city"].lower() and a["billing_state"] == loc["state"])
        return same_zip or same_city

    def name_fallback_match(self, loc, a):
        if a["billing_street"] and not is_po_box(a["billing_street"]):
            return False
        same_city = (a["billing_city"].lower() == loc["city"].lower() and a["billing_state"] == loc["state"])
        return same_city and name_similarity(a["name"], loc["name"]) >= NAME_FALLBACK_THRESHOLD

    # ---------- contacts: corroborating evidence only, never a match key ----------
    def contact_check(self, loc, matched_ids):
        """Compare the website's administrator and phone against CRM contacts.

        Returns (evidence_lines, warnings). A contact can confirm a match, or warn
        that the administrator sits on a different account than the one we matched.
        Names alone are not unique enough to link accounts, so this never changes
        which account is matched.
        """
        if not self.contacts:
            return [], []
        admin = norm_name(loc.get("administrator"))
        phone = digits(loc.get("phone"))
        hits = [c for c in self.contacts
                if (admin and norm_name(c["name"]) == admin) or (phone and digits(c["phone"]) == phone)]
        ev, warn = [], []
        for c in hits:
            acct = self.by_id.get(c["account_id"], {})
            via = "administrator name" if admin and norm_name(c["name"]) == admin else "phone"
            label = f"{c['name']} ({c['title'] or 'no title'}) on {acct.get('name', '?')} [{c['account_id']}]"
            if c["account_id"] in matched_ids:
                ev.append(f"Contact check: website {via} matches CRM contact {label}, the same account matched by address. Confirms the match.")
            else:
                warn.append(f"Contact check: website {via} matches CRM contact {label}, which is NOT the account matched here. "
                            f"Check whether this location already exists under that account before approving.")
        if not hits:
            who = loc.get("administrator") or "the administrator"
            ev.append(f"Contact check: no CRM contact matches {who} or {loc.get('phone') or 'the listed phone'}.")
        return ev, warn

    def contacts_on(self, account_id):
        return [c for c in self.contacts if c["account_id"] == account_id and c.get("is_active", True)]

    GENERIC = {"bellhaven", "of", "at", "the", "and", "senior", "living", "care", "center", "centre",
               "nursing", "rehabilitation", "rehab", "health", "manor", "village", "gardens", "home"}

    def shared_words(self, a, b):
        wa = set(norm_name(a).split()) - self.GENERIC
        wb = set(norm_name(b).split()) - self.GENERIC
        return wa & wb

    def core_similarity(self, a, b):
        """Name similarity with generic words removed, so 'Bellhaven of X' doesn't match every 'Bellhaven of Y'."""
        ca = " ".join(w for w in norm_name(a).split() if w not in self.GENERIC)
        cb = " ".join(w for w in norm_name(b).split() if w not in self.GENERIC)
        return SequenceMatcher(None, ca, cb).ratio() if ca and cb else 0.0

    def near_misses(self, loc, exclude):
        out = []
        for a in self.facilities:
            if a["account_id"] in exclude:
                continue
            sim = self.core_similarity(a["name"], loc["name"])
            same_city = a["billing_city"].lower() == loc["city"].lower() and a["billing_state"] == loc["state"]
            if sim >= NEAR_MISS_NAME_THRESHOLD:
                out.append(f"Similar name but different location, not linked: {self.describe(a)}")
            elif same_city and (sim >= 0.5 or self.shared_words(a["name"], loc["name"])):
                out.append(f"Same city, similar name, but different street address, not linked: {self.describe(a)}")
        return out

    def rank_survivor(self, loc, cands):
        return sorted(cands, key=lambda a: (
            a.get("parent_id") != self.bh_parent,           # already under Bellhaven first
            not (a["lifetime_revenue"] > 0),                 # then accounts with billing history
            a["status"] != "Active",
            norm_street(a["billing_street"]) != norm_street(loc["street"]) or a["billing_street"] != loc["street"],
            -name_similarity(a["name"], loc["name"]),
            a["account_id"],
        ))

    def website_fields(self, loc):
        care = map_care(loc["care_offerings"])
        return {
            "name": loc["name"],
            "billing_street": loc["street"],
            "billing_city": loc["city"],
            "billing_state": loc["state"],
            "billing_zip": loc["zip"],
            "care_type": care[0] if care else "",
            "phone": loc["phone"],
        }

    def field_diffs(self, loc, a):
        """Return {field: new_value} for real differences only (not formatting)."""
        diffs = {}
        if a["name"].strip().lower() != loc["name"].strip().lower():
            diffs["name"] = loc["name"]
        if is_po_box(a["billing_street"]) or not a["billing_street"] or \
                norm_street(a["billing_street"]) != norm_street(loc["street"]):
            diffs["billing_street"] = loc["street"]
        if a["billing_city"].lower() != loc["city"].lower():
            diffs["billing_city"] = loc["city"]
        if a["billing_state"] != loc["state"]:
            diffs["billing_state"] = loc["state"]
        if a["billing_zip"] != loc["zip"]:
            diffs["billing_zip"] = loc["zip"]
        care = map_care(loc["care_offerings"])
        if care and a["care_type"] not in care:
            diffs["care_type"] = care[0]
        if a["status"] != "Active":
            diffs["status"] = "Active"
        return diffs

    def make(self, kind, category, title, key, ops, evidence, comparison=None, target=None, slug=None):
        return {
            "id": proposal_id(kind, key, ops),
            "kind": kind,
            "category": category,
            "title": title,
            "target_account_id": target,
            "website_slug": slug,
            "evidence": evidence,
            "comparison": comparison or [],
            "ops": ops,
        }

    # ---------- main ----------
    def run(self):
        proposals, confirmed = [], []
        claimed = set()

        for loc in self.locations:
            src = f"Website: {loc['name']} at {loc['street']}, {loc['city']}, {loc['state']} {loc['zip']} " \
                  f"({', '.join(loc['care_offerings'])}) - {loc['url']}"
            cands = [a for a in self.facilities if self.addr_match(loc, a)]
            how = "address"
            if not cands:
                cands = [a for a in self.facilities if self.name_fallback_match(loc, a)]
                how = "name + city (CRM street is a PO Box or blank)"
            claimed.update(a["account_id"] for a in cands)

            if not cands:
                p = self.propose_new(loc, src)
                ev, warn = self.contact_check(loc, set())
                p["evidence"] += ev
                p["warnings"] = warn
                proposals.append(p)
                continue

            # Accounts already resolved on an earlier run
            decided_dups = [a for a in cands if a.get("duplicate_of_account")]
            chow_old = [a for a in cands if a.get("chow_current_account")]
            live = [a for a in cands if a not in decided_dups and a not in chow_old]
            for a in chow_old:  # follow CHOW pointers to the current account
                cur = self.by_id.get(a["chow_current_account"])
                if cur and cur not in live:
                    live.append(cur)
                    claimed.add(cur["account_id"])
            if not live:
                continue

            ranked = self.rank_survivor(loc, live)
            survivor, losers = ranked[0], ranked[1:]
            survivor_props = self.propose_survivor(loc, survivor, src, how, confirmed)
            dup_props = [self.propose_duplicate(loc, loser, survivor, src) for loser in losers]
            if len(cands) + len(chow_old) > 1:
                # Show reviewers the whole group of accounts at this address on every related card
                group = self.cluster_rows(survivor, losers, decided_dups, chow_old)
                for p in survivor_props + dup_props:
                    p["cluster"] = group
                if not survivor_props and confirmed and confirmed[-1]["account_id"] == survivor["account_id"]:
                    confirmed[-1]["cluster"] = group
            matched_ids = {a["account_id"] for a in cands} | {a["account_id"] for a in live}
            ev, warn = self.contact_check(loc, matched_ids)
            for p in survivor_props + dup_props:
                p["evidence"] += ev
                p["warnings"] = warn
            if not survivor_props and confirmed and confirmed[-1]["account_id"] == survivor["account_id"]:
                confirmed[-1]["evidence"] += ev
                confirmed[-1]["warnings"] = warn
            proposals.extend(survivor_props + dup_props)

        proposals.extend(self.propose_not_on_website(claimed))
        return proposals, confirmed

    # ---------- proposal builders ----------
    def propose_new(self, loc, src):
        fields = self.website_fields(loc)
        fields.update({"parent_id": self.bh_parent, "status": "Active"})
        offerings = ", ".join(loc["care_offerings"])
        fields["note"] = f"Created {self.run_date} from Bellhaven website listing ({loc['url']}). Care offerings: {offerings}."
        ev = [src, "No CRM account matched by address, and no PO Box/blank-address account matched by name in this city."]
        ev += self.near_misses(loc, set())
        comp = [[k, fields[k], "", fields[k]] for k in ("name", "billing_street", "billing_city", "billing_state", "billing_zip", "care_type", "parent_id")]
        return self.make("NEW", "no_crm_account", f"Create account: {loc['name']}", loc["slug"],
                         [{"op": "create", "ref": "new", "fields": fields}], ev, comp, slug=loc["slug"])

    def propose_survivor(self, loc, a, src, how, confirmed):
        out = []
        diffs = self.field_diffs(loc, a)
        needs_reparent = a.get("parent_id") != self.bh_parent
        base_ev = [src, f"Matched on {how}: {self.describe(a)}"]

        def comp_rows(changes, include_parent):
            rows = [[k, v, a.get(k, ""), v] for k, v in changes.items()]
            if include_parent:
                rows.append(["parent", "Bellhaven Senior Living", self.parent_name(a), "Bellhaven Senior Living"])
            return rows

        if needs_reparent and has_billing_history(a):
            # SOP: preserve old account untouched, create new under Bellhaven, link via chow_current_account
            fields = self.website_fields(loc)
            fields.update({"parent_id": self.bh_parent, "status": "Active",
                           "note": f"Created {self.run_date} via change of ownership from {a['account_id']} "
                                   f"({self.parent_name(a)}). Old account preserved for billing."})
            ev = base_ev + [
                f"Account sits under {self.parent_name(a)} but the facility is listed on the Bellhaven website.",
                f"SOP check: lifetime revenue {money(a['lifetime_revenue'])} > 0 AND outstanding AR "
                f"{money(a['outstanding_ar'])} > 0, so the old account must be preserved.",
                "Action: leave the old account exactly as-is, create a new account under Bellhaven, "
                "and set chow_current_account on the old account to the new account's id.",
            ]
            ops = [{"op": "create", "ref": "new", "fields": fields},
                   {"op": "patch", "account_id": a["account_id"], "fields": {"chow_current_account": "$new"}}]
            out.append(self.make("CHOW", "match_needs_fix", f"Change of ownership: {loc['name']}",
                                 a["account_id"], ops, ev, comp_rows({}, True) + [["chow_current_account", "", "", "(new account id)"]],
                                 target=a["account_id"], slug=loc["slug"]))
            return out

        changes = dict(diffs)
        ev = list(base_ev)
        if needs_reparent:
            changes["parent_id"] = self.bh_parent
            ev.append(f"Account sits under {self.parent_name(a)} but the facility is listed on the Bellhaven website.")
            ev.append(f"SOP check: lifetime revenue {money(a['lifetime_revenue'])}, outstanding AR "
                      f"{money(a['outstanding_ar'])}. Not both > 0, so re-parent the existing account directly.")
        if "name" in diffs:
            ev.append(f"Name differs: CRM '{a['name']}' vs website '{loc['name']}'. Website is treated as current.")
        if "billing_zip" in diffs:
            ev.append(f"Zip differs at the same street address: CRM {a['billing_zip']} vs website {loc['zip']}.")
        if "billing_street" in diffs:
            ev.append(f"Street differs: CRM '{a['billing_street']}' vs website '{loc['street']}'.")
        if "care_type" in diffs:
            ev.append(f"Care type '{a['care_type']}' is not among website offerings ({', '.join(loc['care_offerings'])}).")
        if len(loc["care_offerings"]) > 1:
            ev.append(f"Website lists multiple offerings ({', '.join(loc['care_offerings'])}); CRM holds one care_type, kept as-is if it matches one of them.")

        if not changes:
            confirmed.append({"location": loc["name"], "account_id": a["account_id"], "crm_name": a["name"],
                              "evidence": ev})
            return out

        kind = "REPARENT" if needs_reparent else "UPDATE"
        title = ("Re-parent to Bellhaven" if needs_reparent else "Update fields") + f": {a['name']}"
        out.append(self.make(kind, "match_needs_fix", title, a["account_id"],
                             [{"op": "patch", "account_id": a["account_id"], "fields": changes}],
                             ev, comp_rows({k: v for k, v in changes.items() if k != "parent_id"}, needs_reparent),
                             target=a["account_id"], slug=loc["slug"]))
        return out

    def cluster_rows(self, survivor, losers, decided_dups, chow_old):
        """One row per CRM account at the facility's address, with its role in this proposal set."""
        def row(a, role):
            return {"account_id": a["account_id"], "name": a["name"], "street": a["billing_street"],
                    "parent": self.parent_name(a), "status": a["status"],
                    "revenue": money(a["lifetime_revenue"]), "ar": money(a["outstanding_ar"]), "role": role}
        rows = [row(survivor, "Keep (survivor)")]
        rows += [row(a, "Mark duplicate" if not has_billing_history(a) else "Needs Review (has open AR)") for a in losers]
        rows += [row(a, "Already marked duplicate") for a in decided_dups]
        rows += [row(a, "Old account, linked via CHOW") for a in chow_old]
        return rows

    def propose_duplicate(self, loc, loser, survivor, src):
        ev = [src,
              f"Same facility address also matched: {self.describe(loser)}",
              f"Kept as surviving record: {self.describe(survivor)} "
              f"(preference: already under Bellhaven, then billing history, then closest to website)."]
        if has_billing_history(loser):
            ev.append(f"This copy has revenue {money(loser['lifetime_revenue'])} and AR {money(loser['outstanding_ar'])}; "
                      "flagging for review instead of inactivating so billing is not hidden.")
            fields = {"status": "Needs Review",
                      "note": f"{self.run_date}: likely duplicate of {survivor['account_id']} but has open AR. Confirm with billing before inactivating."}
        else:
            fields = {"duplicate_of_account": survivor["account_id"], "status": "Inactive",
                      "note": f"{self.run_date}: duplicate of {survivor['account_id']} ({survivor['name']}), same address. "
                              f"Facility is listed as {loc['name']} on the Bellhaven website."}
        on_loser = self.contacts_on(loser["account_id"])
        if on_loser:
            names = ", ".join(f"{c['name']} ({c['title'] or 'no title'})" for c in on_loser)
            ev.append(f"This copy has {len(on_loser)} active contact(s): {names}. They stay attached to it; "
                      f"consider moving them to {survivor['account_id']} (contact moves are out of scope for this run).")
        comp = [[k, "", loser.get(k, ""), v] for k, v in fields.items()]
        return self.make("DUPLICATE", "duplicate", f"Mark duplicate: {loser['name']}", loser["account_id"],
                         [{"op": "patch", "account_id": loser["account_id"], "fields": fields}], ev, comp,
                         target=loser["account_id"], slug=loc["slug"])

    def propose_not_on_website(self, claimed):
        out = []
        for a in self.facilities:
            if a["account_id"] in claimed or a.get("parent_id") != self.bh_parent:
                continue
            if a.get("duplicate_of_account") or a.get("chow_current_account") or a["status"] != "Active":
                continue  # already handled on an earlier run
            ev = [f"Under Bellhaven in CRM but not listed on the Bellhaven website: {self.describe(a)}"]
            # Another owner's account at the same address suggests the facility was sold
            others = [o for o in self.facilities if o["account_id"] != a["account_id"]
                      and o.get("parent_id") not in ("", self.bh_parent)
                      and a["billing_street"] and norm_street(o["billing_street"]) == norm_street(a["billing_street"])
                      and o["billing_zip"] == a["billing_zip"]]
            if others:
                new_owner_acct = others[0]
                ev.append(f"Another company's account exists at the same address: {self.describe(new_owner_acct)}. "
                          f"This indicates the facility was sold to {self.parent_name(new_owner_acct)}.")
                if has_billing_history(a):
                    ev.append(f"SOP check: revenue {money(a['lifetime_revenue'])} and AR {money(a['outstanding_ar'])} both > 0, "
                              "so the old account is preserved unchanged. The new owner's account already exists, so instead "
                              "of creating another one, chow_current_account points to it.")
                    fields = {"chow_current_account": new_owner_acct["account_id"]}
                    kind, title = "CHOW_OUT", f"Change of ownership (sold): {a['name']}"
                else:
                    ev.append("SOP check: not both revenue and AR > 0. The new owner's account already exists, so this "
                              "copy is marked as its duplicate rather than re-parented (re-parenting would create two "
                              "accounts for one facility under the same owner).")
                    fields = {"duplicate_of_account": new_owner_acct["account_id"], "status": "Inactive",
                              "note": f"{self.run_date}: facility sold to {self.parent_name(new_owner_acct)}; "
                                      f"current account is {new_owner_acct['account_id']}."}
                    kind, title = "DUPLICATE", f"Sold, mark duplicate: {a['name']}"
                comp = [[k, "", a.get(k, ""), v] for k, v in fields.items()]
                out.append(self.make(kind, "not_on_website", title, a["account_id"],
                                     [{"op": "patch", "account_id": a["account_id"], "fields": fields}], ev, comp,
                                     target=a["account_id"]))
            else:
                ev.append("No other account at this address, so there is no evidence of who owns it now "
                          "(closure, sale, or website omission are all possible). Flagging for a human to confirm "
                          "rather than inactivating or re-parenting.")
                fields = {"status": "Needs Review",
                          "note": f"{self.run_date}: no longer listed on Bellhaven website. Confirm closure or sale before "
                                  f"changing parent or inactivating."}
                comp = [[k, "", a.get(k, ""), v] for k, v in fields.items()]
                out.append(self.make("NOT_ON_SITE", "not_on_website", f"Flag for review: {a['name']}", a["account_id"],
                                     [{"op": "patch", "account_id": a["account_id"], "fields": fields}], ev, comp,
                                     target=a["account_id"]))
        return out
