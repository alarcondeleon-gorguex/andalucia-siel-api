from __future__ import annotations

import json
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Mapping

import requests
import urllib3

BASE_URL = "https://ws040.juntadeandalucia.es/siel-api"


class SielApiError(RuntimeError):
    """Raised when the SIEL API cannot be queried successfully."""

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
        response_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.response_text = response_text


@dataclass(frozen=True)
class SielClient:
    base_url: str = BASE_URL
    timeout: float = 60.0
    retries: int = 3
    pause: float = 0.25
    user_agent: str = "andalucia-siel-api-downloader/0.1"
    verify_tls: bool = False
    pool_size: int = 32
    _thread_local: threading.local = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_thread_local", threading.local())
        if not self.verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def build_url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        path = path if path.startswith("/") else f"/{path}"
        url = f"{self.base_url.rstrip('/')}{path}"
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"
        return url

    def session(self) -> requests.Session:
        current = getattr(self._thread_local, "session", None)
        if current is None:
            current = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=self.pool_size,
                pool_maxsize=self.pool_size,
                max_retries=0,
            )
            current.mount("https://", adapter)
            current.mount("http://", adapter)
            current.headers.update(
                {
                    "Accept": "application/json",
                    "User-Agent": self.user_agent,
                }
            )
            current.verify = self.verify_tls
            self._thread_local.session = current
        return current

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        url = self.build_url(path, params)
        last_error: BaseException | None = None
        last_status_code: int | None = None
        last_response_text: str | None = None

        for attempt in range(self.retries + 1):
            try:
                response = self.session().get(url, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, json.JSONDecodeError) as exc:
                last_error = exc
                response = getattr(exc, "response", None)
                if response is not None:
                    last_status_code = response.status_code
                    last_response_text = response.text[:500]
                if attempt >= self.retries:
                    break
                time.sleep(self.pause * (2**attempt))

        detail = f"HTTP {last_status_code}" if last_status_code else str(last_error)
        raise SielApiError(
            f"Could not fetch JSON from {url}: {detail}",
            url=url,
            status_code=last_status_code,
            response_text=last_response_text,
        ) from last_error

    def api_docs(self) -> Any:
        return self.get_json("/api-docs")

    def opciones(self, nombre: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        data = self.get_json(f"/v1/opciones/{nombre}", params)
        if not isinstance(data, list):
            raise SielApiError(f"Unexpected opciones/{nombre} response: {type(data).__name__}")
        return data

    def resumen_mesa(self, params: Mapping[str, Any]) -> dict[str, Any]:
        data = self.get_json("/v1/resumen/mesa", params)
        if not isinstance(data, dict):
            raise SielApiError(f"Unexpected resumen/mesa response: {type(data).__name__}")
        return data

    def escrutinio_mesa(self, params: Mapping[str, Any]) -> dict[str, Any]:
        data = self.get_json("/v1/escrutinio/ambito/mesa", params)
        if not isinstance(data, dict):
            raise SielApiError(f"Unexpected escrutinio/ambito/mesa response: {type(data).__name__}")
        return data

    def resumen_rausentes(self, ambito: str, params: Mapping[str, Any]) -> dict[str, Any]:
        data = self.get_json(f"/v1/resumen/rausentes/{ambito}", params)
        if not isinstance(data, dict):
            raise SielApiError(f"Unexpected resumen/rausentes/{ambito} response: {type(data).__name__}")
        return data

    def escrutinio_rausentes(self, ambito: str, params: Mapping[str, Any]) -> dict[str, Any]:
        data = self.get_json(f"/v1/escrutinio/ambito/rausentes/{ambito}", params)
        if not isinstance(data, dict):
            raise SielApiError(
                f"Unexpected escrutinio/ambito/rausentes/{ambito} response: {type(data).__name__}"
            )
        return data
