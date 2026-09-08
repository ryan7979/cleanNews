import os
import sqlite3
import datetime
import feedparser
import re
import urllib.request
import ssl
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

# 2. 🌟 完美導入您提供的主流媒體官方原廠 RSS 網址
RSS_SOURCES = {
    "ETtoday 新聞雲": "https://feeds.feedburner.com/ettoday/realtime",
    "自由時報電子報": "https://news.ltn.com.tw/rss/all.xml",
    "科技新報": "https://technews.tw/tn-rss/",
    "風傳媒": "https://www.storm.mg/api/getRss/channel_id/2?path=https%3A%2F%2Fwww.storm.mg%2Farticle"
}

print("開始透過官方正宗源下載新聞...")
today_str = datetime.datetime.now().strftime("%Y-%m-%d")
inserted_count = 0

# 🌟 雲端反阻擋特殊設定：忽略 SSL 憑證檢查（防止國外主機因台灣部分媒體憑證過期而拒絕連線）
ssl_context = ssl._create_unverified_context()

for source_name, url in RSS_SOURCES.items():
    print(f"正在連線抓取官方源: {source_name}")
    
    try:
        # 強力偽裝成極度真實的 Windows 桌面版 Chrome 瀏覽器標頭
        req = urllib.request.Request(
            url, 
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                'Accept': 'application/xml,text/xml,application/xhtml+xml,text/html;q=0.9',
                'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7'
            }
        )
        
        # 加上 context 與增加到 25 秒超時等待，避免高流量時卡死
        with urllib.request.urlopen(req, timeout=25, context=ssl_context) as response:
            rss_text = response.read()
            
        feed = feedparser.parse(rss_text)
        
    except Exception as http_err:
        print(f"❌ 官方源連線被阻擋或超時 {source_name}: {http_err}")
        continue
    
    if not feed.entries:
        print(f"⚠️ 解析成功但目前無新內容: {source_name}")
        continue
        
    for entry in feed.entries[:15]:  # 每次抓最新 15 則
        title = entry.title
        summary = entry.get('summary', '')
        
        # 濾除新聞摘要中的亂碼與 HTML 標籤
        if '<' in summary:
            summary = re.sub(r'<[^>]+>', '', summary)
        summary = summary.strip()[:150]
        
        link = entry.link
        
        # 精準抓取各媒體原廠 RSS 中的封面縮圖網址
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
        
        try:
            cursor.execute("""
            INSERT OR IGNORE INTO filtered_news (title, summary, source, image_url, link, pub_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (title, summary, source_name, img_url, link, today_str, today_str))
            inserted_count += 1
        except Exception as e:
            pass

conn.commit()
print(f" 本次掃描結束！成功處理並寫入 {inserted_count} 則官方新聞至資料庫。")

# 4. 生成 HTML 網頁
os.makedirs("archive", exist_ok=True)

with open("templates/index.html", "r", encoding="utf-8") as f:
    template_html = f.read()
tmpl = Template(template_html)

cursor.execute("SELECT DISTINCT created_at FROM filtered_news ORDER BY created_at DESC")
all_dates = [row[0] for row in cursor.fetchall()]  # 🌟 這裡修正解包，確保傳遞純字串陣列給 Jinja2

print(f"【目前資料庫擁有的歷史日期群】: {all_dates}")

if not all_dates:
    all_dates = [today_str]

# 針對每個日期生成專屬的 HTML 頁面
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
    
    # 寫入歷史分類檔案 (例如 archive/2026-09-08.html)
    with open(f"archive/{date_str}.html", "w", encoding="utf-8") as f:
        f.write(rendered_html)
        
    # 最新一天同時強制覆蓋 index.html 作為預設首頁
    if date_str == all_dates[0]:
        with open("index.html", "w", encoding="utf-8") as f_index:
            f_index.write(rendered_html)

# 自動清理 90 天前的舊資料
cursor.execute("DELETE FROM filtered_news WHERE date(created_at) < date('now', '-90 days')")
conn.commit()
conn.close()
print("🎉 專屬新聞網頁與資料庫已完美更新成功！")
