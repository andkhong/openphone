"""Compatibility shim: the developer API moved to openmob/platform/ (principal, service, rest)."""

from .platform.principal import Principal, resolve  # noqa: F401
from .platform.rest import DeveloperApi  # noqa: F401
from .platform.service import (  # noqa: F401
    V1Service,
    clean_text,
    is_public_address,
    public_event,
    sign_webhook,
    webhook_url_ok,
)
