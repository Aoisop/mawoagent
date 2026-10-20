"""Duration parsing used by the scheduler."""

_UNITS = {
    "s": 1,
    "m": 60,
}


def parse_duration(text):
    """Parse a duration such as "45s" or "3m" and return whole seconds."""
    if not text or text[-1] not in _UNITS:
        raise ValueError(f"unsupported duration: {text!r}")
    digits = text[:-1]
    if not digits.isdigit():
        raise ValueError(f"unsupported duration: {text!r}")
    return int(digits) * _UNITS[text[-1]]
