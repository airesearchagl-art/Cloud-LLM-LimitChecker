"""Generic Usage Allowance read model.

Projects the app's EXISTING normalized cache snapshots onto one
provider-agnostic shape, so a presentation layer can render "allowance
buckets" without hard-coding a provider, a product surface, a window name,
or a model name. The domain unit here is an allowance bucket and its
windows -- never a model. Model names age out; buckets do not.

Boundary, deliberately narrow:

- The only inputs are snapshots already produced by the existing cache
  loaders. This module never reads a file, never starts a process, never
  touches the network or the database, and never writes anything.
  `build_usage_allowance_payload` is a pure function of its arguments; the
  route is responsible for loading the snapshots and passing them in.
- It never sees a raw Codex App Server payload. A v2 Codex auto-fetch cache
  carries canonical buckets -- already selected and deduplicated by the
  adapter (`app.codex_rate_limits_adapter`), with `limit_id`,
  `limit_id_origin`, `display_name`, `plan_type`, `rate_limit_reached_type`
  and each window's real source slot -- and each becomes one allowance
  bucket. A v1 cache has none of that, so it keeps the original projection
  with those fields `null`. `credits`, `rateLimitResetCredits`, account and
  spend-control fields are in no cache and are never emitted.
- Every field is copied one at a time from an explicit allowlist. No source
  dict is ever splatted into the output, so an unknown or future cache field
  can never reach the API by accident.
- Nothing is inferred. A value the source does not carry is `null`; it is
  never estimated, back-filled, or merged from another source or an older
  observation.
"""

from dataclasses import dataclass
from datetime import datetime

# Provenance axis: how a number was obtained, orthogonal to the existing
# `source` identifiers (`codex_app_server`, `claude_code_statusline`, ...),
# which are preserved verbatim as `provenance`.
#
# `ESTIMATED` is deliberately NOT defined: this project never estimates a
# usage value, and defining the name at all would invite misuse. If it is
# ever needed it must arrive as an explicit contract change.
#
# Unavailability is an availability axis, not a source kind -- it is
# expressed by an entry in the response's `unavailable` list.
SOURCE_KIND_OFFICIAL_API = "OFFICIAL_API"
SOURCE_KIND_OFFICIAL_LOCAL_RUNTIME = "OFFICIAL_LOCAL_RUNTIME"
SOURCE_KIND_LOCAL_OBSERVATION = "LOCAL_OBSERVATION"
SOURCE_KIND_MANUAL = "MANUAL"
SOURCE_KIND_TEMPORAL_CORRELATION = "TEMPORAL_CORRELATION"

#: Known values, for display mapping and documentation. The schema keeps
#: `source_kind` an open string: this tuple must never become the only
#: accepted set, or a future source could not be added without a breaking
#: contract change. Phase 1 emits three of these five.
KNOWN_SOURCE_KINDS: tuple[str, ...] = (
    SOURCE_KIND_OFFICIAL_API,
    SOURCE_KIND_OFFICIAL_LOCAL_RUNTIME,
    SOURCE_KIND_LOCAL_OBSERVATION,
    SOURCE_KIND_MANUAL,
    SOURCE_KIND_TEMPORAL_CORRELATION,
)

# Provider / product-surface identifiers are likewise open strings with a
# known display mapping, never closed enums.
PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"

PRODUCT_SURFACE_WORK_CODEX = "work_codex"
PRODUCT_SURFACE_CLAUDE_CODE = "claude_code"
PRODUCT_SURFACE_CLAUDE_DESKTOP_CLOUD = "claude_desktop_cloud"

# The exact cache keys this module is allowed to read off a window. Anything
# else present on a source window is ignored by construction.
_WINDOW_USED_KEY = "used_percentage"
_WINDOW_REMAINING_KEY = "remaining_percentage"
_WINDOW_RESETS_AT_KEY = "resets_at"
_WINDOW_DURATION_KEY = "window_duration_minutes"


@dataclass(frozen=True, slots=True)
class CacheProjection:
    """How one existing cache maps onto a generic allowance bucket."""

    provider: str
    product_surface: str
    source_kind: str
    #: Cache keys holding windows, in display order.
    window_keys: tuple[str, ...]
    #: Whether the cache key is itself a truthful source-provided slot name.
    #: False means the upstream slot identity was lost before the cache was
    #: written, and re-emitting the key would misrepresent the source.
    window_key_is_source_slot: bool


