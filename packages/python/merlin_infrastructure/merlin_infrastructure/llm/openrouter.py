"""OpenRouter-backed ``ClaimDrafterPort`` implementation.

Single httpx call per draft. ``temperature=0`` + seeded by a hash of
the user text for reproducibility. Returns a domain ``ClaimDraft``.

Configuration (all wired through ``Settings``):

- ``OPENROUTER_API_KEY`` — required secret.
- ``OPENROUTER_BASE_URL`` — defaults to ``https://openrouter.ai/api/v1``.
- ``LLM_PRIMARY_MODEL`` — ``openai/gpt-5-mini`` by default. Sprint-1
  live calibration showed it ~4.5x cheaper per extraction than
  ``gemini-3.5-flash`` (the reasoning-mandatory generation inverted the
  historical Gemini-Flash-is-cheaper picture) AND emits better-calibrated
  per-field confidences (0.90-0.98 vs 0.99-flat) — exactly what our
  per-field confidence-gating logic relies on.
- ``LLM_FALLBACK_MODEL`` — ``google/gemini-3.5-flash`` by default.
  OpenRouter auto-routes to it via the request body's ``models``
  array on primary 5xx.
- ``LLM_MONTHLY_USD_BUDGET`` — soft cap; the use case is responsible for
  consulting the running spend and short-circuiting before calling.
  This adapter does not enforce the cap.

By design: the LLM is UX acceleration, never a system
dependency. Every exception raised here becomes a ``ClaimDrafterError``,
the use case catches it, the bot falls back to the button form. No
retry, no second LLM attempt — speed matters more than perfection on
the freeform path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from merlin_core.application.ports import ClaimDrafterError
from merlin_core.contracts.horizons import (
    HORIZON_MAX_SECONDS,
    HORIZON_MIN_SECONDS,
    HorizonParseError,
    parse_horizon_text,
)
from merlin_core.domain import (
    AssetCandidate,
    ClaimDraft,
    Conviction,
    Direction,
    ExtractedFields,
    Horizon,
    ParseMetadata,
)

from merlin_infrastructure.llm.prompts import LATEST_CLAIM_EXTRACTION_PROMPT, load_prompt

_log = logging.getLogger("merlin.infrastructure.llm.openrouter")

# Per-field confidence thresholds. Below threshold, the field appears
# in ``missing_fields`` and the bot surfaces it on the preview rather
# than auto-filling. Calibrated against the v1 prompt's example outputs.
#
# ``_HORIZON_CONFIDENCE_FLOOR`` was originally 0.85, which lined up
# with the v2 example outputs (0.95-0.97 on explicit "3 hours" / "2
# weeks"). Live Sprint-1 traffic surfaced a class of input where the
# duration is unambiguous to a human ("SOL long for 2 hours, must
# recover from a huge drop") but gpt-5-mini's well-calibrated
# confidence dips into 0.70-0.80 because of the surrounding
# contingency clause. The hard 0.85 gate then dropped the value and
# the bot forced the user to set the horizon manually — the exact
# UX failure we built the LLM path to avoid.
#
# Two compensating moves landed alongside the lowered floor:
# (a) the v3 prompt teaches the model that a contingency clause does
# NOT dilute horizon confidence (so the model itself reports higher
# numbers), and (b) ``_horizon_backstop_from_text`` below recovers a
# duration from the user text whenever the gate culls or the model
# returns null. Together the three moves make "for 2 hours" (and
# similar) round-trip even on adversarial phrasing. The range gate
# at ``[HORIZON_MIN_SECONDS, HORIZON_MAX_SECONDS]`` remains the
# real safety net — confidence is a UX gate, not a correctness gate.
_ASSET_CONFIDENCE_FLOOR = 0.85
_DIRECTION_CONFIDENCE_FLOOR = 0.9
_HORIZON_CONFIDENCE_FLOOR = 0.70
_CONVICTION_CONFIDENCE_FLOOR = 0.7

# Conviction reads tone ("could moon", "I'm pretty sure"), not keywords —
# the model gets it right less often than direction (explicit "long"/"short")
# or horizon (explicit "3 hours"/"2 weeks"). Insisting on a high-confidence read
# would force a manual pick on most freeform claims. "medium" is the centre
# of the four-value scale, so defaulting there is the lowest-information /
# least-prejudicial choice; the user can still tap Edit on the preview to
# change it. The default is applied *only* on the freeform-LLM path: the
# form path collects conviction via an explicit picker, never via this code.
_CONVICTION_DEFAULT: Conviction = "medium"

_DEFAULT_TIMEOUT_S = 15.0


class OpenRouterClaimDrafter:
    """Concrete ``ClaimDrafterPort`` using OpenRouter's chat-completions API."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        primary_model: str = "openai/gpt-5-mini",
        fallback_model: str = "google/gemini-3.5-flash",
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Store config; build an httpx client if one isn't passed in.

        Args:
            api_key: OpenRouter bearer token.
            base_url: Override for tests / future routing changes.
            primary_model: First-choice OpenRouter model slug.
            fallback_model: OpenRouter auto-routes to this on primary 5xx
                via the ``models`` array in the request body.
            timeout_s: httpx total timeout. 15s is generous for a
                temperature-0 ~150-token completion.
            http_client: Inject for tests (e.g. ``respx``-mocked).
        """
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._primary_model = primary_model
        self._fallback_model = fallback_model
        self._timeout_s = timeout_s
        self._client = http_client or httpx.AsyncClient(timeout=timeout_s)
        self._prompt_name = LATEST_CLAIM_EXTRACTION_PROMPT
        self._prompt_text = load_prompt(self._prompt_name)
        self._prompt_hash = hashlib.sha256(self._prompt_text.encode()).hexdigest()

    async def draft(self, text: str) -> ClaimDraft:
        """Run a single chat-completion and parse the result into a ``ClaimDraft``.

        Empty / whitespace text short-circuits to an empty draft (the
        use case should not have called us in that case, but we don't
        spend an LLM round-trip to discover it).
        """
        stripped = text.strip()
        if not stripped:
            return ClaimDraft(
                parse_metadata=ParseMetadata(
                    parsed_via="freeform_llm",
                    model=None,
                    raw_user_turns=(text,),
                    extracted_at=datetime.now(UTC),
                )
            )

        body = self._build_request_body(stripped)
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/asmodeoux/merlin",
                    "X-Title": "Merlin",
                },
                json=body,
            )
        except httpx.HTTPError as e:
            _log.warning("openrouter request failed: %s", e)
            raise ClaimDrafterError(f"OpenRouter transport failure: {e}") from e

        if response.status_code >= 500:
            raise ClaimDrafterError(f"OpenRouter 5xx: {response.status_code}")
        if response.status_code == 429:
            raise ClaimDrafterError("OpenRouter rate-limited (429)")
        if response.status_code >= 400:
            raise ClaimDrafterError(
                f"OpenRouter client error {response.status_code}: {response.text[:200]}"
            )

        payload = response.json()
        return self._parse_response(payload, stripped)

    def _build_request_body(self, user_text: str) -> dict[str, Any]:
        """Construct the JSON body for a chat-completions call."""
        # OpenRouter's ``models`` array enables automatic provider fallback
        # without a second adapter-level retry. The prompt is committed,
        # not assembled at runtime — so the hash is deterministic.
        #
        # ``reasoning.effort="minimal"`` is the OpenRouter normalised
        # knob for the lowest legal chain-of-thought budget. Both
        # gpt-5 and gemini-3.5-flash endpoints make reasoning mandatory
        # (a hard 400 if we send ``enabled=False``), but they honour
        # the effort knob — minimal trims hundreds of reasoning tokens
        # off the response and brings per-call cost back in line with
        # the pre-reasoning generation. Extraction is one-shot
        # pattern-matching, not math — minimal is plenty.
        # ``exclude=True`` keeps the reasoning tokens out of the
        # response body (we never read them anyway).
        #
        # ``max_tokens=4096`` is a generous safety margin so reasoning
        # tokens never starve the visible content out of the response
        # (Sprint-1 calibration saw ~720 reasoning tokens at default
        # effort on a 119-token prompt; minimal effort + this ceiling
        # leaves a 10x headroom).
        return {
            "models": [self._primary_model, self._fallback_model],
            "temperature": 0,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
            "reasoning": {"effort": "minimal", "exclude": True},
            "messages": [
                {"role": "system", "content": self._prompt_text},
                {"role": "user", "content": user_text},
            ],
        }

    def _parse_response(self, payload: dict[str, Any], user_text: str) -> ClaimDraft:
        """Pull the JSON content out of the completion and decode into a draft."""
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ClaimDrafterError(f"unexpected OpenRouter shape: {e}") from e

        if content is None or content == "":
            # gpt-5 / o-series occasionally return ``content: null`` when
            # the reasoning budget ate the entire ``max_tokens`` window
            # OR the model refused. Surface ``finish_reason`` so the
            # cause is in the log without us having to reproduce.
            finish_reason = choice.get("finish_reason")
            refusal = choice.get("message", {}).get("refusal")
            raise ClaimDrafterError(
                f"LLM returned empty content (finish_reason={finish_reason!r}, refusal={refusal!r})"
            )

        # Some models wrap their JSON in markdown fences (```json ... ```)
        # despite the ``response_format`` hint. Strip them before parsing.
        content = _strip_code_fences(content)

        try:
            extracted = json.loads(content)
        except json.JSONDecodeError as e:
            raise ClaimDrafterError(f"LLM returned non-JSON: {e}") from e

        if not isinstance(extracted, dict):
            raise ClaimDrafterError("LLM returned non-object JSON")

        model_used = payload.get("model", self._primary_model)
        confidences: dict[str, float] = extracted.get("confidences", {}) or {}

        # Apply per-field confidence floors. Below threshold, the field
        # is None and appears in ``missing_fields``.
        direction = _gated(
            extracted.get("direction"),
            confidences.get("direction"),
            _DIRECTION_CONFIDENCE_FLOOR,
        )
        # Horizon is now an integer-seconds count instead of a Literal
        # bucket. The latest prompt asks the model to emit
        # ``horizon_seconds`` directly; we accept either key so a stale
        # model snapshot that still follows the v1 ``"3h"`` / ``"14d"``
        # shape doesn't break extraction. Range-bound + confidence-gate
        # before we trust it.
        horizon_raw = extracted.get("horizon_seconds")
        if horizon_raw is None:
            horizon_raw = extracted.get("horizon")
        horizon_confidence = confidences.get("horizon_seconds")
        if horizon_confidence is None:
            horizon_confidence = confidences.get("horizon")
        horizon_seconds = _coerce_horizon_seconds(
            _gated(horizon_raw, horizon_confidence, _HORIZON_CONFIDENCE_FLOOR)
        )
        # Deterministic backstop: when the LLM didn't deliver a usable
        # horizon (low confidence, null emission, out-of-range value)
        # but the user text has an unambiguous "for / in / within N
        # <unit>" phrase, recover it via the canonical domain parser.
        # The range gate inside ``_horizon_backstop_from_text`` keeps
        # garbage out; the conservative preposition set keeps false
        # positives down. Logged so the audit trail is reconstructable
        # from the parse_metadata confidences + the application log.
        if horizon_seconds is None:
            backstop_seconds = _horizon_backstop_from_text(user_text)
            if backstop_seconds is not None:
                _log.info(
                    "horizon backstop recovered %ds from user text "
                    "(LLM raw=%r, confidence=%s, floor=%s)",
                    backstop_seconds,
                    horizon_raw,
                    horizon_confidence,
                    _HORIZON_CONFIDENCE_FLOOR,
                )
                horizon_seconds = backstop_seconds
        conviction = _gated(
            extracted.get("conviction"),
            confidences.get("conviction"),
            _CONVICTION_CONFIDENCE_FLOOR,
        )
        asset_query = _gated(
            extracted.get("asset_query"),
            confidences.get("asset_query"),
            _ASSET_CONFIDENCE_FLOOR,
        )

        typed_conviction: Conviction = _typed(
            conviction, ("weak", "medium", "strong", "very_strong")
        )
        if typed_conviction is None:
            # See _CONVICTION_DEFAULT — fall through to "medium" instead of
            # surfacing conviction as a missing field. Log so a low-confidence
            # or out-of-domain extraction is still visible post-hoc.
            _log.info(
                "LLM extraction missing conviction (raw=%r, confidence=%s); defaulting to %r",
                extracted.get("conviction"),
                confidences.get("conviction"),
                _CONVICTION_DEFAULT,
            )
            typed_conviction = _CONVICTION_DEFAULT

        missing: list[str] = []
        if direction is None:
            missing.append("direction")
        if horizon_seconds is None:
            missing.append("horizon")
        if asset_query is None:
            missing.append("asset")

        ef_payload = extracted.get("extracted_fields") or {}
        named_entities_raw = ef_payload.get("named_entities") or ()
        extracted_fields = ExtractedFields(
            schema_version=int(ef_payload.get("schema_version", 1)),
            target_price_usd=_maybe_float(ef_payload.get("target_price_usd")),
            calendar_date_hint=ef_payload.get("calendar_date_hint"),
            rumour_referenced=bool(ef_payload.get("rumour_referenced", False)),
            named_entities=tuple(str(x) for x in named_entities_raw),
        )

        asset_query_str = asset_query if isinstance(asset_query, str) else None
        return ClaimDraft(
            asset_candidates=(),  # API layer fills this after CoinGecko /search
            asset_query=asset_query_str,
            direction=_typed(direction, ("long", "short")),
            horizon=horizon_seconds,
            conviction=typed_conviction,
            thesis_text=extracted.get("thesis_text") or user_text,
            extracted_fields=extracted_fields,
            parse_metadata=ParseMetadata(
                parsed_via="freeform_llm",
                model=model_used,
                model_version=payload.get("model_version"),
                prompt_hash=f"sha256:{self._prompt_hash}",
                raw_user_turns=(user_text,),
                confidences={
                    k: float(v) for k, v in confidences.items() if isinstance(v, int | float)
                },
                extracted_at=datetime.now(UTC),
            ),
            missing_fields=tuple(missing),
        )


def _strip_code_fences(text: str) -> str:
    """Strip a leading/trailing markdown code fence around a JSON payload.

    Handles ``` ```json ... ``` ``` and plain ``` ``` ... ``` ``` variants
    that some models emit even when ``response_format=json_object`` is
    set. Whitespace-only input is returned unchanged.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    # Drop the opening fence (and an optional ``json`` language tag).
    first_newline = stripped.find("\n")
    if first_newline == -1:
        return stripped
    body = stripped[first_newline + 1 :]
    # Drop the closing fence if present.
    if body.rstrip().endswith("```"):
        body = body.rstrip()[: -len("```")].rstrip()
    return body


