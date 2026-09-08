import os
import sqlite3
import datetime
import feedparser
import re
from jinja2 import Template
from playwright.sync_api import sync_playwright

# 1. 初始化資料庫
conn = sqlite3.connect("news.db")
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

# 2. RSS 訂閱來源設定
RSS_SOURCES = {
    "科技新報": "https://technews.tw",
    "公視新聞": "https://pts.org.tw",
    "關鍵評論網": "https://feedburner.com",
    "地球圖輯隊": "https://yam.travel"
}

# 3. 使用 Playwright 模擬真實瀏覽器抓取
print("啟動真實瀏覽器引擎抓取新聞...")
today_str = datetime.datetime.now().strftime("%Y-%m-%d")
inserted_count = 0

with sync_playwright() as p:
    # 啟動背景瀏覽器，並設定與正常電腦一模一樣的解析度與外觀
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
    )
    page = context.new_page()

    for source_name, url in RSS_SOURCES.items():
        print(f"正在透過瀏覽器讀取: {source_name}")
        try:
            # 前往網址並等待網路完全放行
            page.goto(url, wait_until="networkidle", timeout=30000)
            rss_content = page.content()
            
            # 使用 feedparser 解析真實瀏覽器帶回來的 XML 文字
            feed = feedparser.parse(rss_content)
            
            if not feed.entries:
                print(f"⚠️ 瀏覽器抓取成功，但該媒體目前無新文章: {source_name}")
                continue
                
            for entry in feed.entries[:15]:
                title = entry.title
                summary = entry.get('summary', '')
                
                if '<' in summary:
                    summary = re.sub(r'<[^>]+>', '', summary)
                summary = summary.strip()[:150]
                
                link = entry.link
                
                # 抓取圖片
                img_url = ""
                if 'enclosures' in entry and len(entry.enclosures) > 0:
                    img_url = entry.enclosures.get('url', '')
                elif 'media_content' in entry and len(entry.media_content) > 0:
                    img_url = entry.media_content.get('url', '')
                
                cursor.execute("""
                INSERT OR IGNORE INTO filtered_news (title, summary, source, image_url, link, pub_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (title, summary, source_name, img_url, link, today_str, today_str))
                inserted_count += 1
                
        except Exception as e:
            print(f"❌ 瀏覽器模擬失敗 {source_name}: {e}")
            
    browser.close()

conn.commit()
print(f"本次掃描結束，嘗試寫入/更新了 {inserted_count} 則新聞。")

# 4. 生成 HTML 網頁
os.makedirs("archive", exist_ok=True)

with open("templates/index.html", "r", encoding="utf-8") as f:
    template_html = f.read()
tmpl = Template(template_html)

cursor.execute("SELECT DISTINCT created_at FROM filtered_news ORDER BY created_at DESC")
all_dates = [row for row in cursor.fetchall()]

if not all_dates:
    all_dates = [today_str]

# 針對每個日期生成專屬的 HTML
for date_str in all_dates:
    cursor.execute("""
        SELECT title, summary, source, image_url, link, pub_date 
        FROM filtered_news 
        WHERE created_at = ? 
        ORDER BY id DESC
    """, (date_str,))
    rows = cursor.fetchall()
    
    news_list = []
    for r in rows:
        news_list.append({
            "title": r,
            "summary": r,
            "source": r,
            "image_url": r,
            "link": r,
            "pub_date": r
        })
    
    rendered_html = tmpl.render(news_list=news_list, date_list=all_dates)
    
    with open(f"archive/{date_str}.html", "w", encoding="utf-8") as f:
        f.write(rendered_html)
        
    if date_str == all_dates:
        with open("index.html", "w", encoding="utf-8") as f_index:
            f_index.write(rendered_html)

# 自動清理 90 天前的舊資料
cursor.execute("DELETE FROM filtered_news WHERE date(created_at) < date('now', '-90 days')")
conn.commit()
conn.close()
print("網頁與資料庫更新成功！")
