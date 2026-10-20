from config import DEFAULTS


def describe():
    return f"timeout={DEFAULTS['timeout_seconds']}s retries={DEFAULTS['retries']}"
