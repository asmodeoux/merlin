"""Typed CallbackData factories.

Each module exports a typed `CallbackData` subclass + helpers that
return `InlineKeyboardMarkup`. Handlers route on the typed payload via
`CallbackQueryHandler(pattern=...)` — no string parsing.
"""
