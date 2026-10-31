"""Training fixture pages for the demo story.

They imitate what a payment scam links to, using fictional brands only
(CoinHaven, Northwind Paper Co.). Every page carries a full-width
"TRAINING FIXTURE - fictional" banner, asks search engines not to index it,
and contains no form inputs: there is nothing to type into and nothing is
collected.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..bot.samples import demo_account
from ..config import Settings

NOINDEX = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"}

# path under /fixtures -> (template, title shown in the index)
PAGES: dict[str, tuple[str, str]] = {
    "coinhaven/verify": ("fixtures/coinhaven_verify.html", "CoinHaven: fake wallet verification page (phishing imitation)"),
    "northwind": ("fixtures/northwind_home.html", "Northwind Paper Co.: supplier home page with its real payment details"),
    "northwind/invoices/NP-2291": ("fixtures/northwind_invoice.html", "Northwind Paper Co.: genuine invoice NP-2291"),
}


SUPPLIER_ACCOUNT = "northwindpaper.testnet"


def fixtures_router(templates: Jinja2Templates, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/fixtures", include_in_schema=False)
    context = {"supplier_account": demo_account(settings, SUPPLIER_ACCOUNT)}

    def page(template: str) -> Callable[[Request], Awaitable[HTMLResponse]]:
        async def render(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(request, template, context, headers=NOINDEX)

        return render

    async def index(request: Request) -> HTMLResponse:
        entries = [(f"/fixtures/{path}", title) for path, (_, title) in PAGES.items()]
        return templates.TemplateResponse(request, "fixtures/index.html", {"entries": entries}, headers=NOINDEX)

    router.add_api_route("", index, methods=["GET"], response_class=HTMLResponse)
    router.add_api_route("/", index, methods=["GET"], response_class=HTMLResponse)
    for path, (template, _) in PAGES.items():
        router.add_api_route(f"/{path}", page(template), methods=["GET"], response_class=HTMLResponse)
    return router
