"""Chat transport implementations.

Pick one via config (``chat.transport``):

* ``mock``        – a local fake Gemini bot for testing with no credentials.
* ``webhook``     – posts via a Google Chat incoming webhook (send-only).
* ``google_chat`` – full Google Chat REST API client (post AND read replies).
"""

from .base import ChatClient, Reply  # noqa: F401


def build_chat_client(config) -> ChatClient:
    """Factory that instantiates the configured transport."""
    transport = config.chat.transport
    if transport == "mock":
        from .mock import MockChatClient

        return MockChatClient(config)
    if transport == "webhook":
        from .webhook import WebhookChatClient

        return WebhookChatClient(config)
    if transport == "google_chat":
        from .google_chat import GoogleChatClient

        return GoogleChatClient(config)
    raise ValueError(
        f"Unknown chat.transport {transport!r}. "
        "Expected one of: mock, webhook, google_chat."
    )
