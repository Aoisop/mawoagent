from config import DEFAULTS


def request_timeout():
    return DEFAULTS["timeout_seconds"]


def retries():
    return DEFAULTS["retries"]
