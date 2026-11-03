"""Compatibility shim — the FF Glory client now lives in ``app.glory_api``.

Kept so older imports (and any external code) keep working.
"""

from .glory_api import (  # noqa: F401
    DEFAULT_BASE_URL,
    FFGloryAPI,
    FFGloryAPIError,
    FFGloryBalance,
    FFGloryCoupon,
    FFGloryGroup,
    GloryAPI,
    GloryAPIError,
    extract_group_id,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "FFGloryAPI",
    "FFGloryAPIError",
    "FFGloryBalance",
    "FFGloryCoupon",
    "FFGloryGroup",
    "GloryAPI",
    "GloryAPIError",
    "extract_group_id",
]