#: Codex auto-fetch cache. Its legacy `five_hour` / `weekly` keys are
#: assigned by matching `windowDurationMins` (300 / 10080), not by the App
#: Server's own `primary` / `secondary` slot, so the key is NOT a source slot
#: -- when a v1 cache is projected through these keys, `source_slot` stays
#: null rather than claiming a slot the cache does not know. The real
#: duration survives, so the windows remain distinguishable by
#: `window_duration_minutes`. A v2 cache is projected from its canonical
#: buckets instead (see `project_canonical_buckets`).
CODEX_RATE_LIMITS_PROJECTION = CacheProjection(
    provider=PROVIDER_OPENAI,
    product_surface=PRODUCT_SURFACE_WORK_CODEX,
    source_kind=SOURCE_KIND_OFFICIAL_LOCAL_RUNTIME,
    window_keys=("five_hour", "weekly"),
    window_key_is_source_slot=False,
)

#: Manually entered Codex snapshot. The slot names are this app's own input
#: contract, not a lossy re-derivation of an upstream slot, so they are
#: emitted as `source_slot`. No duration is stored, so the slot name is the
#: only thing distinguishing the two windows.
CODEX_MANUAL_PROJECTION = CacheProjection(
    provider=PROVIDER_OPENAI,
    product_surface=PRODUCT_SURFACE_WORK_CODEX,
    source_kind=SOURCE_KIND_MANUAL,
    window_keys=("five_hour", "weekly"),
    window_key_is_source_slot=True,
)

#: Claude Code statusLine bridge cache: observed locally from the payload
#: Claude Code hands its own status line.
CLAUDE_CODE_PROJECTION = CacheProjection(
    provider=PROVIDER_ANTHROPIC,
    product_surface=PRODUCT_SURFACE_CLAUDE_CODE,
    source_kind=SOURCE_KIND_LOCAL_OBSERVATION,
    window_keys=("five_hour", "seven_day"),
    window_key_is_source_slot=True,
)

#: Claude Desktop Cloud values the user read off Claude Desktop and typed in.
CLAUDE_DESKTOP_CLOUD_PROJECTION = CacheProjection(
    provider=PROVIDER_ANTHROPIC,
    product_surface=PRODUCT_SURFACE_CLAUDE_DESKTOP_CLOUD,
    source_kind=SOURCE_KIND_MANUAL,
    window_keys=("five_hour", "seven_day"),
    window_key_is_source_slot=True,
)


#: Status for a source that could not be read at all. Reuses the existing
#: cache vocabulary instead of inventing a parallel one: to a consumer, a
#: loader that blew up is indistinguishable from a cache that could not be
#: read, and both mean "this source has nothing trustworthy to show".
UNREADABLE_SOURCE_STATUS = "invalid_cache"


def sanitized_unavailable_snapshot() -> dict:
    """A fixed, content-free snapshot for a source that could not be read.

    Takes no arguments on purpose. Whatever went wrong, none of it — no
    exception message, no file path, no cache content, no traceback, no
    environment — can reach the response through this value, because none of
    it is an input. A caller that catches a failing loader substitutes this,
    so one broken source costs that source and not the whole aggregate.
    """
    return {
        "available": False,
        "stale": False,
        "status": UNREADABLE_SOURCE_STATUS,
        "observed_at": None,
        "source": None,
        "error_message": None,
    }


def project_window(window: dict | None, *, source_slot: str | None) -> dict | None:
    """Project one validated cache window onto a generic allowance window.

    `None` in means `None` out: a source that has no such window produces no
    window, rather than a zero or a placeholder.

    `remaining_percent` is taken from the snapshot the cache already
    validated (every cache checks that used + remaining sums to ~100 before
    it will load), so nothing is derived here and nothing is combined across
    sources or observations.

    Only the used percentage is required: a window that carries one is worth
    showing even without a duration, a remaining value, or a reset time, so
    each of those three is read with `.get` and absent means `null` — never a
    duration guessed from the key name and never a percentage back-computed
    from the other. A window with no used percentage at all is a contract
    violation by its source, so that one is indexed directly and fails loudly
    rather than being emitted as a hole.
    """
    if window is None:
        return None
    return {
        "source_slot": source_slot,
        "window_duration_minutes": window.get(_WINDOW_DURATION_KEY),
        "used_percent": window[_WINDOW_USED_KEY],
        "remaining_percent": window.get(_WINDOW_REMAINING_KEY),
        "resets_at": window.get(_WINDOW_RESETS_AT_KEY),
    }


