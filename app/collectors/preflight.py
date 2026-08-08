"""Read-only, network-free configuration status for each vendor collector.

Reports whether a vendor has any relevant credential configured, whether
that configuration looks COMPLETE (all required env vars present), and what
auth mode is expected — without ever making a network call or exposing any
part of a credential value (no key values, token values, credential paths,
account/organization identifiers, lengths, prefixes, or hashes are ever
returned). See docs/vendor-collector-production-readiness.md for the
official-documentation evidence behind each vendor's requirements.

`production_ready` is deliberately always False here: a config value being
PRESENT (even well-formed) is not evidence it actually WORKS against the
real vendor API — a key can be revoked, mistyped-but-plausible, missing a
permission, or belong to the wrong account. This project has never
performed a real-credential connection (see the Human Gate section of
docs/vendor-collector-production-readiness.md), so nothing can honestly be
reported as production-ready yet. `live_validation_required` is always True
for the same reason. Once a real connection is confirmed for a vendor
(outside this network-free check), that confirmation belongs in a
Development_Log-style record, not in this function's return value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(slots=True)
class VendorPreflightStatus:
    vendor: str
    # Some relevant credential-like env var is set, even if configuration is
    # incomplete or the value turns out to be the wrong kind (e.g. only
    # GEMINI_API_KEY is set, which alone is insufficient for Gemini).
    configured: bool
    # All env vars required for this collector to attempt a real call are
    # present. Still says nothing about whether those values actually work.
    configuration_complete: bool
    auth_mode: str
    missing_requirements: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Always True in this codebase — no configuration state, however
    # complete, has been confirmed against the real vendor API.
    live_validation_required: bool = True
    # Kept for API/response-shape backward compatibility. Always False: see
    # the module docstring — configuration_complete is not proof of a
    # working credential.
    production_ready: bool = False


_LIVE_VALIDATION_NOTE = (
    "production_ready is always false here: this is a network-free "
    "configuration check, not a live credential test. A real-credential "
    "connection has not been performed for this vendor in this environment "
    "— see the Human Gate section of docs/vendor-collector-production-readiness.md."
)


def _configured_value(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def openai_preflight() -> VendorPreflightStatus:
    api_key = _configured_value("OPENAI_API_KEY")
    complete = api_key is not None
    return VendorPreflightStatus(
        vendor="openai",
        configured=complete,
        configuration_complete=complete,
        auth_mode="organization_admin_api_key",
        missing_requirements=[] if complete else ["OPENAI_API_KEY"],
        notes=[
            "OPENAI_API_KEY must be an Organization Admin API key (created under "
            "Organization -> Admin keys), not a regular/project API key — the "
            "official Usage and Costs endpoints require it.",
            "Budget/spend-cap is write-only in OpenAI's official API "
            "(POST /v1/organization/spend_limit); no official read/GET endpoint "
            "was found, so this collector never reports budget data.",
            "Project-scoped rate-limit quotas (GET /v1/organization/projects/{id}/rate_limits) "
            "exist officially but are not implemented by this collector yet.",
            _LIVE_VALIDATION_NOTE,
        ],
    )


def gemini_preflight() -> VendorPreflightStatus:
    access_token = _configured_value("GOOGLE_CLOUD_ACCESS_TOKEN")
    project_id = _configured_value("GOOGLE_CLOUD_PROJECT") or _configured_value("GEMINI_PROJECT_ID")
    api_key = _configured_value("GEMINI_API_KEY")
    complete = access_token is not None and project_id is not None
    configured = complete or access_token is not None or project_id is not None or api_key is not None
    missing: list[str] = []
    if not access_token:
        missing.append("GOOGLE_CLOUD_ACCESS_TOKEN")
    if not project_id:
        missing.append("GOOGLE_CLOUD_PROJECT (or GEMINI_PROJECT_ID)")
    notes = [
        "Cloud Monitoring and Service Usage/Consumer Quota APIs require an "
        "OAuth2 access token; this collector does not implement Application "
        "Default Credentials discovery/refresh, and never accepts an API key "
        "for these management endpoints (confirmed via official Google Cloud "
        "docs — see docs/vendor-collector-production-readiness.md). The "
        "caller is responsible for obtaining and refreshing the access token.",
    ]
    if api_key and not access_token:
        notes.append(
            "GEMINI_API_KEY is set but is not used for management API calls — a "
            "Google AI Studio API key cannot authenticate to Cloud Monitoring or "
            "Service Usage regardless of how it is transmitted."
        )
    notes.append(
        "Cloud Billing Budget API (billingAccounts.budgets.get, OAuth2/ADC only, "
        "no API-key support) exists officially but is not implemented by this "
        "collector yet."
    )
    notes.append(_LIVE_VALIDATION_NOTE)
    return VendorPreflightStatus(
        vendor="gemini",
        configured=configured,
        configuration_complete=complete,
        auth_mode="oauth2_access_token",
        missing_requirements=missing,
        notes=notes,
    )


def claude_preflight() -> VendorPreflightStatus:
    api_key = _configured_value("ANTHROPIC_API_KEY")
    complete = api_key is not None
    notes = [
        "ANTHROPIC_API_KEY must be an organization Admin API key "
        "(prefix sk-ant-admin01-), not a regular API key — the Usage and Cost "
        "Admin API endpoints require it.",
        "The spend-limit/budget read API is documented as Claude Enterprise-only "
        "and is not available to Claude Console/Platform organizations, so this "
        "collector never reports budget data.",
        "Organization rate-limits (quota) API (GET /v1/organizations/rate_limits) "
        "exists officially but is not implemented by this collector yet.",
    ]
    if api_key is not None and not api_key.startswith("sk-ant-admin01-"):
        # A soft signal only — never a hard block, since prefixes could change
        # or this heuristic could simply be wrong. No part of the configured
        # value itself is included in the message.
        notes.append(
            "The configured key does not match the expected Admin API key prefix. "
            "It may be a regular API key, which the Usage/Cost Admin API will reject."
        )
    notes.append(_LIVE_VALIDATION_NOTE)
    return VendorPreflightStatus(
        vendor="claude",
        configured=complete,
        configuration_complete=complete,
        auth_mode="organization_admin_api_key",
        missing_requirements=[] if complete else ["ANTHROPIC_API_KEY"],
        notes=notes,
    )


def all_vendor_preflight_statuses() -> list[VendorPreflightStatus]:
    return [openai_preflight(), gemini_preflight(), claude_preflight()]