def _gated(value: object, confidence: float | None, floor: float) -> object | None:
    """Return ``value`` when ``confidence >= floor``, else ``None``."""
    if value is None or value == "":
        return None
    if confidence is None:
        return None
    if confidence < floor:
        return None
    return value


def _typed(value: object, allowed: tuple[str, ...]) -> Any:
    """Cast a raw LLM string to the typed Literal alias if it's in the allowed set."""
    if value is None:
        return None
    if isinstance(value, str) and value in allowed:
        return value
    return None


def _coerce_horizon_seconds(value: object) -> int | None:
    """Coerce the LLM's horizon extraction into bounded integer seconds.

    Accepts:
    - ``int`` / ``float`` interpreted as a literal second count.
    - ``str`` in the old prompt-v1 bucket shape (``"3h"`` / ``"14d"``
      / ``"3 months"`` / etc.) so a stale prompt or a model that
      refuses the new schema still works without a 500. Reuses the
      domain parser to honour the canonical mapping.

    Returns ``None`` when the value can't be coerced or falls outside
    ``[HORIZON_MIN_SECONDS, HORIZON_MAX_SECONDS]``. The caller treats
    ``None`` as "the model didn't deliver a usable horizon" and either
    runs ``_horizon_backstop_from_text`` or adds the field to
    ``missing_fields``, which the bot surfaces as the picker.
    """
    seconds = _value_to_seconds(value)
    if seconds is None:
        return None
    if seconds < HORIZON_MIN_SECONDS or seconds > HORIZON_MAX_SECONDS:
        return None
    return seconds


