"""Review app. Run with:  python app.py   then open http://127.0.0.1:5000

Each proposal shows its evidence and the exact changes. Approve writes to the CRM
through the API; Reject records the decision so the daily run never re-proposes it.
Nothing is written without an explicit approval.
"""
from flask import Flask, flash, redirect, render_template_string, request, url_for

import executor
import notify
import pipeline
import store
from config import API_TOKEN, BASE_URL

app = Flask(__name__)
app.secret_key = "local-review-app"

CATEGORY_LABELS = {
    "match_needs_fix": "Match needs a fix",
    "no_crm_account": "No CRM account yet",
    "duplicate": "Duplicate",
    "not_on_website": "Not on website anymore",
}
TABS = ["pending", "approved", "rejected", "held", "failed", "stale", "confirmed"]


def crm_link(account_id):
    return f"{BASE_URL}/crm/{API_TOKEN}/accounts/{account_id}" if account_id else ""


PAGE = """
<!doctype html><html><head><meta charset="utf-8"><title>Bellhaven CRM Review</title>
<style>
:root{--ink:#1f2328;--mute:#656d76;--line:#d8dee4;--bg:#f6f8fa;--green:#1a7f37;--red:#cf222e;--amber:#9a6700;--blue:#0969da}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg)}
header{background:#24292f;color:#fff;padding:14px 24px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
header h1{font-size:18px;margin:0}header small{color:#c9d1d9}
.wrap{max-width:1100px;margin:0 auto;padding:20px 24px 60px}
.tabs a{display:inline-block;padding:6px 12px;margin-right:6px;border-radius:6px;text-decoration:none;color:var(--ink);border:1px solid var(--line);background:#fff}
.tabs a.on{background:var(--blue);color:#fff;border-color:var(--blue)}
.flash{padding:10px 14px;border-radius:6px;margin:14px 0;background:#dafbe1;border:1px solid #aceebb}
.flash.err{background:#ffebe9;border-color:#ffcecb}
.card{background:#fff;border:1px solid var(--line);border-radius:8px;padding:16px 18px;margin:14px 0}
.card h3{margin:0 0 6px;font-size:16px}
.badge{display:inline-block;font-size:11px;padding:1px 8px;border-radius:10px;border:1px solid var(--line);margin-right:6px;color:var(--mute)}
.badge.k{background:#ddf4ff;border-color:#b6e3ff;color:#0550ae}
ul.ev{margin:8px 0;padding-left:18px;color:#3b434b}ul.ev li{margin:3px 0}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}
th,td{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}th{background:var(--bg)}
td.new{background:#dafbe1;font-weight:600}
.actions{display:flex;gap:8px;margin-top:10px}
button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid var(--line);cursor:pointer;background:#fff}
button.ok{background:var(--green);color:#fff;border-color:var(--green)}button.no{color:var(--red)}
button.run{background:#fff;color:#24292f}button.hold{color:var(--amber)}
.actions{flex-wrap:wrap;align-items:center}
details{margin-top:8px;color:var(--mute)}pre{white-space:pre-wrap;font-size:12px;background:var(--bg);padding:8px;border-radius:6px}
a{color:var(--blue)}.mute{color:var(--mute)}.result{font-size:13px;margin-top:8px}
</style></head><body>
{% macro cluster_table(rows, current) %}
<div class="mute" style="margin-top:8px"><b>All CRM accounts at this address ({{rows|length}})</b></div>
<table><tr><th>Account</th><th>Name</th><th>Street</th><th>Parent</th><th>Status</th><th>Revenue</th><th>AR</th><th>Role</th></tr>
{% for r in rows %}<tr{% if r.account_id == current %} style="background:#fff8c5"{% endif %}>
<td><a href="{{link(r.account_id)}}" target="_blank">{{r.account_id}}</a></td><td>{{r.name}}</td><td>{{r.street}}</td>
<td>{{r.parent}}</td><td>{{r.status}}</td><td>{{r.revenue}}</td><td>{{r.ar}}</td><td><b>{{r.role}}</b>{% if r.account_id == current %} (this card){% endif %}</td></tr>{% endfor %}
</table>{% endmacro %}
<header><div><h1>Bellhaven CRM Review Queue</h1>
<small>{% if run %}Last run {{run.run_at}} &middot; {{run.locations}} website locations &middot; {{run.accounts}} CRM accounts &middot; {{run.new_proposals}} new, {{run.skipped_decided}} skipped (already decided), {{run.marked_stale}} stale{% else %}No runs yet{% endif %}</small></div>
<form method="post" action="{{url_for('run')}}"><button class="run">Run pipeline now</button></form></header>
<div class="wrap">
<div class="tabs">{% for t in tabs %}<a href="?tab={{t}}" class="{{'on' if t==tab}}">{{t|capitalize}} ({{counts[t]}})</a>{% endfor %}</div>
{% with msgs = get_flashed_messages(with_categories=true) %}{% for cat,m in msgs %}<div class="flash {{'err' if cat=='error'}}">{{m|safe}}</div>{% endfor %}{% endwith %}

{% if tab == 'confirmed' %}
  <p class="mute">Website locations that matched a CRM account with nothing to change (formatting-only differences such as "Blvd" vs "Boulevard" are ignored).</p>
  {% for c in confirmed %}<div class="card"><h3>{{c.location}}</h3>
  <div class="mute">CRM: {{c.crm_name}} &middot; <a href="{{link(c.account_id)}}" target="_blank">{{c.account_id}}</a></div>
  {% for w in c.warnings or [] %}<div class="flash err">&#9888; {{w}}</div>{% endfor %}
  <ul class="ev">{% for e in c.evidence %}<li>{{e}}</li>{% endfor %}</ul>
  {% if c.cluster %}{{ cluster_table(c.cluster, c.account_id) }}{% endif %}</div>{% endfor %}
{% else %}
  {% for p in items %}{% set pl = p.payload %}
  <div class="card">
    <span class="badge">{{labels[p.category]}}</span><span class="badge k">{{p.kind}}</span>
    <h3>{{p.title}}</h3>
    {% if p.target_account_id %}<div class="mute">Account <a href="{{link(p.target_account_id)}}" target="_blank">{{p.target_account_id}}</a></div>{% endif %}
    {% for w in pl.warnings or [] %}<div class="flash err">&#9888; {{w}}</div>{% endfor %}
    <ul class="ev">{% for e in pl.evidence %}<li>{{e}}</li>{% endfor %}</ul>
    {% if pl.cluster %}{{ cluster_table(pl.cluster, p.target_account_id) }}{% endif %}
    {% if pl.comparison %}<table><tr><th>Field</th><th>Website</th><th>CRM now</th><th>Proposed</th></tr>
    {% for r in pl.comparison %}<tr><td>{{r[0]}}</td><td>{{r[1]}}</td><td>{{r[2]}}</td><td class="new">{{r[3]}}</td></tr>{% endfor %}</table>{% endif %}
    <details><summary>API operations</summary><pre>{{pl.ops|tojson(indent=2)}}</pre></details>
    {% if p.status == 'pending' %}
    <form method="post" class="actions">
      <input type="hidden" name="id" value="{{p.id}}">
      <button class="ok" formaction="{{url_for('approve')}}">Approve &amp; write to CRM</button>
      <button class="no" formaction="{{url_for('reject')}}">Reject</button>
      <input name="reason" placeholder="What needs checking? (for Hold)" style="flex:1;min-width:180px;font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:6px">
      <button class="hold" formaction="{{url_for('hold')}}">Hold for research</button>
    </form>
    {% else %}<div class="result"><b>{{p.status|capitalize}}</b> {{p.decided_at or ''}}
      {% if p.result %}
      {% if p.result.applied %}{% for a in p.result.applied %}<div>{{a.op}} &rarr; <a href="{{link(a.account_id)}}" target="_blank">{{a.account_id}}</a></div>{% endfor %}{% endif %}
      {% if p.result.error %}<div style="color:var(--red)">{{p.result.error}}</div>{% endif %}
      {% if p.result.reason %}<div class="mute">{{p.result.reason}}</div>{% endif %}
      {% if p.result.alert %}<div class="mute">Reviewer alert: {{p.result.alert}}</div>{% endif %}
      {% endif %}</div>
      {% if p.status in reopenable %}
      <form method="post" class="actions"><input type="hidden" name="id" value="{{p.id}}">
        <button formaction="{{url_for('reopen')}}">Reopen (back to pending)</button></form>
      {% endif %}
    {% endif %}
    {% set h = history(p.id) %}{% if h %}
    <details><summary>Decision history ({{h|length}})</summary>
      {% for e in h %}<div style="font-size:12px">{{e.at}} &middot; <b>{{e.action}}</b>{% if e.detail and e.detail.reason %} &middot; {{e.detail.reason}}{% endif %}{% if e.detail and e.detail.from %} (from {{e.detail.from}}){% endif %}</div>{% endfor %}
    </details>{% endif %}
  </div>
  {% else %}<p class="mute">Nothing here.</p>{% endfor %}
{% endif %}
</div></body></html>
"""


