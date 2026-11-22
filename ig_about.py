# -*- coding: utf-8 -*-
"""Instagram「关于此账户」查询。

移动端 App 里点开主页右上角「关于此账户」, 实际走的是一个 bloks 屏:

  POST https://i.instagram.com/api/v1/bloks/apps/com.instagram.interactions.about_this_account/
       params = {"server_params": {"target_user_id": <pk>, "user_id": <pk>}, ...}

返回是一棵 bloks UI 树, 账户所在地 / 加入日期 / 是否认证都嵌在文本里:

  - 账户所在地: 数据项 IG_ABOUT_THIS_ACCOUNT:about_this_account_country 的 initial
  - 加入日期  : 无障碍文本 "加入日期 2012年2月"
  - 是否认证  : 树里出现 "已认证"

用户名 -> pk 走:

  GET https://i.instagram.com/api/v1/users/<username>/usernameinfo/

注意: 老的 /api/v1/users/<pk>/account_details/ 已经下线, 别再用了。
"""
import json
import re
import time
from urllib.parse import quote, urlencode

from ig_session import (APP_ID, BLOKS_VERSION, HOST,
                        make_bearer)  # noqa: F401  (make_bearer 供外部复用)

ABOUT_APP = "com.instagram.interactions.about_this_account"


def base_headers(sess, friendly, endpoint, now=None):
    """移动端鉴权头 —— 只保留查询需要的那些。"""
    now = now or time.time()
    hd = [
        ("accept-language", sess.locale.replace("_", "-")),
        ("authorization", sess.bearer),
        ("ig-intended-user-id", sess.ds_user_id),
        ("ig-u-ds-user-id", sess.ds_user_id),
    ]
    if sess.rur:
        hd.append(("ig-u-rur", sess.rur))
    hd += [
        ("priority", "u=3"),
        ("x-bloks-version-id", BLOKS_VERSION),
        ("x-fb-client-ip", "True"),
        ("x-fb-connection-type", "WIFI"),
        ("x-fb-friendly-name", friendly),
        ("x-fb-server-cluster", "True"),
        ("x-ig-android-id", sess.android_id),
        ("x-ig-app-id", APP_ID),
        ("x-ig-app-locale", sess.locale),
        ("x-ig-client-endpoint", endpoint),
        ("x-ig-capabilities", "3brTv10="),
        ("x-ig-connection-type", "WIFI"),
        ("x-ig-device-id", sess.device_id),
        ("x-ig-device-locale", sess.locale),
        ("x-ig-family-device-id", sess.family_device_id),
        ("x-ig-mapped-locale", sess.locale),
        ("x-ig-timezone-offset", sess.tz),
        ("x-ig-www-claim", sess.www_claim or "0"),
        ("x-mid", sess.mid),
        ("x-pigeon-rawclienttime", "%.3f" % now),
        ("x-pigeon-session-id", sess.pigeon_session),
        ("x-tigon-is-retry", "False"),
        ("accept-encoding", "zstd, gzip, deflate"),
        ("user-agent", sess.ua),
        ("x-fb-http-engine", "Tigon/MNS/TCP"),
    ]
    return hd


def username_info(wire, sess, username):
    """用户名 -> (status, user dict 或 None, Resp)。"""
    url = "https://%s/api/v1/users/%s/usernameinfo/" % (HOST, quote(username))
    hd = base_headers(sess, "IgApi: users/{username}/usernameinfo/",
                      "ProfileMediaTabFragment:profile")
    r = wire.send("GET", url, hd, b"", idempotent=True)
    wire.harvest(r, sess)
    j = r.json()
    if isinstance(j, dict) and j.get("user"):
        return r.status, j["user"], r
    return r.status, None, r


def about_this_account(wire, sess, pk):
    """pk -> (status, bloks 屏原始文本)。"""
    url = "https://%s/api/v1/bloks/apps/%s/" % (HOST, ABOUT_APP)
    hd = base_headers(sess, "IgApi: bloks/apps/%s/" % ABOUT_APP,
                      "AboutThisAccountFragment:about_this_account")
    hd.append(("content-type",
               "application/x-www-form-urlencoded; charset=UTF-8"))
    # 这个屏也认 web 会话 cookie, 带上更稳
    if sess.web_cookie:
        hd.append(("cookie", sess.web_cookie))
        if sess.csrftoken:
            hd.append(("x-csrftoken", sess.csrftoken))
    params = {
        "params": json.dumps({"server_params": {
            "target_user_id": str(pk), "user_id": str(pk)}}),
        "bk_client_context": json.dumps(
            {"bloks_version": BLOKS_VERSION, "styles_id": "instagram"}),
        "bloks_versioning_id": BLOKS_VERSION,
    }
    r = wire.send("POST", url, hd, urlencode(params).encode(), idempotent=True)
    wire.harvest(r, sess)
    return r.status, r.text()


# 多语言「加入日期」标签, 用来从无障碍文本里剥掉标签只留日期
_JOINED_LABELS = ("加入日期", "加入時間", "Date joined", "Joined",
                  "Fecha de incorporación", "Data de entrada")
_DATE_RX = re.compile(
    r"(\d{4}\s*年\s*\d{1,2}\s*月"
    r"|[A-Za-zÀ-ÿ]+\.?\s+(?:de\s+)?\d{4}"
    r"|\d{1,2}\s+[A-Za-zÀ-ÿ]+\.?\s+\d{4})")


def parse_about(text):
    """从 bloks 文本里抠出 所在地 / 加入日期 / 认证 / 所在地是否公开。"""
    out = {"country": None, "country_shared": None,
           "date_joined": None, "is_verified": None, "title": None}

    m = re.search(r'about_this_account_country"[^}]*?"initial":"([^"]*)"', text)
    if m:
        out["country"] = _unescape(m.group(1))

    m = re.search(r'about_this_account_country_visibility"[^}]*?"initial":\s*'
                  r'(true|false)', text)
    if m:
        out["country_shared"] = (m.group(1) == "true")

    # 兜底: 树里 (e2a (dnt "..") "美国" "未分享") 的第一个字面量
    if not out["country"]:
        m = re.search(r'\(e2a \(dnt \\?"[^"]+\\?"\) \\?"((?:\\u[0-9a-fA-F]{4})+'
                      r'|[^"\\]+)\\?"', text)
        if m:
            out["country"] = _unescape(m.group(1))

    for s in re.findall(r'"\$":"([^"]*)"', text):
        s = _unescape(s)
        if any(lb in s for lb in _JOINED_LABELS):
            d = _DATE_RX.search(s)
            out["date_joined"] = d.group(1).strip() if d else s
            break
    if not out["date_joined"]:
        d = _DATE_RX.search(_unescape(text))
        if d:
            out["date_joined"] = d.group(1).strip()

    out["is_verified"] = ("已认证" in text or "已認證" in text
                          or '"Verified"' in text or "认证徽章" in text)

    m = re.search(r'"㗙":\{"\$":"([^"]*)"', text)
    if m:
        out["title"] = _unescape(m.group(1))
    return out


def _unescape(s):
    try:
        return json.loads('"%s"' % s.replace('"', '\\"'))
    except Exception:
        return s
