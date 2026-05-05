"""CapSolver-based image CAPTCHA solver.

Drop-in alternativa a TwoCaptchaSolver — mismo Protocol, distinta API
externa. Comparten el patrón retry/log de `BaseImageCaptchaSolver`.
"""

from __future__ import annotations

from typing import Any

from .base import BaseImageCaptchaSolver


class CapSolverSolver(BaseImageCaptchaSolver):
    name: str = "capsolver"
    # Keys donde CapSolver puede colocar el texto resuelto, por orden de
    # preferencia. Top-level y `solution.*` (la base helper revisa ambos).
    _ANSWER_KEYS = ("text", "answer", "captcha")

    def __init__(self, api_key: str, max_retries: int = 3) -> None:
        super().__init__(max_retries=max_retries)
        from capsolver_python import ImageToTextTask

        self._task = ImageToTextTask(client_key=api_key)

    def _call_provider(self, b64_image: str) -> str:
        task_id = self._task.create_task(base64_encoded_image=b64_image)
        result: Any = self._task.join_task_result(task_id)
        return self._extract_text_from_dict(result, self._ANSWER_KEYS)
