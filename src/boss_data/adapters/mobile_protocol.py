"""BOSS Android read APIs, verified with the 14.110 application session.

Protocol references: the APK's net.bosszhipin.base.m / AbsBatchApiRequest and
https://github.com/1013503897/boss-cli (9923cf4dc3b0ff29ad53465ad37dcccbfbb9689f).
Local live search/detail captures established POST batch transport and response
header decoding. The account session stays inside this client.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import time
import uuid
import zlib
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import lz4.block


_HOST = "https://api5.zhipin.com"
_BATCH_PATH = "/api/batch/requests"
_SEARCH_PATH = "/api/zpgeek/app/geek/search/cardlist"
_CAPTCHA_TYPE_PATH = "/api/zpsecureflow/captcha/gettype"
_CAPTCHA_VALIDATE_PATH = "/api/zpsecureflow/captcha/validate"
_PROTOCOL_SALT = b"a308f3628b3f39f7d35cdebeb6920e21"
_FRAME_MAGIC = b"BZPBlock"
_RESPONSE_HEADERS = (
    "content-type", "content-length", "zp-encoding", "zp-encrypting", "zp-compressing",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _query(parameters: Mapping[str, Any]) -> str:
    """Match Java URLEncoder: spaces use '+', '*' stays literal, '~' is escaped."""
    pairs = []
    for name, value in sorted(parameters.items()):
        if value is None:
            text = ""
        elif isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)
        encoded = quote_plus(text, safe="*-._").replace("~", "%7E")
        pairs.append(f"{name}={encoded}")
    return "&".join(pairs)


def _rc4(data: bytes, account_key: str | None) -> bytes:
    key = _PROTOCOL_SALT + (account_key or "").encode("utf-8")
    permutation = list(range(256))
    right = 0
    for left in range(256):
        right = (right + permutation[left] + key[left % len(key)]) % 256
        permutation[left], permutation[right] = permutation[right], permutation[left]

    output = bytearray(len(data))
    left = right = 0
    for offset, value in enumerate(data):
        left = (left + 1) % 256
        right = (right + permutation[left]) % 256
        permutation[left], permutation[right] = permutation[right], permutation[left]
        stream = permutation[(permutation[left] + permutation[right]) % 256]
        output[offset] = value ^ stream
    return bytes(output)


def _encode_payload(data: bytes, account_key: str | None) -> bytes:
    compressed = lz4.block.compress(data, store_size=False)
    packed_size, original_size = len(compressed), len(data)
    header = _FRAME_MAGIC + struct.pack(
        "<IIII", 0, packed_size, original_size, packed_size ^ original_size,
    )
    return _rc4(header + compressed, account_key)


def _signature(data: str, account_key: str | None) -> str:
    signed = data.encode("utf-8") + _PROTOCOL_SALT + (account_key or "").encode("utf-8")
    return "V3.0" + hashlib.md5(signed).hexdigest()


def decode_response(body: bytes, headers: Mapping[str, str], account_key: str | None = None) -> dict:
    """Decode the response's declared base64, RC4 and BZPBlock/LZ4 transforms."""
    header_map = {name.lower(): value for name, value in headers.items()}
    encoding = header_map.get("zp-encoding", "0")
    encryption = header_map.get("zp-encrypting", "0")
    compression = header_map.get("zp-compressing", "0")
    if encoding not in ("0", "1") or encryption not in ("0", "1") or compression not in ("0", "2"):
        raise ValueError(f"Unsupported BOSS response codecs: {encoding}/{encryption}/{compression}")
    if encoding == "1":
        body = base64.b64decode(body.translate(bytes.maketrans(b"-_~", b"+/=")))
    if encryption == "1":
        body = _rc4(body, account_key)
    if compression == "2":
        if body[:8] != _FRAME_MAGIC:
            raise ValueError("Unsupported BOSS compression frame")
        _, packed_size, original_size, _ = struct.unpack_from("<IIII", body, 8)
        body = lz4.block.decompress(body[24:24 + packed_size], uncompressed_size=original_size)
    return json.loads(body)


