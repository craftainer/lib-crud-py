"""SQLAlchemy model for the shared, cross-resource revision history log.

One table for every resource that opts into revision history (keyed by
`resource`+`record_id`), not a table per resource -- matches Archivable/
Draftable/etc.'s "purely additive, nothing resource-specific" shape. See
crud.interfaces.base.RevisionSink for the opt-in CRUDInterface hook that writes
into this table.

`snapshot` stores the *entire* record view verbatim, with no field-level
redaction hook -- fine for Hero (no sensitive field), but a future resource
that opts into revisions (see crud.interfaces.base.RepositoryRevisionSink) and
carries a sensitive field (a token, a password hash, etc.) would persist it
here in plaintext, readable by anyone with read access to revision history
even without access to the live record. Add redaction at that point (e.g. an
excluded-fields set passed through CRUDInterface to RevisionSink.record), not
speculatively now.
"""

from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from crud.models.base import IdentifiedBase


class Revision(IdentifiedBase):
    """One append-only log entry: who changed what record of what resource, and how."""

    __tablename__ = "revisions"

    resource: Mapped[str] = mapped_column(index=True)
    record_id: Mapped[int] = mapped_column(index=True)
    action: Mapped[str]
    snapshot: Mapped[dict[str, object]] = mapped_column(postgresql.JSONB)
    actor: Mapped[str]
