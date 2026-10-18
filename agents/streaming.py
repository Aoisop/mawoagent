"""Keep failed stream attempts out of committed answers and learning data."""
from __future__ import annotations

from .ui import print_info


class StreamAttempts:
    def _capture_stream_output(self):
        return {"turn": len(self._turn_output_buffer or []),
                "output": len(self._output_buffer or [])}

    def _discard_stream_output(self, state) -> None:
        emitted = self._turn_output_buffer is not None and len(self._turn_output_buffer) > state["turn"]
        if self._turn_output_buffer is not None:
            del self._turn_output_buffer[state["turn"]:]
        if self._output_buffer is not None:
            del self._output_buffer[state["output"]:]
        elif emitted:
            # Terminal text cannot be recalled; explicitly invalidate the partial
            # attempt. Browser adapters replace their provisional entries.
            print_info("Response interrupted; the partial answer above was discarded.")

    async def _with_stream_retry(self, request, *, retry):
        async def attempt():
            state = self._capture_stream_output()
            try:
                return await request()
            except BaseException:
                self._discard_stream_output(state)
                raise
        return await retry(attempt)
