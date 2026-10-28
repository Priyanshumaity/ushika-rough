"""Thin client for the WhatsApp Cloud API (Meta Graph API).

This is the only module that makes HTTP calls to Meta. Idempotent GETs (media
lookups and downloads) are retried on 429/5xx; message POSTs are never retried
automatically, because a retried send can deliver the same message twice.
"""

import logging

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Graph API error codes that are worth retrying later (throttling / temporary outages).
RETRYABLE_ERROR_CODES = {1, 2, 4, 17, 80007, 130429, 131000, 131016, 133004}


class WhatsAppAPIError(Exception):
    def __init__(self, message, *, status_code=None, code=None, title="", details="", payload=None, retryable=False):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.title = title or message
        self.details = details
        self.payload = payload or {}
        self.retryable = retryable

    @classmethod
    def from_response(cls, response: requests.Response) -> "WhatsAppAPIError":
        try:
            body = response.json()
        except ValueError:
            body = {}
        error = body.get("error") if isinstance(body, dict) else None
        error = error if isinstance(error, dict) else {}
        code = error.get("code")
        message = error.get("message") or f"HTTP {response.status_code} from WhatsApp Cloud API"
        return cls(
            message,
            status_code=response.status_code,
            code=code,
            title=error.get("error_user_title") or message,
            details=(error.get("error_data") or {}).get("details", "") or error.get("error_user_msg", ""),
            payload=body if isinstance(body, dict) else {},
            retryable=response.status_code == 429 or response.status_code >= 500 or code in RETRYABLE_ERROR_CODES,
        )


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class WhatsAppClient:
    def __init__(self, *, access_token=None, phone_number_id=None, session=None):
        config = settings.WHATSAPP
        self.access_token = access_token or config["ACCESS_TOKEN"]
        self.phone_number_id = phone_number_id or config["PHONE_NUMBER_ID"]
        self.base_url = f"{config['GRAPH_BASE_URL'].rstrip('/')}/{config['API_VERSION']}"
        self.timeout = config["REQUEST_TIMEOUT"]
        self.session = session or build_session()

    def _headers(self) -> dict:
        if not self.access_token:
            raise WhatsAppAPIError("WHATSAPP_ACCESS_TOKEN is not configured")
        return {"Authorization": f"Bearer {self.access_token}"}

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            response = self.session.request(method, url, headers=self._headers(), timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise WhatsAppAPIError(f"Could not reach WhatsApp Cloud API: {exc}", retryable=True) from exc
        if not response.ok:
            error = WhatsAppAPIError.from_response(response)
            logger.warning("WhatsApp API %s %s failed: %s (code %s)", method, response.status_code, error, error.code)
            raise error
        return response

    def send_message(self, payload: dict) -> dict:
        """POST /{phone_number_id}/messages. Returns Meta's JSON response."""
        if not self.phone_number_id:
            raise WhatsAppAPIError("WHATSAPP_PHONE_NUMBER_ID is not configured")
        response = self._request("POST", f"{self.base_url}/{self.phone_number_id}/messages", json=payload)
        return response.json()

    def get_media_info(self, media_id: str) -> dict:
        """GET /{media_id}: a short-lived download URL plus mime_type, sha256 and file_size."""
        return self._request("GET", f"{self.base_url}/{media_id}").json()

    def open_media_stream(self, url: str) -> requests.Response:
        """Start streaming a media file. The caller must close the response."""
        return self._request("GET", url, stream=True)
