from config import DEFAULTS


def request_timeout():
    return DEFAULTS["timeout_s"]


def retries():
    return DEFAULTS["retries"]
