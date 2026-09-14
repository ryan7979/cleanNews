import os
import sqlite3
import datetime
import feedparser
import re
import urllib.request
import ssl
import json
import warnings
import time
import sys
from email.utils import parsedate_to_datetime
from google import genai
from google.genai import types
from string import Template

warnings.filterwarnings("ignore", category=FutureWarning)

APP_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_exe.log")


class Tee:
    def __init__(self, console, log_file):
        self.console = console
        self.log_file = log_file

    def write(self, message):
        self.console.write(message)
        self.log_file.write(message)
        self.log_file.flush()

    def flush(self):
        self.console.flush()
        self.log_file.flush()


app_log_file = open(APP_LOG, "w", encoding="utf-8", buffering=1)
sys.stdout = Tee(sys.__stdout__, app_log_file)
sys.stderr = Tee(sys.__stderr__, app_log_file)
print(f"Application execution started: {datetime.datetime.now().isoformat()}")

# 讀取外部 app.config；設定缺失或格式錯誤時直接中止。
config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.config")
try:
    with open(config_path, "r", encoding="utf-8") as cfg_f:
        config_data = json.load(cfg_f)
    if not isinstance(config_data, dict):
        raise ValueError("app.config 必須是 JSON object")

    RSS_SOURCES = config_data.get("RSS_SOURCES", {})
    AI_PROMPT = config_data.get("AI_PROMPT", "")
    AI_API_URL = config_data.get("AI_API_URL", "https://generativelanguage.googleapis.com")
    AI_MODEL = config_data.get("AI_MODEL", "gemini-2.5-flash")
    AI_TIMEOUT_SECONDS = config_data.get("AI_TIMEOUT_SECONDS", 60)
    AI_MAX_RETRIES = config_data.get("AI_MAX_RETRIES", 2)
    AI_MAX_TOKENS = config_data.get("AI_MAX_TOKENS", 2048)
    MAX_NEWS_TOTAL = config_data.get("MAX_NEWS_TOTAL", 15)
    force_reset_db = config_data.get("FORCE_RESET_DB", False)
    print("📁 成功自建入外部 app.config 核心配置參數！")
except Exception as error:
    print(f"[ERROR] 外部 app.config 讀取失敗: {error}", file=sys.stderr)
    raise

# 🌟 核心修正：依據 config 參數判定是否在雲端直接炸掉舊資料庫
if force_reset_db and os.path.exists("news.db"):
    try:
        os.remove("news.db")
        print("💥 FORCE_RESET_DB 已開啟！已強制抹除舊的 news.db 資料庫，啟動全新乾淨大抓取！")
    except Exception as e:
        print(f"抹除資料庫失敗: {e}")
elif not force_reset_db:
    print("🔒 FORCE_RESET_DB 已關閉！進入正常累積模式（僅抓取並新增未重複之最新新聞）。")

# 1. 初始化 Gemini AI 設定
local_config_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "app.local.config"
)
GEMINI_API_KEY = ""
if os.path.exists(local_config_path):
    with open(local_config_path, "r", encoding="utf-8") as local_cfg_f:
        local_config_data = json.load(local_cfg_f)
    GEMINI_API_KEY = local_config_data.get("GEMINI_API_KEY", "").strip()

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
AI_OUTPUT_LOG = APP_LOG
with open(AI_OUTPUT_LOG, "a", encoding="utf-8") as log_file:
    log_file.write("\nAI API call log begins\n")


