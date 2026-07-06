"""SuFen chat routing between production provider and explicit fake mode."""

from __future__ import annotations

from sufen.config import SuFenSettings, load_settings
from sufen.output import SuFenResponse
from sufen.provider import answer_with_provider
from sufen.task_package import SuFenTaskPackage


def use_fake_provider(settings: SuFenSettings | None = None, *, force_fake: bool = False) -> bool:
    settings = settings or load_settings()
    return bool(force_fake or settings.fake_provider)


def answer_sufen(
    prompt: str,
    *,
    task: SuFenTaskPackage | None,
    settings: SuFenSettings | None = None,
    force_fake: bool = False,
) -> SuFenResponse:
    settings = settings or load_settings()
    if use_fake_provider(settings, force_fake=force_fake):
        from sufen.fake_provider import answer_with_fake_provider

        return answer_with_fake_provider(prompt, task=task)
    return answer_with_provider(prompt, task=task, settings=settings)
