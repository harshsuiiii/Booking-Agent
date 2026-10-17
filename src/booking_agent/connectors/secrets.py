import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretsProvider(Protocol):
    """Interface for secret retrieval. Concrete implementations hide the backing store."""

    def get(self, key: str) -> str:
        """Return the secret value for *key*. Raise KeyError if not found."""
        ...


class EnvSecretsProvider:
    """
    Dev-only implementation. Reads from environment variables.
    Do NOT use in production — use a secrets manager (AWS Secrets Manager, etc.).
    """

    def get(self, key: str) -> str:
        value = os.environ.get(key)
        if value is None:
            raise KeyError(f"Environment variable not set: {key}")
        return value
