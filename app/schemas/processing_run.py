import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.processing_run import ProcessingRunStatus
from app.models.processing_step import ProcessingStepStatus


class ProcessingRunRead(BaseModel):
    """A single processing run as returned by ``GET /api/v1/processing-runs``."""

    id: uuid.UUID
    filename: str
    status: ProcessingRunStatus
    events_extracted_count: int
    created_at: datetime
    # Logs-grid expanded-details column: the first error line for a failed run, or a
    # "N log lines" count otherwise. Computed from the run's steps, not stored.
    detail_summary: str


class ProcessingRunListRead(BaseModel):
    """One page of the current user's processing runs, plus enough to render pagination controls."""

    runs: list[ProcessingRunRead]
    page: int
    page_size: int
    total: int


class ProcessingStepRead(BaseModel):
    """One pipeline step within a run."""

    step_number: int
    step_name: str
    status: ProcessingStepStatus
    started_at: datetime | None
    completed_at: datetime | None


class ProcessingRunDetailRead(BaseModel):
    """Full run detail with steps."""

    id: uuid.UUID
    filename: str
    status: ProcessingRunStatus
    events_extracted_count: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    steps: list[ProcessingStepRead]


class ProcessingLogLineRead(BaseModel):
    """Log lines for one step, paginated."""

    step_number: int
    step_name: str
    log_lines: list[str]
    page: int
    page_size: int
    total: int


class ProcessingStepLogsRead(BaseModel):
    """One step's full (unpaginated) log lines, for the run logs download."""

    step_number: int
    step_name: str
    status: ProcessingStepStatus
    log_lines: list[str]


class ProcessingRunLogsDownloadRead(BaseModel):
    """A run's full structured logs across all steps."""

    run_id: uuid.UUID
    filename: str
    steps: list[ProcessingStepLogsRead]
