"""Apply an approved proposal to the CRM. Only called from the review app on approval.

Before writing, it re-reads the live account and re-checks the conditions the
proposal was built on, in case the CRM changed since the pipeline ran.
"""
from crm_client import CRM
from matcher import has_billing_history

import store


class PreconditionFailed(Exception):
    pass


def check_preconditions(crm, proposal):
    kind = proposal["kind"]
    target = proposal.get("target_account_id")
    if not target:
        return
    live = crm.get_account(target)
    if kind == "REPARENT" and has_billing_history(live):
        raise PreconditionFailed(
            f"Account now has revenue {live['lifetime_revenue']} and AR {live['outstanding_ar']}; "
            "the SOP requires a change of ownership instead of a direct re-parent. Re-run the pipeline.")
    if kind in ("CHOW", "CHOW_OUT") and live.get("chow_current_account"):
        raise PreconditionFailed(f"Account already has chow_current_account={live['chow_current_account']}.")
    if kind == "DUPLICATE" and live.get("duplicate_of_account"):
        raise PreconditionFailed(f"Account already marked duplicate_of_account={live['duplicate_of_account']}.")


def apply(proposal_id):
    rec = store.get_proposal(proposal_id)
    if not rec or rec["status"] != "pending":
        raise ValueError("Proposal is not pending")
    proposal = rec["payload"]
    crm = CRM()
    try:
        check_preconditions(crm, proposal)
        refs, log = {}, []
        for op in proposal["ops"]:
            fields = {k: (refs[v[1:]] if isinstance(v, str) and v.startswith("$") else v)
                      for k, v in op["fields"].items()}
            if op["op"] == "create":
                created = crm.create_account(fields)
                created = created.get("data", created)
                refs[op["ref"]] = created["account_id"]
                log.append({"op": "create", "account_id": created["account_id"], "fields": fields})
            elif op["op"] == "patch":
                crm.update_account(op["account_id"], fields)
                log.append({"op": "patch", "account_id": op["account_id"], "fields": fields})
        store.set_decision(proposal_id, "approved", {"applied": log})
        return log
    except Exception as e:  # record failures so they're visible and not silently retried
        store.set_decision(proposal_id, "failed", {"error": str(e)})
        raise
