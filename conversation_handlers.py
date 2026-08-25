"""Optional multi-turn contract per challenge-brief.md §7.4.

The bot's HTTP layer (bot.py) already runs this exact logic against live
conversation state for /v1/reply. This module just exposes it as the
standalone `respond(state, merchant_message) -> dict` function the brief
asks for, for direct/offline testing.
"""
from bot import respond, conv_state  # noqa: F401
