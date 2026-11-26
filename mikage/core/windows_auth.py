"""
統合 Windows 認証 (IWA): requests signed in with the Windows logon of the
person at the PC (Kerberos, or NTLM where Kerberos cannot be used), for the
pages of a portal behind IIS's Windows authentication (`signIn` method
`iwa`, core/sign_in.py: its generateToken and `community/self`).

Python's standard library only: the HTTP exchange is `http.client` on one
kept connection (NTLM's challenge and answer must go on the connection that
asked), the tokens of the exchange come from Windows' own SSPI (secur32.dll,
the Negotiate package) through ctypes. No library is added (the usual one,
requests-negotiate-sspi, needs pywin32 and its DLLs in the plugin's folder,
see core/win64.py), and QGIS's own network stack is not used for these: it
answers a server's 401 with a user name and password window.

The connection is direct (no proxy: the portal is inside the network) and
TLS is checked with the system's certificates (on Windows the certificate
store, where a company's own root certificate is).
"""
from __future__ import annotations

import base64
import http.client
import ssl
import sys
import threading
from typing import Callable, Optional
from urllib.parse import urlencode, urlsplit

from .esri import BusyError, EsriError, UnreachableError

#: The schemes a server may ask for, best first.
SCHEMES = ("Negotiate", "NTLM")
#: Legs of an exchange before it is given up (Kerberos takes 1, NTLM 2).
MAX_LEGS = 4
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class SspiError(Exception):
    """Windows could not make the sign-in's next token (no logon to the domain, no such server, …)."""


class Context:
    """One exchange's security context: the next token from the server's (None at first)."""

    def step(self, token: Optional[bytes]) -> bytes:  # pragma: no cover (an interface)
        raise NotImplementedError

    def close(self) -> None:
        pass


#: Makes the context of one exchange: (scheme, target name `HTTP/host`) → Context.
ContextFactory = Callable[[str, str], Context]


def available() -> bool:
    """Whether this PC can sign in with the Windows logon (Windows only)."""
    return sys.platform == "win32"


