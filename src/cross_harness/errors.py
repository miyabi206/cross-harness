class HarnessError(Exception):
    """Expected fail-closed error suitable for a short user-facing message."""


class ConfigError(HarnessError):
    """Configuration is missing, unknown, or unsafe."""


class AuthError(HarnessError):
    """Subscription authentication could not be proven."""


class DirtyWorktreeError(HarnessError):
    """A write delegation would overlap user changes."""


class SupervisorDiedError(HarnessError):
    """A detached delegation supervisor exited before finalizing its run."""
