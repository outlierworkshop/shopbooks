"""Products & services catalog routes."""
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

import db
import ledger
import migrate
from webutil import categories, ctx, templates

router = APIRouter()

@router.get("/items", response_class=HTMLResponse)
def items_page(request: Request):
    con = db.connect()
    try:
        err = request.query_params.get("err", "")
        msg = request.query_params.get("msg", "")
        
        items = con.execute(
            "SELECT i.*, a.name as account_name FROM items i "
            "LEFT JOIN accounts a ON a.id=i.income_account_id "
            "ORDER BY i.name"
        ).fetchall()
        
        income_accounts = categories(con, types=("income",))
        
        total_items = sum(1 for it in items if it["active"])
        mapped_items = sum(1 for it in items if it["income_account_id"] and it["active"])
        
        return templates.TemplateResponse(request, "items.html", ctx(
            request, con,
            items=items,
            income_accounts=income_accounts,
            total_items=total_items,
            mapped_items=mapped_items,
            err=err,
            msg=msg
        ))
    finally:
        con.close()

@router.post("/items")
def items_create(name: str = Form(...), sku: str = Form(""), description: str = Form(""),
                 unit_price: str = Form("0.00"), income_account_id: str = Form(""),
                 taxable: str = Form("")):
    con = db.connect()
    try:
        if not name.strip():
            return RedirectResponse("/items?err=Name is required", status_code=303)

        unit_cents = 0
        if unit_price.strip():
            try:
                unit_cents = ledger.parse_amount_to_cents(unit_price)
            except ValueError:
                return RedirectResponse("/items?err=Invalid price format", status_code=303)

        acct_id = int(income_account_id) if income_account_id.strip() else None

        con.execute(
            "INSERT INTO items(name, sku, description, unit_cents, income_account_id, taxable) VALUES(?,?,?,?,?,?)",
            (name.strip(), sku.strip() or None, description.strip(), unit_cents, acct_id, 1 if taxable else 0)
        )
        con.commit()
        return RedirectResponse("/items?msg=Product/service added successfully", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/items?err={str(e)}", status_code=303)
    finally:
        con.close()

@router.post("/items/update")
def items_update(item_id: int = Form(...), name: str = Form(...), sku: str = Form(""),
                 description: str = Form(""), unit_price: str = Form("0.00"),
                 income_account_id: str = Form(""), active: str = Form("0"), taxable: str = Form("")):
    con = db.connect()
    try:
        if not name.strip():
            return RedirectResponse("/items?err=Name is required", status_code=303)

        unit_cents = 0
        if unit_price.strip():
            try:
                unit_cents = ledger.parse_amount_to_cents(unit_price)
            except ValueError:
                return RedirectResponse("/items?err=Invalid price format", status_code=303)

        acct_id = int(income_account_id) if income_account_id.strip() else None
        is_active = 1 if active == "1" else 0

        con.execute(
            "UPDATE items SET name=?, sku=?, description=?, unit_cents=?, income_account_id=?, active=?, taxable=? WHERE id=?",
            (name.strip(), sku.strip() or None, description.strip(), unit_cents, acct_id, is_active, 1 if taxable else 0, item_id)
        )
        con.commit()
        return RedirectResponse("/items?msg=Product/service updated successfully", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/items?err={str(e)}", status_code=303)
    finally:
        con.close()

@router.post("/items/import-qbo")
async def items_import_qbo(file: UploadFile = File(...)):
    con = db.connect()
    try:
        contents = await file.read()
        parsed = migrate.parse_items(con, contents)
        created, updated, skipped = migrate.import_items(con, parsed)
        con.commit()
        return RedirectResponse(
            f"/items?msg=Import complete: {created} items created, {updated} updated",
            status_code=303
        )
    except Exception as e:
        return RedirectResponse(f"/items?err={str(e)}", status_code=303)
    finally:
        con.close()
