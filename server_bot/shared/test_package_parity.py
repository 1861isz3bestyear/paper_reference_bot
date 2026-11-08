# Copyright 2026 anchored-vwam-backtest contributors
"""Both deployable projects must carry exactly the same shared source tree."""

from pathlib import Path

import pytest


def test_calculator_and_server_shared_directories_are_identical() -> None:
    """Verify calculator and server shared directories are identical."""
    for ancestor in Path(__file__).resolve().parents:
        calculator = ancestor / "backtest/shared"
        server = ancestor / "paper/server_bot/server_bot/shared"
        if calculator.is_dir() and server.is_dir():
            break
    else:
        pytest.skip(
            "Shared parity requires both sibling projects in the workspace"
        )

    def source_files(directory: Path) -> dict[Path, bytes]:
        return {
            path.relative_to(directory): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
            and not {"__pycache__", ".pytest_cache"}.intersection(
                path.relative_to(directory).parts
            )
            and path.suffix not in {".pyc", ".pyo"}
        }

    calculator_files = source_files(calculator)
    server_files = source_files(server)
    assert calculator_files.keys() == server_files.keys(), (
        "Only in calculator/shared:"
        f" {sorted(calculator_files.keys() - server_files.keys())}; only in"
        " server/shared:"
        f" {sorted(server_files.keys() - calculator_files.keys())}"
    )
    differences = [
        name
        for name in calculator_files
        if calculator_files[name] != server_files[name]
    ]
    assert (
        not differences
    ), f"Shared files differ byte-for-byte: {sorted(differences)}"
