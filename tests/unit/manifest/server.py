"""Build the conformance server `tests/fixtures/manifest/server.json` describes.

Real routers over the `tests/support` models, registered through the library's own
factories, with a bearer-token role check standing in for the host's OIDC dependency. A
caller is described the way the fixtures' `*.request.json` do: authenticated or not, plus
roles. The token is the roles joined by commas (an authenticated caller with no roles
sends `-`).
"""

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from craftainer_crud.controllers.crud_manifest import api_catalog_router, crud_manifest_router
from craftainer_crud.controllers.crud_router import build_json_router, build_resource_router
from craftainer_crud.manifest import ManifestOptions, ManifestRegistry
from tests.support import quest_views
from tests.support.hero_model import Hero
from tests.support.hero_v1 import HeroV1, HeroV1Create, HeroV1Row, HeroV1Update
from tests.support.hero_views import HeroV2, HeroV2Create, HeroV2Update
from tests.support.quest_model import Quest as QuestRow

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "manifest"

_oauth2 = OAuth2PasswordBearer(tokenUrl="token")


def require_roles(*roles: str) -> Any:  # noqa: ANN401 -- a Depends(...) object
    """Return a `Depends` requiring one of `roles`, shaped like a host's `require_roles`."""

    async def _dependency(token: Annotated[str, Depends(_oauth2)]) -> list[str]:
        granted = set(token.split(","))
        if not granted.intersection(roles):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return sorted(granted)

    _dependency.roles = roles  # type: ignore[attr-defined]
    return Depends(_dependency)


def _unused() -> None:
    """A CRUD/revision/event dependency: these tests never call a resource route."""


Stub = Annotated[Any, Depends(_unused)]


def token_headers(request: dict[str, Any]) -> dict[str, str]:
    """Return the `Authorization` header a `*.request.json` case describes (none if anonymous)."""
    if not request["authenticated"]:
        return {}
    return {"Authorization": f"Bearer {','.join(request['roles']) or '-'}"}


def load_json(path: Path) -> dict[str, Any]:
    """Read a JSON object from `path`."""
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


def build_server(
    *, registry: ManifestRegistry | None = None, with_catalog: bool = True
) -> tuple[FastAPI, ManifestRegistry]:
    """Return the app described by server.json, and the registry its routers filled."""
    server = load_json(FIXTURES / "server.json")
    resources = {(r["name"], r["api_version"]): r for r in server["resources"]}
    registry = registry if registry is not None else ManifestRegistry()
    app = FastAPI()

    def roles(entry: dict[str, Any], key: str) -> Any:  # noqa: ANN401
        return require_roles(*entry.get(key, []))

    v1 = resources[("heroes", "v1")]
    v1_base = "/api/crud/v1/heroes/v1/json"
    app.include_router(
        build_json_router(
            prefix="",
            tags=["heroes"],
            resource_label="Hero",
            schema=HeroV1,
            create_schema=HeroV1Create,
            update_schema=HeroV1Update,
            crud_dependency=Stub,
            read_roles=roles(v1, "read_roles"),
            write_roles=roles(v1, "write_roles"),
            delete_roles=roles(v1, "delete_roles"),
            resource="hero",
            event_source_dependency=Stub,
            manifest=ManifestOptions(
                registry=registry,
                name="heroes",
                api_version="v1",
                model=HeroV1Row,
                owner_scoped=True,
                base_path=v1_base,
            ),
        ),
        prefix=v1_base,
    )

    v2 = resources[("heroes", "v2")]
    app.include_router(
        build_resource_router(
            prefix="",
            api_prefix="/api/crud/v1/heroes/v2",
            tags=["heroes"],
            resource_label="Hero",
            resource="hero",
            item_tag="hero",
            list_tag="heroes",
            fields=("name", "powers", "power_level"),
            schema=HeroV2,
            create_schema=HeroV2Create,
            update_schema=HeroV2Update,
            crud_dependency=Stub,
            read_roles=roles(v2, "read_roles"),
            write_roles=roles(v2, "write_roles"),
            delete_roles=roles(v2, "delete_roles"),
            draft_schema=HeroV2Update,
            archivable=True,
            revision_repository_dependency=Stub,
            event_source_dependency=Stub,
            stats_enabled=True,
            manifest=ManifestOptions(
                registry=registry,
                name="heroes",
                api_version="v2",
                model=Hero,
                owner_scoped=True,
                read_roles=v2["read_roles"],
                write_roles=v2["write_roles"],
                delete_roles=v2["delete_roles"],
            ),
        ),
        prefix="/api/crud/v1/heroes/v2",
    )

    quests = resources[("quests", "v1")]
    quests_base = "/api/crud/v1/quests/v1/json"
    app.include_router(
        build_json_router(
            prefix="",
            tags=["quests"],
            resource_label="Quest",
            schema=quest_views.Quest,
            create_schema=quest_views.QuestCreate,
            update_schema=quest_views.QuestUpdate,
            crud_dependency=Stub,
            read_roles=Depends(_unused),
            write_roles=roles(quests, "write_roles"),
            delete_roles=roles(quests, "delete_roles"),
            manifest=ManifestOptions(
                registry=registry,
                name="quests",
                api_version="v1",
                model=QuestRow,
                public_read=True,
                write_roles=quests["write_roles"],
                delete_roles=quests["delete_roles"],
                base_path=quests_base,
            ),
        ),
        prefix=quests_base,
    )

    app.include_router(
        crud_manifest_router(
            registry,
            service_name=server["service"]["name"],
            service_version=server["service"]["version"],
            stats_ttl=server["stats_ttl_seconds"],
            oidc_discovery_url=server["oidc_discovery_url"],
        )
    )
    if with_catalog:
        app.include_router(api_catalog_router(manifest_url="/api/_crud/manifest"))
    return app, registry
