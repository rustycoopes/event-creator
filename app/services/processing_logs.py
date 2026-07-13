"""Shared log line search/pagination for a processing step (Slice 6.2/6.3).

Used by both the JSON API and the HTMX HTML partial so the two stay in lockstep instead of
duplicating (and potentially diverging on) the same filtering logic.
"""

LOG_PAGE_SIZE = 50


def filter_log_lines(log_lines: list[str], search: str | None) -> list[str]:
    """Case-insensitive substring match against each log line's text.

    A plain substring check, not a SQL ``LIKE`` pattern — the search term is matched literally,
    including any ``%``/``_`` characters, so no escaping is applied.
    """
    if not search:
        return log_lines
    needle = search.lower()
    return [line for line in log_lines if needle in line.lower()]


def paginate_log_lines(
    log_lines: list[str], page: int, page_size: int = LOG_PAGE_SIZE
) -> tuple[list[str], int]:
    """One page of ``log_lines`` plus the total count (pre-pagination)."""
    total = len(log_lines)
    start = (page - 1) * page_size
    end = start + page_size
    return log_lines[start:end], total
