"""Single source of truth for the runtime version.

The version lives in one importable place so the CLI, the packaging metadata a
future pyproject will need, and the release tag can all agree.
"""

__version__ = "0.2.0"
