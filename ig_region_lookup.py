# -*- coding: utf-8 -*-
"""批量查询 Instagram「账户所在地」并按地区分类保存。

输入
----
  ck.txt         工作号池, 一行一个(格式见 ig_session.py 顶部说明)。
                 查询必须带登录态, 轮流使用, 撞限频/掉线自动换下一个。
  targets.txt    要查的目标账号, 一行一个; 兼容两种行格式:
                   - 用户名             (最简单)
                   - 用户名----密码----... (老格式, 只取第一段)
                   - {"username": "xxx", ...} (JSON 行, 只取 username)

输出
----
  regions/美国.txt、regions/日本.txt ...   按账户所在地分文件, 存目标原始整行
  result.csv                                明细: 用户名,国家,地区,状态
  _done.txt                                 已查过的用户名(中断后可续查)

用法
----
  python ig_region_lookup.py                  # 交互输入并发数
  python ig_region_lookup.py --threads 8      # 8 线程
  python ig_region_lookup.py --delay 2.5      # 同一个号两次查询间隔(秒)
  python ig_region_lookup.py --reset          # 清空进度和结果重查
  python ig_region_lookup.py --limit 20       # 只查 20 个(调试)

并发模型
--------
  工作号池 + 目标队列 + N 个线程。线程"借号 -> 查一个 -> 还号",
  同一个号任一时刻只被一个线程使用(天然限频); 号失效就踢出池,
  目标塞回队列给别的号。并发数和号数解耦, 线程多于可用号时自动排队。
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
import ig_about as A

# ── Windows 控制台 ANSI 颜色 ──
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

# 英文国家名(IG en_US 返回) -> 中文文件名。没映射到的用英文原名。
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


# ────────────────────────────── 配置 ──────────────────────────────

def load_proxy_tpl():
    """从 config.ini [proxy] url 读代理模板; 没有返回空(不走代理)。"""
    if not os.path.isfile(CONFIG_FILE):
        return ""
    cp = configparser.ConfigParser(interpolation=None)   # 关插值, 避免 % 报错
    try:
        cp.read(CONFIG_FILE, encoding="utf-8-sig")
        if cp.has_option("proxy", "url"):
            return (cp.get("proxy", "url") or "").strip()
    except Exception as e:
        print(YELLOW + "[!] 读 config.ini 代理失败: %s" % e + RESET)
    return ""


# ────────────────────────────── 文件工具 ──────────────────────────────

def read_lines(path):
    if not os.path.isfile(path):
        return []
    return [ln.rstrip("\n").rstrip("\r")
            for ln in io.open(path, encoding="utf-8-sig", errors="replace")
            if ln.strip() and not ln.lstrip().startswith("#")]


def target_username(line):
    """从目标行提取 IG 用户名, 兼容纯用户名 / ---- 老格式 / JSON 行。"""
    s = (line or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:                              # JSON 行: 抠出 {...}
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
    return re.sub(r'[\\/:*?"<>|]', "_", (s or "").strip()) or "_空"


def safe_append(path, text):
    """追加写文件; 文件被占用(比如 Excel 打开)时重试几次, 仍失败只警告。"""
    for _ in range(6):
        try:
            with io.open(path, "a", encoding="utf-8") as f:
                f.write(text)
            return True
        except (PermissionError, OSError):
            time.sleep(0.3)
    if path not in _warned_locked:
        _warned_locked.add(path)
        print(YELLOW + "[!] 写不进 %s (是不是被别的程序打开了?), 已跳过"
              % os.path.basename(path) + RESET)
    return False


_warned_locked = set()


def bucket_for(country, status):
    """-> 分桶名(中文国家 / 失效 / 未公开所在地)。"""
    if status.startswith("目标查不到") or status.startswith("跳过"):
        return "失效"
    c = (country or "").strip()
    if not c:
        return "未公开所在地"
    return CN.get(c, c)


def _strip_count(stem):
    """去掉文件名尾部的 _数字 (如 中国_3835 -> 中国)。"""
    return re.sub(r"_\d+$", "", stem)


def normalize_region_dir(d):
    """跑前把 regions/ 里带数量的文件名还原成纯名, 便于续查继续追加。"""
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
    """跑完给每个文件名加数量: 中国.txt -> 中国_3835.txt, 并做总量守恒校验。"""
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
    """已查过的用户名集合(续查用)。"""
    return {ln.strip().lstrip("@").lower()
            for ln in io.open(PROGRESS_FILE, encoding="utf-8", errors="replace")
            if ln.strip()} if os.path.isfile(PROGRESS_FILE) else set()


# ────────────────────────────── 工作号 ──────────────────────────────

def build_workers(proxy_tpl=""):
    """ck.txt -> [Session], 每个号分配独立出口 IP。"""
    workers = load_sessions(CK_FILE)
    for w in workers:
        w.proxy = proxy_for(w.ds_user_id, proxy_tpl)
        w.wire = Wire(proxy=(w.proxy or None), timeout=40)
        w.dead = False
    return workers


def prune_ckfile(workers):
    """把死号从 ck.txt 删掉, 只留活号的原始行。"""
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
    """工作号本身不可用(需重新登录/同意条款/被挑战/限频)。"""
    b = (body or "").lower()
    if status in (401, 403):
        return True
    return any(sig in b for sig in (
        "login_required", "checkpoint_required", "challenge_required",
        "consent_required", "please log back in", "logged out",
        "csrf", "feedback_required", "rate_limit"))


def query_once(sess, username):
    """用一个工作号查一个目标 -> (flag, country, status)。

    flag = "done"   有定论(写入结果 + 记进度)
    flag = "dead"   工作号认证失效, 踢出池
    flag = "retry"  临时失败(限频/网络), 目标塞回队列, 号冷却
    """
    try:
        st, user, resp = A.username_info(sess.wire, sess, username)
    except Exception as e:
        return "retry", None, "usernameinfo异常:%s" % str(e)[:50]
    if user is None:
        body = resp.text() if resp else ""
        if st == 429 or "rate" in body.lower() or "wait a few" in body.lower():
            return "retry", None, "限频(HTTP 429)"
        if _worker_dead(st, body):
            return "dead", None, "worker_dead(HTTP %s)" % st
        if st == 404:
            return "done", None, "目标查不到(HTTP 404)"
        return "retry", None, "usernameinfo异常(HTTP %s)" % st

    pk = str(user.get("pk") or user.get("pk_id") or "")
    try:
        st2, body2 = A.about_this_account(sess.wire, sess, pk)
    except Exception as e:
        return "retry", None, "about异常:%s" % str(e)[:50]
    if "bloks_payload" not in body2:        # 不是真面板, 多半是错误/闸门/限频
        if st2 == 429 or "rate" in body2.lower() or "wait a few" in body2.lower():
            return "retry", None, "限频(HTTP 429)"
        if _worker_dead(st2, body2):
            return "dead", None, "worker_dead(HTTP %s)" % st2
        return "retry", None, "about异常(HTTP %s)" % st2

    info = A.parse_about(body2)
    country = info.get("country")
    if country in ("", "未分享", "Not shared", "未公開"):
        country = None
    return "done", country, "ok" if country else "所在地未公开"


# ────────────────────────────── 主流程 ──────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=None,
                    help="并发线程数(不填则启动时交互输入)")
    ap.add_argument("--delay", type=float, default=2.5,
                    help="同一个号两次查询的间隔秒")
    ap.add_argument("--cooldown", type=float, default=20,
                    help="工作号被限频(429)后的冷却秒数")
    ap.add_argument("--reset", action="store_true",
                    help="清空进度和分类结果, 从头重新查")
    ap.add_argument("--limit", type=int, default=0,
                    help="本次最多查几个(0=不限, 调试用)")
    args = ap.parse_args()

    # ── 线程数: 启动页交互 ──
    if args.threads is None:
        try:
            raw = input("并发线程数 (回车默认 5): ").strip()
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
        print("[+] 已重置进度和结果, 从头开始")

    targets = read_lines(TARGET_FILE)
    if not targets:
        print("[!] targets.txt 为空或不存在")
        return

    # ── 启动体检: 提前暴露取不出用户名的坏行 ──
    n_json = n_old = n_bad = 0
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
            n_old += 1
    print("[+] 目标体检: JSON格式 %d | 普通格式 %d | 取不出用户名 %d"
          % (n_json, n_old, n_bad))
    if n_bad:
        print(YELLOW + "    [!] 有 %d 行取不出有效用户名(将跳过), 例如:" % n_bad + RESET)
        for s in bad_samples:
            print(YELLOW + "        " + s + RESET)

    proxy_tpl = load_proxy_tpl()
    if proxy_tpl:
        print("[+] 代理: %s (每号独立会话IP)" % mask_proxy(proxy_tpl))
    else:
        print(YELLOW + "[!] 未配置代理(config.ini 为空), 从本机 IP 出, 易被 429" + RESET)

    workers = build_workers(proxy_tpl)
    if not workers:
        print("[!] ck.txt 没解析出可用工作号")
        return

    done = load_done()
    pending = [(i, ln) for i, ln in enumerate(targets, 1)
               if target_username(ln).lower() not in done]
    if args.limit > 0:
        pending = pending[:args.limit]
    nthreads = max(1, min(args.threads, len(workers)))
    already = len(targets) - len(pending)
    print("[+] 工作号 %d | 当前目标 %d(已查 %d, 待查 %d) | 并发 %d 线程"
          % (len(workers), len(targets), already, len(pending), nthreads))
    if not pending:
        print(GREEN + "[+] 全部查过了, 无待查目标。--reset 可从头重查。" + RESET)
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    normalize_region_dir(OUT_DIR)
    tally = {}

    if not os.path.isfile(OUT_CSV):
        try:
            with io.open(OUT_CSV, "w", encoding="utf-8-sig") as f:
                f.write("用户名,国家,地区,状态\n")
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

    def record(username, line, country, status):
        """定论结果: 按国家写 regions/<中文国家>.txt + CSV + 进度。"""
        bucket = bucket_for(country, status)
        path = os.path.join(OUT_DIR, safe_name(bucket) + ".txt")
        with lock:
            safe_append(path, line + "\n")
            safe_append(OUT_CSV, '%s,%s,%s,%s\n' % (
                username, (country or "").replace(",", " "), bucket, status))
            safe_append(PROGRESS_FILE, username + "\n")
            tally[bucket] = tally.get(bucket, 0) + 1
            done_n[0] += 1
            print("[%d/%d] @%-22s -> %-11s [%s] (%s)" % (
                done_n[0], total, username, country or "?", bucket, status))

    def consumer():
        try:
            _consume()
        except Exception as e:
            import traceback
            with lock:
                print(RED + "[线程异常] %r" % e + RESET)
                traceback.print_exc()

    def _consume():
        while not stop.is_set():
            try:
                idx, line = target_q.get_nowait()
            except queue.Empty:
                return
            username = target_username(line)
            if not username or "{" in username or len(username) > 32:
                record(username or "?", line, None, "跳过(取不出有效用户名)")
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
                    print(YELLOW + "[删号] 工作号 uid=%s 已失效(%s), 已从 ck.txt 删除"
                          % (acc.ds_user_id, status) + RESET)
                target_q.put((idx, line))
                continue

            if flag == "retry":
                target_q.put((idx, line))
                with lock:
                    print(YELLOW + "[重试] @%s %s, 号 uid=%s 冷却 %ds"
                          % (username, status, acc.ds_user_id, args.cooldown) + RESET)
                time.sleep(args.cooldown)
                worker_q.put(acc)
                continue

            record(username, line, country, status)
            worker_q.put(acc)
            time.sleep(args.delay)

    print("[+] 开始查询...")
    ts = [threading.Thread(target=consumer, daemon=True) for _ in range(nthreads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    prune_ckfile(workers)

    stats, tb, ta = finalize_region_counts(OUT_DIR)
    print("\n[+] 本次完成。各地区累计(regions/):")
    for name, n in sorted(stats, key=lambda x: -x[1]):
        print("    %-14s %d" % (name, n))
    print("    合计 %d 行 %s" % (ta, "(守恒OK)" if tb == ta else "(守恒异常:%d!=%d)" % (tb, ta)))
    print("    明细: %s | 进度: %s" % (OUT_CSV, PROGRESS_FILE))

    alive = sum(1 for w in workers if not w.dead)
    dead = len(workers) - alive
    if dead:
        print(YELLOW + "    已从 ck.txt 删除 %d 个失效号, 剩余活号 %d 个"
              % (dead, alive) + RESET)
    left = target_q.qsize()
    if alive == 0:
        print(RED + "=" * 56 + RESET)
        print(RED + "[!] 所有工作号都失效了(多半卡在「同意新条款」或掉线)," + RESET)
        print(RED + "    ck.txt 已清空, 补充可用号后再运行会自动续查。" + RESET)
        print(RED + "=" * 56 + RESET)
    elif left:
        print(YELLOW + "    还有 %d 个目标没查(工作号不够), 补号后再运行会自动续查"
              % left + RESET)


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            input("\n按回车键退出...")
        except EOFError:
            pass
