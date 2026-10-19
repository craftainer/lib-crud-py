"""The package ships its PEP 561 marker, so type checkers use its inline annotations."""

from importlib.resources import files


def test_package_ships_the_py_typed_marker() -> None:
    """Without py.typed, mypy/pyright treat every import of the package as untyped."""
    assert files("craftainer_tool_crud").joinpath("py.typed").is_file()
