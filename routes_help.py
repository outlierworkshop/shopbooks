"""In-app Help: renders the project guides (docs/*.md) so they're readable inside ShopBooks."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import helpdocs
from webutil import ctx, get_con, templates

router = APIRouter()


@router.get("/help", response_class=HTMLResponse)
def help_index(request: Request, con=Depends(get_con)):
    return templates.TemplateResponse(request, "help.html", ctx(
        request, con, docs=helpdocs.list_docs(), current=None, title=None, body=None))


@router.get("/help/{slug}", response_class=HTMLResponse)
def help_doc(request: Request, slug: str, con=Depends(get_con)):
    doc = helpdocs.get(slug)
    if not doc:
        return RedirectResponse("/help", status_code=303)
    title, body = doc
    return templates.TemplateResponse(request, "help.html", ctx(
        request, con, docs=helpdocs.list_docs(), current=slug, title=title, body=body))
