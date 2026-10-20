"""Importing the renamed distribution warns that it moved to `craftainer-crud`."""

import importlib

import pytest

import craftainer_tool_crud


def test_import_emits_the_rename_deprecation_warning() -> None:
    """The final 0.1.x release points users at the new distribution and import name."""
    match = "craftainer-tool-crud is renamed to craftainer-crud"
    with pytest.warns(DeprecationWarning, match=match) as record:
        importlib.reload(craftainer_tool_crud)
    assert "import craftainer_crud" in str(record[0].message)
    assert "no further releases" in str(record[0].message)
