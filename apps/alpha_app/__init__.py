"""Explicit offline Alpha composition root.

Importing this package constructs no launcher, safety, exchange, or network
component.  Runtime compositions belong to later task-specific applications.
"""

from .composition import BoundLocalData, OfflineAlphaApp, OfflineAlphaResult

__all__ = ["BoundLocalData", "OfflineAlphaApp", "OfflineAlphaResult"]
