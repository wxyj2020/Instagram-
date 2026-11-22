# -*- coding: utf-8 -*-
"""Instagram 移动端会话 + 传输层。

这个模块只做两件事:

1) 把 ck 文件里的一行解析成 Session 对象(账号、cookie、设备指纹、代理)
2) 把请求按移动端 App 的头序发出去, 并把服务端下发的会话头收回来

ck 文件每行支持两种写法, 混排也行:

  A) JSON dict(推荐, 设备信息可以自己写死):
     {"ds_user_id": "123456", "sessionid": "123456%3A...",
      "csrftoken": "...", "mid": "...", "ig_did": "...", "datr": "...",
      "android_id": "android-xxxxxxxxxxxxxxxx", "device_id": "<uuid>",
      "family_device_id": "<uuid>", "ua": "Instagram ... Android ...",
      "proxy": "socks5h://user:pass@host:port"}

  B) cookie 串(从浏览器/抓包里复制的那种):
     datr=..; ig_did=..; mid=..; csrftoken=..; ds_user_id=123; sessionid=123%3A..

  cookie 串里没有设备信息, 会按 ds_user_id 派生一套**固定**的值 ——
  同一个号每次算出来都一样, 不会因为换进程就变成"新设备"。
"""
import base64
import hashlib
import json
import re
import uuid

try:                                    # 有 rnet 就用它的 TLS/HTTP2 指纹
    import rnet
except ImportError:                     # 没有就退回 requests
    rnet = None
    import requests

try:
    import zstandard
except ImportError:
    zstandard = None

# ── 客户端标识: 不含任何写死的版本号, 全部由 config.ini [client] 提供 ──
# 请求头里需要 x-ig-app-id / x-bloks-version-id / User-Agent 里的版本号,
# 这些随 App 更新会变, 所以留给使用者自己填(见 config.example.ini)。
APP_ID = "567067343352427"
HOST = "i.instagram.com"

CLIENT = {
    "app_version": "",
    "version_code": "",
    "bloks_version": "",
    "ua": "",
}


def configure(app_version="", version_code="", bloks_version="", ua=""):
    """由主程序从 config.ini 注入客户端标识。"""
    CLIENT["app_version"] = (app_version or "").strip()
    CLIENT["version_code"] = (version_code or "").strip()
    CLIENT["bloks_version"] = (bloks_version or "").strip()
    CLIENT["ua"] = (ua or "").strip()
    if not CLIENT["ua"] and CLIENT["app_version"]:
        CLIENT["ua"] = ("Instagram %s Android (28/9; 480dpi; 1080x1920; "
                        "Xiaomi; MI 6; sagit; qcom; zh_CN; %s)"
                        % (CLIENT["app_version"], CLIENT["version_code"]))


def require_client():
    """缺必需项就抛错, 别让请求发出去才发现头是空的。"""
    need = [k for k in ("app_version", "version_code", "bloks_version", "ua")
            if not CLIENT[k]]
    if need:
        raise RuntimeError(
            "config.ini 缺少 [client] 配置: %s\n"
            "  请参考 config.example.ini, 用抓包工具从已登录的 App 里取:\n"
            "    app_version   -> User-Agent 里的版本号\n"
            "    version_code  -> User-Agent 末尾的数字\n"
            "    bloks_version -> 请求头 x-bloks-version-id" % ", ".join(need))

_COOKIE_KEYS = ("sessionid", "ds_user_id", "csrftoken", "mid", "ig_did", "datr")
_SESSID_RX = re.compile(r"(sessid-)[^-@:/]+")   # 代理模板里可替换的会话位


# ────────────────────────────── 账号解析 ──────────────────────────────

def make_bearer(ds_user_id, sessionid):
    """App 的 Authorization: Bearer IGT:2:<base64>。

    解出来就是 {"ds_user_id": "...", "sessionid": "..."}(sessionid 保持 URL 编码),
    所以只凭 cookie 串就能拼出来, 不需要额外登录。
    """
    payload = json.dumps(
        {"ds_user_id": str(ds_user_id), "sessionid": sessionid},
        separators=(",", ":")).encode("utf-8")
    return "Bearer IGT:2:" + base64.b64encode(payload).decode("ascii")


def parse_cookie_line(line):
    """一行 -> dict(裸 cookie 字段)。解析不出返回 None。"""
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None

    # A) JSON dict: 行里有什么用什么
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

    # B) cookie 串: 可能整行, 也可能藏在 ---- 分隔的某一段
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
    """按 uid 派生一套固定的设备字段(不落盘, 同一个号永远同一套)。"""
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
    """一个工作号的完整会话状态。"""

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

        # 服务端下发后会回填
        self.www_claim = d.get("www_claim") or "0"
        self.rur = d.get("rur") or ""
        # 每号固定一个 pigeon session
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
    """读 ck 文件 -> [Session], 同一个 uid 只保留第一条。"""
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


# ────────────────────────────── 代理 ──────────────────────────────

def proxy_for(uid, tpl):
    """按 uid 生成独立会话代理: 每个号固定一个出口 IP。

    模板里 {sid} 会被替换; 写成 sessid-xxxx 的也会自动替换;
    都没有就原样使用(所有号共用一个出口)。
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
    """打印时遮掉代理密码。"""
    try:
        head, tail = url.split("://", 1)
        if "@" in tail:
            cred, host = tail.split("@", 1)
            user = cred.split(":", 1)[0]
            return "%s://%s:***@%s" % (head, user, host)
    except Exception:
        pass
    return url


# ────────────────────────────── 传输 ──────────────────────────────

class Resp(object):
    def __init__(self, status, headers, body):
        self.status = int(status)
        self.headers = headers          # 键全小写
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
            # IG 的 zstd 是流式帧且帧头不带 content-size, 必须跨帧读
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
    """发请求。rnet 可用时用 OkHttp5/Android 指纹, 否则用 requests。"""

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
        """headers 是 [(k, v)] 列表, 保持顺序。

        idempotent=False 的请求(比如发消息)只对"根本没发出去"的连接错误重试。
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
                return Resp(r.status_code, rh, _decompress(r.content,
                                                            rh.get("content-encoding", "")))
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
        """把响应头里服务端下发的会话值收进 Session。"""
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
