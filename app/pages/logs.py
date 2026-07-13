"""The Processing History logs page: a filterable, sortable grid (ported from organize-me's Slice
6.1/#83, redesigned as a grid in #111, to Event Creator in Slice R8/R9).

Displays a paginated, filterable, sortable grid of the user's processing runs (date, filename,
status, event count, and an expanded-details column), mirroring the dashboard's HTMX filter
pattern: the filter form and column-header sort links target ``#logs-body`` and this route detects
the ``HX-Request`` header to return just that fragment (``partials/logs_body.html``) instead of
the full page, so filtering/sorting never triggers a full page reload. Each row still links to its
run detail page. Anonymous visitors are redirected to the Host's ``/login`` like the other
authenticated pages (app.core.auth), not the monolith's fastapi-users login.
"""

import uuid
from datetime import date as date_
from functools import partial
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.processing_runs import (
    PAGE_SIZE,
    SortColumn,
    SortDir,
    build_run_detail_summaries,
    list_user_processing_runs,
    parse_date_param,
    to_processing_run_read,
)
from app.core.auth import current_user_id_optional
from app.core.templating import templates
from app.db.session import get_db
from app.models.processing_run import ProcessingRunStatus
from app.models.processing_step import ProcessingStep

router = APIRouter(tags=["pages"])

_SORT_COLUMNS_TUPLE: tuple[SortColumn, ...] = ("date", "filename", "status")


def _logs_url(
    *,
    page: int,
    run_status: ProcessingRunStatus | None,
    date_from: date_ | None,
    date_to: date_ | None,
    sort_by: SortColumn,
    sort_dir: SortDir,
) -> str:
    """Build a /logs URL carrying only the non-default filters/sort, so an unfiltered link stays
    as short as ``/logs?page=2`` (matches the existing pagination tests' expected form)."""
    params: dict[str, str] = {}
    if run_status is not None:
        params["status"] = run_status.value
    if date_from is not None:
        params["date_from"] = date_from.isoformat()
    if date_to is not None:
        params["date_to"] = date_to.isoformat()
    if sort_by != "date":
        params["sort_by"] = sort_by
    if sort_dir != "desc":
        params["sort_dir"] = sort_dir
    params["page"] = str(page)
    return f"/logs?{urlencode(params)}"


def _toggle_sort_dir(
    *, active_column: SortColumn, active_dir: SortDir, column: SortColumn
) -> SortDir:
    """Clicking a column header sorts that column ascending first; clicking the already-ascending
    active column flips it to descending. Clicking a different column always starts ascending,
    matching a typical spreadsheet's sort-header behaviour."""
    if active_column == column and active_dir == "asc":
        return "desc"
    return "asc"


@router.get("/logs", response_model=None)
async def logs_page(
    request: Request,
    page: int = Query(default=1, ge=1),
    run_status: ProcessingRunStatus | None = Query(default=None, alias="status"),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sort_by: SortColumn = Query(default="date"),
    sort_dir: SortDir = Query(default="desc"),
    user_id: uuid.UUID | None = Depends(current_user_id_optional),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    if user_id is None:
        return RedirectResponse("/login", status_code=302)

    # The filter form is a plain <form> HTMX serializes as-is, so an untouched date picker submits
    # "" rather than omitting the param - parse_date_param treats that the same as unset.
    parsed_date_from = parse_date_param(date_from)
    parsed_date_to = parse_date_param(date_to)
    runs, total = await list_user_processing_runs(
        db,
        user_id,
        page,
        run_status=run_status,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    # Bound to the current filters/sort so every call site below only has to vary page - keeping
    # a filter/sort param in sync across prev/next/sort-header/redirect isn't possible otherwise
    # since there's only one place they're threaded through.
    url_for = partial(
        _logs_url,
        run_status=run_status,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    if total > 0 and page > total_pages:
        return RedirectResponse(url_for(page=total_pages), status_code=302)

    steps_result = await db.scalars(
        select(ProcessingStep)
        .where(ProcessingStep.run_id.in_([r.id for r in runs]))
        .order_by(ProcessingStep.step_number)
    )
    summaries = build_run_detail_summaries(runs, list(steps_result.all()))

    def sort_url_for(column: SortColumn) -> str:
        return url_for(
            page=1,
            sort_by=column,
            sort_dir=_toggle_sort_dir(active_column=sort_by, active_dir=sort_dir, column=column),
        )

    has_active_filters = bool(run_status or parsed_date_from or parsed_date_to)
    context = {
        "dark_mode": False,
        "runs": [to_processing_run_read(r, summaries.get(r.id, "")) for r in runs],
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "statuses": list(ProcessingRunStatus),
        "has_active_filters": has_active_filters,
        "filters": {
            "status": run_status.value if run_status else "",
            "date_from": parsed_date_from.isoformat() if parsed_date_from else "",
            "date_to": parsed_date_to.isoformat() if parsed_date_to else "",
            "sort_by": sort_by,
            "sort_dir": sort_dir,
        },
        "first_url": url_for(page=1) if page > 1 else None,
        "prev_url": url_for(page=page - 1) if page > 1 else None,
        "next_url": url_for(page=page + 1) if page < total_pages else None,
        "last_url": url_for(page=total_pages) if page < total_pages else None,
        "sort_urls": {column: sort_url_for(column) for column in _SORT_COLUMNS_TUPLE},
    }
    template_name = (
        "partials/logs_body.html" if request.headers.get("hx-request") == "true" else "pages/logs.html"
    )
    return templates.TemplateResponse(request, template_name, context)
