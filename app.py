import os
import sqlite3
import datetime
import feedparser
import re
from jinja2 import Template

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

# 2. RSS 訂閱來源設定 (這裡提供多個台灣穩定更新的媒體)
RSS_SOURCES = {
    "科技新報": "https://technews.tw",
    "公視新聞": "https://pts.org.tw",
    "關鍵評論網": "https://feedburner.com",
    "地球圖輯隊": "https://yam.travel"
}

# 3. 抓取並直接放行所有新聞
print("開始抓取新聞...")
today_str = datetime.datetime.now().strftime("%Y-%m-%d")
inserted_count = 0

for source_name, url in RSS_SOURCES.items():
    print(f"正在抓取: {source_name}")
    feed = feedparser.parse(url)
    
    if not feed.entries:
        print(f"警告：無法讀取 {source_name} 的 RSS")
        continue
        
    for entry in feed.entries[:15]:  # 每次抓各媒體最新 15 則
        title = entry.title
        summary = entry.get('summary', '')
        
        # 清除摘要中多餘的 HTML 標籤，保留前 150 字純文字
        if '<' in summary:
            summary = re.sub(r'<[^>]+>', '', summary)
        summary = summary.strip()[:150]
        
        link = entry.link
        
        # 尋找 RSS 中的圖片網址 (擴充多種媒體常用的標籤格式)
        img_url = ""
        if 'enclosures' in entry and len(entry.enclosures) > 0:
            img_url = entry.enclosures[0].get('url', '')
        elif 'media_content' in entry and len(entry.media_content) > 0:
            img_url = entry.media_content[0].get('url', '')
        elif 'links' in entry:
            for l in entry.links:
                if 'image' in l.get('type', ''):
                    img_url = l.get('href', '')
                    break
        
        # 直接放行，寫入資料庫
        try:
            cursor.execute("""
            INSERT OR IGNORE INTO filtered_news (title, summary, source, image_url, link, pub_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (title, summary, source_name, img_url, link, today_str, today_str))
            inserted_count += 1
        except Exception as e:
            print(f"寫入失敗: {e}")

conn.commit()
print(f"本次掃描結束，嘗試寫入/更新了 {inserted_count} 則新聞。")

# 4. 生成 HTML 網頁
os.makedirs("archive", exist_ok=True)

# 讀取 Jinja2 模板
with open("templates/index.html", "r", encoding="utf-8") as f:
    template_html = f.read()
tmpl = Template(template_html)

# 撈出資料庫裡所有存在的日期列表
cursor.execute("SELECT DISTINCT created_at FROM filtered_news ORDER BY created_at DESC")
all_dates = [row[0] for row in cursor.fetchall()]  # 🌟 修正：確保提取出純字串 ['2026-09-08']

print(f"【偵錯資訊】目前資料庫中擁有的日期群：{all_dates}")

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
    
    # 🌟 修正：將 Tuple 完美解包轉為 Dict 字典格式，供 HTML 網頁正確讀取
    news_list = []
    for r in rows:
        news_list.append({
            "title": r[0],
            "summary": r[1],
            "source": r[2],
            "image_url": r[3],
            "link": r[4],
            "pub_date": r[5]
        })
    
    print(f"【偵錯資訊】日期 {date_str} 成功撈出 {len(news_list)} 則新聞，準備渲染網頁...")
    rendered_html = tmpl.render(news_list=news_list, date_list=all_dates)
    
    # 寫入歷史存檔
    with open(f"archive/{date_str}.html", "w", encoding="utf-8") as f:
        f.write(rendered_html)
        
    # 如果是最新的一天，同時覆蓋 index.html 作為網站首頁
    if date_str == all_dates[0]:
        with open("index.html", "w", encoding="utf-8") as f_index:
            f_index.write(rendered_html)

# 自動清理 90 天前的舊資料
cursor.execute("DELETE FROM filtered_news WHERE date(created_at) < date('now', '-90 days')")
conn.commit()
conn.close()
print("網頁與資料庫更新成功！所有新聞已成功放行。")
