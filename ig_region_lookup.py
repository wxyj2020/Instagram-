# -*- coding: utf-8 -*-
"""Batch-lookup Instagram account regions and export them grouped by country.

Inputs
------
  ck.txt         Worker account pool, one per line (format documented in
                 ig_session.py). Lookups require a logged-in session; workers are
                 rotated and replaced automatically on rate limits or expiry.
  targets.txt    Usernames to look up, one per line. Three formats are accepted:
                   - username
                   - username----password----...        (only the first field is used)
                   - {"username": "xxx", ...}           (JSON line)

Outputs
-------
  regions/<Region>.txt   One file per account region, holding the original target lines
  result.csv             username,country,region,status
  _done.txt              Usernames already processed (used to resume)

Usage
-----
  python ig_region_lookup.py                  # asks for concurrency
  python ig_region_lookup.py --threads 8      # 8 threads
  python ig_region_lookup.py --delay 2.5      # seconds between lookups on one worker
  python ig_region_lookup.py --names cn       # Chinese region filenames
  python ig_region_lookup.py --reset          # wipe progress and output
  python ig_region_lookup.py --limit 20       # only 20 targets (debugging)

Concurrency model
-----------------
  Worker pool + target queue + N threads. A thread borrows a worker, looks up one
  target, and returns the worker. A worker is used by at most one thread at a time,
  which rate-limits naturally. Dead workers leave the pool and their target goes
  back to the queue; concurrency is independent of the pool size.
"""
import argparse
import configparser
import glob
import io
import json
import os
import queue
import re
import sys
import threading
import time

from ig_session import (Wire, load_sessions, mask_proxy, proxy_for)
import ig_session as S
import ig_about as A

# ── ANSI colors on Windows ──
try:
    import colorama
    colorama.just_fix_windows_console()
except Exception:
    try:
        os.system("")
    except Exception:
        pass
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"

HERE = os.path.dirname(os.path.abspath(__file__))
CK_FILE = os.path.join(HERE, "ck.txt")
TARGET_FILE = os.path.join(HERE, "targets.txt")
CONFIG_FILE = os.path.join(HERE, "config.ini")
PROGRESS_FILE = os.path.join(HERE, "_done.txt")

OUT_DIR = os.path.join(HERE, "regions")
OUT_CSV = os.path.join(HERE, "result.csv")

# English region name (returned for en_US) -> Chinese filename, used with --names cn.
CN = {
    "United States": "美国", "China": "中国", "Hong Kong": "香港",
    "Taiwan": "台湾", "Singapore": "新加坡", "South Korea": "韩国",
    "North Korea": "朝鲜", "Japan": "日本", "India": "印度", "Myanmar": "缅甸",
    "Iran": "伊朗", "Vietnam": "越南", "Canada": "加拿大", "Indonesia": "印度尼西亚",
    "Australia": "澳大利亚", "Cambodia": "柬埔寨", "Mexico": "墨西哥",
    "Dominican Republic": "多米尼加", "Peru": "秘鲁", "Colombia": "哥伦比亚",
    "Syria": "叙利亚", "Macau": "澳门", "Pakistan": "巴基斯坦", "Malaysia": "马来西亚",
    "Guyana": "圭亚那", "Ecuador": "厄瓜多尔", "Yemen": "也门", "Brazil": "巴西",
    "Côte d'Ivoire": "科特迪瓦", "Philippines": "菲律宾", "Maldives": "马尔代夫",
    "Turkey": "土耳其", "Jamaica": "牙买加", "Bangladesh": "孟加拉国",
    "Costa Rica": "哥斯达黎加", "Nigeria": "尼日利亚", "Liberia": "利比里亚",
    "Russia": "俄罗斯", "Thailand": "泰国", "United Kingdom": "英国",
    "France": "法国", "Germany": "德国", "Spain": "西班牙", "Italy": "意大利",
    "Netherlands": "荷兰", "Belgium": "比利时", "Switzerland": "瑞士",
    "Sweden": "瑞典", "Norway": "挪威", "Denmark": "丹麦", "Finland": "芬兰",
    "Poland": "波兰", "Austria": "奥地利", "Ireland": "爱尔兰", "Portugal": "葡萄牙",
    "Greece": "希腊", "Ukraine": "乌克兰", "Nepal": "尼泊尔", "Sri Lanka": "斯里兰卡",
    "Laos": "老挝", "United Arab Emirates": "阿联酋", "Saudi Arabia": "沙特阿拉伯",
    "Egypt": "埃及", "Iraq": "伊拉克", "Afghanistan": "阿富汗", "Argentina": "阿根廷",
    "Chile": "智利", "Venezuela": "委内瑞拉",
}


