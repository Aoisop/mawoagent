"""Bounded, recoverable Skill learning jobs owned by the main runtime."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import uuid
import hashlib


class BackgroundSkillTasks:
    def _capture_skill_feedback(self, feedback: str) -> None:
        window = self._pop_pending_skill_extraction_window(feedback)
        if window is not None:
            self._skill_feedback_outbox.append(window)
            # Save before requesting the next answer: its failure/cancellation
            # must not consume feedback for an already completed turn.
            self._auto_save(strict=True)

    def _flush_skill_feedback(self) -> None:
        if self.permission_mode == "plan" or not self._online_evolution_enabled():
            return
        while self._skill_feedback_outbox:
            window = self._skill_feedback_outbox[0]
            identity = json.dumps(window, ensure_ascii=False, sort_keys=True)
            job_id = "feedback-" + hashlib.sha256(identity.encode()).hexdigest()
            accepted = self._schedule_background_skill_task(
                self._run_online_skill_evolution(window),
                job={"kind": "evolution", "window": window}, job_id=job_id)
            if not accepted:
                break
            self._skill_feedback_outbox.pop(0)
            self._auto_save(strict=True)

    def _background_jobs_dir(self) -> Path:
        from .skill_evolution import get_evolution_dir
        return get_evolution_dir() / "pending-jobs"

    def _resume_background_skill_jobs(self) -> None:
        if self.is_sub_agent or not self._online_evolution_enabled() or self.permission_mode == "plan":
            return
        for path in sorted(self._background_jobs_dir().glob("*.json")):
            if len(self._background_skill_tasks) >= self.background_skill_queue_limit:
                break
            if path.stem in self._background_job_ids:
                continue
            try:
                job = json.loads(path.read_text())
                if not isinstance(job, dict):
                    raise ValueError("Invalid persisted Skill job")
                if job.get("attempts", 0) >= 3:
                    continue
                if job.get("owner_pid") and job.get("owner_session") != self.session_id:
                    if os.name == "nt":
                        continue
                    try:
                        os.kill(job["owner_pid"], 0)
                        continue
                    except ProcessLookupError:
                        pass
                data = job["data"]
                if data["kind"] == "evolution":
                    coro = self._run_online_skill_evolution(data["window"])
                elif data["kind"] == "usage":
                    coro = self._run_skill_usage_tracking(data["user"], data["assistant"], hits=data["hits"])
                else:
                    continue
                from .storage import state_lock
                with state_lock(path):
                    # Re-read ownership after acquiring the cross-process claim.
                    latest = json.loads(path.read_text())
                    if latest.get("owner_pid") and latest.get("owner_session") != job.get("owner_session"):
                        coro.close()
                        continue
                    self._schedule_background_skill_task(coro, job=data, job_id=path.stem)
            except (OSError, ValueError, KeyError, TypeError):
                logging.exception("Unable to recover background Skill job %s", path)

    def _schedule_background_skill_task(self, coro, *, job: dict | None = None, job_id: str | None = None) -> bool:
        if self.permission_mode == "plan" or (job is not None and not self._online_evolution_enabled()):
            try:
                coro.close()
            except Exception:
                pass
            return False
        if job_id and job_id in self._background_job_ids:
            coro.close()
            return True
        from .storage import write_json
        path = None
        record = None
        if job is not None:
            job_id = job_id or uuid.uuid4().hex
            path = self._background_jobs_dir() / f"{job_id}.json"
            try:
                previous = json.loads(path.read_text()) if path.exists() else {}
                record = {"data": job, "status": "queued", "attempts": previous.get("attempts", 0),
                          "owner_pid": os.getpid(), "owner_session": self.session_id}
                write_json(path, record)
            except Exception:
                coro.close()
                raise
        if len(self._background_skill_tasks) >= self.background_skill_queue_limit:
            coro.close()
            logging.warning("Background Skill queue full; persisted jobs will retry on the next turn")
            return path is not None
        if self._background_semaphore is None:
            self._background_semaphore = asyncio.Semaphore(self.background_skill_concurrency)
        async def bounded():
            try:
                async with self._background_semaphore:
                    if path:
                        record.update(status="running", attempts=record["attempts"] + 1)
                        write_json(path, record)
                    result = await coro
                    if path:
                        if isinstance(result, dict) and result.get("ok") is False:
                            record.update(status="failed", error=str(result.get("error", "operation failed")))
                            write_json(path, record)
                        else:
                            path.unlink(missing_ok=True)
            except asyncio.CancelledError:
                if path:
                    record["status"] = "queued"
                    record["owner_pid"] = None
                    write_json(path, record)
                raise
            except Exception as exc:
                if path:
                    record.update(status="failed", error=str(exc))
                    write_json(path, record)
                raise
            finally:
                coro.close()
        task = asyncio.create_task(bounded())
        self._background_skill_tasks.add(task)
        if job_id:
            self._background_job_ids.add(job_id)

        def _done(done_task: asyncio.Task) -> None:
            self._background_skill_tasks.discard(done_task)
            if job_id:
                self._background_job_ids.discard(job_id)
            try:
                done_task.result()
            except asyncio.CancelledError:
                logging.info("Background Skill task cancelled")
            except Exception:
                logging.exception("Background Skill task failed")

        task.add_done_callback(_done)
        return True

    async def drain_background_skill_tasks(self, *, cancel_on_timeout: bool = False) -> None:
        """Wait for evaluation inputs; only shutdown may cancel unfinished work."""
        tasks = [task for task in self._background_skill_tasks if not task.done()]
        if tasks:
            if not cancel_on_timeout:
                await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
                return
            _, pending = await asyncio.wait(tasks, timeout=self.shutdown_timeout)
            if pending:
                logging.warning("Cancelling %s background Skill tasks after %.1fs", len(pending), self.shutdown_timeout)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