# Conservative preposition set: "for / in / within / over" are the
# four phrasings that idiomatically introduce a horizon in a trading
# claim ("for 2 hours", "in 3 days", "within 2 weeks", "over 24
# hours"). Deliberately excludes "after" / "by" / "next" because
# those frequently introduce referential time phrases that are NOT
# the user's horizon ("after 2 hours of analysis", "by 2 weeks ago",
# "the next analysis I do"). The preposition must sit immediately
# before the number — "over the next 24 hours" is intentionally
# missed (no number right after "over"); the v3 prompt teaches the
# model to handle that phrasing itself. Better to under-recover than
# over-guess — the user can always edit the horizon on the preview,
# but a wrong auto-fill costs a "wait, that's not right" beat of
# confusion.
#
# The duration body is intentionally loose (any digits / decimals or
# the word-numbers ``a/an/one..twelve``, then any letters) — we
# delegate the actual validation to ``parse_horizon_text`` rather
# than re-encoding the unit table here. That keeps the canonical
# parser (``merlin_core.contracts.horizons``) as the single source
# of truth for what counts as a valid duration and what doesn't.
_HORIZON_BACKSTOP_RE = re.compile(
    r"\b(?:for|in|within|over)\s+"
    r"((?:\d+(?:\.\d+)?|"
    r"a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"\s*[A-Za-z]+)\b",
    re.IGNORECASE,
)


