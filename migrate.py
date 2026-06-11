"""QuickBooks Online migration: parse QBO report CSV exports into ShopBooks.

QBO has no single full-export; migration runs off four report CSVs:
  1. Account List report          -> chart of accounts (name + type)
  2. Transaction Detail by Account-> staged transactions (Review queue), categories
                                     preserved via the Split column
  3. Customers export             -> customers
  4. Mileage export               -> mileage log
plus manually-entered opening balances (posted against Owner's Equity).

QBO reports are "grouped" CSVs: title rows, section headers, Total rows, blank
lines. Parsers find the real header row, then keep only rows with a parseable
date / required fields.
"""
import csv
import io

from ledger import normalize_date, parse_amount_to_cents

# QBO "Type" column on the Account List report -> ShopBooks (type, kind)
QBO_TYPE_MAP = {
    "bank": ("asset", "bank"),
    "credit card": ("liability", "card"),
    "accounts receivable": ("asset", "category"),
    "other current assets": ("asset", "category"),
    "other current asset": ("asset", "category"),
    "fixed assets": ("asset", "category"),
    "fixed asset": ("asset", "category"),
    "other assets": ("asset", "category"),
    "accounts payable": ("liability", "category"),
    "other current liabilities": ("liability", "category"),
    "other current liability": ("liability", "category"),
    "long term liabilities": ("liability", "category"),
    "long-term liabilities": ("liability", "category"),
    "credit card payable": ("liability", "category"),
    "equity": ("equity", "category"),
    "income": ("income", "category"),
    "other income": ("income", "category"),
    "expenses": ("expense", "category"),
    "expense": ("expense", "category"),
    "other expense": ("expense", "category"),
    "other expenses": ("expense", "category"),
    "cost of goods sold": ("expense", "category"),
}


def _rows(raw_bytes):
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    return list(csv.reader(io.StringIO(text)))


def _header_index(rows, *required):
    """Find the first row containing all required header names (case-insensitive).

    A header row must have at least two non-empty cells, otherwise QBO's
    one-cell report-title rows ("Customers", "Account List") false-match.
    """
    for i, row in enumerate(rows):
        lowered = [c.strip().lower() for c in row]
        if sum(1 for c in lowered if c) < 2:
            continue
        if all(any(req in cell for cell in lowered) for req in required):
            return i, lowered
    return None, None


def _col(headers, *candidates, exclude=()):
    for cand in candidates:
        for i, h in enumerate(headers):
            if h == cand and not any(x in h for x in exclude):
                return i
    for cand in candidates:
        for i, h in enumerate(headers):
            if cand in h and not any(x in h for x in exclude):
                return i
    return None


def _get(row, idx):
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def leaf_name(qbo_name):
    """QBO sub-accounts come as 'Expenses:Office:Supplies' - keep the leaf."""
    return qbo_name.split(":")[-1].strip()


# ---------- 1. Account List ----------

def parse_accounts(raw_bytes):
    """Returns list of {name, type, kind} from a QBO Account List report CSV."""
    rows = _rows(raw_bytes)
    hi, headers = _header_index(rows, "name", "type")
    if hi is None:
        raise ValueError("Couldn't find the header row (needs Account name and Type columns). "
                         "Export the 'Account List' report to CSV.")
    name_i = _col(headers, "account name", "full name", "name")
    type_i = _col(headers, "type", exclude=("detail",))
    out, seen = [], set()
    for row in rows[hi + 1:]:
        name = leaf_name(_get(row, name_i))
        qtype = _get(row, type_i).lower()
        if not name or not qtype or qtype not in QBO_TYPE_MAP:
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        typ, kind = QBO_TYPE_MAP[qtype]
        out.append({"name": name, "type": typ, "kind": kind})
    if not out:
        raise ValueError("No accounts recognized in that file.")
    return out


def import_accounts(con, parsed):
    """Create any accounts that don't exist yet (matched case-insensitively). Returns (created, matched)."""
    created = matched = 0
    for a in parsed:
        row = con.execute("SELECT id FROM accounts WHERE lower(name)=lower(?)", (a["name"],)).fetchone()
        if row:
            matched += 1
        else:
            con.execute("INSERT INTO accounts(name,type,kind) VALUES(?,?,?)",
                        (a["name"], a["type"], a["kind"]))
            created += 1
    return created, matched


# ---------- 2. Transaction Detail by Account ----------

def account_lookup(con):
    """name(lower) -> row, for both full and leaf matching."""
    return {r["name"].lower(): r for r in con.execute("SELECT * FROM accounts WHERE active=1")}


def _match_account(lookup, qbo_name):
    if not qbo_name:
        return None
    return lookup.get(qbo_name.strip().lower()) or lookup.get(leaf_name(qbo_name).lower())


