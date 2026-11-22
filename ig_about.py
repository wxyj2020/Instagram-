# -*- coding: utf-8 -*-
"""Instagram "About this account" lookup.

In the mobile app, tapping "About this account" opens a bloks screen, which is
backed by:

  POST https://i.instagram.com/api/v1/bloks/apps/com.instagram.interactions.about_this_account/
       params = {"server_params": {"target_user_id": <pk>, "user_id": <pk>}, ...}

The response is a bloks UI tree; the region, join date and verified badge are all
embedded as text inside it:

  - region      : the ``initial`` value of about_this_account_country
  - join date   : accessibility text like "Date joined February 2012"
  - verified    : the tree contains "Verified"

Username -> pk goes through:

  GET https://i.instagram.com/api/v1/users/<username>/usernameinfo/

Note: the old /api/v1/users/<pk>/account_details/ endpoint is retired.
"""
import json
import re
import time
from urllib.parse import quote, urlencode

import ig_session as S

ABOUT_APP = "com.instagram.interactions.about_this_account"


def base_headers(sess, friendly, endpoint, now=None):
    """Mobile auth headers -- only the ones a lookup needs."""
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
        ("x-bloks-version-id", S.CLIENT["bloks_version"]),
        ("x-fb-client-ip", "True"),
        ("x-fb-connection-type", "WIFI"),
        ("x-fb-friendly-name", friendly),
        ("x-fb-server-cluster", "True"),
        ("x-ig-android-id", sess.android_id),
        ("x-ig-app-id", S.APP_ID),
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
    """Username -> (status, user dict or None, Resp)."""
    url = "https://%s/api/v1/users/%s/usernameinfo/" % (S.HOST, quote(username))
    hd = base_headers(sess, "IgApi: users/{username}/usernameinfo/",
                      "ProfileMediaTabFragment:profile")
    r = wire.send("GET", url, hd, b"", idempotent=True)
    wire.harvest(r, sess)
    j = r.json()
    if isinstance(j, dict) and j.get("user"):
        return r.status, j["user"], r
    return r.status, None, r


def about_this_account(wire, sess, pk):
    """pk -> (status, raw bloks screen text)."""
    url = "https://%s/api/v1/bloks/apps/%s/" % (S.HOST, ABOUT_APP)
    hd = base_headers(sess, "IgApi: bloks/apps/%s/" % ABOUT_APP,
                      "AboutThisAccountFragment:about_this_account")
    hd.append(("content-type",
               "application/x-www-form-urlencoded; charset=UTF-8"))
    # This screen also accepts the web session cookie, which makes it more reliable.
    if sess.web_cookie:
        hd.append(("cookie", sess.web_cookie))
        if sess.csrftoken:
            hd.append(("x-csrftoken", sess.csrftoken))
    params = {
        "params": json.dumps({"server_params": {
            "target_user_id": str(pk), "user_id": str(pk)}}),
        "bk_client_context": json.dumps(
            {"bloks_version": S.CLIENT["bloks_version"], "styles_id": "instagram"}),
        "bloks_versioning_id": S.CLIENT["bloks_version"],
    }
    r = wire.send("POST", url, hd, urlencode(params).encode(), idempotent=True)
    wire.harvest(r, sess)
    return r.status, r.text()


# Localized "date joined" labels, used to strip the label and keep the date.
_JOINED_LABELS = ("Date joined", "Joined", "加入日期", "加入時間",
                  "Fecha de incorporación", "Data de entrada")
_DATE_RX = re.compile(
    r"(\d{4}\s*年\s*\d{1,2}\s*月"
    r"|[A-Za-zÀ-ÿ]+\.?\s+(?:de\s+)?\d{4}"
    r"|\d{1,2}\s+[A-Za-zÀ-ÿ]+\.?\s+\d{4})")


def parse_about(text):
    """Extract region / join date / verified / region visibility from bloks text."""
    out = {"country": None, "country_shared": None,
           "date_joined": None, "is_verified": None, "title": None}

    m = re.search(r'about_this_account_country"[^}]*?"initial":"([^"]*)"', text)
    if m:
        out["country"] = _unescape(m.group(1))

    m = re.search(r'about_this_account_country_visibility"[^}]*?"initial":\s*'
                  r'(true|false)', text)
    if m:
        out["country_shared"] = (m.group(1) == "true")

    # Fallback: first literal in (e2a (dnt "..") "United States" "Not shared")
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
