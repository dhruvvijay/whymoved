"""Framework-free application layer: accounts, workspaces, uploads, analyses, share links.

The HTTP layer (whymoved/api.py) only translates requests into calls on `Service`, so every
rule that matters (who may see what, size limits, password hashing, share-link scope) is
tested without a web framework.
"""

from whymoved.service.core import Service, ServiceError, ServiceSettings

__all__ = ["Service", "ServiceError", "ServiceSettings"]
