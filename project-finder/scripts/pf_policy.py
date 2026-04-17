#!/usr/bin/env python3
"""
ProjectFinder — единая точка для политик auto-send.

До этого флаг `auto_send_first_message` был определён в трёх местах:
  1. config/auto-reply-config.json.global_defaults.auto_send_first_message
  2. config/auto-reply-config.json.first_message_policy.default
  3. config/developers/<id>.json.auto_reply_settings.auto_send_first_message

И ещё скилл `evaluate-and-initiate` жёстко зашивал таблицу:
    A/B без borderline + policy=='auto_send' → auto_send

Итог: разные части кода выбирали разные значения, профильный переопределитель
не читался, непонятно, что главнее. Эта функция — единственный источник правды.
Любой скилл или демон, которому нужно понять «какой статус ставить outgoing?»,
обязан звать её.

Приоритеты, от слабого к сильному:
  1. Дефолт из global_cfg.first_message_policy.default (обычно "auto_send").
  2. developer.auto_reply_settings.auto_send_first_message
     (true → разрешает auto_send, false → принуждает к review независимо
      от score_letter).
  3. job.match.auto_send (если задано на уровне конкретной вакансии).

Плюс безусловные правила (они побеждают всегда):
  - score_letter == "Skip" — outgoing вообще не создаётся, функция вернёт None.
  - borderline == True    — всегда needs_review.
  - score_letter == "C"   — всегда needs_review (C по определению пограничный).
  - confidence == "LOW"   — всегда needs_review + высокий urgency в уведомлении.
  - global_cfg.first_message_policy.default == "always_review" — всегда review.
"""

from __future__ import annotations

from typing import Literal, Optional

OutgoingStatus = Literal["ready", "needs_review"]


def decide_outgoing_status(
    *,
    score_letter: str,
    borderline: bool,
    developer: Optional[dict] = None,
    global_cfg: Optional[dict] = None,
    job_override: Optional[bool] = None,
    confidence: Optional[str] = None,
) -> Optional[OutgoingStatus]:
    """
    Возвращает "ready" | "needs_review" для новой outgoing-записи, либо None
    если outgoing не должно существовать (Skip).

    Аргументы:
      score_letter    — 'A'/'B'/'C'/'Skip' из evaluate-job.
      borderline      — флаг «есть реальные сомнения» (см. Фазу 1 скилла).
      developer       — dict загруженного developers/<id>.json, либо None.
      global_cfg      — dict загруженного auto-reply-config.json, либо None.
      job_override    — bool|None; если задано (обычно в match_json.auto_send)
                        — перебивает dev/global (True → allow auto, False → force review).
      confidence      — опционально 'HIGH'/'MEDIUM'/'LOW'; LOW всегда review.
    """
    if score_letter == "Skip":
        return None

    # Безусловные правила, побеждают всегда
    if confidence and str(confidence).upper() == "LOW":
        return "needs_review"
    if borderline:
        return "needs_review"
    if score_letter == "C":
        return "needs_review"

    # Глобальная политика (дефолт — auto_send)
    fmp_default = "auto_send"
    if isinstance(global_cfg, dict):
        fmp = (global_cfg.get("first_message_policy") or {}).get("default")
        if isinstance(fmp, str):
            fmp_default = fmp
    if fmp_default == "always_review":
        return "needs_review"

    # Переопределение из профиля разработчика
    dev_allow: Optional[bool] = None
    if isinstance(developer, dict):
        ars = developer.get("auto_reply_settings") or {}
        if "auto_send_first_message" in ars:
            dev_allow = bool(ars["auto_send_first_message"])

    # Переопределение из самой вакансии (match_json.auto_send)
    if job_override is not None:
        final_allow = bool(job_override)
    elif dev_allow is not None:
        final_allow = dev_allow
    else:
        # Только глобальная политика осталась. auto_send → True, иное → False.
        final_allow = (fmp_default == "auto_send")

    return "ready" if final_allow else "needs_review"


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    assert decide_outgoing_status(score_letter="A", borderline=False) == "ready"
    assert decide_outgoing_status(score_letter="A", borderline=True) == "needs_review"
    assert decide_outgoing_status(score_letter="C", borderline=False) == "needs_review"
    assert decide_outgoing_status(score_letter="B", borderline=False,
                                  confidence="LOW") == "needs_review"
    assert decide_outgoing_status(score_letter="Skip", borderline=False) is None

    g = {"first_message_policy": {"default": "always_review"}}
    assert decide_outgoing_status(score_letter="A", borderline=False,
                                  global_cfg=g) == "needs_review"

    d_force_review = {"auto_reply_settings": {"auto_send_first_message": False}}
    assert decide_outgoing_status(score_letter="A", borderline=False,
                                  developer=d_force_review) == "needs_review"

    d_allow = {"auto_reply_settings": {"auto_send_first_message": True}}
    assert decide_outgoing_status(score_letter="B", borderline=False,
                                  developer=d_allow,
                                  global_cfg={"first_message_policy":
                                              {"default": "auto_send"}}) == "ready"

    # job_override перебивает всё
    assert decide_outgoing_status(score_letter="A", borderline=False,
                                  developer=d_force_review,
                                  job_override=True) == "ready"
    print("pf_policy: all smoke tests passed.")
