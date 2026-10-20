from config import DEFAULTS


def describe():
    return f"timeout={DEFAULTS['timeout_s']}s retries={DEFAULTS['retries']}"