def request_ai_content(prompt, source_name):
    with open(AI_OUTPUT_LOG, "a", encoding="utf-8") as log_file:
        log_file.write(
            f"\n\n===== RSS來源: {source_name} =====\n"
            f"[INPUT]\n{prompt}\n"
        )

    if not ai_client:
        error_message = "GEMINI_API_KEY 未設定"
        with open(AI_OUTPUT_LOG, "a", encoding="utf-8") as log_file:
            log_file.write(f"[ERROR]\n{error_message}\n===== END =====\n")
        raise RuntimeError(error_message)

    for attempt in range(AI_MAX_RETRIES + 1):
        try:
            chat = ai_client.chats.create(
                model=AI_MODEL,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=AI_MAX_TOKENS,
                    response_mime_type="application/json",
                ),
            )
            response = chat.send_message(message=prompt)
            output = (response.text or "").strip()
            with open(AI_OUTPUT_LOG, "a", encoding="utf-8") as log_file:
                log_file.write(f"[AI OUTPUT]\n{output}\n===== END =====\n")
            return output
        except Exception as error:
            error_message = str(error)
            with open(AI_OUTPUT_LOG, "a", encoding="utf-8") as log_file:
                log_file.write(
                    f"[ERROR - attempt {attempt + 1}]\n"
                    f"{error_message}\n"
                )
            error_text = str(error).lower()
            transient_error = (
                "429" in error_text
                or "500" in error_text
                or "502" in error_text
                or "503" in error_text
                or "504" in error_text
                or "timeout" in error_text
                or "unavailable" in error_text
                or "resource exhausted" in error_text
                or "rate limit" in error_text
            )
            if not transient_error or attempt >= AI_MAX_RETRIES:
                raise

            delay = 2 ** attempt
            print(f"   -> Gemini API 暫時無法服務，{delay} 秒後重試...", flush=True)
            time.sleep(delay)

    error_message = "Gemini API request failed"
    with open(AI_OUTPUT_LOG, "a", encoding="utf-8") as log_file:
        log_file.write(f"{error_message}\n===== END =====\n")
    raise RuntimeError(error_message)


def parse_news_date(value):
    try:
        parsed_date = parsedate_to_datetime(value)
        if parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(tzinfo=datetime.timezone.utc)
        return parsed_date
    except (TypeError, ValueError, OverflowError):
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

