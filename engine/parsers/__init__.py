"""
engine/parsers — source-agnostic inventory parser interface.

New inventory sources (Azure Migrate, DR Migrate, etc.) should implement
the InventoryParser Protocol so they can be consumed by the pipeline without
modifications to downstream layers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable, TYPE_CHECKING

if TYPE_CHECKING:
    from engine.rvtools_parser import RVToolsInventory


@runtime_checkable
class InventoryParser(Protocol):
    """
    Protocol that all inventory parsers must satisfy.

    ``parse()`` accepts a file path and optional keyword arguments (sheet
    overrides, column aliases, etc.) and returns a normalised
    ``RVToolsInventory`` object that the consumption builder and the rest of
    the pipeline can consume unchanged.
    """

    def parse(self, path: str | Path, **kwargs) -> "RVToolsInventory": ...
