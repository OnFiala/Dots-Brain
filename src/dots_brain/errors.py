"""Errors safe to present through the CLI and MCP boundary."""


class BrainError(Exception):
    code = "brain_error"


class ConflictError(BrainError):
    code = "revision_conflict"


class SuppressedError(BrainError):
    code = "source_suppressed"


class NotFoundError(BrainError):
    code = "not_found"


class InputError(BrainError):
    code = "invalid_input"


class CapabilityError(BrainError):
    code = "capability_unavailable"
