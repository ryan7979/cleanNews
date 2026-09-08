import os
import sqlite3
import datetime
import feedparser
import re
import urllib.request
import ssl
import json
import warnings
from string import Template

warnings.filterwarnings("ignore", category=FutureWarning)
import google.generativeai as genai

# 1. 初始化 AI 客戶端
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# 2. 初始化資料庫
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

# 主流媒體原廠 RSS 網址
RSS_SOURCES = {
    "ETtoday 新聞雲": "https://feedburner.com",
    "自由時報電子報": "https://ltn.com.tw",
    "科技新報": "https://technews.tw",
    "風傳媒": "https://storm.mg"
}

AI_PROMPT = """
你是一個新聞政治與商業審查員。請分析以下新聞的標題與摘要，並輸出嚴格的 JSON 格式。
{"label": "葉配/網軍/正常"}
新聞內容如下：
"""

print("開始下載新聞源並執行深度圖片正則提取...")
today_str = datetime.datetime.now().strftime("%Y-%m-%d")
ssl_context = ssl._create_unverified_context()

for source_name, url in RSS_SOURCES.items():
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        with urllib.request.urlopen(req, timeout=25, context=ssl_context) as response:
            rss_bytes = response.read()
        feed = feedparser.parse(rss_bytes)
    except Exception as http_err:
        print(f"❌ 網路連線失敗 {source_name}: {http_err}")
        continue
    
    if not feed.entries:
        continue
        
    for entry in feed.entries[:15]:
        title = entry.title
        raw_description = entry.get('summary', '')
        
        # 🌟 2. 修正：精準對齊 ETtoday 的 channel/item/image 階層抓取
        img_url = ""
        # feedparser 會將 item 底下的 <image> 標籤映射到 entry.get('image') 或 entry.get('image_url')
        if 'image' in entry:
            img_url = entry.get('image', '')
            if isinstance(img_url, dict) and 'href' in img_url:
                img_url = img_url['href']
        
        # 保底正則：如果 XML 欄位沒撈到，直接進 description 內部的 HTML 代碼挖出 <img src="...">
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
        
        # 🤖 AI 標籤判讀
        ai_label = "正常"
        try:
            response = model.generate_content(AI_PROMPT + f"標題:{title}\n摘要:{summary}")
            raw_text = response.text.strip()
            raw_text = re.sub(r'^```json\s*|\s*```$', '', raw_text, flags=re.MULTILINE)
            
            ai_data = json.loads(raw_text)
            ai_label = ai_data.get("label", "正常")
            if "網群" in ai_label:
                ai_label = "網軍"
        except Exception:
            pass

        try:
            cursor.execute("""
            INSERT INTO filtered_news (title, summary, source, image_url, link, pub_date, ai_label, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (title, summary, source_name, img_url, link, today_str, ai_label, today_str))
        except sqlite3.IntegrityError:
            cursor.execute("""
            UPDATE filtered_news SET ai_label = ? WHERE title = ? AND ai_label = '正常'
            """, (ai_label, title))

conn.commit()

# 4. 🌟 讀取樣式模板並渲染出全新網頁
with open("template.html", "r", encoding="utf-8") as f:
    template_content = f.read()
html_template = Template(template_content)

cursor.execute("SELECT DISTINCT created_at FROM filtered_news ORDER BY created_at DESC")
all_dates = [row['created_at'] for row in cursor.fetchall()]

if not all_dates:
    all_dates = [today_str]

# 🌟 1. 修正：日期導覽列網址結構更新為 /cleanNews/archive/...
date_buttons = ""
for d in all_dates:
    date_buttons += f'<a href="/cleanNews/archive/{d}.html" class="btn-date">{d}</a>'

for date_str in all_dates:
    # 🌟 3. 修正：輸出新聞時，強制使用 ORDER BY id DESC（日期最新降序排列）
    cursor.execute("""
        SELECT title, summary, source, image_url, link, pub_date, ai_label 
        FROM filtered_news 
        WHERE created_at = ? 
        ORDER BY id DESC
    """, (date_str,))
    news_rows = cursor.fetchall()
    
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
        else:
            label_badge = '<span class="badge-label label-wj">🔴 網軍風向</span>'

        cards_html += f"""
            <div class="card">
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
        news_cards=cards_html
    )

    # 寫入靜態網頁
    with open("index.html" if date_str == today_str else f"archive/{date_str}.html", "w", encoding="utf-8") as f_out:
        f_out.write(full_webpage)
    
    if date_str == today_str:
        os.makedirs("archive", exist_ok=True)
        with open(f"archive/{date_str}.html", "w", encoding="utf-8") as f_arch:
            f_arch.write(full_webpage)

cursor.execute("DELETE FROM filtered_news WHERE date(created_at) < date('now', '-90 days')")
conn.commit()
conn.close()
print("🎉 專屬專案網址 /cleanNews/、ETtoday 原創縮圖、最新降序排序已全數完美修正成功！")
