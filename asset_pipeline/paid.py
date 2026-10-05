"""Guard for paid APIs (Gemini, Claude, ...). CLAUDE.md: never call one without asking.

Paid backends call require_paid_allowed() before every request. It is off by default
and enabled only by the person running the pipeline, for the current process:

    AP_ALLOW_PAID_APIS=1 ap ...

so tests, scripts and agents can't spend money by accident.
"""
import os


class PaidAPIBlocked(RuntimeError):
    pass


def paid_allowed() -> bool:
    return os.environ.get("AP_ALLOW_PAID_APIS") == "1"


def require_paid_allowed(service: str) -> None:
    if not paid_allowed():
        raise PaidAPIBlocked(
            f"{service} is a paid API and paid calls are disabled. "
            f"Set AP_ALLOW_PAID_APIS=1 for this run to allow it.")