# ────────────────────────────── config ──────────────────────────────

def load_proxy_tpl():
    """Read [proxy] url from config.ini; empty means no proxy."""
    if not os.path.isfile(CONFIG_FILE):
        return ""
    cp = configparser.ConfigParser(interpolation=None)   # no % interpolation
    try:
        cp.read(CONFIG_FILE, encoding="utf-8-sig")
        if cp.has_option("proxy", "url"):
            return (cp.get("proxy", "url") or "").strip()
    except Exception as e:
        print(YELLOW + "[!] failed to read proxy from config.ini: %s" % e + RESET)
    return ""


def load_client_cfg():
    """Read client identifiers (versions, bloks version, UA) from [client]."""
    out = {"app_version": "", "version_code": "", "bloks_version": "", "ua": ""}
    if not os.path.isfile(CONFIG_FILE):
        return out
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(CONFIG_FILE, encoding="utf-8-sig")
        for k in out:
            if cp.has_option("client", k):
                out[k] = (cp.get("client", k) or "").strip()
    except Exception as e:
        print(YELLOW + "[!] failed to read [client] from config.ini: %s" % e + RESET)
    return out


# ────────────────────────────── file helpers ──────────────────────────────

def read_lines(path):
    if not os.path.isfile(path):
        return []
    return [ln.rstrip("\n").rstrip("\r")
            for ln in io.open(path, encoding="utf-8-sig", errors="replace")
            if ln.strip() and not ln.lstrip().startswith("#")]


def target_username(line):
    """Extract the IG username, accepting plain / ---- legacy / JSON lines."""
    s = (line or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:                              # JSON line: pull out {...}
        try:
            c = json.loads(s[i:j + 1])
            if isinstance(c, dict):
                u = (c.get("username") or c.get("user") or "").strip()
                if u:
                    return u.lstrip("@")
        except Exception:
            pass
    return s.split("----", 1)[0].strip().lstrip("@").split()[0] if s else ""


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]', "_", (s or "").strip()) or "_empty"


def safe_append(path, text):
    """Append to a file; retry when locked (e.g. open in Excel), warn once on failure."""
    for _ in range(6):
        try:
            with io.open(path, "a", encoding="utf-8") as f:
                f.write(text)
            return True
        except (PermissionError, OSError):
            time.sleep(0.3)
    if path not in _warned_locked:
        _warned_locked.add(path)
        print(YELLOW + "[!] cannot write %s (opened by another program?), skipped"
              % os.path.basename(path) + RESET)
    return False


_warned_locked = set()


def bucket_for(country, status, use_cn=False):
    """-> bucket name (region / Invalid / Not shared)."""
    if status.startswith("not found") or status.startswith("skipped"):
        return "失效" if use_cn else "Invalid"
    c = (country or "").strip()
    if not c:
        return "未公开所在地" if use_cn else "Not shared"
    return CN.get(c, c) if use_cn else c


def _strip_count(stem):
    """Drop the trailing _<count>, e.g. Japan_3835 -> Japan."""
    return re.sub(r"_\d+$", "", stem)


