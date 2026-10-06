"""旅行セール情報を自動収集して、一覧ページを作るスクリプト。

GitHub Actions で定期実行し、結果を GitHub Pages の一覧ページに出す。ローカルでも `python main.py` で動きます。
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urljoin

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "seen.json"
PAGE_FILE = ROOT / "docs" / "index.html"
JST = timezone(timedelta(hours=9))
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.8",
}
NOW = datetime.now(JST)


# ---------- 共通 ----------
def log(msg: str) -> None:
    print(f"[{datetime.now(JST):%H:%M:%S}] {msg}", flush=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"items": {}, "pages": {}, "status": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


LAST_ERROR: dict[str, str] = {}


def fetch(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers=UA, timeout=25)
        r.raise_for_status()
        return r.content
    except Exception as e:  # 1つ失敗しても全体は止めない
        code = getattr(getattr(e, "response", None), "status_code", None)
        LAST_ERROR[url] = (f"HTTP {code}（アクセスを拒否された可能性）" if code in (401, 403, 429)
                           else f"HTTP {code}" if code else type(e).__name__)
        log(f"取得失敗: {url} ({e})")
        return None


def set_status(state: dict, name: str, url: str, ok: bool, note: str) -> None:
    state.setdefault("status", {})[name] = {
        "url": url, "ok": ok, "note": note, "checked": NOW.isoformat()}


def norm_title(t: str) -> str:
    """Google ニュースの「タイトル - 媒体名」から媒体名を外して重複判定に使う。"""
    t = re.sub(r"\s+-\s+[^-]{1,30}$", "", t).strip()
    return re.sub(r"\s+", "", t)


# ---------- 日付抽出 ----------
DATE_PATTERNS = [
    re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日"),
    re.compile(r"(?<!\d)(?:(\d{4})/)?(\d{1,2})/(\d{1,2})(?!\d)"),
]


# 日付の前にこの言葉があれば「販売（セール）の日付」/「搭乗・旅行の日付」
SALE_LABEL = re.compile(r"販売期間|発売期間|予約期間|購入期間|セール期間|受付期間|販売開始|発売開始|"
                        r"予約開始|予約受付|販売日|発売日|開催期間")
TRAVEL_LABEL = re.compile(r"搭乗期間|搭乗日|対象搭乗|ご搭乗|旅行期間|出発期間|出発日|宿泊期間|宿泊日|"
                          r"利用期間|対象期間|運航期間|設定期間|チェックイン|旅行日")
# 日付のすぐ後ろにこの言葉があれば搭乗側（例:「11月1日搭乗分」「12/1出発」）
TRAVEL_AFTER = re.compile(r"搭乗|出発|宿泊|旅行|利用|運航|チェックイン|発着")
SALE_AFTER = re.compile(r"販売|発売|予約|セール|開始|スタート|開催|受付|まで")


def extract_dates_full(text: str) -> tuple[list[str], list[str]]:
    """文中の日付を (セール販売の日付, 搭乗・旅行の日付) に分けて返す。
    年が書かれていれば、その後ろの年なしの日付にも同じ年を使う（例: 2026年6月10日～6月16日）。
    年が一度も無ければ、近い未来/直近の日付として推定する。"""
    text = unicodedata.normalize("NFKC", text)  # 全角数字・記号を半角に
    matches = sorted((m.start(), m.end(), m.groups()) for pat in DATE_PATTERNS for m in pat.finditer(text))
    labels = sorted([(m.start(), "sale") for m in SALE_LABEL.finditer(text)] +
                    [(m.start(), "travel") for m in TRAVEL_LABEL.finditer(text)])
    sale, travel, last_year, prev = set(), set(), None, None
    for pos, endpos, (y, m, d) in matches:
        try:
            m, d = int(m), int(d)
            if y:
                last_year = int(y)
                dt = date(last_year, m, d)
            elif last_year:
                dt = date(last_year, m, d)
                if prev and dt < prev:  # 12月28日～1月5日 のような年またぎ
                    dt = date(last_year + 1, m, d)
            else:
                dt = date(NOW.year, m, d)
                if dt < NOW.date() - timedelta(days=60):
                    dt = date(NOW.year + 1, m, d)  # 12月に「1月10日」など
            prev = dt
        except ValueError:
            continue

        # どちらの日付か判定：①直前のラベル ②すぐ後ろの言葉 ③どちらも無ければセールの日付
        before = [kind for lp, kind in labels if lp < pos]
        if before:
            kind = before[-1]
        else:
            after = re.split(r"[\s、。,]", text[endpos:endpos + 14], maxsplit=1)[0]  # 区切りまで
            t, sl = TRAVEL_AFTER.search(after), SALE_AFTER.search(after)
            kind = "travel" if t and (not sl or t.start() < sl.start()) else "sale"
        (travel if kind == "travel" else sale).add(dt.isoformat())
    return sorted(sale), sorted(travel)


def extract_dates(text: str) -> list[str]:
    """セール（販売）の日付だけ。"""
    return extract_dates_full(text)[0]


def has_any_date(text: str) -> bool:
    s, t = extract_dates_full(text)
    return bool(s or t)


# ---------- 収集 ----------
def classify(title: str, cfg: dict) -> dict | None:
    if not any(k.lower() in title.lower() for k in cfg["include_keywords"]):
        return None
    if any(k in title for k in cfg.get("exclude_keywords", [])):
        return None
    return {
        "announce": any(k in title for k in cfg.get("announce_keywords", [])),
        "dates": extract_dates(title),
        "travel": extract_dates_full(title)[1],
    }


def entry_time(e) -> str:
    t = e.get("published_parsed") or e.get("updated_parsed")
    if t:
        return datetime(*t[:6], tzinfo=timezone.utc).astimezone(JST).isoformat()
    return NOW.isoformat()


def collect_feeds(cfg: dict, state: dict) -> list[dict]:
    feeds = []
    for q in cfg.get("google_news_queries", []):
        url = ("https://news.google.com/rss/search?q="
               f"{quote(q + ' when:7d')}&hl=ja&gl=JP&ceid=JP:ja")
        feeds.append((f"Google ニュース「{q}」", url))
    for f in cfg.get("rss_feeds", []):
        feeds.append((f["name"], f["url"]))

    results = []
    for name, url in feeds:
        raw = fetch(url)
        if not raw:
            if not name.startswith("Google"):
                set_status(state, name, url, False, LAST_ERROR.get(url, "取得失敗"))
            continue
        parsed = feedparser.parse(raw)
        if not name.startswith("Google"):
            set_status(state, name, url, bool(parsed.entries), f"記事 {len(parsed.entries)}件"
                       if parsed.entries else "RSSとして読めませんでした")
        for e in parsed.entries:
            title = html.unescape(e.get("title", "")).strip()
            info = classify(title, cfg)
            if not info:
                continue
            src = e.get("source", {}).get("title") if e.get("source") else None
            results.append({
                "id": hashlib.sha1(norm_title(title).encode()).hexdigest()[:16],
                "title": title,
                "link": e.get("link", ""),
                "source": src or name,
                "published": entry_time(e),
                **info,
            })
        log(f"{name}: {len(parsed.entries)}件中 該当を抽出")
    return results


# 公式サイトから拾う語（見出し・リンク・画像の説明文はこれだけで拾う）
PAGE_KEYS = ["セール", "sale", "タイムセール", "キャンペーン", "販売期間", "販売開始",
             "予約開始", "発売", "特価", "半額"]


def page_snippets(raw: bytes, base_url: str) -> tuple[dict[str, str], int]:
    """ページからセールに関係しそうな短い文を集める。{文: リンク先}"""
    soup = BeautifulSoup(raw, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "form"]):
        t.decompose()
    text_len = len(soup.get_text(" ", strip=True))
    out: dict[str, str] = {}

    def add(text: str, link: str, strong: bool) -> None:
        text = unicodedata.normalize("NFKC", re.sub(r"\s+", " ", text or "")).strip()
        if not (4 <= len(text) <= 140):
            return
        if not any(k in text.lower() for k in PAGE_KEYS):
            return
        if not strong and not has_any_date(text):  # 本文の文は日付つきだけ（注意書きを除くため）
            return
        out.setdefault(text, link)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        href = base_url if href.startswith(("javascript", "#")) else urljoin(base_url, href)
        add(a.get_text(" "), href, True)
        for img in a.find_all("img", alt=True):
            add(img["alt"], href, True)
    for img in soup.find_all("img", alt=True):
        add(img["alt"], base_url, True)
    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        add(h.get_text(" "), base_url, True)
    for el in soup.find_all(["p", "li", "dd", "dt", "td", "th", "div", "span"]):
        if el.find(["p", "li", "div", "ul", "ol", "table", "dl"]):
            continue  # いちばん内側の要素だけ見る
        add(el.get_text(" "), base_url, False)
    return out, text_len


def check_pages(cfg: dict, state: dict) -> list[dict]:
    """各社公式ページを読み、前回は無かったセール関連の文を新着として返す。"""
    found = []
    for p in cfg.get("watch_pages") or []:
        name, url = p["name"], p["url"]
        raw = fetch(url)
        if not raw:
            set_status(state, name, url, False, LAST_ERROR.get(url, "取得失敗"))
            continue
        snippets, text_len = page_snippets(raw, url)
        if not snippets:
            note = ("中身を読み取れませんでした（JavaScriptで表示するサイトの可能性）"
                    if text_len < 800 else "セール関連の文は見つかりませんでした（正常）")
            set_status(state, name, url, text_len >= 800, note)
        else:
            set_status(state, name, url, True, f"セール関連の文 {len(snippets)}件")

        prev = state["pages"].get(url)
        first_time = not isinstance(prev, list)  # 初めて見るページ（旧形式も含む）
        prev_set = set(prev or []) if not first_time else set()
        state["pages"][url] = sorted(snippets)

        for text, link in snippets.items():
            if text in prev_set:
                continue
            short = name.split()[0]  # 「ANA 国内線タイムセール」→「ANA」
            title = text if short.lower() in text.lower() else f"{name}｜{text}"
            found.append({
                "id": hashlib.sha1((url + text).encode()).hexdigest()[:16],
                "title": title,
                "link": link,
                "source": f"{name}（公式）",
                "published": NOW.isoformat(),
                "announce": any(k in text for k in cfg.get("announce_keywords", []) + ["予定", "販売期間"]),
                "dates": extract_dates(text),
                "travel": extract_dates_full(text)[1],
                "_page_first": first_time,
            })
        log(f"{name}: 文{len(snippets)}件 / 新規{sum(1 for t in snippets if t not in prev_set)}件")
    return found


# ---------- 一覧ページ ----------
HOTEL_WORDS = ["ホテル", "宿", "旅館", "ツアー", "楽天トラベル", "じゃらん", "一休", "Expedia",
               "エクスペディア", "パッケージ", "宿泊"]


def split_title(i: dict) -> tuple[str, str]:
    """(見出し, 補足) を返す。公式ページは「ページ名｜文」なのでページ名を見出しにする。"""
    t = re.sub(r"\s+-\s+[^-]{1,30}$", "", i["title"]).strip()
    if "｜" in t:
        head, body = t.split("｜", 1)
        return head, body
    return t, ""


def source_name(i: dict) -> str:
    """表示用の媒体名。Google ニュース経由ならタイトル末尾の媒体名を使う。"""
    src = i["source"].replace("（公式）", "")
    if src.startswith("Google ニュース"):
        m = re.search(r"\s+-\s+([^-]{1,30})$", i["title"])
        return m.group(1).strip() if m else "ニュース"
    return src


def category(i: dict) -> str:
    if i["source"].endswith("（公式）"):
        return "official"
    return "hotel" if any(w.lower() in i["title"].lower() for w in HOTEL_WORDS) else "flight"


# 各社の色（タイトルや媒体名に名前が出てきたら、その会社の色で表示）
BRANDS = [
    (r"ANA|全日空|全日本空輸", "ANA", "#1a4ba0"),
    (r"JAL|日本航空", "JAL", "#d7000f"),
    (r"Peach|ピーチ", "Peach", "#e5007e"),
    (r"ジェットスター|Jetstar", "Jetstar", "#ff5a00"),
    (r"ZIPAIR|ジップエア", "ZIPAIR", "#00a37a"),
    (r"スカイマーク|Skymark|SKYMARK", "SKY", "#f2b600"),
    (r"スプリング|Spring Japan|春秋", "SPRING", "#86bc25"),
    (r"スターフライヤー|StarFlyer|STARFLYER", "SFJ", "#2b2b2b"),
    (r"ソラシド", "ソラシド", "#00a0d2"),
    (r"AIRDO|エア・ドゥ|エアドゥ", "AIRDO", "#0071bc"),
    (r"楽天", "楽天", "#bf0000"),
    (r"じゃらん", "じゃらん", "#ff6b00"),
    (r"一休", "一休", "#1c2c4c"),
    (r"Expedia|エクスペディア", "Expedia", "#1e243a"),
]
DARK_TEXT_BRANDS = {"SKY"}  # 黄色など、白文字だと読みにくい色
INK_OVERRIDE = {"SKY": "#b38600"}  # 白背景で数字が読みにくい色は濃いめに


def brand(i: dict) -> tuple[str, str]:
    """一番最初に名前が出てくる会社を返す。(表示名, 色)"""
    text = i["title"] + " " + i["source"]
    best = None
    for pat, label, color in BRANDS:
        m = re.search(pat, text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), label, color)
    return (best[1], best[2]) if best else ("旅行", "#8a8f98")


# ---------- 同じセールの記事をまとめる ----------
def sale_kind(title: str) -> str:
    t = title.lower()
    if "タイムセール" in t:
        return "timesale"
    if "スーパーsale" in t or "スーパーセール" in t:
        return "super"
    if "ブラックフライデー" in t:
        return "blackfriday"
    return "sale"


def route_kind(title: str) -> str:
    return "intl" if "国際線" in title else "dom" if "国内線" in title else ""


def _bigrams(t: str) -> set[str]:
    t = re.sub(r"[\s\W\d_]", "", re.sub(r"\s+-\s+[^-]{1,30}$", "", t))
    return {t[k:k + 2] for k in range(len(t) - 1)}


def similar(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    return len(A & B) / max(1, len(A | B))


def _is_official(i: dict) -> bool:
    return i["source"].endswith("（公式）")


def group_items(items: list[dict]) -> list[dict]:
    """同じ会社・同じセールの記事を1つのグループにまとめる。
    同じとみなす条件:
      日付つき同士 … 会社が同じで、セール期間が重なる（または開始日の差が2日以内）
      日付なし     … 会社が同じで、セールの種類（タイムセール等）が同じ、掲載が10日以内
      会社不明     … タイトルがよく似ている
    国内線と国際線ははっきり書き分けられていれば別扱い。"""
    order = sorted(items, key=lambda i: (not i["dates"], not _is_official(i), i["published"]))
    groups: list[dict] = []
    for i in order:
        b = brand(i)[0]
        st = min(i["dates"]) if i["dates"] else None
        en = max(i["dates"]) if i["dates"] else None
        pub = datetime.fromisoformat(i["published"])
        target = None
        for g in groups:
            if g["brand"] != b:
                continue
            rk, gk = route_kind(i["title"]), g["route"]
            if rk and gk and rk != gk:
                continue
            if b == "旅行":
                ok = similar(i["title"], g["rep"]["title"]) >= 0.5
            elif st and g["start"]:
                ok = (st <= g["end"] and g["start"] <= en) or \
                     abs((date.fromisoformat(st) - date.fromisoformat(g["start"])).days) <= 2
            elif st and not g["start"]:
                ok = False
            else:  # 日付なしの記事 → 種類と掲載時期で判断
                near = abs((pub - g["pub"]).days) <= 10
                ok = near and (sale_kind(i["title"]) == g["kind"]
                               or similar(i["title"], g["rep"]["title"]) >= 0.45)
            if ok:
                target = g
                break
        if target is None:
            groups.append({"brand": b, "rep": i, "members": [i], "start": st, "end": en, "pub": pub,
                           "kind": sale_kind(i["title"]), "route": route_kind(i["title"])})
        else:
            target["members"].append(i)
            if not target["route"]:
                target["route"] = route_kind(i["title"])

    for g in groups:
        ms = g["members"]
        # 代表の記事：公式 > 日付つき > 予告 > 新しい順
        official = [m for m in ms if _is_official(m)]
        pool = official or ms
        pool = [m for m in pool if m["dates"]] or pool
        g["rep"] = max(pool, key=lambda m: (m["announce"], m["published"]))
        rep = g["rep"]
        if rep["dates"]:
            g["start"], g["end"] = min(rep["dates"]), max(rep["dates"])
        g["travel"] = next((m["travel"] for m in [rep] + ms if m.get("travel")), [])
        g["members"] = sorted(ms, key=lambda m: m["published"], reverse=True)
        g["published"] = max(m["published"] for m in ms)
        g["first_seen"] = max(m.get("first_seen", "") for m in ms)
    return groups


TEMPLATE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#111111" media="(prefers-color-scheme: dark)">
<title>旅行セール</title>
<style>
:root{
  --bg:#ffffff; --bg2:#f7f8fa; --fill:#f1f2f4; --ink:#111111; --sub:#777b82; --mute:#a3a7ad;
  --line:#efefef; --red:#ff334b; --green:#06c755;
  box-sizing:border-box; padding-top:env(safe-area-inset-top,0px); padding-bottom:env(safe-area-inset-bottom,0px);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#111111; --bg2:#1b1c1e; --fill:#232427; --ink:#f2f3f5; --sub:#9a9ea5; --mute:#6b6f76; --line:#232427;}}
:root[data-theme="dark"]{--bg:#111111; --bg2:#1b1c1e; --fill:#232427; --ink:#f2f3f5; --sub:#9a9ea5; --mute:#6b6f76; --line:#232427;}
*{box-sizing:border-box}
html{scroll-padding-top:env(safe-area-inset-top,0px)}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Hiragino Sans","Hiragino Kaku Gothic ProN","Noto Sans JP",Meiryo,sans-serif;
  -webkit-font-smoothing:antialiased;font-feature-settings:"palt"}
a{color:inherit;text-decoration:none}
:focus-visible{outline:2px solid var(--green);outline-offset:2px}
.wrap{max-width:640px;margin:0 auto}

header{padding:28px 20px 6px}
h1{margin:0;font-size:26px;font-weight:800;letter-spacing:-.01em}
.updated{margin:4px 0 0;font-size:12px;color:var(--mute)}
.summary{display:flex;gap:10px;margin:18px 0 0}
.sum{flex:1;background:var(--bg2);border-radius:12px;padding:12px 14px}
.sum b{display:block;font-size:22px;font-weight:800;font-variant-numeric:tabular-nums}
.sum span{font-size:12px;color:var(--sub)}

.bar{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:var(--bg)}
.search-box{padding:14px 20px 4px}
.search{width:100%;height:40px;padding:0 14px;font:inherit;font-size:14px;color:var(--ink);background:var(--fill);border:0;border-radius:10px}
.search::placeholder{color:var(--mute)}
.tabs{display:flex;gap:22px;padding:0 20px;overflow-x:auto;scrollbar-width:none;border-bottom:1px solid var(--line)}
.tabs::-webkit-scrollbar{display:none}
.tab{flex:none;font:inherit;font-size:15px;font-weight:600;color:var(--mute);background:none;border:0;
  padding:12px 0 10px;border-bottom:2px solid transparent;cursor:pointer}
.tab[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--ink);font-weight:800}

section{padding-top:8px}
h2{display:flex;justify-content:space-between;align-items:baseline;margin:0;padding:22px 20px 6px;font-size:18px;font-weight:800}
h2 span{font-size:13px;font-weight:500;color:var(--sub)}
.day{padding:8px 20px;font-size:12px;font-weight:700;color:var(--sub);background:var(--bg2)}

.group{border-bottom:1px solid var(--line)}
.cell{display:flex;gap:14px;align-items:center;padding:14px 20px}
.cell:active{background:var(--bg2)}
.logo{flex:none;width:44px;height:44px;border-radius:12px;background:var(--brand);color:#fff;
  display:grid;place-items:center;font-size:10.5px;font-weight:800;letter-spacing:-.02em;text-align:center;line-height:1.1}
.logo.dark-text{color:#1a1a1a}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .logo{box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}}
:root[data-theme="dark"] .logo{box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}
.small .logo{width:36px;height:36px;border-radius:10px;font-size:9px}
.main{flex:1;min-width:0}
.t{font-size:15px;font-weight:700;line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.small .t{font-weight:500;font-size:14.5px}
.s{margin-top:2px;font-size:13px;color:var(--sub);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.m{margin-top:4px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:12px;color:var(--mute)}
.new{color:var(--red);font-weight:800}
.tag{padding:1px 6px;border-radius:4px;background:var(--fill);color:var(--sub);font-weight:600;font-size:11px}

.cd{flex:none;text-align:right;min-width:62px}
.cd small{display:block;font-size:11px;color:var(--sub)}
.cd b{display:block;font-size:24px;font-weight:800;line-height:1.15;color:var(--brand-ink);font-variant-numeric:tabular-nums}
.cd b i{font-style:normal;font-size:12px;margin-left:1px}
.cd .live{display:inline-block;padding:1px 6px;border-radius:4px;background:var(--red);color:#fff;font-weight:700}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .cd b{color:color-mix(in srgb,var(--brand) 62%,#fff)}}
:root[data-theme="dark"] .cd b{color:color-mix(in srgb,var(--brand) 62%,#fff)}

.more{margin:-6px 0 0;padding:0 20px 12px 78px}
.small .more,.group.small .more{padding-left:70px}
.more summary{display:inline-block;font-size:12px;font-weight:600;color:var(--sub);cursor:pointer;list-style:none;
  padding:3px 10px;border-radius:999px;background:var(--fill)}
.more summary::-webkit-details-marker{display:none}
.more[open] summary{margin-bottom:6px}
.more a{display:block;padding:7px 0;border-top:1px solid var(--line);font-size:13px;line-height:1.45}
.more a:first-of-type{border-top:0}
.more a span{display:block;font-size:11.5px;color:var(--mute)}
.ended{padding:1px 6px;border-radius:4px;background:var(--fill);color:var(--mute);font-weight:600;font-size:11px}
.empty{margin:8px 20px;padding:28px 16px;text-align:center;color:var(--sub);font-size:14px;background:var(--bg2);border-radius:12px}
details.sites-box{margin:28px 20px 0;background:var(--bg2);border-radius:12px}
details.sites-box>summary{padding:14px 16px;font-size:14px;font-weight:700;cursor:pointer;list-style:none;display:flex;justify-content:space-between}
details.sites-box>summary::-webkit-details-marker{display:none}
details.sites-box>summary span{font-weight:500;color:var(--sub)}
.site{display:flex;gap:10px;padding:10px 16px;border-top:1px solid var(--line);font-size:13px}
.site i{font-style:normal;font-weight:800;color:var(--green)}.site.ng i{color:var(--red)}
.site small{display:block;color:var(--sub);font-size:12px}
footer{padding:18px 20px 44px;font-size:12px;color:var(--mute);line-height:1.6}
[hidden]{display:none!important}
</style></head><body>
<div class="wrap">
<header>
  <h1>旅行セール</h1>
  <p class="updated">%%UPDATED%% 更新</p>
  <div class="summary">
    <div class="sum"><b>%%N_UP%%</b><span>販売予定・販売中</span></div>
    <div class="sum"><b>%%N_NEW%%</b><span>24時間の新着</span></div>
  </div>
</header>
<div class="bar">
  <div class="search-box"><input class="search" id="q" type="search" placeholder="航空会社・行き先で検索" aria-label="キーワードで絞り込み"></div>
  <div class="tabs" role="tablist" aria-label="種類で絞り込み">
    <button class="tab" role="tab" data-f="all" aria-selected="true">すべて</button>
    <button class="tab" role="tab" data-f="new" aria-selected="false">新着</button>
    <button class="tab" role="tab" data-f="flight" aria-selected="false">航空券</button>
    <button class="tab" role="tab" data-f="hotel" aria-selected="false">ホテル・ツアー</button>
    <button class="tab" role="tab" data-f="official" aria-selected="false">公式の告知</button>
  </div>
</div>
<main>
  <section><h2>これから始まるセール<span>販売開始が近い順</span></h2>%%UPCOMING%%</section>
  <section><h2>最近の記事</h2>%%RECENT%%</section>
  <p class="empty" id="nohit" hidden>条件に合うセールはありません</p>
  <details class="sites-box"><summary>見ているサイト<span>%%SITE_SUM%%</span></summary>%%SITES%%</details>
</main>
<footer>ニュースと各社公式サイトの文章から自動で集めています。日付を読み違えることがあるため、予約前にリンク先で条件を確認してください。</footer>
</div>
<script>
(function(){
  var DAY=864e5,today=new Date();today.setHours(0,0,0,0);
  function md(d){return (d.getMonth()+1)+'/'+d.getDate()+'('+'日月火水木金土'[d.getDay()]+')';}
  document.querySelectorAll('.group[data-start]').forEach(function(c){
    var s=new Date(c.dataset.start+'T00:00:00'),e=new Date(c.dataset.end+'T00:00:00'),cd=c.querySelector('.cd');
    var ds=Math.round((s-today)/DAY),de=Math.round((e-today)/DAY);
    if(ds>0) cd.innerHTML='<small>販売開始まで</small><b>'+ds+'<i>日</i></b><small>'+md(s)+'</small>';
    else if(ds===0) cd.innerHTML='<small>今日から</small><b>販売</b><small>'+md(s)+'</small>';
    else if(de>=0) cd.innerHTML='<small><span class="live">販売中</span></small><b>'+de+'<i>日</i></b><small>'+md(e)+'まで</small>';
    else {c.hidden=true;c.dataset.gone='1';}
  });
  var q=document.getElementById('q'),f='all';
  function apply(){
    var v=q.value.trim().toLowerCase(),shown=0;
    document.querySelectorAll('[data-text]').forEach(function(el){
      if(el.dataset.gone)return;
      var ok=(!v||el.dataset.text.indexOf(v)>=0)&&(f==='all'||(f==='new'?el.dataset.new==='1':el.dataset.cat===f));
      el.hidden=!ok;if(ok)shown++;
    });
    document.querySelectorAll('.day').forEach(function(h){
      var n=h.nextElementSibling,any=false;
      while(n&&!n.classList.contains('day')){if(!n.hidden&&n.dataset.text)any=true;n=n.nextElementSibling;}
      h.hidden=!any;
    });
    document.getElementById('nohit').hidden=shown>0;
  }
  q.addEventListener('input',apply);
  document.querySelectorAll('.tab').forEach(function(t){
    t.addEventListener('click',function(){
      document.querySelectorAll('.tab').forEach(function(x){x.setAttribute('aria-selected','false');});
      t.setAttribute('aria-selected','true');f=t.dataset.f;apply();
    });
  });
})();
</script></body></html>"""