# 2. 初始化資料庫（若被抹除則會重新建立空白檔案）
conn = sqlite3.connect("news.db")
conn.row_factory = sqlite3.Row  
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS filtered_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT UNIQUE,
    summary TEXT,
    source TEXT,
    image_url TEXT,
    link TEXT,
    pub_date TEXT,
    created_at TEXT
)
""")
conn.commit()

try:
    cursor.execute("ALTER TABLE filtered_news ADD COLUMN ai_label TEXT DEFAULT '正常'")
    conn.commit()
except sqlite3.OperationalError:
    pass

print("開始下載新聞源並執行深度圖片正則提取...")
today_str = datetime.datetime.now().strftime("%Y-%m-%d")
inserted_count = 0
updated_count = 0
processed_news_count = 0
ssl_context = ssl._create_unverified_context()

for source_name, url in RSS_SOURCES.items():
    if processed_news_count >= MAX_NEWS_TOTAL:
        break
    print(f"正在連線抓取: {source_name}")
    try:
        req = urllib.request.Request(
            url, 
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/xml,text/xml,application/xhtml+xml,text/html;q=0.9'
            }
        )
        with urllib.request.urlopen(req, timeout=25, context=ssl_context) as response:
            rss_bytes = response.read()
        
        rss_text = rss_bytes.decode('utf-8', errors='ignore')
        feed = feedparser.parse(rss_text)
        
    except Exception as http_err:
        print(f"❌ 網路連線失敗 {source_name}: {http_err}")
        continue
    
    print(f"   -> 成功下載！該媒體當前 RSS 內包含 {len(feed.entries)} 則新聞。")
    if not feed.entries:
        continue
        
    pending_news = []
    remaining_news = MAX_NEWS_TOTAL - processed_news_count
    for entry in feed.entries[:min(15, remaining_news)]:
        title = entry.title
        raw_description = entry.get('summary', entry.get('description', ''))
        
        # 精準匹配標準 RSS 中的圖片階層格式
        img_url = ""
        if 'image' in entry:
            img_data = entry.get('image', '')
            if isinstance(img_data, dict) and 'href' in img_data:
                img_url = img_data['href']
            elif isinstance(img_data, str):
                img_url = img_data
        
        if not img_url and 'enclosures' in entry and len(entry.enclosures) > 0:
            img_url = entry.enclosures.get('url', '')
        elif not img_url and 'media_content' in entry and len(entry.media_content) > 0:
            img_url = entry.media_content.get('url', '')
        
        # 保底正則：深入內文描述抓取 <img> 標籤
        if not img_url and '<img' in raw_description:
            img_match = re.search(r'src=["\'](https?://[^"\']+\.(?:jpg|jpeg|png|gif|webp|JPG))["\']', raw_description, re.IGNORECASE)
            if img_match:
                img_url = img_match.group(1)

        # 濾除新聞摘要中的所有 HTML 標籤
        summary = raw_description
        if '<' in summary:
            summary = re.sub(r'<[^>]+>', '', summary)
        summary = summary.strip()[:150]
        
        link = entry.link
        pub_date_str = entry.get('published', entry.get('pubDate', today_str))

        cursor.execute("SELECT 1 FROM filtered_news WHERE link = ? LIMIT 1", (link,))
        if cursor.fetchone():
            print(f"   -> URL 已存在，跳過新聞: {title[:30]}...")
            continue

        pending_news.append({
            "title": title,
            "summary": summary,
            "source": source_name,
            "image_url": img_url,
            "link": link,
            "pub_date": pub_date_str,
        })

    if not pending_news:
        continue

    # 每個來源只呼叫一次 Gemini，避免每篇新聞各自發送請求。
    labels = ["AI異常"] * len(pending_news)
    try:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY 未設定")

        batch_items = [
            {"index": index, "title": news["title"], "summary": news["summary"]}
            for index, news in enumerate(pending_news)
        ]
        batch_prompt = (
            f"{AI_PROMPT}\n"
            "以下是多則新聞。請逐則分析，並只輸出 JSON 陣列；index 必須對應輸入順序，"
            "label 只能是「葉配」、「網軍」、「垃圾新聞」或「正常」。垃圾新聞是轉載網路網紅、名嘴的個人意見，缺乏獨立採訪或實質新聞資訊。格式："
            '[{"index": 0, "label": "正常"}]\n'
            f"新聞清單：{json.dumps(batch_items, ensure_ascii=False)}"
        )
        raw_text = request_ai_content(batch_prompt, source_name)

        print(f"   -> Gemini 批次回應: {raw_text[:120]}...")

        json_match = re.search(r"\[.*\]", raw_text, flags=re.DOTALL)
        if not json_match:
            raise ValueError(f"Gemini 批次回應未包含 JSON array: {raw_text[:120]!r}")

        ai_results = json.loads(json_match.group(0))
        for result in ai_results:
            index = result.get("index")
            label = result.get("label", "AI異常")
            if isinstance(index, int) and 0 <= index < len(labels):
                if "網群" in label:
                    label = "網軍"
                elif "垃圾" in label:
                    label = "垃圾新聞"
                elif label not in {"葉配", "網軍", "正常"}:
                    label = "AI異常"
                labels[index] = label
    except Exception as e:
        print(f"   ⚠️ Gemini 批次判讀受限: {e}...")

    for news, ai_label in zip(pending_news, labels):
        # 寫入或更新
        try:
            cursor.execute("""
            INSERT INTO filtered_news (title, summary, source, image_url, link, pub_date, ai_label, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (news["title"], news["summary"], news["source"], news["image_url"], news["link"], news["pub_date"], ai_label, today_str))
            inserted_count += 1
        except sqlite3.IntegrityError:
            cursor.execute("""
            UPDATE filtered_news SET ai_label = ? WHERE title = ? AND ai_label = '正常'
            """, (ai_label, news["title"]))
            if cursor.rowcount > 0:
                updated_count += 1

    processed_news_count += len(pending_news)

