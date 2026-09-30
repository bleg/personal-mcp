"""Mail providers. Each provider implements the MailProvider interface."""
from typing import Protocol


class MailProvider(Protocol):
    name: str

    def search(self, account: str, query: str, limit: int = 10) -> list[dict]: ...

    def get_message(self, account: str, message_id: str) -> dict: ...

    def get_conversation(self, account: str, conversation_id: str) -> list[dict]: ...

    def create_draft(
        self,
        account: str,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict: ...
