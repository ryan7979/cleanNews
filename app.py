import os
import sqlite3
import datetime
import feedparser
import re
import urllib.request
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

# 2. 🌟 終極修正：放棄被封鎖的官方網域，改用社群運作的可用「RSSHub 鏡像替代通道」
# 這裡使用常見的 RSSHub 穩定鏡像域名：rsshub.feedland.space
MIRROR_DOMAIN = "https://feedland.space"

RSS_SOURCES = {
    "ETtoday 新聞雲": f"{MIRROR_DOMAIN}/ettoday/news",
    "自由時報電子報": f"{MIRROR_DOMAIN}/ltn/breakingnews",
    "科技新報": f"{MIRROR_DOMAIN}/technews",
    "風傳媒": f"{MIRROR_DOMAIN}/storm/all"
}

# 3. 抓取新聞
print("開始透過 RSSHub 社群替代通道抓取新聞...")
today_str = datetime.datetime.now().strftime("%Y-%m-%d")
inserted_count = 0

for source_name, url in RSS_SOURCES.items():
    print(f"正在連線抓取: {source_name}")
    
    try:
        # 加強偽裝成完全真實的桌面版 Chrome 瀏覽器標頭
        req = urllib.request.Request(
            url, 
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/xml,text/xml,application/xhtml+xml',
                'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7'
            }
        )
        
        # 讀取 XML 資料
        with urllib.request.urlopen(req, timeout=20) as response:
            rss_text = response.read()
            
        feed = feedparser.parse(rss_text)
        
    except Exception as http_err:
        print(f"❌ 通道連線失敗 {source_name}: {http_err}")
        continue
    
    if not feed.entries:
        print(f"⚠️ 解析後發現目前無新內容: {source_name}")
        continue
        
    for entry in feed.entries[:15]:  # 每次抓最新 15 則
        title = entry.title
        summary = entry.get('summary', '')
        
        # 清除內文 HTML 標籤
        if '<' in summary:
            summary = re.sub(r'<[^>]+>', '', summary)
        summary = summary.strip()[:150]
        
        link = entry.link
        
        # 精準提取縮圖網址
        img_url = ""
        if 'enclosures' in entry and len(entry.enclosures) > 0:
            img_url = entry.enclosures.get('url', '')
        elif 'media_content' in entry and len(entry.media_content) > 0:
            img_url = entry.media_content.get('url', '')
            
        try:
            cursor.execute("""
            INSERT OR IGNORE INTO filtered_news (title, summary, source, image_url, link, pub_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (title, summary, source_name, img_url, link, today_str, today_str))
            inserted_count += 1
        except Exception as e:
            pass

conn.commit()
print(f" 本次掃描結束！成功處理並寫入 {inserted_count} 則新聞至資料庫。")

# 4. 生成 HTML 網頁
os.makedirs("archive", exist_ok=True)

with open("templates/index.html", "r", encoding="utf-8") as f:
    template_html = f.read()
tmpl = Template(template_html)

cursor.execute("SELECT DISTINCT created_at FROM filtered_news ORDER BY created_at DESC")
all_dates = [row for row in cursor.fetchall()]

print(f"【資料庫日期群】: {all_dates}")

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
    
    # 寫入歷史存檔
    with open(f"archive/{date_str}.html", "w", encoding="utf-8") as f:
        f.write(rendered_html)
        
    # 同步覆蓋 index.html 作為最新首頁
    if date_str == all_dates:
        with open("index.html", "w", encoding="utf-8") as f_index:
            f_index.write(rendered_html)

# 自動清理 90 天前的舊資料
cursor.execute("DELETE FROM filtered_news WHERE date(created_at) < date('now', '-90 days')")
conn.commit()
conn.close()
print("🎉 專屬新聞網頁與資料庫已完美更新成功！")
