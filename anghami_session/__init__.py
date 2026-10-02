"""Authenticated Anghami reads using a saved session, without a running browser."""

from .client import AnghamiSession
from .errors import SessionError

__all__ = ["AnghamiSession", "SessionError"]
