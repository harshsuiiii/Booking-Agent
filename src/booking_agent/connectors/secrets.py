"""
Secrets provider interface and implementations.

Production should use AwsSecretsManagerProvider (or equivalent) instead of
EnvSecretsProvider so that credentials are never stored in env vars or config.
"""

import os
import time
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SecretsProvider(Protocol):
    """Interface for secret retrieval. Concrete implementations hide the backing store."""

    def get(self, key: str) -> str:
        """Return the secret value for *key*. Raise KeyError if not found."""
        ...


# ── dev / test ────────────────────────────────────────────────────────────────


class EnvSecretsProvider:
    """
    Dev-only implementation: reads from environment variables.
    Do NOT use in production — use AwsSecretsManagerProvider.
    """

    def get(self, key: str) -> str:
        value = os.environ.get(key)
        if value is None:
            raise KeyError(f"Environment variable not set: {key}")
        return value


# ── production ────────────────────────────────────────────────────────────────


class AwsSecretsManagerProvider:
    """
    Retrieve secrets from AWS Secrets Manager.

    Secret naming convention:
        booking-agent/{env}/{tenant_id}/{key}

    Where {env} is taken from the AWS_ENV environment variable (default: "prod").

    Rotation support: secrets are re-fetched when `cache_ttl_seconds` elapses,
    so a rotation takes effect within one TTL without a restart.

    Requires boto3 (install separately: pip install boto3).
    """

    def __init__(
        self,
        tenant_id: str,
        env: str | None = None,
        region: str | None = None,
        cache_ttl_seconds: float = 300.0,
    ) -> None:
        self._tenant_id = tenant_id
        self._env = env or os.getenv("AWS_ENV", "prod")
        self._region = region or os.getenv("AWS_DEFAULT_REGION", "ap-south-1")
        self._ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[str, float]] = {}  # key → (value, fetched_at)
        self._client: Any = None

    def _boto_client(self) -> Any:
        if self._client is None:
            try:
                import boto3  # type: ignore[import-not-found]
            except ImportError as exc:
                raise ImportError(
                    "boto3 is required for AwsSecretsManagerProvider. "
                    "Install it with: pip install boto3"
                ) from exc
            self._client = boto3.client("secretsmanager", region_name=self._region)
        return self._client

    def _secret_name(self, key: str) -> str:
        return f"booking-agent/{self._env}/{self._tenant_id}/{key}"

    def get(self, key: str) -> str:
        """
        Return the secret value, using a TTL cache to avoid excessive API calls.
        Raises KeyError if the secret does not exist.
        """
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None:
            value, fetched_at = cached
            if now - fetched_at < self._ttl:
                return value

        secret_name = self._secret_name(key)
        client = self._boto_client()
        try:
            # boto3 type stubs not installed in all envs; use getattr to call
            response = client.get_secret_value(SecretId=secret_name)
        except Exception as exc:
            # Wrap ResourceNotFoundException and others as KeyError
            raise KeyError(f"Secret not found: {secret_name}") from exc

        secret: str = response.get("SecretString", "")
        self._cache[key] = (secret, now)
        return secret

    def invalidate(self, key: str) -> None:
        """Force a re-fetch on the next get() — call after a known rotation."""
        self._cache.pop(key, None)
