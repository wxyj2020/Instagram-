# -*- coding: utf-8 -*-
"""Instagram mobile session + transport layer.

This module does two things:

1) Turn one line of a ck file into a Session object (account, cookies, device
   fingerprint, proxy).
2) Send requests with the mobile app's header order and harvest the session
   headers the server sends back.

Each ck line can use either format, mixing is fine:

  A) JSON dict (recommended, device fields optional):
     {"ds_user_id": "123456", "sessionid": "123456%3A...",
      "csrftoken": "...", "mid": "...", "ig_did": "...", "datr": "...",
      "android_id": "android-xxxxxxxxxxxxxxxx", "device_id": "<uuid>",
      "family_device_id": "<uuid>", "ua": "Instagram ... Android ...",
      "proxy": "socks5h://user:pass@host:port"}

  B) Cookie string (what you copy from a browser or a capture):
     datr=..; ig_did=..; mid=..; csrftoken=..; ds_user_id=123; sessionid=123%3A..

  Cookie strings carry no device info, so a fixed set is derived from ds_user_id:
  the same account always maps to the same device, across runs and machines.
"""
import base64
import hashlib
import json
import re
import uuid

try:                                    # prefer rnet for its TLS/HTTP2 fingerprint
    import rnet
except ImportError:                     # otherwise fall back to requests
    rnet = None
    import requests

try:
    import zstandard
except ImportError:
    zstandard = None

# ── Client identity: no version numbers live in the source ──
# Requests need x-ig-app-id / x-bloks-version-id / a User-Agent with the app
# version. Those change with every app release, so the user supplies them
# through config.ini (see config.example.ini).
APP_ID = "567067343352427"
HOST = "i.instagram.com"

CLIENT = {
    "app_version": "",
    "version_code": "",
    "bloks_version": "",
    "ua": "",
}


def configure(app_version="", version_code="", bloks_version="", ua=""):
    """Inject client identifiers from config.ini (called by the entry point)."""
    CLIENT["app_version"] = (app_version or "").strip()
    CLIENT["version_code"] = (version_code or "").strip()
    CLIENT["bloks_version"] = (bloks_version or "").strip()
    CLIENT["ua"] = (ua or "").strip()
    if not CLIENT["ua"] and CLIENT["app_version"]:
        CLIENT["ua"] = ("Instagram %s Android (28/9; 480dpi; 1080x1920; "
                        "Xiaomi; MI 6; sagit; qcom; zh_CN; %s)"
                        % (CLIENT["app_version"], CLIENT["version_code"]))


def require_client():
    """Fail fast when the client identity is incomplete."""
    need = [k for k in ("app_version", "version_code", "bloks_version", "ua")
            if not CLIENT[k]]
    if need:
        raise RuntimeError(
            "config.ini is missing [client] values: %s\n"
            "  See config.example.ini and capture them from a logged-in app:\n"
            "    app_version   -> version number in the User-Agent\n"
            "    version_code  -> trailing number in the User-Agent\n"
            "    bloks_version -> x-bloks-version-id request header" % ", ".join(need))

_COOKIE_KEYS = ("sessionid", "ds_user_id", "csrftoken", "mid", "ig_did", "datr")
_SESSID_RX = re.compile(r"(sessid-)[^-@:/]+")   # session slot in a proxy template


# ────────────────────────────── account parsing ──────────────────────────────

def make_bearer(ds_user_id, sessionid):
    """Build the app's ``Authorization: Bearer IGT:2:<base64>`` header.

    The decoded payload is just {"ds_user_id": "...", "sessionid": "..."}
    (sessionid keeps its URL encoding), so a cookie string is all you need.
    """
    payload = json.dumps(
        {"ds_user_id": str(ds_user_id), "sessionid": sessionid},
        separators=(",", ":")).encode("utf-8")
    return "Bearer IGT:2:" + base64.b64encode(payload).decode("ascii")