def render_page(items: list[dict], status: dict) -> None:
    e = html.escape
    today = NOW.date().isoformat()
    new_since = (NOW - timedelta(hours=24)).isoformat()
    week = "月火水木金土日"

    def md(iso: str) -> str:
        d = date.fromisoformat(iso)
        return f"{d.month}/{d.day}({week[d.weekday()]})"

    def rng(ds: list[str]) -> str:
        return md(ds[0]) if len(ds) == 1 or ds[0] == ds[-1] else f"{md(ds[0])}〜{md(ds[-1])}"

    def group_html(g: dict, right: str = "", small: bool = False, ended: bool = False) -> str:
        i = g["rep"]
        is_new = g["first_seen"] >= new_since
        head, sub = split_title(i)
        label, color = brand(i)
        src = source_name(i)
        meta = '<span class="new">新着</span>' if is_new else ""
        meta += '<span class="ended">終了</span>' if ended else ""
        meta += '<span class="tag">公式</span>' if _is_official(i) else ""
        meta += "" if src == head else f"<span>{e(src)}</span>"
        if g["travel"]:
            meta += f"<span>搭乗・旅行 {rng(g['travel'])}</span>"
        others = [m for m in g["members"] if m is not i]
        text = " ".join(m["title"] + m["source"] for m in g["members"]).lower()
        extra = f' data-start="{g["start"]}" data-end="{g["end"]}"' if right else ""
        more = ""
        if others:
            links = "".join(
                f'<a href="{e(m["link"])}" target="_blank" rel="noopener">{e(" ".join(split_title(m)).strip())}'
                f'<span>{e(source_name(m))}</span></a>' for m in others)
            more = f'<details class="more"><summary>関連する記事 {len(others)}件</summary>{links}</details>'
        return (f'<div class="group{" small" if small else ""}" style="--brand:{color};--brand-ink:{INK_OVERRIDE.get(label, color)}" '
                f'data-text="{e(text)}" data-cat="{category(i)}" data-new="{1 if is_new else 0}"{extra}>'
                f'<a class="cell" href="{e(i["link"])}" target="_blank" rel="noopener">'
                f'<span class="logo{" dark-text" if label in DARK_TEXT_BRANDS else ""}">{e(label)}</span>'
                f'<div class="main"><div class="t">{e(head)}</div>'
                + (f'<div class="s">{e(sub)}</div>' if sub else "")
                + (f'<div class="m">{meta}</div>' if meta else "")
                + f'</div>{right}</a>{more}</div>')

    groups = group_items(items)

    # これから始まる／開催中（セールの販売期間で判断）
    upcoming = [g for g in groups if g["start"] and g["end"] >= today]
    upcoming.sort(key=lambda g: g["start"] if g["start"] >= today else g["end"])
    up_cells = []
    for g in upcoming:
        start, end = g["start"], g["end"]
        ds = (date.fromisoformat(start) - NOW.date()).days
        de = (date.fromisoformat(end) - NOW.date()).days
        if ds > 0:
            cd = f'<small>販売開始まで</small><b>{ds}<i>日</i></b><small>{md(start)}</small>'
        elif ds == 0:
            cd = f'<small>今日から</small><b>販売</b><small>{md(start)}</small>'
        else:
            cd = f'<small><span class="live">販売中</span></small><b>{de}<i>日</i></b><small>{md(end)}まで</small>'
        up_cells.append(group_html(g, f'<div class="cd">{cd}</div>'))
    up_html = "".join(up_cells) or '<p class="empty">販売開始日が分かっているセールはまだありません</p>'

    # 最近の記事（日付なし、または販売期間が終わったもの）を日ごとに
    recent = sorted([g for g in groups if g not in upcoming], key=lambda g: g["published"], reverse=True)
    parts, last = [], None
    for g in recent:
        d = datetime.fromisoformat(g["published"]).astimezone(JST).date()
        diff = (NOW.date() - d).days
        label = "今日" if diff == 0 else "昨日" if diff == 1 else md(d.isoformat())
        if label != last:
            parts.append(f'<div class="day">{label}</div>')
            last = label
        parts.append(group_html(g, small=True, ended=bool(g["end"] and g["end"] < today)))
    recent_html = "".join(parts) or '<p class="empty">まだ記事がありません</p>'

    ok = sum(1 for v in status.values() if v["ok"])
    sites = "".join(
        f'<div class="site{"" if v["ok"] else " ng"}"><i>{"○" if v["ok"] else "×"}</i>'
        f'<div><a href="{e(v["url"])}" target="_blank" rel="noopener">{e(k)}</a><small>{e(v["note"])}</small></div></div>'
        for k, v in sorted(status.items(), key=lambda kv: kv[1]["ok"]))
    n_new = sum(1 for g in groups if g["first_seen"] >= new_since)

    page = (TEMPLATE
            .replace("%%UPDATED%%", f"{NOW.month}月{NOW.day}日 {NOW:%H:%M}")
            .replace("%%N_UP%%", str(len(upcoming)))
            .replace("%%N_NEW%%", str(n_new))
            .replace("%%UPCOMING%%", up_html)
            .replace("%%RECENT%%", recent_html)
            .replace("%%SITE_SUM%%", f"{ok} / {len(status)} 読めています" if status else "未巡回")
            .replace("%%SITES%%", sites or '<div class="site">初回の巡回後に表示されます</div>'))
    PAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAGE_FILE.write_text(page, encoding="utf-8")
    log(f"一覧ページを生成: {len(items)}記事 → {len(groups)}件にまとめ（予定{len(upcoming)} / 最近{len(recent)}）")