class BossMobileClient:
    """Use a local Android session for one Worker-selected API operation at a time."""

    def __init__(self, session_path: str | Path, egress: str = "direct", interval_seconds: float = 26,
                 on_started=None, on_finished=None, on_admit=None):
        self.session_path = Path(session_path).resolve()
        self._session = json.loads(self.session_path.read_text(encoding="utf-8-sig"))
        self.egress = egress
        self.log_path = Path(session_path).parent / "requests.jsonl"
        self.interval_seconds = interval_seconds
        self.on_started = on_started
        self.on_finished = on_finished
        self.on_admit = on_admit
        if egress == "direct":
            self._opener = build_opener(ProxyHandler({}))
        else:
            import socks
            from sockshandler import SocksiPyHandler
            # Proxy is supplied explicitly by the data-tool configuration.
            proxy = urlsplit(egress)
            self._opener = build_opener(ProxyHandler({}), SocksiPyHandler(
                socks.SOCKS5, proxy.hostname, proxy.port, rdns=True))
        self.last_response: dict[str, Any] = {}

    def _common_parameters(self) -> dict[str, str]:
        session = self._session
        info = session["client_info"]
        return {
            "client_info": info if isinstance(info, str) else _json(info),
            "curidentity": str(session["curidentity"]),
            "v": str(session["v"]),
            "uniqid": session["uniqid"],
            "req_time": str(time.time_ns() // 1_000_000),
        }

    def _request(self, method: str, path: str, parameters: dict[str, Any], *,
                 account_key: str | None, payload: dict | None = None,
                 authenticated: bool = True, form_body: bool = False) -> dict:
        from .mobile_request_queue import request_slot
        with request_slot(self.log_path.parent, self.interval_seconds) as (pacing, mark_started):
            return self._send_request(method, path, parameters, account_key=account_key, payload=payload,
                                      authenticated=authenticated, pacing=pacing, mark_started=mark_started,
                                      form_body=form_body)

    def _send_request(self, method: str, path: str, parameters: dict[str, Any], *,
                      account_key: str | None, payload: dict | None,
                      authenticated: bool, pacing: dict, mark_started,
                      form_body: bool = False) -> dict:
        if self.on_admit:
            self.on_admit()
        plain_query = _query({**self._common_parameters(), **parameters})
        encoded_parameters = _encode_payload(plain_query.encode("utf-8"), account_key)
        parameter_blob = base64.b64encode(encoded_parameters).translate(bytes.maketrans(b"+/=", b"-_~")).decode("ascii")
        body = None
        signed_text = path + plain_query
        if payload is not None:
            body = _encode_payload(_json(payload).encode("utf-8"), account_key)
            signed_text += str(zlib.crc32(body) & 0xffffffff)
        wire_query = (
            plain_query + "&sp=" + quote(parameter_blob, safe="")
            + "&sig=" + _signature(signed_text, account_key)
            + "&app_id=" + str(self._session["app_id"])
        )
        headers = {
            "User-Agent": self._session["user_agent"],
            "traceId": "A-" + str(uuid.uuid4()),
            "zp-accept-encoding": "1",
            "zp-accept-compressing": "3",
            "zp-accept-encrypting": "1",
        }
        if authenticated:
            headers["t2"] = self._session["t2"]
        if payload is not None:
            headers["Content-Type"] = "application/json"
        url = _HOST + path + "?" + wire_query
        if form_body:
            body = wire_query.encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            url = _HOST + path
        request = Request(url, data=body, headers=headers, method=method)
        self.last_response = {"method": method, "endpoint": path, "egress": self.egress,
                              "observed_at": datetime.now(timezone.utc).isoformat(), "pacing": pacing}
        mark_started(self.last_response["observed_at"])
        if self.on_started:
            self.on_started(dict(self.last_response))
        started = time.monotonic()
        try:
            response = self._opener.open(request, timeout=25)
        except HTTPError as error:
            with error:
                self._record_response(error)
                result = {"code": error.code, "message": str(error.reason)}
        except (URLError, TimeoutError, OSError) as error:
            result = {"code": None, "message": str(getattr(error, "reason", error)),
                      "transport_error": type(error).__name__}
        else:
            with response:
                self._record_response(response)
                body = response.read()
                try:
                    result = decode_response(body, response.headers, account_key)
                except (ValueError, UnicodeError, struct.error) as error:
                    result = {"code": None, "message": "Response decoding failed", "decode_error": type(error).__name__}
        self.last_response["elapsed_seconds"] = round(time.monotonic() - started, 3)
        self.last_response["code"] = result.get("code")
        self.last_response["message"] = result.get("message", "")
        if result.get("transport_error"):
            self.last_response["transport_error"] = result["transport_error"]
        sub = (result.get("zpData") or {}).get(_SEARCH_PATH) or {}
        if sub:
            self.last_response.update(sub_code=sub.get("code"), sub_message=sub.get("message", ""))
        sub_responses = [{"endpoint": endpoint, "code": value.get("code"), "message": value.get("message", "")}
                         for endpoint, value in (result.get("zpData") or {}).items()
                         if path.startswith("/api/batch/") and isinstance(value, dict) and "code" in value]
        if sub_responses:
            self.last_response["sub_responses"] = sub_responses
        challenge_codes = sorted({value for value in [result.get("code"), *(sub["code"] for sub in sub_responses)]
                                  if value in (35, 36)})
        if challenge_codes:
            directory = self.log_path.parent / "challenges"
            directory.mkdir(exist_ok=True)
            context_path = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex + ".local.json")
            self.last_response["challenge_context"] = {"path": str(context_path.resolve()), "codes": challenge_codes}
            context_path.write_text(json.dumps({
                "session_file": str(self.session_path),
                "request": {"method": method, "endpoint": path, "parameters": parameters, "payload": payload},
                "response_metadata": self.last_response, "response": result,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(_json(self.last_response) + "\n")
        if self.on_finished:
            self.on_finished(dict(self.last_response), result)
        return result

    def _record_response(self, response: Any) -> None:
        self.last_response.update(
            status=response.status,
            headers={name: response.headers[name] for name in _RESPONSE_HEADERS if name in response.headers},
        )

    def _get(self, path: str, parameters: dict[str, Any]) -> dict:
        return self._request("GET", path, parameters, account_key=self._session["secretKey"])

    def city(self) -> dict:
        return self._request("GET", "/api/zpCommon/config/city", {}, account_key=None, authenticated=False)

    def captcha_type(self) -> dict:
        """Initialize the APK's logged-in verification flow using its whitelist signing branch."""
        # mr.a.v3/w3 are in config.m.a: key=None, while BaseApiRequest still sends t2.
        return self._request("GET", _CAPTCHA_TYPE_PATH, {}, account_key=None)

    def validate_captcha(self, captcha_info: dict[str, Any]) -> dict:
        """Submit the actual SDK callback to MachineVerifyConfirmRequest's endpoint."""
        return self._request("POST", _CAPTCHA_VALIDATE_PATH,
                             {"captcha_info": _json(captcha_info)}, account_key=None, form_body=True)

    def search(self, query: str, city_code: str, page: int = 1, sort: int = -1,
               filters: dict[str, Any] | None = None, *, is_supply_search: bool = False) -> dict:
        conditions = {"cityCode": str(city_code), "chattedJob": 0, "switchCity": 1, **(filters or {})}
        parameters = {
            "query": query, "page": str(page), "sort": str(sort), "searchType": "3",
            "source": "1", "prefix": "q", "isFromJd": "0", "isSupplySearch": str(is_supply_search).lower(),
            "maskComType": "0", "noCorrect": "0", "filterParams": _json(conditions),
        }
        payload = {"subReqs": [{"method": "GET", "path": _SEARCH_PATH, "query": _query(parameters)}]}
        return self._request("POST", _BATCH_PATH, {}, account_key=None, payload=payload)

    def detail(self, security_id: str, lid: str = "", query: str = "") -> dict:
        return self._get("/api/zpgeek/jobapp/geek/job/querydetail", {
            "securityId": security_id, "lid": lid, "query": query, "keyword": query,
            "page": "1", "needRelatedJob": "true",
        })

    def filters(self, parameters: dict[str, Any]) -> dict:
        return self._get("/api/zpgeek/app/filter/data", parameters)

    def similar(self, parameters: dict[str, Any]) -> dict:
        return self._get("/api/zpgeek/jobapp/search/similar/joblist", parameters)

    def nearby(self, parameters: dict[str, Any]) -> dict:
        return self._get("/api/zpgeek/app/f1/nearest/joblist", parameters)

    def recommend(self, parameters: dict[str, Any]) -> dict:
        from .mobile_payload import RECOMMEND_PATH
        payload = {"subReqs": [{"method": "GET", "path": RECOMMEND_PATH, "query": _query(parameters)}]}
        return self._request("POST", _BATCH_PATH, {}, account_key=None, payload=payload)

    def company_jobs(self, parameters: dict[str, Any]) -> dict:
        """Read company jobs with the company-page context supplied by the caller."""
        path = "/api/zpgeek/app/geek/brand/queryjoblist"
        payload = {"subReqs": [{"method": "GET", "path": path, "query": _query(parameters)}]}
        return self._request("POST", _BATCH_PATH, {}, account_key=None, payload=payload)

    def company_info(self, parameters: dict[str, Any]) -> dict:
        path = "/api/zpgeek/app/brandInfo/getBrandInfo"
        payload = {"subReqs": [{"method": "GET", "path": path, "query": _query(parameters)}]}
        return self._request("POST", _BATCH_PATH, {}, account_key=None, payload=payload)

    def own_expectations(self) -> dict:
        return self._get("/api/zpgeek/cvapp/geek/baseinfo/query", {"userId": "0"})