class _Sspi(Context):
    """Windows' SSPI through ctypes (secur32.dll)."""

    _lib = None
    _lib_lock = threading.Lock()

    SECPKG_CRED_OUTBOUND = 2
    SECURITY_NATIVE_DREP = 0x10
    SECBUFFER_VERSION = 0
    SECBUFFER_TOKEN = 2
    #: ISC_REQ_CONNECTION | ISC_REQ_CONFIDENTIALITY | ISC_REQ_REPLAY_DETECT (as curl asks for HTTP).
    FLAGS = 0x800 | 0x10 | 0x4
    SEC_E_OK = 0
    SEC_I_CONTINUE_NEEDED = 0x00090312
    SEC_I_COMPLETE_NEEDED = 0x00090313
    SEC_I_COMPLETE_AND_CONTINUE = 0x00090314

    @classmethod
    def _api(cls):
        with cls._lib_lock:
            if cls._lib is not None:
                return cls._lib
            import ctypes

            class SecHandle(ctypes.Structure):
                _fields_ = [("dwLower", ctypes.c_size_t), ("dwUpper", ctypes.c_size_t)]

            class TimeStamp(ctypes.Structure):
                _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long)]

            class SecBuffer(ctypes.Structure):
                _fields_ = [("cbBuffer", ctypes.c_ulong), ("BufferType", ctypes.c_ulong), ("pvBuffer", ctypes.c_void_p)]

            class SecBufferDesc(ctypes.Structure):
                _fields_ = [("ulVersion", ctypes.c_ulong), ("cBuffers", ctypes.c_ulong), ("pBuffers", ctypes.POINTER(SecBuffer))]

            class SecPkgInfoW(ctypes.Structure):
                _fields_ = [("fCapabilities", ctypes.c_ulong), ("wVersion", ctypes.c_ushort), ("wRPCID", ctypes.c_ushort),
                            ("cbMaxToken", ctypes.c_ulong), ("Name", ctypes.c_wchar_p), ("Comment", ctypes.c_wchar_p)]

            dll = ctypes.WinDLL("secur32.dll")
            p_handle, p_desc = ctypes.POINTER(SecHandle), ctypes.POINTER(SecBufferDesc)
            api = {
                "QuerySecurityPackageInfoW": ([ctypes.c_wchar_p, ctypes.POINTER(ctypes.POINTER(SecPkgInfoW))]),
                "FreeContextBuffer": ([ctypes.c_void_p]),
                "AcquireCredentialsHandleW": ([ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_void_p, ctypes.c_void_p, p_handle, ctypes.POINTER(TimeStamp)]),
                "InitializeSecurityContextW": ([p_handle, p_handle, ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                                p_desc, ctypes.c_ulong, p_handle, p_desc, ctypes.POINTER(ctypes.c_ulong),
                                                ctypes.POINTER(TimeStamp)]),
                "CompleteAuthToken": ([p_handle, p_desc]),
                "DeleteSecurityContext": ([p_handle]),
                "FreeCredentialsHandle": ([p_handle]),
            }
            for name, args in api.items():
                fn = getattr(dll, name)
                fn.argtypes, fn.restype = args, ctypes.c_long
            cls._lib = (ctypes, dll, SecHandle, TimeStamp, SecBuffer, SecBufferDesc, SecPkgInfoW)
            return cls._lib

    def __init__(self, package: str, target: str):
        ctypes, dll, SecHandle, TimeStamp, _b, _d, SecPkgInfoW = self._api()
        self.target = target
        info = ctypes.POINTER(SecPkgInfoW)()
        status = dll.QuerySecurityPackageInfoW(package, ctypes.byref(info))
        if status != self.SEC_E_OK:
            raise SspiError(f"QuerySecurityPackageInfoW({package}) 0x{status & 0xFFFFFFFF:08X}")
        self._max = int(info.contents.cbMaxToken)
        dll.FreeContextBuffer(info)
        self._cred, self._ctx = SecHandle(), SecHandle()
        self._has_ctx = False
        status = dll.AcquireCredentialsHandleW(None, package, self.SECPKG_CRED_OUTBOUND, None, None, None, None,
                                               ctypes.byref(self._cred), ctypes.byref(TimeStamp()))
        if status != self.SEC_E_OK:
            raise SspiError(f"AcquireCredentialsHandleW 0x{status & 0xFFFFFFFF:08X}")

    def step(self, token: Optional[bytes]) -> bytes:
        ctypes, dll, _h, TimeStamp, SecBuffer, SecBufferDesc, _i = self._api()
        out_bytes = ctypes.create_string_buffer(self._max)
        out_buf = SecBuffer(self._max, self.SECBUFFER_TOKEN, ctypes.cast(out_bytes, ctypes.c_void_p))
        out_desc = SecBufferDesc(self.SECBUFFER_VERSION, 1, ctypes.pointer(out_buf))
        in_desc = None
        if token is not None:
            in_bytes = ctypes.create_string_buffer(token, len(token))
            in_buf = SecBuffer(len(token), self.SECBUFFER_TOKEN, ctypes.cast(in_bytes, ctypes.c_void_p))
            in_desc = ctypes.pointer(SecBufferDesc(self.SECBUFFER_VERSION, 1, ctypes.pointer(in_buf)))
        attributes = ctypes.c_ulong()
        status = dll.InitializeSecurityContextW(
            ctypes.byref(self._cred), ctypes.byref(self._ctx) if self._has_ctx else None, self.target, self.FLAGS, 0,
            self.SECURITY_NATIVE_DREP, in_desc, 0, ctypes.byref(self._ctx), ctypes.byref(out_desc),
            ctypes.byref(attributes), ctypes.byref(TimeStamp()))
        if status < 0:
            raise SspiError(f"InitializeSecurityContextW 0x{status & 0xFFFFFFFF:08X}")
        self._has_ctx = True
        if status in (self.SEC_I_COMPLETE_NEEDED, self.SEC_I_COMPLETE_AND_CONTINUE):
            status = dll.CompleteAuthToken(ctypes.byref(self._ctx), ctypes.byref(out_desc))
            if status < 0:
                raise SspiError(f"CompleteAuthToken 0x{status & 0xFFFFFFFF:08X}")
        return out_bytes.raw[:out_buf.cbBuffer]

    def close(self) -> None:
        try:
            _ctypes, dll, *_rest = self._api()
            if self._has_ctx:
                dll.DeleteSecurityContext(_ctypes.byref(self._ctx))
                self._has_ctx = False
            dll.FreeCredentialsHandle(_ctypes.byref(self._cred))
        except Exception:  # noqa: BLE001 (closing only frees what Windows holds)
            pass


def sspi_context(scheme: str, target: str) -> Context:
    """The Windows logon's context for one exchange (Negotiate: Kerberos, else NTLM)."""
    if not available():
        raise SspiError("統合 Windows 認証は Windows だけ")
    return _Sspi(scheme, target)