def _horizon_backstop_from_text(text: str) -> int | None:
    """Recover an explicit duration from the user text when the LLM punts.

    Scans ``text`` for the strict ``<prep> <number> <unit>`` shape
    (``"for 2 hours"``, ``"in 1.5 days"``, ``"within two weeks"``) and
    returns the first match that the canonical horizon parser
    accepts AND that falls inside the legal range. Used as a
    deterministic safety net when the LLM extraction produced a None
    horizon (low confidence + culled, null emission, out-of-range
    value) despite the user clearly stating a duration.

    Returns ``None`` when nothing matches. Callers then surface
    ``horizon`` in ``missing_fields`` so the bot opens the picker.
    """
    for match in _HORIZON_BACKSTOP_RE.finditer(text):
        candidate = match.group(1).strip()
        try:
            return parse_horizon_text(candidate)
        except (HorizonParseError, ValueError):
            # ``HorizonRangeError`` is a ``ValueError`` subclass — out-
            # of-range candidates fall through to the next match
            # rather than getting accepted with a clamp, since
            # "for 6 months" almost always means the user wants a
            # value the system can't yet resolve, not 90 days.
            continue
    return None


def _value_to_seconds(value: object) -> int | None:
    """Translate the raw LLM payload into an integer second count.

    Single place that owns the "what shapes does the model emit?"
    decision. The range gate lives in the caller so both the int /
    float and string paths share one set of bounds.

    ``bool`` is rejected explicitly because it's a subclass of
    ``int`` — without the guard, ``True`` would slip through as
    ``1`` second.

    ``HorizonParseError`` and the broader ``ValueError``
    (``HorizonRangeError`` subclasses ``ValueError``) are caught
    uniformly: we never want a parse-vs-range distinction to leak
    out of this helper; the caller's range gate is the single
    source of truth.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            return parse_horizon_text(value)
        except (HorizonParseError, ValueError):
            return None
    return None


def _maybe_float(value: object) -> float | None:
    """Best-effort float coercion that returns ``None`` for non-numeric inputs."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# Help mypy with the Literal type aliases that come back as objects above.
_Direction = Direction  # re-export so docstrings link cleanly
_Horizon = Horizon
_Conviction = Conviction
_AssetCandidate = AssetCandidate
