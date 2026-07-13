"""Pydantic schema for the LLM's extracted-events payload (Slice 4.1, #52; ported to Event
Creator in Slice R8).

Step 5 of the pipeline (Parse LLM Response) validates Gemini's raw JSON text against
``ExtractedEvent`` before the deduplicate-and-save step persists it. Keeping the shape here
(rather than trusting the raw dict) means a malformed LLM response fails the run with a clear
validation error instead of surfacing later as a database or attribute error.

Mirrors the fields in ``examples/example.lmmoutput.txt`` and the ``events`` table columns
(app.models.event.Event); ``resolved_date_earliest`` is derived later (not from the LLM).
"""

from pydantic import BaseModel, Field


class ExtractedEvent(BaseModel):
    """One agreed event as returned by the extraction LLM.

    ``agreed_by`` and ``raw_date_text`` default to empty rather than being required so a slightly
    sparse (but otherwise valid) LLM item still parses; ``type``/``description``/``resolved_date``
    are the meaningful identity of an event and must be present.
    """

    type: str
    description: str
    resolved_date: str
    raw_date_text: str = ""
    agreed_by: list[str] = Field(default_factory=list)