def _offered(response: http.client.HTTPResponse) -> dict[str, Optional[bytes]]:
    """The schemes a 401 offers, each with the token it carries (None when none)."""
    out: dict[str, Optional[bytes]] = {}
    for header in response.headers.get_all("WWW-Authenticate") or []:
        for part in header.split(","):
            words = part.strip().split(None, 1)
            if not words:
                continue
            scheme = next((s for s in SCHEMES if s.lower() == words[0].lower()), None)
            if scheme is None:
                continue
            token = None
            if len(words) > 1:
                try:
                    token = base64.b64decode(words[1].strip(), validate=True)
                except ValueError:
                    token = None
            if token is not None or scheme not in out:
                out[scheme] = token
    return out


#: Hosts that asked for a scheme before: the next exchange with them starts with it (one round trip less).
_asked: dict[str, str] = {}


def _connection(url: str, timeout: float, ssl_context: Optional[ssl.SSLContext]):
    u = urlsplit(url)
    host = u.hostname or ""
    if u.scheme == "https":
        return http.client.HTTPSConnection(host, u.port, timeout=timeout, context=ssl_context or ssl.create_default_context())
    if u.scheme == "http" and host in _LOCAL_HOSTS:
        return http.client.HTTPConnection(host, u.port, timeout=timeout)
    raise EsriError(code="E104", detail=f"{url.split('?', 1)[0]}：統合 Windows 認証は https だけ")


def ask(url: str, form: Optional[dict] = None, timeout: float = 60.0, context: Optional[ContextFactory] = None,
        ssl_context: Optional[ssl.SSLContext] = None, body: Optional[bytes] = None,
        content_type: str = "application/x-www-form-urlencoded") -> bytes:
    """
    POSTs `form` (x-www-form-urlencoded; or `body` as `content_type`; GET
    when neither) to `url`, signing in with the Windows logon when the server
    asks for it (401 Negotiate or NTLM), and gives the body of the answer.
    Raises EsriError (E203 when the sign-in is refused, UnreachableError when
    the server cannot be reached).
    """
    factory = context or sspi_context
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    address = url.split("?", 1)[0]
    if form is not None:
        body = urlencode(form).encode("utf-8")
    method = "POST" if body is not None else "GET"
    headers = {"Accept": "application/json", "User-Agent": "MIKAGE", "Connection": "keep-alive"}
    if body is not None:
        headers["Content-Type"] = content_type
    conn = _connection(url, timeout, ssl_context)
    sspi: Optional[Context] = None

    def send(authorization: Optional[str]):
        sent = dict(headers)
        if authorization:
            sent["Authorization"] = authorization
        conn.request(method, path, body=body, headers=sent)
        response = conn.getresponse()
        return response, response.read()

    try:
        scheme = _asked.get(host)
        try:
            if scheme:
                sspi = factory(scheme, f"HTTP/{host}")
                response, answer = send(f"{scheme} {base64.b64encode(sspi.step(None)).decode('ascii')}")
            else:
                response, answer = send(None)
            legs = 0
            while response.status == 401:
                offered = _offered(response)
                if sspi is None:
                    scheme = next((s for s in SCHEMES if s in offered), None)
                    if scheme is None:
                        break
                    _asked[host] = scheme
                    sspi = factory(scheme, f"HTTP/{host}")
                    token = sspi.step(None)
                else:
                    challenge = offered.get(scheme) if scheme else None
                    if not challenge or legs >= MAX_LEGS:
                        break
                    token = sspi.step(challenge)
                legs += 1
                response, answer = send(f"{scheme} {base64.b64encode(token).decode('ascii')}")
        except SspiError as error:
            raise EsriError(code="E203", detail=f"{address}：Windows のサインインを作れない（{error}）") from None
        except (OSError, http.client.HTTPException) as error:
            raise UnreachableError(detail=f"{address}：{type(error).__name__} {error}") from None
    finally:
        if sspi is not None:
            sspi.close()
        conn.close()
    status = response.status
    if 200 <= status < 300:
        return answer
    if status == 401:
        _asked.pop(host, None)
        raise EsriError(code="E203", detail=f"{address}：HTTP 401（Windows のサインインが通らない）")
    if status >= 500:
        raise BusyError(code="E103", detail=f"{address}：HTTP {status}")
    raise EsriError(code={403: "E202"}.get(status, "E104"), detail=f"{address}：HTTP {status}")
