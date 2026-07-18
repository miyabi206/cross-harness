from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def expand_user(value: str, home: Path) -> Path:
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(os.path.expandvars(value)).expanduser()


@dataclass(frozen=True)
class UserPaths:
    home: Path

    @property
    def config(self) -> Path:
        return self.home / ".config/cross-harness/config.toml"

    @property
    def install_root(self) -> Path:
        return self.home / ".local/share/cross-harness/current"

    @property
    def executable(self) -> Path:
        return self.home / ".local/bin/cross-harness"

    @property
    def claude(self) -> Path:
        return self.home / ".claude"

    @property
    def codex(self) -> Path:
        return self.home / ".codex"


def user_paths(home: str | Path | None = None) -> UserPaths:
    actual = Path(home) if home is not None else Path.home()
    return UserPaths(actual.resolve())
