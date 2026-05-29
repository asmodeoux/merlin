"""``/alpha`` ConversationHandler — freeform-first claim capture (Q-028).

Two entry points, one downstream path:

- **Freeform.** User types anything that isn't a command. Bot calls
  ``POST /claims/draft`` with the raw text. The LLM extracts a
  prefilled ``ClaimDraft`` (asset query + direction + horizon +
  conviction + thesis + extracted_fields + confidences). The bot
  proxies the ``asset_candidates`` from CoinGecko's ``/search`` for
  ambiguous queries.
- **``/alpha`` command.** Bot shows "Describe a bet or tap to use
  buttons" and waits. An empty / no-text reply enters the deterministic
  button flow directly.

Both paths converge on **`step_preview.py`** — the prefilled summary
card. From the preview the user can:

- ``Confirm`` → bot composes the ``SubmitClaimRequest`` and POSTs to
  ``/claims``. Sends back the receipt (calendar resolve date, no
  internal scoring fields).
- ``Edit asset/direction/horizon/conviction/thesis`` → bot re-enters
  the matching button step inline, then returns to the preview with
  the field overwritten.
- ``Cancel`` → drop the draft from PTB memory. Zero DB writes.

If ``POST /claims/draft`` fails (LLM 5xx, OpenRouter rate-limited,
budget exceeded), the bot transparently enters the button flow
starting at the asset step. **No fallback paths in any downstream
component** — the rest of the system never sees whether the claim
came from LLM or buttons (it sees ``parse_metadata.parsed_via`` if
it cares).

Submodules:

- ``states.py`` — IntEnum of conversation states.
- ``conversation.py`` — ``ConversationHandler`` factory wiring everything.
- ``step_freeform.py`` — entry point that calls the LLM.
- ``step_preview.py`` — renders prefilled fields, Confirm/Edit/Cancel.
- ``step_asset.py`` — search-driven asset picker (Q-027 lazy resolve).
  Reachable as both the form-flow start and the "Edit asset" inline
  re-entry.
- ``step_direction.py`` / ``step_horizon.py`` / ``step_conviction.py``
  / ``step_thesis.py`` — single-question form steps. Reachable inline
  from the preview's "Edit X" buttons.
"""

from merlin_bot.handlers.alpha.conversation import build_handler

__all__ = ["build_handler"]
