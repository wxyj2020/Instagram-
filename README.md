# IG Region Lookup

批量查询 Instagram 账号的「账户所在地」(About this account) 并按国家/地区分类导出。

用工作号池 + 目标队列 + 多线程的方式跑：每个线程借一个工作号、查一个目标、再还回去。
同一个号同一时刻只被一个线程使用，天然限频；号掉线或被限频会自动换号、冷却、重试，
目标不会丢。

## 功能

- 两种输入行格式都支持：纯用户名、`用户名----密码----...`、JSON 行（自动取 `username`）
- 工作号池自动轮换，遇 429 / 登录失效会冷却或踢出
- 每个工作号可绑定独立代理出口（按 uid 派生 `{sid}`，一个号固定一个 IP）
- 断点续查：进度写在 `_done.txt`，中断后重跑不会重复请求
- 结果按真实国家分文件（`regions/美国.txt`、`regions/日本.txt` …），另出 `result.csv` 明细
- 失效工作号自动从 `ck.txt` 删除，避免反复撞同一个死号

## 目录结构

```
ig-region-lookup/
├── ig_region_lookup.py     # 主程序: 队列 / 线程池 / 分类落盘
├── ig_about.py             # 「关于此账户」bloks 接口 + 解析
├── ig_session.py           # 会话、cookie 解析、设备派生、代理、传输层
├── config.example.ini      # 代理配置样例(复制成 config.ini 再改)
├── ck.example.txt          # 工作号样例(复制成 ck.txt)
├── targets.example.txt     # 目标账号样例(复制成 targets.txt)
└── requirements.txt
```

## 安装

```bash
git clone <this-repo>
cd ig-region-lookup
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt
```

`rnet` 用于模拟 Android OkHttp 的 TLS/HTTP2 指纹。装不上时会自动退回 `requests`
（多数情况下也能用，但更容易被限频）。

## 准备三个文件

### 1. `ck.txt` —— 工作号（必须已登录）

每行一个号，可以混排。二选一：

**JSON 行**（推荐，设备信息可自己固定）：

```json
{"ds_user_id": "1234567890", "sessionid": "1234567890%3Aabc...", "csrftoken": "xxx", "mid": "xxx", "ig_did": "xxx", "datr": "xxx"}
```

**cookie 串**（从浏览器或抓包复制）：

```
datr=xxx; ig_did=xxx; mid=xxx; csrftoken=xxx; ds_user_id=1234567890; sessionid=1234567890%3Aabc...
```

cookie 串里没有设备字段时，会按 `ds_user_id` 派生一套**固定**的 `android_id` /
`device_id` / `family_device_id`，同一个号每次运行都是同一套，不会变成"新设备"。

### 2. `targets.txt` —— 要查的账号

```
cristiano
somebody_else
{"username": "third_one", "note": "任意附加字段"}
```

### 3. `config.ini` —— 代理（可选但强烈建议）

```ini
[proxy]
url = socks5h://user:pass@gate.example.com:1080
```

模板里带 `{sid}` 时，会按工作号 uid 替换成独立会话 id，让每个号走不同出口 IP：

```ini
url = socks5h://user:pass@gate.example.com:1080?sessid={sid}
```

## 运行

```bash
python ig_region_lookup.py                # 交互输入并发数
python ig_region_lookup.py --threads 8    # 8 线程
python ig_region_lookup.py --delay 3      # 同一个号两次查询间隔 3 秒
python ig_region_lookup.py --limit 20     # 只查 20 个, 调试用
python ig_region_lookup.py --reset        # 清空进度和结果重查
```

参数说明：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--threads` | 交互输入 / 5 | 并发线程数，不会超过工作号数量 |
| `--delay` | 2.5 | 同一个号两次查询之间的间隔（秒） |
| `--cooldown` | 20 | 号被限频后冷却多久再放回池子 |
| `--limit` | 0 | 本次最多查几个，0 = 不限 |
| `--reset` | - | 删除 `_done.txt`、`regions/`、`result.csv` 后重跑 |

## 输出

```
regions/美国.txt      # 账户所在地 = 美国 的目标行
regions/日本.txt
regions/失效.txt      # 目标不存在 / 取不出用户名
regions/未公开所在地.txt
result.csv           # 用户名,国家,地区,状态
_done.txt            # 已查用户名(续查用)
```

`regions/` 里的文件名在结束时会被加上行数后缀（`美国_3835.txt`），
下次启动会自动还原成纯名继续追加，并做总量守恒校验。

## 实现要点

- **鉴权**：`Authorization: Bearer IGT:2:<base64>`，payload 就是
  `{"ds_user_id": "...", "sessionid": "..."}`，所以只有 cookie 串也能拼出来。
- **用户名 → pk**：`GET /api/v1/users/<username>/usernameinfo/`
- **账户所在地**：`POST /api/v1/bloks/apps/com.instagram.interactions.about_this_account/`，
  返回 bloks 树，从 `about_this_account_country` 的 `initial` 取值。
  旧的 `/api/v1/users/<pk>/account_details/` 已下线。
- **语言**：请求统一按 `en_US` 发，国家名是英文，再做中文映射，归类更稳。
- **并发模型**：号池 + 目标队列 + 线程池；`retry` 的目标回队列、`dead` 的号踢出池。

## 注意

- 账号所在地不是 IP 归属地，也不是注册地，是账号自己设置/系统记录的地区；
  对方隐藏或接口没返回时归到 `未公开所在地`。
- 请求频率过高会触发 429。建议：号多一点、`--delay` 不要低于 2 秒、每个号独立代理。
- 工作号必须处于正常登录状态；卡在「同意新条款 / 身份验证」的号会被判为失效并删除。
- 本项目仅用于学习与协议研究。使用自动化手段访问 Instagram 可能违反其服务条款，
  由此产生的账号封禁等后果由使用者自行承担。

## License

MIT
