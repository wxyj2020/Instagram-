# IG Region Lookup

Batch-lookup the **account region** of Instagram profiles (the "About this account"
panel) and export them grouped by country/region.

It runs a pool of logged-in worker accounts against a queue of target usernames:
each thread borrows a worker, looks up one target, and returns the worker.
One worker is only ever used by one thread at a time, which naturally rate-limits
requests. Rate limits, expired sessions and network errors are handled by cooling
down the worker, dropping it from the pool, or putting the target back in the queue.

## Features

- Accepts plain usernames, legacy `username----password----...` lines, and JSON lines
- Worker pool with automatic rotation; 429s trigger a cooldown
- Optional per-account proxy session (derived from the account uid), one egress IP per worker
- Resumable: finished usernames go to `_done.txt`, re-running picks up where you stopped
- Results grouped into `regions/<Country>.txt` plus a `result.csv` summary
- Dead workers are removed from `ck.txt` automatically

## Layout

```
ig-region-lookup/
├── ig_region_lookup.py    # entry point: queue / thread pool / output
├── ig_about.py            # About-this-account bloks endpoint + parser
├── ig_session.py          # session, cookie parsing, device derivation, proxy, transport
├── config.example.ini     # copy to config.ini
├── ck.example.txt         # copy to ck.txt
├── targets.example.txt    # copy to targets.txt
└── requirements.txt
```

## Install

```bash
git clone https://github.com/wxyj2020/Instagram-.git
cd Instagram-
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt
```

`rnet` is used to mimic an Android OkHttp TLS/HTTP2 fingerprint. If it cannot be
installed the tool falls back to `requests` — it still works, but rate limits hit sooner.

## Prepare three files

### 1. `ck.txt` — worker accounts (must be logged in)

One account per line, formats can be mixed.

**JSON line** (recommended, device fields optional):

```json
{"ds_user_id": "1234567890", "sessionid": "1234567890%3Aabc...", "csrftoken": "xxx", "mid": "xxx", "ig_did": "xxx", "datr": "xxx"}
```

**Cookie string** (copied from a browser or a capture):

```
datr=xxx; ig_did=xxx; mid=xxx; csrftoken=xxx; ds_user_id=1234567890; sessionid=1234567890%3Aabc...
```

When device fields are missing they are derived **deterministically** from `ds_user_id`
(`android_id`, `device_id`, `family_device_id`, `mid`), so the same account always
looks like the same device across runs.

### 2. `targets.txt` — usernames to look up

```
cristiano
somebody_else
{"username": "third_one", "note": "extra fields are ignored"}
```

### 3. `config.ini` — client identifiers + proxy

Copy `config.example.ini` to `config.ini` and fill in `[client]` (required):

```ini
[client]
app_version = x.y.z.w
version_code = 1234567890
bloks_version = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ua =
```

| Key | Where to get it |
|---|---|
| `app_version` | the version right after `Instagram` in a logged-in request's `User-Agent` |
| `version_code` | the number at the end of the same `User-Agent` |
| `bloks_version` | the `x-bloks-version-id` request header |
| `ua` | optional; an Android UA is built from the two values above |

Any capture tool (Charles, Proxyman, mitmproxy) on a logged-in app request gives you
all three. No version numbers are hardcoded in the source, so an app update only
requires editing `config.ini`.

Proxy (optional but strongly recommended):

```ini
[proxy]
url = socks5h://user:pass@gate.example.com:1080
```

If the template contains `{sid}`, it is replaced per worker uid so every account
uses its own sticky egress IP:

```ini
url = socks5h://user:pass@gate.example.com:1080?sessid={sid}
```

## Run

```bash
python ig_region_lookup.py                 # asks for concurrency interactively
python ig_region_lookup.py --threads 8     # 8 threads
python ig_region_lookup.py --delay 3       # 3s between two lookups on the same worker
python ig_region_lookup.py --limit 20      # only 20 targets, for debugging
python ig_region_lookup.py --reset         # wipe progress and output, start over
```

| Flag | Default | Meaning |
|---|---|---|
| `--threads` | prompt / 5 | concurrent threads, capped by the number of workers |
| `--delay` | 2.5 | seconds between two lookups on the same worker |
| `--cooldown` | 20 | how long a rate-limited worker stays out of the pool |
| `--limit` | 0 | max targets this run, 0 = unlimited |
| `--reset` | - | delete `_done.txt`, `regions/` and `result.csv` first |

## Output

```
regions/United States.txt    # target lines whose account region is the US
regions/Japan.txt
regions/Invalid.txt          # 404 / unusable lines
regions/Not shared.txt       # region not public
result.csv                   # username,country,region,status
_done.txt                    # usernames already processed (resume)
```

At the end each region file gets a row-count suffix (`United States_3835.txt`);
the next run strips it back to the plain name, appends to it, and verifies that
the total row count is preserved.

## How it works

- **Auth**: `Authorization: Bearer IGT:2:<base64>`, where the payload is just
  `{"ds_user_id": "...", "sessionid": "..."}` — so a cookie string is enough.
- **Username -> pk**: `GET /api/v1/users/<username>/usernameinfo/`
- **Account region**: `POST /api/v1/bloks/apps/com.instagram.interactions.about_this_account/`
  returns a bloks tree; the region is the `initial` value of
  `about_this_account_country`. The old `/api/v1/users/<pk>/account_details/`
  endpoint is retired.
- **Language**: requests are sent with `en_US`, so region names come back in English
  and are used directly as output filenames (`regions/United States.txt`).
- **Concurrency**: worker queue + target queue + thread pool. `retry` puts the
  target back, `dead` removes the worker.
- **Client identifiers**: `app_version`, `version_code` and `bloks_version` all come
  from `config.ini`; nothing is pinned in source.

## Notes

- "Account region" is the region the account reports in About-this-account — not the
  IP location and not the signup country. Profiles that hide it land in `Not shared`.
- High request rates trigger 429. Use more workers, keep `--delay` above 2s, and give
  every worker its own proxy.
- Workers must be in a normal logged-in state. Accounts stuck on "agree to new terms"
  or a checkpoint are treated as dead and removed from `ck.txt`.
- This project is for learning and protocol research. Automating Instagram may violate
  its Terms of Service; any account restrictions are your own responsibility.

## Contact

- Telegram: [@wxyjw](https://t.me/wxyjw)
- Telegram: [@insshopMall1](https://t.me/insshopMall1)
- Mall address: https://wxyj.cc/
- Telegram channel: https://t.me/Instagram_Mall

## License

MIT
