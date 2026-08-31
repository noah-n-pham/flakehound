"""Fact, control-plane, and derived tables (SPEC §4).

Empty for now: the next slice writes the single migration that creates every
entity at once. Alembic imports this module so autogenerate sees the metadata.
"""

from app.db import Base

__all__ = ["Base"]
