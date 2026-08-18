"""Export history model for tracking query result exports."""

from sqlmodel import SQLModel, Field
from datetime import datetime, timezone
from enum import Enum


class ExportFormat(str, Enum):
    """Export format types."""

    CSV = "csv"
    JSON = "json"


class ExportStatus(str, Enum):
    """Export status types."""

    SUCCESS = "success"
    FAILED = "failed"
    PENDING = "pending"


class ExportHistory(SQLModel, table=True):
    """Export history entity stored in SQLite."""

    __tablename__ = "exporthistory"

    id: int = Field(default=None, primary_key=True)
    database_name: str = Field(index=True, max_length=50)
    sql_query: str = Field(index=True)
    format: ExportFormat = Field(default=ExportFormat.CSV)
    row_count: int = Field(default=0)
    file_size_bytes: int = Field(default=0)
    file_name: str = Field(max_length=255)
    export_path: str | None = None
    status: ExportStatus = Field(default=ExportStatus.SUCCESS)
    error_message: str | None = None
    execution_time_ms: int = Field(default=0)
    export_time_ms: int = Field(default=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    exported_by: str | None = Field(max_length=100)  # User identifier