def parse_transactions(con, raw_bytes):
    """Parse a Transaction Detail by Account CSV (with an Account column).

    Keeps only rows whose Account is a ShopBooks bank/card account (rows on
    income/expense accounts are the same transactions seen from the other side).
    Returns (txns_by_source_account_id, skipped_counts) where each txn is
    {date, description, amount_cents, category_id} in ShopBooks staged convention
    (positive = money out).
    """
    rows = _rows(raw_bytes)
    hi, headers = _header_index(rows, "date", "amount")
    if hi is None:
        raise ValueError("Couldn't find the header row (needs Date and Amount). Export "
                         "'Transaction Detail by Account' to CSV with Date, Account, Split, Amount columns.")
    date_i = _col(headers, "date", exclude=("created", "due", "modified"))
    acct_i = _col(headers, "account", exclude=("#", "number"))
    split_i = _col(headers, "split")
    amt_i = _col(headers, "amount", exclude=("open", "foreign"))
    name_i = _col(headers, "name", exclude=("account", "full",))
    memo_i = _col(headers, "memo/description", "memo", "description")
    type_i = _col(headers, "transaction type", "type")
    if acct_i is None:
        raise ValueError("No 'Account' column - in QBO, Customize the report and add the Account column, "
                         "then export again.")
    lookup = account_lookup(con)
    by_source = {}
    skipped = {"not_bank_card": 0, "unparseable": 0, "total_rows": 0}
    for row in rows[hi + 1:]:
        date_raw = _get(row, date_i)
        if not date_raw:
            continue
        try:
            date = normalize_date(date_raw)
        except ValueError:
            continue  # section header / total / beginning-balance rows
        skipped["total_rows"] += 1
        src = _match_account(lookup, _get(row, acct_i))
        if not src or src["kind"] not in ("bank", "card"):
            skipped["not_bank_card"] += 1
            continue
        try:
            qbo_cents = parse_amount_to_cents(_get(row, amt_i))
        except ValueError:
            skipped["unparseable"] += 1
            continue
        # QBO amount sign is relative to the account: positive = account increased.
        # Staged convention: positive = money out of your pocket.
        #   bank (asset): increase = money in  -> flip
        #   card (liability): increase = charge = money out -> keep
        cents = qbo_cents if src["type"] == "liability" else -qbo_cents
        split_name = _get(row, split_i)
        cat = None
        if split_name and split_name.lower() not in ("-split-", "split", ""):
            cat_row = _match_account(lookup, split_name)
            if cat_row and cat_row["id"] != src["id"]:
                cat = cat_row["id"]
        desc = " - ".join(x for x in (_get(row, name_i), _get(row, memo_i)) if x) \
               or _get(row, type_i) or "QBO import"
        by_source.setdefault(src["id"], []).append(
            {"date": date, "description": desc, "amount_cents": cents, "category_id": cat})
    if not any(by_source.values()):
        raise ValueError("No usable rows found. Check the file has an Account column and "
                         "that your bank/card accounts were created in step 1.")
    return by_source, skipped


def import_transactions(con, by_source, filename):
    """Stage parsed transactions, one batch per source account. Returns staged count."""
    staged = 0
    for source_id, txns in by_source.items():
        cur = con.execute("INSERT INTO batches(filename,account_id) VALUES(?,?)",
                          (f"QBO: {filename}", source_id))
        batch_id = cur.lastrowid
        for t in txns:
            con.execute(
                "INSERT INTO staged(batch_id,date,description,amount_cents,category_id) VALUES(?,?,?,?,?)",
                (batch_id, t["date"], t["description"], t["amount_cents"], t["category_id"]))
            staged += 1
    return staged


# ---------- 3. Customers ----------

def parse_customers(raw_bytes):
    rows = _rows(raw_bytes)
    hi, headers = _header_index(rows, "customer")
    if hi is None:
        hi, headers = _header_index(rows, "name")
    if hi is None:
        raise ValueError("Couldn't find a header row with a customer/name column.")
    name_i = _col(headers, "customer full name", "customer", "display name", "full name", "name",
                  exclude=("company", "file"))
    email_i = _col(headers, "email")
    phone_i = _col(headers, "phone")
    addr_i = _col(headers, "billing address", "address")
    out, seen = [], set()
    for row in rows[hi + 1:]:
        name = _get(row, name_i)
        if not name or name.lower().startswith("total") or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "email": _get(row, email_i),
                    "phone": _get(row, phone_i), "address": _get(row, addr_i)})
    if not out:
        raise ValueError("No customers found in that file.")
    return out


def import_customers(con, parsed):
    created = 0
    for c in parsed:
        if con.execute("SELECT 1 FROM customers WHERE lower(name)=lower(?)", (c["name"],)).fetchone():
            continue
        con.execute("INSERT INTO customers(name,email,phone,address) VALUES(?,?,?,?)",
                    (c["name"], c["email"], c["phone"], c["address"]))
        created += 1
    return created


# ---------- 4. Mileage ----------

def parse_mileage(raw_bytes):
    rows = _rows(raw_bytes)
    hi, headers = _header_index(rows, "date")
    if hi is None:
        raise ValueError("Couldn't find a header row with a date column.")
    date_i = _col(headers, "trip start date", "start date", "trip date", "date")
    miles_i = _col(headers, "distance (mi)", "distance", "miles", "mileage")
    purpose_i = _col(headers, "purpose", "description", "notes", "trip purpose")
    from_i = _col(headers, "start location", "starting point", "from")
    to_i = _col(headers, "end location", "destination", "to")
    if miles_i is None:
        raise ValueError("Couldn't find a distance/miles column.")
    out = []
    for row in rows[hi + 1:]:
        try:
            date = normalize_date(_get(row, date_i))
            miles = float(_get(row, miles_i).replace(",", "").replace("mi", "").strip())
        except ValueError:
            continue
        if miles <= 0:
            continue
        out.append({"date": date, "miles": miles, "purpose": _get(row, purpose_i),
                    "from_loc": _get(row, from_i), "to_loc": _get(row, to_i)})
    if not out:
        raise ValueError("No trips found in that file.")
    return out


def import_mileage(con, parsed):
    created = 0
    for t in parsed:
        if con.execute("SELECT 1 FROM mileage WHERE date=? AND miles=? AND purpose=?",
                       (t["date"], t["miles"], t["purpose"])).fetchone():
            continue
        con.execute("INSERT INTO mileage(date,miles,purpose,from_loc,to_loc) VALUES(?,?,?,?,?)",
                    (t["date"], t["miles"], t["purpose"], t["from_loc"], t["to_loc"]))
        created += 1
    return created
