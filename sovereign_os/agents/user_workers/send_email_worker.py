"""
SendEmailWorker: User-defined worker for skill "send_email".
Generated via Web UI. Edit this file to customize behavior.
"""

from __future__ import annotations

import logging
from sovereign_os.agents.base import BaseWorker, TaskInput, TaskResult

logger = logging.getLogger(__name__)


class SendEmailWorker(BaseWorker):
    """help me send email to the customer"""

    async def execute(self, task: TaskInput) -> TaskResult:
        desc = (task.description or "").strip() or task.task_id
        if not self.llm:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=f"[{self.__class__.__name__}] No LLM; echo: {desc[:200]}",
                metadata={"worker": "SendEmailWorker"},
            )
        prompt = f"Task: {desc}"
        try:
            system = (self.system_prompt or "You are a helpful assistant.").strip() or "You are a helpful assistant."
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]
            content = await self.llm.chat(messages)
            output = (content or "").strip() or "[No output]"
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=output[:65536],
                metadata={"worker": "SendEmailWorker"},
            )
        except Exception as e:
            logger.exception("SendEmailWorker execute failed: %s", e)
            return TaskResult(
                task_id=task.task_id,
                success=False,
                output=f"[{self.__class__.__name__}] Error: {e}",
                metadata={"worker": "SendEmailWorker", "error": str(e)},
            )
