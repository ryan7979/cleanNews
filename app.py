import os
import sqlite3
import datetime
import feedparser
import re
import urllib.request
import ssl
import json
import warnings
from jinja2 import Template

# 強制隱藏 Google 官方的 Deprecated 升級警告
warnings.filterwarnings("ignore", category=FutureWarning)
import google.generativeai as genai

# 1. 初始化 AI 客戶端
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# 2. 初始化資料庫並啟動「自動補欄位防護」
conn = sqlite3.connect("news.db")
cursor = conn.cursor()

# 建立基礎資料表
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

# 自動幫舊資料庫升級補齊 reporter 與 ai_label 欄位
try:
    cursor.execute("ALTER TABLE filtered_news ADD COLUMN reporter TEXT DEFAULT '編輯台'")
    conn.commit()
except sqlite3.OperationalError:
    pass

try:
    cursor.execute("ALTER TABLE filtered_news ADD COLUMN ai_label TEXT DEFAULT '正常'")
    conn.commit()
except sqlite3.OperationalError:
    pass


# 3. 主流媒體官方原廠 RSS 網址
RSS_SOURCES = {
    "ETtoday 新聞雲": "https://feedburner.com",
    "自由時報電子報": "https://ltn.com.tw",
    "科技新報": "https://technews.tw",
    "風傳媒": "https://storm.mg"
}

AI_PROMPT = """
你是一個新聞政治與商業審查員。請分析以下新聞的標題與摘要，並輸出嚴格的 JSON 格式。

【判定定義】
1. reporter: 請找出新聞的記者姓名（如：張三），若找不到或屬於編譯/社群中心，請填「編輯台」。
2. label: 請從以下三個標籤中，精準選擇一個：
   - 「葉配」：明顯替特定廠商、建案、醫美、產品宣傳、開箱體驗、缺乏客觀新聞價值者。
   - 「網軍」：帶有強烈政治公關帶風向、刻意抹黑、刻意造神、特定派系打手、引導網民情緒、事實根據不足的政治口水文。
   - 「正常」：客觀客觀的國內外大事、科技趨勢、社會新聞、公共政策探討。

【輸出限制】
必須只輸出標準 JSON 格式，不要有任何 Markdown 的 ```json 標籤，格式如下：
{"reporter": "記者名字", "label": "葉配/網軍/正常"}

新聞內容如下：
"""

print("開始透過官方正宗源下載新聞並進行 AI 判讀...")
today_str = datetime.datetime.now().strftime("%Y-%m-%d")
inserted_count = 0
updated_count = 0
ssl_context = ssl._create_unverified_context()

for source_name, url in RSS_SOURCES.items():
    print(f"正在連線抓取官方源: {source_name}")
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        with urllib.request.urlopen(req, timeout=25, context=ssl_context) as response:
            rss_text = response.read()
        feed = feedparser.parse(rss_text)
    except Exception as http_err:
        print(f"❌ 官方源連線失敗 {source_name}: {http_err}")
        continue
    
    if not feed.entries:
        continue
        
    for entry in feed.entries[:15]:
        title = entry.title
        summary = entry.get('summary', '')
        
        # 尋找並還原被隱藏的新聞圖片網址
        img_url = ""
        if 'enclosures' in entry and len(entry.enclosures) > 0:
            img_url = entry.enclosures.get('url', '')
        elif 'media_content' in entry and len(entry.media_content) > 0:
            img_url = entry.media_content.get('url', '')
        elif 'links' in entry:
            for l in entry.links:
                if 'image' in l.get('type', ''):
                    img_url = l.get('href', '')
                    break
        
        if not img_url and '<img' in summary:
            img_match = re.search(r'src=["\'](https://[^"\']+)["\']', summary)
            if img_match:
                img_url = img_match.group(1)

        if '<' in summary:
            summary = re.sub(r'<[^>]+>', '', summary)
        summary = summary.strip()[:150]
        link = entry.link
        
        # 🤖 呼叫 Gemini AI 進行 JSON 判讀
        reporter = "編輯台"
        ai_label = "正常"
        try:
            response = model.generate_content(AI_PROMPT + f"標題:{title}\n摘要:{summary}")
            raw_text = response.text.strip()
            raw_text = re.sub(r'^```json\s*|\s*```$', '', raw_text, flags=re.MULTILINE)
            
            ai_data = json.loads(raw_text)
            reporter = ai_data.get("reporter", "編輯台")
            ai_label = ai_data.get("label", "正常")
            if "網群" in ai_label:
                ai_label = "網軍"
        except Exception as ai_err:
            # 🌟 保底機制：AI 失敗不阻擋，沿用預設值放行
            print(f"AI 判讀異常，啟動防護放行: {ai_err}")

        # 🌟 核心修正：不論如何都先嘗試寫入。如果是新新聞，直接帶有判讀標籤。
        try:
            cursor.execute("""
            INSERT INTO filtered_news (title, summary, source, image_url, link, pub_date, reporter, ai_label, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (title, summary, source_name, img_url, link, today_str, reporter, ai_label, today_str))
            inserted_count += 1
        except sqlite3.IntegrityError:
            # 🌟 核心修正：如果是舊新聞（已存在），強迫更新其 AI 標籤與記者欄位，防止舊資料沒被判讀到
            cursor.execute("""
            UPDATE filtered_news 
            SET reporter = ?, ai_label = ? 
            WHERE title = ? AND (reporter = '編輯台' AND ai_label = '正常')
            """, (reporter, ai_label, title))
            if cursor.rowcount > 0:
                updated_count += 1

conn.commit()
print(f"掃描結束！新增了 {inserted_count} 則新聞，回頭幫 {updated_count} 則舊新聞補上了 AI 標籤。")

# 4. 生成 HTML 網頁
os.makedirs("archive", exist_ok=True)

with open("templates/index.html", "r", encoding="utf-8") as f:
    template_html = f.read()
tmpl = Template(template_html)

cursor.execute("SELECT DISTINCT created_at FROM filtered_news ORDER BY created_at DESC")
all_dates = [row[0] for row in cursor.fetchall()]  # 🌟 修正：確保提取出純字串陣列

print(f"【資料庫歷史日期群】: {all_dates}")

if not all_dates:
    all_dates = [today_str]

for date_str in all_dates:
    cursor.execute("""
        SELECT title, summary, source, image_url, link, pub_date, reporter, ai_label 
        FROM filtered_news 
        WHERE created_at = ? 
        ORDER BY id DESC
    """, (date_str,))
    rows = cursor.fetchall()
    
    news_list = []
    for r in rows:
        news_list.append({
            "title": r[0], "summary": r[1], "source": r[2], "image_url": r[3],
            "link": r[4], "pub_date": r[5], "reporter": r[6], "ai_label": r[7]
        })
    
    rendered_html = tmpl.render(news_list=news_list, date_list=all_dates)
    with open(f"archive/{date_str}.html", "w", encoding="utf-8") as f:
        f.write(rendered_html)
        
    if date_str == today_str:
        with open("index.html", "w", encoding="utf-8") as f_index:
            f_index.write(rendered_html)

cursor.execute("DELETE FROM filtered_news WHERE date(created_at) < date('now', '-90 days')")
conn.commit()
conn.close()
print("🎉 網頁與資料庫更新成功！")
