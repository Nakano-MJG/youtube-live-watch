"""
YouTube ライブ配信ウォッチャー

channels.csv に書いたチャンネルが「いまライブ配信中か」「同時視聴者数は何人か」を調べ、
  - 配信開始 / 配信終了を Teams に通知（通知先の Secret を設定した場合）
  - 配信中の同時視聴者数を data/viewers_YYYY-MM.csv に記録
  - 配信1回ごとのまとめ（開始・終了・最大/平均視聴者数）を data/sessions.csv に記録
します。GitHub Actions から定期実行する想定です。外部ライブラリは使いません（Python標準のみ）。

必要な環境変数
  YOUTUBE_API_KEY    : YouTube Data API v3 のAPIキー（必須）
  TEAMS_<通知先名>    : 通知先ごとの Webhook URL（例: TEAMS_SAPPORO）。Secret を作り、watch.yml に1行追記して渡す
                       channels.csv の notify 列に通知先名（例: SAPPORO）を書くと、そこへ送られる
  TEAMS_WEBHOOK_URL  : notify 列が空のチャンネルの通知先（任意。どちらも未設定なら通知しない）
"""

import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHANNELS_FILE = os.path.join(BASE_DIR, "channels.csv")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
DATA_DIR = os.path.join(BASE_DIR, "data")

API_BASE = "https://www.googleapis.com/youtube/v3/"
RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
JST = timezone(timedelta(hours=9))

# 各チャンネルの「最近の動画」を何件までさかのぼって配信中か調べるか（1〜50）
RECENT_VIDEOS = 50


class ApiError(Exception):
    pass


# ---------------------------------------------------------------- 小道具
def now_jst():
    return datetime.now(JST)


def fmt_jst(dt):
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M")