def normalize_region_dir(d):
    """Restore counted filenames before a run so results keep appending."""
    if not os.path.isdir(d):
        return
    for fp in glob.glob(os.path.join(d, "*.txt")):
        stem = os.path.basename(fp)[:-4]
        plain = _strip_count(stem)
        if plain == stem:
            continue
        dst = os.path.join(d, plain + ".txt")
        if os.path.abspath(dst) == os.path.abspath(fp):
            continue
        if os.path.exists(dst):
            with io.open(dst, "a", encoding="utf-8") as f:
                f.write(io.open(fp, encoding="utf-8", errors="replace").read())
            os.remove(fp)
        else:
            os.rename(fp, dst)


def finalize_region_counts(d):
    """Append row counts to filenames and verify no rows were lost."""
    files = glob.glob(os.path.join(d, "*.txt"))
    before = {}
    for fp in files:
        before[fp] = sum(1 for ln in io.open(fp, encoding="utf-8", errors="replace")
                         if ln.strip())
    total_before = sum(before.values())
    stats = []
    for fp in files:
        stem = _strip_count(os.path.basename(fp)[:-4])
        n = before[fp]
        dst = os.path.join(d, "%s_%d.txt" % (stem, n))
        if os.path.abspath(dst) != os.path.abspath(fp) and not os.path.exists(dst):
            os.rename(fp, dst)
        stats.append((stem, n))
    return stats, total_before, sum(n for _, n in stats)


def load_done():
    """Usernames already processed (for resuming)."""
    return {ln.strip().lstrip("@").lower()
            for ln in io.open(PROGRESS_FILE, encoding="utf-8", errors="replace")
            if ln.strip()} if os.path.isfile(PROGRESS_FILE) else set()


# ────────────────────────────── workers ──────────────────────────────

def build_workers(proxy_tpl=""):
    """ck.txt -> list of Sessions, each with its own egress IP."""
    workers = load_sessions(CK_FILE)
    for w in workers:
        w.proxy = proxy_for(w.ds_user_id, proxy_tpl)
        w.wire = Wire(proxy=(w.proxy or None), timeout=40)
        w.dead = False
    return workers


def prune_ckfile(workers):
    """Rewrite ck.txt keeping only live workers."""
    alive = [w.raw_line for w in workers if not w.dead and w.raw_line]
    tmp = CK_FILE + ".tmp"
    for _ in range(6):
        try:
            with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(("\n".join(alive) + "\n") if alive else "")
            os.replace(tmp, CK_FILE)
            return
        except OSError:
            time.sleep(0.3)


def _worker_dead(status, body):
    """The worker account itself is unusable (relogin / consent / challenge / limit)."""
    b = (body or "").lower()
    if status in (401, 403):
        return True
    return any(sig in b for sig in (
        "login_required", "checkpoint_required", "challenge_required",
        "consent_required", "please log back in", "logged out",
        "csrf", "feedback_required", "rate_limit"))


def query_once(sess, username):
    """Look up one target with one worker -> (flag, country, status).

    flag = "done"   final answer (write to output + progress)
    flag = "dead"   worker auth is broken, drop it from the pool
    flag = "retry"  temporary failure (rate limit/network), requeue target, cool down worker
    """
    try:
        st, user, resp = A.username_info(sess.wire, sess, username)
    except Exception as e:
        return "retry", None, "usernameinfo error: %s" % str(e)[:50]
    if user is None:
        body = resp.text() if resp else ""
        if st == 429 or "rate" in body.lower() or "wait a few" in body.lower():
            return "retry", None, "rate limited (HTTP 429)"
        if _worker_dead(st, body):
            return "dead", None, "worker_dead (HTTP %s)" % st
        if st == 404:
            return "done", None, "not found (HTTP 404)"
        return "retry", None, "usernameinfo failed (HTTP %s)" % st

    pk = str(user.get("pk") or user.get("pk_id") or "")
    try:
        st2, body2 = A.about_this_account(sess.wire, sess, pk)
    except Exception as e:
        return "retry", None, "about error: %s" % str(e)[:50]
    if "bloks_payload" not in body2:        # not the real panel: error/gate/rate limit
        if st2 == 429 or "rate" in body2.lower() or "wait a few" in body2.lower():
            return "retry", None, "rate limited (HTTP 429)"
        if _worker_dead(st2, body2):
            return "dead", None, "worker_dead (HTTP %s)" % st2
        return "retry", None, "about failed (HTTP %s)" % st2

    info = A.parse_about(body2)
    country = info.get("country")
    if country in ("", "未分享", "Not shared", "未公開"):
        country = None
    return "done", country, "ok" if country else "region not shared"