conn.commit()
print(f" Bars 📊 掃描結束！本次成功「全新寫入」 {inserted_count} 則新聞，「更新標籤」 {updated_count} 則舊新聞。")

# 4. 讀取樣式模板並渲染出全新網頁
with open("template.html", "r", encoding="utf-8") as f:
    template_content = f.read()
html_template = Template(template_content)

cursor.execute("SELECT DISTINCT created_at FROM filtered_news ORDER BY created_at DESC")
all_dates = [row['created_at'] for row in cursor.fetchall()]

if not all_dates:
    all_dates = [today_str]

date_buttons = ""
for d in all_dates:
    date_buttons += f'<a href="/cleanNews/archive/{d}.html" class="btn-date">{d}</a>'

source_tags = '<button type="button" class="btn-tag active" data-filter="all">全部</button>'
for source_name in RSS_SOURCES:
    source_tags += f'<button type="button" class="btn-tag" data-filter="{source_name}">{source_name}</button>'

for date_str in all_dates:
    cursor.execute("""
        SELECT title, summary, source, image_url, link, pub_date, ai_label 
        FROM filtered_news 
        WHERE created_at = ? 
    """, (date_str,))
    news_rows = sorted(
        cursor.fetchall(),
        key=lambda row: parse_news_date(row["pub_date"]),
        reverse=True,
    )
    
    cards_html = ""
    for r in news_rows:
        img_tag = ""
        if r["image_url"] and "http" in r["image_url"]:
            img_tag = f"""
            <div class="img-container">
                <img src="{r['image_url']}" class="card-img" alt="新聞圖片" onerror="this.parentNode.style.display='none'">
            </div>
            """

        label_text = r["ai_label"]
        if label_text == "正常":
            label_badge = '<span class="badge-label label-normal">🟢 正常新聞</span>'
        elif label_text == "葉配":
            label_badge = '<span class="badge-label label-yp">🟡 廠商業配</span>'
        elif label_text == "網軍":
            label_badge = '<span class="badge-label label-wj">🔴 網軍風向</span>'
        elif label_text == "垃圾新聞":
            label_badge = '<span class="badge-label label-trash">🗑️ 垃圾新聞</span>'
        else:
            label_badge = '<span class="badge-label label-err">⚪ AI異常</span>'

        cards_html += f"""
            <div class="card" data-source="{r['source']}">
                <div>
                    {img_tag}
                    <div class="meta-row">
                        <span class="badge-source">{r['source']}</span>
                        {label_badge}
                    </div>
                    <div class="card-body">
                        <h2 class="card-title">
                            <a href="{r['link']}" target="_blank">{r['title']}</a>
                        </h2>
                        <p class="card-text">{r['summary']}</p>
                    </div>
                </div>
                <div class="card-footer">
                    <span class="news-date">🕒 {r['pub_date']}</span>
                    <a href="{r['link']}" target="_blank" class="btn-link">
                        前往原文 <span class="arrow">→</span>
                    </a>
                </div>
            </div>
        """

    if not cards_html:
        cards_html = '<div class="no-data"><p>今日尚無過濾完畢的新聞資料。</p></div>'

    full_webpage = html_template.substitute(
        date_buttons=date_buttons,
        source_tags=source_tags,
        news_cards=cards_html
    )

    with open("index.html" if date_str == today_str else f"archive/{date_str}.html", "w", encoding="utf-8") as f_out:
        f_out.write(full_webpage)
    
    if date_str == today_str:
        os.makedirs("archive", exist_ok=True)
        with open(f"archive/{date_str}.html", "w", encoding="utf-8") as f_arch:
            f_arch.write(full_webpage)

cursor.execute("DELETE FROM filtered_news WHERE date(created_at) < date('now', '-90 days')")
conn.commit()
conn.close()
print("🎉 網頁與資料庫更新成功！")