def parse_iso(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "youtube-live-watch/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def api(endpoint, **params):
    """YouTube Data API を呼ぶ。失敗したら ApiError。"""
    params["key"] = os.environ["YOUTUBE_API_KEY"]
    url = API_BASE + endpoint + "?" + urllib.parse.urlencode(params)
    try:
        return json.loads(http_get(url))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        reason = ""
        try:
            reason = json.loads(body)["error"]["errors"][0]["reason"]
        except Exception:
            pass
        hint = {
            "quotaExceeded": "1日のAPI利用上限に達しました（日本時間16〜17時ごろにリセット）。",
            "keyInvalid": "APIキーが正しくありません。Secrets の YOUTUBE_API_KEY を確認してください。",
            "accessNotConfigured": "Google Cloud で YouTube Data API v3 が有効になっていません。",
            "forbidden": "APIキーの制限設定でブロックされています。",
        }.get(reason, "")
        raise ApiError(f"YouTube API エラー {e.code} {reason} {hint}\n{body[:500]}")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def append_csv(path, header, rows):
    """Excelで文字化けしないよう、新規作成時はBOM付きUTF-8にする。"""
    if not rows:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


# ---------------------------------------------------------------- チャンネル
def read_channels():
    """channels.csv を読む。列: name（表示名・任意）, channel（@ハンドル / UC〜のID / URL）"""
    items = []
    with open(CHANNELS_FILE, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            ch = (row.get("channel") or "").strip()
            if not ch or (row.get("name") or "").lstrip().startswith("#"):
                continue
            items.append({"name": (row.get("name") or "").strip(), "input": ch,
                          "notify": (row.get("notify") or "").strip()})
    return items


def parse_channel_input(text):
    """('id', 'UC...') か ('handle', '@xxx') を返す。"""
    text = urllib.parse.unquote(text.strip())
    m = re.search(r"(UC[\w-]{22})", text)
    if m:
        return "id", m.group(1)
    m = re.search(r"@([\w.\-]+)", text)
    if m:
        return "handle", "@" + m.group(1)
    return "handle", "@" + text.lstrip("@")


def resolve_channels(channel_inputs, cache):
    """入力をチャンネルID・正式名・アップロード再生リストIDに変換（結果は state にキャッシュ）。"""
    resolved = []
    for item in channel_inputs:
        key = item["input"]
        info = cache.get(key)
        if not info:
            kind, value = parse_channel_input(key)
            params = {"part": "snippet,contentDetails"}
            params["id" if kind == "id" else "forHandle"] = value
            res = api("channels", **params)
            if not res.get("items"):
                print(f"[警告] チャンネルが見つかりません: {key}", file=sys.stderr)
                continue
            c = res["items"][0]
            info = {
                "id": c["id"],
                "title": c["snippet"]["title"],
                "uploads": c["contentDetails"]["relatedPlaylists"]["uploads"],
            }
            cache[key] = info
        resolved.append({**info, "name": item["name"] or info["title"], "notify": item["notify"]})
    return resolved


# ---------------------------------------------------------------- 動画の取得
def recent_video_ids_from_rss(channel_id):
    """RSSフィード（API使用量ゼロ）から最近の動画IDを取る。失敗しても無視。"""
    try:
        root = ET.fromstring(http_get(RSS_URL.format(channel_id), timeout=10))
    except Exception:
        return []
    ns = {"yt": "http://www.youtube.com/xml/schemas/2015"}
    return [e.text for e in root.iterfind(".//yt:videoId", ns) if e.text]


def recent_video_ids_from_api(uploads_playlist):
    try:
        res = api("playlistItems", part="contentDetails",
                  playlistId=uploads_playlist, maxResults=RECENT_VIDEOS)
    except ApiError as e:
        if " 404 " in str(e):  # 動画が1本もないチャンネル
            return []
        raise
    return [i["contentDetails"]["videoId"] for i in res.get("items", [])]


def fetch_videos(video_ids):
    """動画IDのリストから {id: video} を返す（50件ずつまとめて取得、1回=1ユニット）。"""
    out = {}
    ids = list(dict.fromkeys(video_ids))
    for i in range(0, len(ids), 50):
        res = api("videos", part="snippet,liveStreamingDetails", id=",".join(ids[i:i + 50]))
        for v in res.get("items", []):
            out[v["id"]] = v
    return out


def is_live(video):
    d = video.get("liveStreamingDetails") or {}
    return bool(d.get("actualStartTime")) and not d.get("actualEndTime")


def viewers_of(video):
    v = (video.get("liveStreamingDetails") or {}).get("concurrentViewers")
    # 視聴者0人のときや非公開設定のときは値が返ってこないので、0人として扱う
    return int(v) if v is not None else 0


# ---------------------------------------------------------------- Teams 通知
def load_webhooks():
    """Secret TEAMS_WEBHOOKS（1行に「通知先名=URL」）を辞書にする。"""
    hooks = {}
    for line in os.environ.get("TEAMS_WEBHOOKS", "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, url = line.split("=", 1)  # URL内の「=」は区切りにしない
        hooks[key.strip()] = url.strip()
    return hooks


RESERVED = {"TEAMS_WEBHOOKS", "TEAMS_WEBHOOK_URL"}


def load_secret_webhooks():
    """環境変数「TEAMS_<通知先名>」を {通知先名(大文字): URL} にする。"""
    return {k[len("TEAMS_"):]: v.strip() for k, v in os.environ.items()
            if k.upper().startswith("TEAMS_") and k.upper() not in RESERVED and v.strip()}


def webhook_urls(notify):
    """notify 列の値（「;」区切りで複数可）から送信先URLのリストを返す。"""
    hooks = load_webhooks()
    secret_hooks = {k.upper(): v for k, v in load_secret_webhooks().items()}
    default = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    keys = [k for k in re.split(r"[;|\s]+", notify or "") if k]
    if not keys:
        return [default] if default else []
    urls = []
    for k in keys:
        if k in hooks:
            urls.append(hooks[k])
        elif k.upper() in secret_hooks:
            urls.append(secret_hooks[k.upper()])
        else:
            print(f"[警告] 通知先「{k}」が見つかりません（Secret「TEAMS_{k.upper()}」の登録と watch.yml への追記を確認してください）",
                  file=sys.stderr)
            if default:
                urls.append(default)
    return list(dict.fromkeys(urls))


def notify_teams(notify, title, color, facts, url):
    webhooks = webhook_urls(notify)
    if not webhooks:
        return
    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": title, "weight": "Bolder",
                     "size": "Medium", "color": color, "wrap": True},
                    {"type": "FactSet",
                     "facts": [{"title": k, "value": str(v)} for k, v in facts]},
                ],
                "actions": [{"type": "Action.OpenUrl", "title": "YouTubeで開く", "url": url}],
            },
        }],
    }
    for webhook in webhooks:
        req = urllib.request.Request(
            webhook, data=json.dumps(card).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:  # 通知失敗で監視全体を止めない
            print(f"[警告] Teams通知に失敗: {e}", file=sys.stderr)


def fmt_viewers(n):
    return "非公開" if n is None else f"{n:,} 人"


def fmt_duration(start, end):
    minutes = int((end - start).total_seconds() // 60)
    return f"{minutes // 60}時間{minutes % 60:02d}分"


# ---------------------------------------------------------------- メイン
def main():
    if not os.environ.get("YOUTUBE_API_KEY"):
        sys.exit("YOUTUBE_API_KEY が設定されていません。")

    first_run = not os.path.exists(STATE_FILE)  # 初回は今の配信中を記録するだけで通知しない
    state = load_state()
    cache = state.setdefault("channel_cache", {})
    live_state = state.setdefault("live", {})
    now = now_jst()

    channels = resolve_channels(read_channels(), cache)
    by_id = {c["id"]: c for c in channels}

    # 1) 調べる動画IDを集める（RSS ＋ アップロード一覧 ＋ 前回まで配信中だった動画）
    candidates = {}
    for c in channels:
        ids = recent_video_ids_from_rss(c["id"]) + recent_video_ids_from_api(c["uploads"])
        for vid in ids:
            candidates[vid] = c["id"]
    for vid, s in live_state.items():
        candidates.setdefault(vid, s["channel_id"])

    videos = fetch_videos(list(candidates))

    viewer_rows, session_rows = [], []
    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    # 2) いま配信中の動画
    current_live = {vid: v for vid, v in videos.items()
                    if is_live(v) and v["snippet"]["channelId"] in by_id}
    for vid, v in current_live.items():
        ch = by_id[v["snippet"]["channelId"]]
        title = v["snippet"]["title"]
        n = viewers_of(v)
        url = f"https://www.youtube.com/watch?v={vid}"
        started = parse_iso(v["liveStreamingDetails"]["actualStartTime"])

        s = live_state.get(vid)
        if s is None:
            s = live_state[vid] = {
                "channel_id": ch["id"], "channel_name": ch["name"], "title": title,
                "started_at": started.isoformat(), "peak": 0, "sum": 0, "samples": 0,
                "notify": ch["notify"],
            }
            if not first_run:
                notify_teams(ch["notify"], f"🔴 配信開始：{ch['name']}", "Attention", [
                    ("タイトル", title),
                    ("開始時刻", fmt_jst(started)),
                    ("同時視聴者数", fmt_viewers(n)),
                ], url)
            print(f"[{'初回記録' if first_run else '開始'}] {ch['name']} / {title}")

        s["title"] = title
        s["notify"] = ch["notify"]  # 途中で通知先を変えた場合は終了通知に反映
        if n is not None:
            s["peak"] = max(s["peak"], n)
            s["sum"] += n
            s["samples"] += 1
        viewer_rows.append([ts, ch["name"], ch["id"], vid, title, "" if n is None else n, url])
        print(f"[配信中] {ch['name']} / {fmt_viewers(n)} / {title}")

    # 3) 前回は配信中だったが、いまは配信中でない → 終了
    for vid in [v for v in live_state if v not in current_live]:
        s = live_state.pop(vid)
        v = videos.get(vid)
        d = (v or {}).get("liveStreamingDetails") or {}
        ended = parse_iso(d.get("actualEndTime")) or now
        started = parse_iso(s["started_at"])
        avg = round(s["sum"] / s["samples"]) if s["samples"] else None
        peak = s["peak"] if s["samples"] else None
        url = f"https://www.youtube.com/watch?v={vid}"
        notify_teams(s.get("notify", ""), f"⏹ 配信終了：{s['channel_name']}", "Default", [
            ("タイトル", s["title"]),
            ("配信時間", f"{fmt_jst(started)} 〜 {fmt_jst(ended)}（{fmt_duration(started, ended)}）"),
            ("最大視聴者数", fmt_viewers(peak)),
            ("平均視聴者数", fmt_viewers(avg)),
        ], url)
        session_rows.append([
            s["channel_name"], s["channel_id"], vid, s["title"],
            fmt_jst(started), fmt_jst(ended), fmt_duration(started, ended),
            "" if peak is None else peak, "" if avg is None else avg, s["samples"], url,
        ])
        print(f"[終了] {s['channel_name']} / {s['title']}")

    # 4) 記録して保存
    append_csv(os.path.join(DATA_DIR, f"viewers_{now:%Y-%m}.csv"),
               ["日時", "チャンネル", "チャンネルID", "動画ID", "タイトル", "同時視聴者数", "URL"],
               viewer_rows)
    append_csv(os.path.join(DATA_DIR, "sessions.csv"),
               ["チャンネル", "チャンネルID", "動画ID", "タイトル", "開始", "終了", "配信時間",
                "最大視聴者数", "平均視聴者数", "計測回数", "URL"],
               session_rows)
    # channels.csv から消したチャンネルのキャッシュは捨てる
    keep = {c["input"] for c in read_channels()}
    state["channel_cache"] = {k: v for k, v in cache.items() if k in keep}
    save_state(state)

    if not current_live:
        print(f"配信中のチャンネルはありません（監視 {len(channels)} ch）")


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        sys.exit(str(e))