# ---------- メイン ----------
def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    state = load_state()
    first_run = not state["items"]
    # 日付の読み取りルールを直したときのため、保存済みの記事も毎回読み直す
    for it in state["items"].values():
        it["dates"], it["travel"] = extract_dates_full(it["title"])

    # 初回は全部NEWになると見づらいので、NEW扱いにしない
    old = (NOW - timedelta(days=2)).isoformat()
    seen_at = old if first_run else NOW.isoformat()
    new_items = []
    for it in collect_feeds(cfg, state) + check_pages(cfg, state):
        if it["id"] in state["items"]:
            continue
        # 初めて監視したページの内容は「今あるもの」なのでNEWにしない
        it["first_seen"] = old if it.pop("_page_first", False) else seen_at
        state["items"][it["id"]] = it
        if it["first_seen"] != old:
            new_items.append(it)

    # 古い記事を削除（ただし未来の日付つきは残す）
    cutoff = (NOW - timedelta(days=cfg.get("keep_days", 45))).isoformat()
    today = NOW.date().isoformat()
    state["items"] = {k: v for k, v in state["items"].items()
                      if v["published"] >= cutoff or (v["dates"] and max(v["dates"]) >= today)}

    log(f"新着 {len(new_items)}件" + ("（初回登録）" if first_run else ""))
    render_page(list(state["items"].values()), state.get("status", {}))
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
