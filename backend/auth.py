"""
Bearer-token authentication for the control plane (Phase 1).

Phase 0 §2.1 ranked this the highest-severity issue before any prop
account: `POST /api/control` could start the engine, or set a
one-second trading interval, from anywhere that could reach port 8000,
with no credential at all.

Design
------
* One shared token from `.env` (`API_TOKEN`). This is a single-operator
  system on a single host; a user table would be ceremony.
* Applied to every `/api/*` route EXCEPT `/api/health`, which stays open
  so an external watchdog can probe liveness without holding a secret.
* Compared with `secrets.compare_digest`, so a wrong token cannot be
  recovered a byte at a time from response timing.
* When `API_TOKEN` is unset the dependency is a no-op, so a purely
  local run needs no configuration. `main.py` refuses to serve on a
  non-loopback interface in that state unless ALLOW_INSECURE_BIND is
  explicitly set.

The HTML pages are NOT token-protected: they are served to a browser on
the same host, and they receive the token server-side so their fetch()
calls can authenticate. Anyone who can load the page is already on the
machine.
"""

import secrets

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from backend import config


# Routes that never require a token.
#
# /api/health is deliberately open: Phase 12 puts an external watchdog
# on it, and a liveness probe that needs a secret is a liveness probe
# that fails for the wrong reasons.
PUBLIC_PATHS = frozenset({"/api/health"})


def _extract_token(request: Request):
    """
    Read the token from the Authorization header, falling back to
    X-API-Token.

    The fallback exists because some reverse proxies and browser
    fetch wrappers strip or rewrite Authorization.
    """

    header = request.headers.get("authorization")

    if header:
        parts = header.split(None, 1)

        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()

        # Tolerate a bare token, but never treat another scheme
        # (Basic, Digest) as if it were ours.
        if len(parts) == 1:
            return parts[0].strip()

        return None

    return request.headers.get("x-api-token")


def require_token(request: Request):
    """
    FastAPI dependency. Raises 401 unless the request carries the
    configured token.

    A no-op when API_TOKEN is unset.
    """

    if not config.AUTH_ENABLED:
        return None

    if request.url.path in PUBLIC_PATHS:
        return None

    supplied = _extract_token(request)

    if not supplied:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Constant-time: a plain == would leak the token through timing.
    if not secrets.compare_digest(supplied, config.API_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return supplied


# ---------------------------------------------------------------------
# Middleware enforcement
#
# Applied as middleware rather than as a per-route dependency on
# purpose: a route added in a later phase is protected automatically.
# A per-route dependency is one that somebody eventually forgets.
# ---------------------------------------------------------------------

PROTECTED_PREFIX = "/api/"


def path_requires_token(path):
    """True when this path must carry the token."""

    if not config.AUTH_ENABLED:
        return False

    if not path.startswith(PROTECTED_PREFIX):
        return False

    return path not in PUBLIC_PATHS


def check_request(request: Request):
    """
    Validate a request's token.

    Returns None when the request may proceed, or a 401 JSONResponse
    when it may not. Returning a response (rather than raising) is what
    lets this run as middleware.
    """

    supplied = _extract_token(request)

    if not supplied:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={
                "status": "error",
                "message": (
                    "Missing bearer token. Send 'Authorization: Bearer "
                    "<API_TOKEN>' or unset API_TOKEN for local-only use."
                ),
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not secrets.compare_digest(supplied, config.API_TOKEN):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"status": "error", "message": "Invalid bearer token"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    return None


def token_for_template():
    """
    The value handed to the Jinja templates so the dashboards can
    authenticate their own fetch() calls.

    Empty string when auth is disabled, which the templates treat as
    "send no header".
    """

    return config.API_TOKEN or ""
