"""Shared HTTP setup.

Building an SSL context from certifi's bundle took 6-7 s per client on Windows (measured),
and the LLM adapters used to build a new client per call. One cached context from the OS
trust store (~0.5 s, once) is shared by every client instead.
"""

import ssl
from functools import lru_cache


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()
