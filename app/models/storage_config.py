import uuid
from datetime import datetime
from enum import Enum

from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import DateTime, ForeignKey
from sqlalchemy import Enum as SAEnum
from sqlalchemy import func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StorageProviderType(str, Enum):
    GOOGLE_DRIVE = "google_drive"
    DROPBOX = "dropbox"
    S3 = "s3"


class StorageConfig(Base):
    """A user's single active storage connection (one row per user - unique on user_id).

    Credential columns (OAuth tokens, S3 keys) hold values encrypted at rest. Adopted from the
    organize-me monolith's Slice R1 schema separation; the read/write endpoints land in Slice R7.
    """

    __tablename__ = "storage_configs"
    __table_args__ = {"schema": "event_creator"}

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("host.users.id", ondelete="cascade"), nullable=False, unique=True
    )
    provider: Mapped[StorageProviderType] = mapped_column(
        SAEnum(
            StorageProviderType,
            name="storage_provider",
            schema="event_creator",
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    folder_path: Mapped[str] = mapped_column(nullable=False)
    oauth_access_token: Mapped[str | None] = mapped_column(nullable=True)
    oauth_refresh_token: Mapped[str | None] = mapped_column(nullable=True)
    oauth_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    s3_access_key: Mapped[str | None] = mapped_column(nullable=True)
    s3_secret_key: Mapped[str | None] = mapped_column(nullable=True)
    s3_bucket_name: Mapped[str | None] = mapped_column(nullable=True)
    s3_region: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
