"""Statement extraction: the model now reports each amount with the sign AS SHOWN on the statement
(money in = positive, money out = negative), and extract_* flips once to the app's internal convention
(positive = money out). This is the fix for a screenshot import that read the SIGNS wrong (the model
inferred direction from the description, so 'Credit Card Payment' rows and 'Online Services' income
flipped). No network — the backend extractor is stubbed. Isolation: SHOPBOOKS_DATA_DIR first."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="shopbooks_signs_")

import db  # noqa: E402
db.init()
import ai  # noqa: E402
from testutil import ok  # noqa: E402

con = db.connect()
db.set_setting(con, "anthropic_api_key", "sk-test")   # makes ai.available() true, backend=claude
con.commit()

# The model, per the new prompt, returns amounts with the sign it SEES on the statement:
# a deposit is +, a card payment / purchase is -. Mirrors Ben's real statement.
AS_SHOWN = [
    {"date": "2026-07-20", "description": "Chase Credit Card Payments", "amount": -86.00},   # money out
    {"date": "2026-07-20", "description": "Mobile Check Deposit", "amount": 1150.00},         # money in
    {"date": "2026-07-20", "description": "Square Online Services", "amount": 9.00},          # money in
    {"date": "2026-07-09", "description": "Check 1071", "amount": -1700.00},                  # money out
]
_orig = ai._claude_statement_image
ai._claude_statement_image = lambda con, path, account_name: [dict(t) for t in AS_SHOWN]
try:
    out = ai.extract_statement_image(con, "stmt.png", "USAA Checking")
    by_desc = {t["description"]: t["amount"] for t in out}
    # internal convention: positive = money OUT, negative = money IN — the FLIP of what the model saw
    ok(by_desc["Chase Credit Card Payments"] == 86.00, "a card PAYMENT (shown -) becomes money OUT (+)")
    ok(by_desc["Check 1071"] == 1700.00, "a check (shown -) becomes money OUT (+)")
    ok(by_desc["Mobile Check Deposit"] == -1150.00, "a DEPOSIT (shown +) becomes money IN (-)")
    ok(by_desc["Square Online Services"] == -9.00, "Square income (shown +) becomes money IN (-)")
    # every sign flipped, none dropped
    ok(len(out) == 4 and all(o["amount"] == -a["amount"] for o, a in zip(out, AS_SHOWN)),
       "every row's sign is flipped to internal convention")
finally:
    ai._claude_statement_image = _orig

# robustness: None / empty / a row with no amount don't blow up the flip
ok(ai._to_internal_signs(None) is None, "None passes through")
ok(ai._to_internal_signs([]) == [], "empty passes through")
ok(ai._to_internal_signs([{"description": "x"}]) == [{"description": "x"}], "a row with no amount is untouched")

# the prompt tells the model to copy the printed sign and not guess from the description
ok("AUTHORITATIVE" in ai._STATEMENT_RULES and "never the wording" in ai._STATEMENT_RULES,
   "the prompt makes the printed sign authoritative over the description")

con.close()
print("\nSTATEMENT SIGN TESTS DONE")
