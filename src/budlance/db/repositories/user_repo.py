"""User repository for managing Telegram-linked user identities."""

from uuid import UUID, uuid4
from typing import Any
from supabase import Client
from budlance.db.client import get_supabase_client
from budlance.db.models import User, utc_now


class UserRepository:
    """Data access repository for users."""

    def __init__(self, client: Client | None = None) -> None:
        self._client = client or get_supabase_client()
        # In-memory storage for offline / testing mode
        self._memory_store: dict[int, User] = {}

    def get_or_create_user(
        self,
        telegram_user_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> User:
        """Fetch an existing user by telegram_user_id or create a new record."""
        if self._client:
            response = (
                self._client.table("users")
                .select("*")
                .eq("telegram_user_id", telegram_user_id)
                .execute()
            )
            if response.data:
                return User.model_validate(response.data[0])

            # Insert new user
            new_user = User(
                id=uuid4(),
                telegram_user_id=telegram_user_id,
                username=username,
                first_name=first_name,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            res = (
                self._client.table("users")
                .insert({
                    "id": str(new_user.id),
                    "telegram_user_id": new_user.telegram_user_id,
                    "username": new_user.username,
                    "first_name": new_user.first_name,
                    "created_at": new_user.created_at.isoformat(),
                    "updated_at": new_user.updated_at.isoformat(),
                })
                .execute()
            )
            return User.model_validate(res.data[0])

        # Offline / in-memory fallback
        if telegram_user_id in self._memory_store:
            return self._memory_store[telegram_user_id]

        user = User(
            id=uuid4(),
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
        )
        self._memory_store[telegram_user_id] = user
        return user

    def get_user_by_telegram_id(self, telegram_user_id: int) -> User | None:
        """Find a user by Telegram ID."""
        if self._client:
            response = (
                self._client.table("users")
                .select("*")
                .eq("telegram_user_id", telegram_user_id)
                .execute()
            )
            if response.data:
                return User.model_validate(response.data[0])
            return None

        return self._memory_store.get(telegram_user_id)