def project_cache_snapshot(
    snapshot: dict, *, projection: CacheProjection
) -> tuple[dict | None, dict | None]:
    """Split one cache snapshot into `(bucket, unavailable)`.

    Exactly one of the two is not `None`. A cache that has never been written
    (`not_observed`) or cannot be read (`invalid_cache`) yields an
    availability entry carrying its status -- never a bucket with fabricated
    zeroes. `error_message` is intentionally not projected: it adds nothing
    the status does not already say, and keeping it out keeps error text off
    this surface entirely.
    """
    if not snapshot.get("available"):
        return None, {
            "provider": projection.provider,
            "product_surface": projection.product_surface,
            "status": snapshot["status"],
            "source_kind": projection.source_kind,
        }

    windows = []
    for window_key in projection.window_keys:
        projected = project_window(
            snapshot.get(window_key),
            source_slot=window_key if projection.window_key_is_source_slot else None,
        )
        if projected is not None:
            windows.append(projected)

    bucket = {
        "provider": projection.provider,
        "product_surface": projection.product_surface,
        # Not carried by any cache projected through this path. Emitted as
        # null rather than synthesized, so a consumer can tell "not carried
        # by this source" apart from a real value.
        "limit_id": None,
        "limit_id_origin": None,
        "display_name": None,
        "plan_type": None,
        # Bucket-level in the source contract. It is deliberately NOT turned
        # into a per-window `reached` flag: the source says which bucket was
        # reached, never which window, and some of its values describe
        # credit depletion rather than a window at all.
        "rate_limit_reached_type": None,
        "status": snapshot["status"],
        "source_kind": projection.source_kind,
        "provenance": snapshot.get("source"),
        "observed_at": snapshot.get("observed_at"),
        "windows": windows,
    }
    return bucket, None


def project_canonical_buckets(snapshot: dict, *, projection: CacheProjection) -> list[dict]:
    """Project an available snapshot's canonical buckets, one allowance bucket each.

    The buckets were already selected and deduplicated upstream, so every
    one is emitted and nothing is added: the legacy `five_hour` / `weekly`
    keys of the same snapshot are deliberately NOT projected as well, since
    they describe one of these buckets again. Each bucket carries its own
    freshness `status`; every field is copied from an explicit allowlist.
    """
    allowances = []
    for source_bucket in snapshot["buckets"]:
        windows = [
            project_window(window, source_slot=window["source_slot"]) for window in source_bucket["windows"]
        ]
        allowances.append(
            {
                "provider": projection.provider,
                "product_surface": projection.product_surface,
                "limit_id": source_bucket["limit_id"],
                "limit_id_origin": source_bucket["limit_id_origin"],
                "display_name": source_bucket["display_name"],
                "plan_type": source_bucket["plan_type"],
                "rate_limit_reached_type": source_bucket["rate_limit_reached_type"],
                "status": source_bucket["status"],
                "source_kind": projection.source_kind,
                "provenance": snapshot.get("source"),
                "observed_at": snapshot.get("observed_at"),
                "windows": windows,
            }
        )
    return allowances


def build_usage_allowance_payload(
    *,
    generated_at: datetime,
    codex_rate_limits: dict,
    codex_manual: dict,
    claude_code: dict,
    claude_desktop_cloud: dict,
) -> dict:
    """Pure projection of the four existing cache snapshots.

    Takes already-loaded snapshots and returns a plain dict: no I/O of any
    kind happens here, so this stays deterministic and directly testable
    without a filesystem, a client, or a clock.

    `generated_at` is when this projection ran -- deliberately a different
    field name from each bucket's own `observed_at`, so a caller can never
    mistake "the server answered just now" for "the number is from just now".
    """
    allowances: list[dict] = []
    unavailable: list[dict] = []

    for snapshot, projection in (
        (codex_rate_limits, CODEX_RATE_LIMITS_PROJECTION),
        (codex_manual, CODEX_MANUAL_PROJECTION),
        (claude_code, CLAUDE_CODE_PROJECTION),
        (claude_desktop_cloud, CLAUDE_DESKTOP_CLOUD_PROJECTION),
    ):
        # Only the v2 Codex auto-fetch cache carries canonical buckets; no
        # other source is ever projected through them.
        if projection is CODEX_RATE_LIMITS_PROJECTION and snapshot.get("available") and snapshot.get("buckets"):
            allowances.extend(project_canonical_buckets(snapshot, projection=projection))
            continue
        bucket, missing = project_cache_snapshot(snapshot, projection=projection)
        if bucket is not None:
            allowances.append(bucket)
        else:
            unavailable.append(missing)

    return {
        "generated_at": generated_at.isoformat(),
        "allowances": allowances,
        "unavailable": unavailable,
    }
