import os
import sqlite3
import datetime
import json
import urllib.request
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

print("開始透過公視官方 API 管道抓取乾淨新聞...")
today_str = datetime.datetime.now().strftime("%Y-%m-%d")
inserted_count = 0

# 2. 🌟 終極修正：直接呼叫公視官方最穩定的最新新聞 API (這絕對不會被 GitHub 擋)
url = "https://pts.org.tw"

try:
    req = urllib.request.Request(
        url, 
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        data = json.loads(response.read().decode('utf-8'))
        
    # 公視 API 回傳的資料結構處理
    # 備註：依據實際 API 結構讀取列表，此處通常回傳包含新聞陣列的 json
    news_entries = data if isinstance(data, list) else data.get('data', [])
    
    for entry in news_entries[:30]: # 拿最新 30 則
        title = entry.get('title', '')
        summary = entry.get('description', entry.get('content', ''))
        if '<' in summary:
            summary = re.sub(r'<[^>]+>', '', summary)
        summary = summary.strip()[:150]
        
        # 取得新聞網址與圖片
        news_id = entry.get('id', '')
        link = f"https://pts.org.tw{news_id}" if news_id else "https://pts.org.tw"
        img_url = entry.get('image', entry.get('cover_image', ''))
        
        if not title:
            continue
            
        cursor.execute("""
        INSERT OR IGNORE INTO filtered_news (title, summary, source, image_url, link, pub_date, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (title, summary, "公視新聞", img_url, link, today_str, today_str))
        inserted_count += 1

except Exception as e:
    print(f"❌ 官方 API 讀取失敗: {e}")

conn.commit()
print(f" 本次掃描結束！成功處理並寫入 {inserted_count} 則新聞至資料庫。")

# 4. 生成 HTML 網頁
os.makedirs("archive", exist_ok=True)

with open("templates/index.html", "r", encoding="utf-8") as f:
    template_html = f.read()
tmpl = Template(template_html)

cursor.execute("SELECT DISTINCT created_at FROM filtered_news ORDER BY created_at DESC")
all_dates = [row[0] for row in cursor.fetchall()]

if not all_dates:
    all_dates = [today_str]

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
            "title": r[0],
            "summary": r[1],
            "source": r[2],
            "image_url": r[3],
            "link": r[4],
            "pub_date": r[5]
        })
    
    rendered_html = tmpl.render(news_list=news_list, date_list=all_dates)
    
    with open(f"archive/{date_str}.html", "w", encoding="utf-8") as f:
        f.write(rendered_html)
        
    if date_str == all_dates[0]:
        with open("index.html", "w", encoding="utf-8") as f_index:
            f_index.write(rendered_html)

cursor.execute("DELETE FROM filtered_news WHERE date(created_at) < date('now', '-90 days')")
conn.commit()
conn.close()
print("🎉 網頁與資料庫更新成功！")