def parse_cookie_line(line):
    """Parse one line into a dict of raw cookie fields. Returns None on failure."""
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None

    # A) JSON dict: use whatever the line provides
    if line.startswith("{"):
        try:
            c = json.loads(line)
        except Exception:
            c = None
        if isinstance(c, dict):
            sid = c.get("sessionid") or ""
            uid = str(c.get("ds_user_id") or "")
            if not uid and sid:
                m = re.match(r"(\d+)(?:%3A|:)", sid)
                if m:
                    uid = m.group(1)
            if not uid:
                return None
            c["ds_user_id"] = uid
            return c

    # B) cookie string: may be the whole line or one ---- separated segment
    seg = line
    for part in line.split("----"):
        if "sessionid=" in part:
            seg = part
            break
    c = {}
    for kv in seg.split(";"):
        k, _, v = kv.strip().partition("=")
        k = k.strip().lower()
        if k in _COOKIE_KEYS and v:
            c[k] = v.strip()
    if not c.get("sessionid"):
        return None
    if not c.get("ds_user_id"):
        m = re.match(r"(\d+)(?:%3A|:)", c["sessionid"])
        if m:
            c["ds_user_id"] = m.group(1)
    return c if c.get("ds_user_id") else None


def derive_device(ds_user_id):
    """Derive a stable device fingerprint from the uid (nothing is stored)."""
    key = ("ig-region-lookup:" + str(ds_user_id)).encode()
    h = (hashlib.sha256(key).hexdigest()
         + hashlib.sha256(key + b"|device").hexdigest())
    return {
        "android_id": "android-" + h[:16],
        "device_id": str(uuid.UUID(h[16:48])),
        "family_device_id": str(uuid.UUID(h[48:80])),
        "mid": h[80:112][:30],
        "ua": CLIENT["ua"],
    }


class Session(object):
    """Full session state for one worker account."""

    def __init__(self, d):
        self.ds_user_id = str(d.get("ds_user_id") or "")
        self.sessionid = d.get("sessionid") or ""
        self.csrftoken = d.get("csrftoken") or ""
        self.ig_did = d.get("ig_did") or ""
        self.datr = d.get("datr") or ""
        self.proxy = d.get("proxy") or ""

        dev = derive_device(self.ds_user_id)
        self.android_id = d.get("android_id") or dev["android_id"]
        self.device_id = d.get("device_id") or dev["device_id"]
        self.family_device_id = d.get("family_device_id") or dev["family_device_id"]
        self.mid = d.get("mid") or dev["mid"]
        self.ua = d.get("ua") or dev["ua"]

        self.locale = d.get("locale") or "en_US"
        self.tz = d.get("tz") or "0"
        self.bearer = d.get("bearer") or make_bearer(self.ds_user_id, self.sessionid)

        # Filled in from server response headers.
        self.www_claim = d.get("www_claim") or "0"
        self.rur = d.get("rur") or ""
        # One pigeon session per account, stable for its lifetime.
        self.pigeon_session = "UFS-%s-1" % uuid.uuid4()

        self.web_cookie = self._build_web_cookie(d)
        self.raw_line = ""
        self.dead = False

    def _build_web_cookie(self, d):
        bits = []
        for k in ("datr", "ig_did", "mid", "csrftoken", "ds_user_id", "sessionid"):
            v = d.get(k) or ""
            if k == "mid":
                v = v or self.mid
            if k == "ds_user_id":
                v = v or self.ds_user_id
            if v:
                bits.append("%s=%s" % (k, v))
        return "; ".join(bits)

    def missing(self):
        return [k for k in ("ds_user_id", "sessionid") if not getattr(self, k)]


def load_sessions(path, region="mix"):
    """Read a ck file into a list of Sessions, keeping only the first line per uid."""
    out, seen = [], set()
    try:
        import io
        lines = io.open(path, encoding="utf-8-sig", errors="replace")
    except OSError:
        return out
    for line in lines:
        c = parse_cookie_line(line)
        if not c:
            continue
        uid = str(c.get("ds_user_id") or "")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        sess = Session(c)
        if sess.missing():
            continue
        sess.raw_line = line.strip()
        out.append(sess)
    return out


# ────────────────────────────── proxy ──────────────────────────────

def proxy_for(uid, tpl):
    """Build a sticky proxy URL for one account.

    ``{sid}`` is replaced per uid; a literal ``sessid-xxxx`` is rewritten the same
    way; with neither, the template is returned unchanged (all accounts share one IP).
    """
    tpl = (tpl or "").strip()
    if not tpl:
        return ""
    sid = "ig%s" % (str(uid)[-10:] if uid else "x")
    if "{sid}" in tpl:
        return tpl.replace("{sid}", sid)
    if _SESSID_RX.search(tpl):
        return _SESSID_RX.sub(r"\g<1>" + sid, tpl)
    return tpl


