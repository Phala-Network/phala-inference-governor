"""Soft throughput advice; no request admission or resource ownership."""
from .core import Governor, RevisionConflict

__all__ = ["Governor", "RevisionConflict"]
