"""Small statistics helpers used by the reporting code."""


def mean(values):
    """Return the arithmetic mean of a non-empty sequence of numbers."""
    return sum(values) / len(values)
