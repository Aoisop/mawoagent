"""Text helpers."""


def slugify(text):
    return "-".join(text.lower().split())