# ────────────────────────────── main ──────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=None,
                    help="concurrent threads (prompted when omitted)")
    ap.add_argument("--delay", type=float, default=2.5,
                    help="seconds between two lookups on the same worker")
    ap.add_argument("--cooldown", type=float, default=20,
                    help="seconds a rate-limited worker stays out of the pool")
    ap.add_argument("--names", choices=("en", "cn"), default="en",
                    help="region filename language (default: en)")
    ap.add_argument("--reset", action="store_true",
                    help="clear progress and output, start over")
    ap.add_argument("--limit", type=int, default=0,
                    help="max targets this run (0 = unlimited, for debugging)")
    args = ap.parse_args()

    # ── concurrency: interactive prompt ──
    if args.threads is None:
        try:
            raw = input("Concurrent threads (Enter for 5): ").strip()
        except EOFError:
            raw = ""
        args.threads = int(raw) if raw.isdigit() and int(raw) > 0 else 5

    if args.reset:
        if os.path.isdir(OUT_DIR):
            for fp in glob.glob(os.path.join(OUT_DIR, "*.txt")):
                try:
                    os.remove(fp)
                except OSError:
                    pass
        for p in (OUT_CSV, PROGRESS_FILE):
            try:
                os.remove(p)
            except OSError:
                pass
        print("[+] progress and output cleared")

    # ── client identity (version numbers etc.) must come from config.ini ──
    S.configure(**load_client_cfg())
    try:
        S.require_client()
    except RuntimeError as e:
        print(RED + "[!] " + str(e) + RESET)
        return

    targets = read_lines(TARGET_FILE)
    if not targets:
        print("[!] targets.txt is empty or missing")
        return

    # ── preflight: surface lines whose username cannot be parsed ──
    n_json = n_plain = n_bad = 0
    bad_samples = []
    for ln in targets:
        s = ln.strip()
        u = target_username(ln)
        is_json = s.find("{") >= 0 and s.rfind("}") > s.find("{")
        if not u or "{" in u or len(u) > 32:
            n_bad += 1
            if len(bad_samples) < 3:
                bad_samples.append(s[:60])
        elif is_json:
            n_json += 1
        else:
            n_plain += 1
    print("[+] targets: JSON %d | plain %d | unparsable %d"
          % (n_json, n_plain, n_bad))
    if n_bad:
        print(YELLOW + "    [!] %d line(s) have no usable username (will be skipped), e.g.:"
              % n_bad + RESET)
        for s in bad_samples:
            print(YELLOW + "        " + s + RESET)

    proxy_tpl = load_proxy_tpl()
    if proxy_tpl:
        print("[+] proxy: %s (one egress IP per worker)" % mask_proxy(proxy_tpl))
    else:
        print(YELLOW + "[!] no proxy configured (config.ini empty); "
              "requests leave from this machine and hit 429 quickly" + RESET)

    workers = build_workers(proxy_tpl)
    if not workers:
        print("[!] no usable workers parsed from ck.txt")
        return

    done = load_done()
    pending = [(i, ln) for i, ln in enumerate(targets, 1)
               if target_username(ln).lower() not in done]
    if args.limit > 0:
        pending = pending[:args.limit]
    nthreads = max(1, min(args.threads, len(workers)))
    already = len(targets) - len(pending)
    print("[+] workers %d | targets %d (done %d, pending %d) | threads %d"
          % (len(workers), len(targets), already, len(pending), nthreads))
    if not pending:
        print(GREEN + "[+] nothing to do, everything is already processed. "
              "Use --reset to start over." + RESET)
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    normalize_region_dir(OUT_DIR)
    tally = {}

    if not os.path.isfile(OUT_CSV):
        try:
            with io.open(OUT_CSV, "w", encoding="utf-8-sig") as f:
                f.write("username,country,region,status\n")
        except OSError:
            pass

    target_q = queue.Queue()
    for item in pending:
        target_q.put(item)
    worker_q = queue.Queue()
    for w in workers:
        worker_q.put(w)

    lock = threading.Lock()
    stop = threading.Event()
    done_n = [0]
    total = len(pending)
    use_cn = (args.names == "cn")

    def record(username, line, country, status):
        """Final answer: write to regions/<bucket>.txt, CSV and progress."""
        bucket = bucket_for(country, status, use_cn)
        path = os.path.join(OUT_DIR, safe_name(bucket) + ".txt")
        with lock:
            safe_append(path, line + "\n")
            safe_append(OUT_CSV, '%s,%s,%s,%s\n' % (
                username, (country or "").replace(",", " "), bucket, status))
            safe_append(PROGRESS_FILE, username + "\n")
            tally[bucket] = tally.get(bucket, 0) + 1
            done_n[0] += 1
            print("[%d/%d] @%-22s -> %-16s [%s] (%s)" % (
                done_n[0], total, username, country or "?", bucket, status))

    def consumer():
        try:
            _consume()
        except Exception as e:
            import traceback
            with lock:
                print(RED + "[thread error] %r" % e + RESET)
                traceback.print_exc()

    def _consume():
        while not stop.is_set():
            try:
                idx, line = target_q.get_nowait()
            except queue.Empty:
                return
            username = target_username(line)
            if not username or "{" in username or len(username) > 32:
                record(username or "?", line, None, "skipped (invalid username)")
                continue

            acc = None
            while not stop.is_set():
                try:
                    acc = worker_q.get(timeout=1)
                except queue.Empty:
                    if all(w.dead for w in workers):
                        stop.set()
                        break
                    continue
                break
            if acc is None:
                return

            flag, country, status = query_once(acc, username)
            if flag == "dead":
                acc.dead = True
                with lock:
                    prune_ckfile(workers)
                    print(YELLOW + "[drop] worker uid=%s is dead (%s), removed from ck.txt"
                          % (acc.ds_user_id, status) + RESET)
                target_q.put((idx, line))
                continue

            if flag == "retry":
                target_q.put((idx, line))
                with lock:
                    print(YELLOW + "[retry] @%s %s, worker uid=%s cooling down %ds"
                          % (username, status, acc.ds_user_id, args.cooldown) + RESET)
                time.sleep(args.cooldown)
                worker_q.put(acc)
                continue

            record(username, line, country, status)
            worker_q.put(acc)
            time.sleep(args.delay)

    print("[+] starting...")
    ts = [threading.Thread(target=consumer, daemon=True) for _ in range(nthreads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    prune_ckfile(workers)

    stats, tb, ta = finalize_region_counts(OUT_DIR)
    print("\n[+] done. Totals per region (regions/):")
    for name, n in sorted(stats, key=lambda x: -x[1]):
        print("    %-20s %d" % (name, n))
    print("    total %d rows %s" % (ta, "(OK)" if tb == ta else "(MISMATCH %d!=%d)" % (tb, ta)))
    print("    detail: %s | progress: %s" % (OUT_CSV, PROGRESS_FILE))

    alive = sum(1 for w in workers if not w.dead)
    dead = len(workers) - alive
    if dead:
        print(YELLOW + "    removed %d dead worker(s) from ck.txt, %d left"
              % (dead, alive) + RESET)
    left = target_q.qsize()
    if alive == 0:
        print(RED + "=" * 60 + RESET)
        print(RED + "[!] every worker is dead (stuck on new terms or logged out)." + RESET)
        print(RED + "    ck.txt was emptied; add working accounts and run again to resume."
              + RESET)
        print(RED + "=" * 60 + RESET)
    elif left:
        print(YELLOW + "    %d target(s) left (not enough workers); "
              "add accounts and run again to resume" % left + RESET)


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            input("\nPress Enter to exit...")
        except EOFError:
            pass
