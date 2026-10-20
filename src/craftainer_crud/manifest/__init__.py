"""The CRUD manifest: a registry the router factories fill and the builder that renders it.

See `docs/manifest.md` for the contract and `craftainer_crud.controllers.crud_manifest`
for the routes that serve it.
"""

from craftainer_crud.manifest.builder import build_manifest
from craftainer_crud.manifest.models import MANIFEST_PROFILE, MANIFEST_VERSION, Manifest
from craftainer_crud.manifest.registry import ManifestOptions, ManifestRegistry, RegisteredResource

__all__ = (
    "MANIFEST_PROFILE",
    "MANIFEST_VERSION",
    "Manifest",
    "ManifestOptions",
    "ManifestRegistry",
    "RegisteredResource",
    "build_manifest",
)
