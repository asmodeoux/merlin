"""Cross-cutting middleware-style handlers (rate limiting, etc.).

PTB doesn't have first-class middleware; we approximate it with
``TypeHandler`` registered at ``group=-1`` so it runs before every
business handler.
"""
