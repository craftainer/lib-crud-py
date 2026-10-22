"""Server-minted, resource-scoped event-stream subscriber ids.

A subscriber id is a capability: presenting it resumes that subscriber's session (the
MQTT persistent session and the events queued in it, or the in-memory queue), so a
client must not be able to choose one. The server mints every id and a client can only
hand one back. An id is 128 random bits plus a 128-bit HMAC-SHA256 tag over the
resource and those bits, 64 lowercase hex characters (`<32 nonce hex><32 tag hex>`),
the same construction as lib-crud-rs:

    tag = HMAC-SHA256(secret, resource + b"\\x00" + nonce_hex)[:16]

`verify_subscriber_id` accepts an id only when its tag verifies for the resource being
subscribed; anything else (absent, forged, low-entropy, minted for another resource, or
minted before a restart under a random secret) is treated as no id at all, so the
caller gets a fresh id and a fresh session and a made-up id never takes over a live one.
Clients pass the id back opaquely.

The secret is `CrudConfig.subscriber_secret` (at least 32 bytes, shared by every
instance and restart that must accept each other's ids), else a random per-process
secret.
"""

import hashlib
import hmac
import secrets

from craftainer_crud.config import get_config

_NONCE_BYTES = 16
_TAG_BYTES = 16
_ID_LENGTH = 2 * (_NONCE_BYTES + _TAG_BYTES)
_HEX = frozenset("0123456789abcdef")

# Used when the host configures no secret: ids then survive neither a restart nor a
# second worker process, which is the safe default (a resume simply gets a fresh session).
_PROCESS_SECRET = secrets.token_bytes(32)


def _tag(resource: str, nonce: str) -> str:
    secret = get_config().subscriber_secret or _PROCESS_SECRET
    message = resource.encode() + b"\x00" + nonce.encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()[: 2 * _TAG_BYTES]


def mint_subscriber_id(resource: str) -> str:
    """Return a new subscriber id bound to `resource`."""
    nonce = secrets.token_hex(_NONCE_BYTES)
    return nonce + _tag(resource, nonce)


def verify_subscriber_id(resource: str, raw: str | None) -> str | None:
    """Return `raw` (stripped) if this server minted it for `resource`, else None."""
    if raw is None:
        return None
    candidate = raw.strip()
    if len(candidate) != _ID_LENGTH or not _HEX.issuperset(candidate):
        return None
    nonce, tag = candidate[: 2 * _NONCE_BYTES], candidate[2 * _NONCE_BYTES :]
    return candidate if hmac.compare_digest(tag, _tag(resource, nonce)) else None


def resolve_subscriber_id(resource: str, raw: str | None) -> str:
    """Return `raw` if it is a valid id for `resource`, else a newly minted one."""
    return verify_subscriber_id(resource, raw) or mint_subscriber_id(resource)