def mask_proxy(url):
    """Hide the password before printing."""
    try:
        head, tail = url.split("://", 1)
        if "@" in tail:
            cred, host = tail.split("@", 1)
            user = cred.split(":", 1)[0]
            return "%s://%s:***@%s" % (head, user, host)
    except Exception:
        pass
    return url


# ────────────────────────────── transport ──────────────────────────────

class Resp(object):
    def __init__(self, status, headers, body):
        self.status = int(status)
        self.headers = headers          # all keys lowercased
        self.body = body

    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        try:
            return json.loads(self.body)
        except Exception:
            return None

    @property
    def ok(self):
        return 200 <= self.status < 300


def _decompress(body, enc):
    if not body:
        return b""
    try:
        if enc == "zstd" and zstandard:
            import io as _io
            # IG sends zstd as streaming frames without content-size in the
            # header, so it must be read across frames.
            with zstandard.ZstdDecompressor().stream_reader(
                    _io.BytesIO(body), read_across_frames=True) as r:
                return r.read()
        if enc == "gzip":
            import gzip
            return gzip.decompress(body)
    except Exception:
        pass
    return body


class Wire(object):
    """HTTP client: OkHttp5/Android fingerprint when rnet is available."""

    def __init__(self, proxy=None, timeout=40):
        self.proxy = proxy or None
        self.timeout = timeout
        self._new_client()

    @property
    def accept_encoding(self):
        return "zstd, gzip, deflate" if rnet else "gzip, deflate"

    def _new_client(self):
        if rnet:
            kw = {
                "impersonate": rnet.ImpersonateOption(
                    rnet.Impersonate.OkHttp5, rnet.ImpersonateOS.Android),
                "timeout": int(self.timeout or 40),
            }
            if self.proxy:
                kw["proxies"] = [rnet.Proxy.all(self.proxy)]
            self.client = rnet.BlockingClient(**kw)
        else:
            self.client = requests.Session()
            if self.proxy:
                self.client.proxies = {"http": self.proxy, "https": self.proxy}
            self.client.headers.clear()

    def send(self, method, url, headers, body=b"", retry=2, idempotent=True):
        """``headers`` is a list of (key, value) pairs; order is preserved.

        For ``idempotent=False`` requests (e.g. sending a message) only connection
        errors -- requests that never left the machine -- are retried.
        """
        hd = dict(headers)
        last = None
        tries = retry + 1
        for i in range(tries):
            if i:
                hd["x-tigon-is-retry"] = "True"
            try:
                if rnet:
                    if method.upper() == "POST":
                        r = self.client.post(url, headers=hd, body=body or b"")
                    else:
                        r = self.client.get(url, headers=hd)
                    rh = {}
                    for k, v in r.headers.items():
                        k = k.decode() if isinstance(k, bytes) else k
                        v = v.decode() if isinstance(v, bytes) else v
                        rh[k.lower()] = v
                    raw = r.bytes() if hasattr(r, "bytes") else bytes(r.content)
                    return Resp(r.status, rh,
                                _decompress(raw, rh.get("content-encoding", "")))
                r = self.client.request(method.upper(), url, headers=hd,
                                        data=body or None, timeout=self.timeout)
                rh = {k.lower(): v for k, v in r.headers.items()}
                return Resp(r.status_code, rh,
                            _decompress(r.content, rh.get("content-encoding", "")))
            except Exception as e:
                last = e
                if i >= tries - 1:
                    raise
                if not idempotent and not _is_connect_error(e):
                    raise
                import time
                time.sleep(min(1.2 * (i + 1), 5))
                self._new_client()
        raise last

    def harvest(self, resp, sess):
        """Store session values the server returned in headers."""
        for h, attr in (("ig-set-x-mid", "mid"), ("ig-set-ig-u-rur", "rur"),
                        ("x-ig-set-www-claim", "www_claim")):
            v = resp.headers.get(h)
            if v:
                setattr(sess, attr, v)


def _is_connect_error(e):
    s = str(e)
    return any(x in s for x in ("Connect", "ConnectionRefused", "dns error",
                                "TimedOut", "connect timed out",
                                "X509VerifyError", "MidHandshakeSslStream"))