@app.route("/")
def index():
    tab = request.args.get("tab", "pending")
    allp = store.list_proposals()
    run = store.last_run()
    counts = {t: sum(1 for p in allp if p["status"] == t) for t in TABS}
    counts["confirmed"] = len(run["confirmed"]) if run else 0
    order = {"match_needs_fix": 0, "duplicate": 1, "no_crm_account": 2, "not_on_website": 3}
    items = sorted([p for p in allp if p["status"] == tab], key=lambda p: (order.get(p["category"], 9), p["title"]))
    return render_template_string(PAGE, tab=tab, tabs=TABS, items=items, counts=counts, run=run,
                                  confirmed=run["confirmed"] if run else [], labels=CATEGORY_LABELS, link=crm_link,
                                  reopenable=store.REOPENABLE, history=store.history)


@app.post("/approve")
def approve():
    pid = request.form["id"]
    try:
        log = executor.apply(pid)
        links = ", ".join(f'<a href="{crm_link(a["account_id"])}" target="_blank">{a["op"]} {a["account_id"]}</a>' for a in log)
        flash(f"Approved and written to CRM: {links}")
    except Exception as e:
        flash(f"Not applied: {e}", "error")
    return redirect(url_for("index", tab="pending"))


@app.post("/reject")
def reject():
    store.set_decision(request.form["id"], "rejected", {"reason": "Rejected by reviewer"})
    flash("Rejected. It will not be proposed again unless the underlying data changes.")
    return redirect(url_for("index", tab="pending"))


@app.post("/hold")
def hold():
    pid = request.form["id"]
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Add a short note on what needs checking before holding an item.", "error")
        return redirect(url_for("index", tab="pending"))
    rec = store.get_proposal(pid)
    alert = notify.send_hold_alert(rec["payload"], reason, crm_link(rec["target_account_id"]))
    store.set_decision(pid, "held", {"reason": reason, "alert": alert})
    flash(f"Held for research. Nothing was written to the CRM. Reviewer alert: {alert}.")
    return redirect(url_for("index", tab="pending"))


@app.post("/reopen")
def reopen():
    pid = request.form["id"]
    try:
        store.reopen(pid)
        flash("Reopened. The item is back in the pending queue.")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("index", tab="pending"))


@app.post("/run")
def run():
    pipeline.main()
    flash("Pipeline run complete.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=False, port=5000)
