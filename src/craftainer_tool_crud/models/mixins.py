"""Opt-in record-lifecycle mixins: archive, draft, scheduled publish, and lock.

Each mixin adds one plain column, following crud.models.base.IdentifiedBase's own
`created_at`/`updated_at` pattern -- a model opts in with plain multiple
inheritance (`class Hero(IdentifiedBase, Archivable, Draftable, ...)`), and
crud.repositories/crud.interfaces detect which of these a bound model carries via
`hasattr`, mirroring how crud.repositories.memory already special-cases
`created_at`/`updated_at`. A model that doesn't inherit a mixin is completely
unaffected -- no shared base class changes, no new required column anywhere else.

See docs/adrs/0012-soft-delete-via-marker-column.md for why Archivable reuses the
same row/table instead of a second archive table.
"""

from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column


class Archivable:
    """Mixin adding a nullable `archived_at` marker column (soft-delete via marker).

    `None` means "active"; a timestamp means "archived at that time". Repository
    delete/delete_many set this instead of issuing a real DELETE for a model
    carrying this column, and list/get/count exclude archived rows by default
    -- see crud.repositories.sqlalchemy/crud.repositories.memory.
    """

    archived_at: Mapped[datetime | None] = mapped_column(default=None)


class Draftable:
    """Mixin adding an `is_draft` flag, defaulting to True.

    A plain, filterable/sortable column -- no repository/interface changes are
    needed for draft itself; see crud.controllers.crud_router's `/draft`/`/publish`
    routes for the create-partial/complete-and-publish flow built on top of it.

    Unlike Archivable, a plain unfiltered `GET <prefix>` includes drafts by
    default (no default-exclusion clause is added anywhere for `is_draft`) --
    Hero's own worked example takes this as its provisional answer to "should a
    draft be hidden from a normal list the way an archived row is?" since a
    content-management use case usually still wants an editor to see their own
    drafts in the main list. A resource where that's wrong can filter drafts out
    itself with the already-generic `?is_draft=false`; resolve this properly
    against whichever real (non-Hero) resource first needs draft and has a firm
    opinion, the same "decide once a real caller exists" reasoning
    docs/adrs/0011-owner-scoped-crud-example-resource.md used for ownership
    scoping.

    Interaction with crud.interfaces.base.OwnerScope: Hero combines both (see
    app.crud_1.heroes.heroes_v2.get_hero_crud), but with `read_scoped=False`,
    so every caller already sees every hero -- including every draft -- and
    "does a caller's own drafts stay visible to them specifically" never comes
    up for the demo. A future resource pairing Draftable with a *read-scoped*
    OwnerScope (`read_scoped=True`) would need an explicit answer (e.g. an
    owner always sees their own drafts even when reads are otherwise scoped
    away from other owners' records) -- still open, since no resource combines
    them that way yet.
    """

    is_draft: Mapped[bool] = mapped_column(default=True)


class Schedulable:
    """Mixin adding `publish_at`/`unpublish_at`, both defaulting to None (unscheduled).

    Visibility is computed live at read time from these two columns (see
    crud.repositories.sqlalchemy/crud.repositories.memory's default-exclusion
    logic) -- no background job ever needs to touch the row.
    """

    publish_at: Mapped[datetime | None] = mapped_column(default=None)
    unpublish_at: Mapped[datetime | None] = mapped_column(default=None)


class Lockable:
    """Mixin adding an `is_locked` flag, defaulting to False.

    Enforced in the repository layer (see crud.repositories.base.RecordLockedError)
    so it's atomic with the mutation itself -- update/update_many/delete/delete_many
    refuse to touch a locked row, except an update whose own data sets
    `is_locked=False` and nothing else.

    That escape hatch requires `data == {"is_locked": False}` exactly -- a `PATCH
    ?id= {"is_locked": false, "name": "new name"}` against a locked record still
    raises RecordLockedError, same as any other field-changing PATCH against a
    locked row. Unlock is its own request; a caller edits the now-unlocked record
    in a follow-up PATCH.
    """

    is_locked: Mapped[bool] = mapped_column(default=False)
