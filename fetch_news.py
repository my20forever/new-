"""
每日新聞摘要 → 推播到 LINE
------------------------------------------------
台灣（3則）來源：公視新聞網、中央社 (CNA)
國際（3則）來源：Reuters、Associated Press (AP)

技術說明：
- 公視、中央社都有官方公開 RSS，直接讀取。
- Reuters / AP 官方已不再提供公開免費 RSS，這裡改用
  Google News 的搜尋 RSS（site:reuters.com / site:apnews.com）
  當作免費替代方案。這是非官方 workaround，Google 若調整
  規則可能會失效，屆時需要更新 INTL_QUERIES 或改用付費新聞 API。
- 國際新聞（英文）會自動翻譯成繁體中文再送出，使用 deep-translator
  套件（背後呼叫 Google 翻譯免費網頁版接口）。同樣是非官方用法，
  翻譯品質為機器翻譯，重要資訊建議點連結回原文確認。

需要的環境變數（GitHub Actions 用 Secrets 注入）：
    LINE_CHANNEL_ACCESS_TOKEN   LINE 官方帳號的 Messaging API channel access token
"""

import os
import sys
import html
import re
import time
from datetime import datetime, timezone

import feedparser
import requests
from deep_translator import GoogleTranslator

# ---------- 新聞來源設定 ----------

DOMESTIC_FEEDS = {
    "公視新聞": "https://about.pts.org.tw/rss/XML/newsfeed.xml",
    "中央社-要聞": "https://feeds.feedburner.com/rsscna/politics",
    "中央社-社會": "https://feeds.feedburner.com/rsscna/social",
    "中央社-地方": "https://feeds.feedburner.com/rsscna/local",
    "中央社-產經": "https://feeds.feedburner.com/rsscna/finance",
    "中央社-科技": "https://feeds.feedburner.com/rsscna/technology",
    "中央社-生活": "https://feeds.feedburner.com/rsscna/lifehealth",
}

# Google News 搜尋 RSS：鎖定特定網域，模擬「訂閱該媒體」
INTL_QUERIES = {
    "Reuters": "https://news.google.com/rss/search?q=site:reuters.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    "AP (Associated Press)": "https://news.google.com/rss/search?q=site:apnews.com+when:1d&hl=en-US&gl=US&ceid=US:en",
}

DOMESTIC_COUNT = 3
INTL_COUNT = 3

CLEAN_HTML_TAG = re.compile(r"<[^>]+>")


def clean_text(raw: str, limit: int = 80) -> str:
    """去掉 HTML 標籤、換行，並截斷成摘要長度。"""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = CLEAN_HTML_TAG.sub("", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def entry_timestamp(entry):
    """取得可比較的發布時間，抓不到就給 0（排到最後）。"""
    for key in ("published_parsed", "updated_parsed"):
        value = getattr(entry, key, None)
        if value:
            return time.mktime(value)
    return 0


def translate_to_zh(text: str) -> str:
    """把英文標題/摘要翻成繁體中文；失敗就回傳原文，不讓整個腳本掛掉。"""
    if not text:
        return text
    try:
        return GoogleTranslator(source="auto", target="zh-TW").translate(text)
    except Exception as e:
        print(f"[警告] 翻譯失敗，改用原文：{e}", file=sys.stderr)
        return text


def resolve_final_url(url: str) -> str:
    """Google News RSS 的連結是導轉連結，嘗試追蹤到真正的原始網址。"""
    try:
        resp = requests.get(url, allow_redirects=True, timeout=10)
        return resp.url
    except Exception:
        return url  # 失敗就先回傳原始（導轉）連結，至少還能點開


def fetch_domestic():
    items = []
    for source_name, url in DOMESTIC_FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[警告] 讀取 {source_name} 失敗：{e}", file=sys.stderr)
            continue

        for entry in feed.entries[:5]:  # 每個來源最多先看前5則
            items.append(
                {
                    "source": source_name,
                    "title": clean_text(entry.get("title", ""), limit=60),
                    "summary": clean_text(entry.get("summary", ""), limit=80),
                    "link": entry.get("link", "").strip(),
                    "ts": entry_timestamp(entry),
                }
            )

    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:DOMESTIC_COUNT]


def fetch_international():
    items = []
    for source_name, url in INTL_QUERIES.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[警告] 讀取 {source_name} 失敗：{e}", file=sys.stderr)
            continue

        for entry in feed.entries[:3]:
            raw_link = entry.get("link", "").strip()
            items.append(
                {
                    "source": source_name,
                    "title": clean_text(entry.get("title", ""), limit=60),
                    "summary": clean_text(entry.get("summary", ""), limit=80),
                    "link": raw_link,
                    "ts": entry_timestamp(entry),
                }
            )

    items.sort(key=lambda x: x["ts"], reverse=True)
    top = items[:INTL_COUNT]

    # 只對最後真的要用到的幾則做處理（節省時間／翻譯額度）
    for item in top:
        if item["link"]:
            item["link"] = resolve_final_url(item["link"])
        item["title"] = translate_to_zh(item["title"])
        item["summary"] = translate_to_zh(item["summary"])

    return top


def build_message(domestic_items, intl_items) -> str:
    today = datetime.now(timezone.utc).astimezone().strftime("%Y/%m/%d")
    lines = [f"📰 每日新聞摘要 {today}", ""]

    lines.append("【台灣】")
    if domestic_items:
        for i, item in enumerate(domestic_items, start=1):
            lines.append(f"{i}. {item['title']}（{item['source']}）")
            if item["summary"]:
                lines.append(f"   {item['summary']}")
            lines.append(f"   {item['link']}")
    else:
        lines.append("（今天沒抓到資料）")

    lines.append("")
    lines.append("【國際】")
    if intl_items:
        for i, item in enumerate(intl_items, start=1):
            lines.append(f"{i}. {item['title']}（{item['source']}）")
            if item["summary"]:
                lines.append(f"   {item['summary']}")
            lines.append(f"   {item['link']}")
    else:
        lines.append("（今天沒抓到資料）")

    return "\n".join(lines)


def send_to_line(message: str):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        print("[錯誤] 缺少 LINE_CHANNEL_ACCESS_TOKEN 環境變數。", file=sys.stderr)
        sys.exit(1)

    # 用 broadcast：不需要抓 userId，訊息會送給所有加你官方帳號好友的人
    # （只有你自己加了好友，所以等同私人推播）
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    # LINE 單則文字上限 5000 字，這裡的內容不會超過，若之後加更多則數要注意
    payload = {"messages": [{"type": "text", "text": message}]}

    resp = requests.post(url, json=payload, headers=headers, timeout=15)

    if resp.status_code != 200:
        print(f"[錯誤] LINE 推播失敗：{resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(1)

    print("推播成功！")


def main():
    domestic_items = fetch_domestic()
    intl_items = fetch_international()
    message = build_message(domestic_items, intl_items)
    print(message)  # 印在 GitHub Actions log 方便除錯
    send_to_line(message)


if __name__ == "__main__":
    main()
