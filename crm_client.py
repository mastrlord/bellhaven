"""Thin client for the CRM sandbox API."""
import json
import os

import requests

from config import API_BASE, API_TOKEN, DATA_DIR


class CRM:
    def __init__(self, token=API_TOKEN):
        if not token:
            raise SystemExit("Set BH_API_TOKEN in your environment.")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _get(self, path, **params):
        r = self.s.get(f"{API_BASE}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def list_accounts(self, **filters):
        return self._list_all("/accounts", **filters)

    def list_contacts(self, **filters):
        return self._list_all("/contacts", **filters)

    def _list_all(self, path, **filters):
        """Page through a list endpoint and return every row."""
        rows, page = [], 1
        while True:
            body = self._get(path, page=page, page_size=100, **filters)
            data = body.get("data", [])
            rows.extend(data)
            if not data or len(rows) >= body.get("total", len(rows)) or len(data) < 100:
                return rows
            page += 1

    def get_account(self, account_id):
        return self._get(f"/accounts/{account_id}")

    def update_account(self, account_id, fields):
        r = self.s.patch(f"{API_BASE}/accounts/{account_id}", json=fields, timeout=30)
        r.raise_for_status()
        return r.json()

    def create_account(self, fields):
        r = self.s.post(f"{API_BASE}/accounts", json=fields, timeout=30)
        r.raise_for_status()
        return r.json()


if __name__ == "__main__":
    crm = CRM()
    accounts = crm.list_accounts()
    out = os.path.join(DATA_DIR, "crm_accounts.json")
    with open(out, "w") as f:
        json.dump(accounts, f, indent=2)
    print(f"Pulled {len(accounts)} accounts -> {out}")
