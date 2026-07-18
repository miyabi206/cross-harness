"""Contract tests that compare generated commands with installed CLI help."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from cross_harness.runner import _claude_command, _codex_command


HELP_TIMEOUT_SECONDS = 10
_OPTION = re.compile(r"(?<!\w)(--[A-Za-z0-9][A-Za-z0-9-]*|-[A-Za-z0-9])")


def _help_options(output: str) -> dict[str, bool]:
    """Return options from help and whether each option consumes one value.

    CLI help renders each option declaration on an indented line and separates
    its description with two or more spaces.  Restricting parsing to those
    declarations avoids treating example flags in prose as accepted options.
    """
    options: dict[str, bool] = {}
    for line in output.splitlines():
        declaration = line.lstrip()
        if not declaration.startswith("-"):
            continue
        declaration = re.split(r"\s{2,}", declaration, maxsplit=1)[0]
        flags = _OPTION.findall(declaration)
        if not flags:
            continue
        consumes_value = bool(re.search(r"<[^>]+>|\[[^]]+\]", declaration))
        for flag in flags:
            options[flag] = consumes_value
    return options


def _installed_help(cli_name: str, arguments: list[str]) -> dict[str, bool]:
    executable = shutil.which(cli_name)
    if executable is None:
        pytest.skip(f"{cli_name} is not installed")
    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            timeout=HELP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        pytest.skip(f"could not run {cli_name} help: {error}")
    if result.returncode:
        pytest.skip(f"{cli_name} help exited with {result.returncode}")
    options = _help_options(result.stdout + result.stderr)
    if not options:
        pytest.fail(f"could not parse any options from {cli_name} help")
    return options


def _command_flags(command: Iterable[str], options: dict[str, bool]) -> set[str]:
    """Extract flags from the implementation's exact command sequence.

    The option metadata parsed from help determines when the following command
    token is a value, preventing values that begin with ``-`` from being
    mistaken for flags.
    """
    arguments = list(command)
    flags: set[str] = set()
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--":
            break
        if token == "-" or not token.startswith("-"):
            index += 1
            continue
        flag = token.split("=", 1)[0]
        flags.add(flag)
        index += 1
        if "=" not in token and options.get(flag, False) and index < len(arguments):
            index += 1
    return flags


def _assert_command_flags_are_accepted(command: Iterable[str], options: dict[str, bool]) -> None:
    unsupported = _command_flags(command, options) - options.keys()
    assert not unsupported, f"CLI help does not accept generated flags: {sorted(unsupported)}"


def _codex_role() -> dict[str, object]:
    return {"harness": "codex", "model": "gpt-5.6", "effort": "medium", "write": True}


def test_codex_initial_command_flags_match_exec_help(tmp_path: Path) -> None:
    options = _installed_help("codex", ["help", "exec"])

    command = _codex_command(Path(shutil.which("codex")), _codex_role(), tmp_path, tmp_path / "run")

    _assert_command_flags_are_accepted(command, options)


def test_codex_resume_command_flags_match_resume_help(tmp_path: Path) -> None:
    options = _installed_help("codex", ["help", "exec", "resume"])

    command = _codex_command(
        Path(shutil.which("codex")), _codex_role(), tmp_path, tmp_path / "run", "thread-id"
    )

    _assert_command_flags_are_accepted(command, options)


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--sandbox", "workspace-write"),
        ("-C", "/tmp"),
        ("--add-dir", "/tmp"),
    ],
)
def test_codex_resume_contract_rejects_initial_only_flags(
    tmp_path: Path, flag: str, value: str
) -> None:
    options = _installed_help("codex", ["help", "exec", "resume"])
    command = _codex_command(
        Path(shutil.which("codex")), _codex_role(), tmp_path, tmp_path / "run", "thread-id"
    )
    command[-1:-1] = [flag, value]

    with pytest.raises(AssertionError, match=re.escape(flag)):
        _assert_command_flags_are_accepted(command, options)


def test_claude_command_flags_match_help(tmp_path: Path) -> None:
    options = _installed_help("claude", ["--help"])
    role = {"harness": "claude", "model": "sonnet", "effort": "high", "write": False}
    command = _claude_command(
        Path(shutil.which("claude")), "reviewer", role, tmp_path, tmp_path / "run", tmp_path / "agents"
    )

    _assert_command_flags_are_accepted(command, options)
