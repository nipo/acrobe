"""REST enumeration client.

Thin async wrapper around `aiohttp.ClientSession`. Returns the
parsed JSON dict described by `wire/server/rest.py`. Designed to be
used as an async context manager so connection cleanup is explicit:

    async with EnumerationClient("http://host:8080") as client:
        root = await client.enumerate("")
        chain = await client.enumerate("ub3-/jtag/chain")
"""

from urllib.parse import quote

import aiohttp


REST_PATH_PREFIX = "/v1/node"


class NodeNotFound(Exception):
    """Raised when a 404 comes back from the server. Carries the path."""

    def __init__(self, path: str, detail: str = ""):
        super().__init__(f"node not found: {path!r} ({detail})"
                         if detail else f"node not found: {path!r}")
        self.path = path
        self.detail = detail


class EnumerationClient:
    """Async REST client for `GET /v1/node/<path>`.

    `base_url` should not include the `/v1/node` prefix — the client
    appends it. Constructed with a fresh `aiohttp.ClientSession` by
    default; pass `session=` to share an existing one.
    """

    def __init__(self, base_url: str, *,
                 session: aiohttp.ClientSession | None = None):
        self.base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "EnumerationClient":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def enumerate(self, path: str) -> dict:
        """Fetch the node at `path`. Empty string = root."""
        if self._session is None:
            raise RuntimeError(
                "EnumerationClient must be used as an async context "
                "manager, or constructed with an explicit session")

        url = self._url_for(path)
        async with self._session.get(url) as resp:
            payload = await resp.json()
            if resp.status == 404:
                raise NodeNotFound(path, payload.get("detail", ""))
            resp.raise_for_status()
            return payload

    def _url_for(self, path: str) -> str:
        clean = path.strip("/")
        if not clean:
            return f"{self.base_url}{REST_PATH_PREFIX}"
        encoded = "/".join(quote(seg, safe="") for seg in clean.split("/"))
        return f"{self.base_url}{REST_PATH_PREFIX}/{encoded}"
