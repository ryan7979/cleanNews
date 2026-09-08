import os
import sqlite3
import datetime
import feedparser
import google.generativeai as genai
from jinja2 import Template

# 1. 初始化 AI 與資料庫
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

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

# 2. RSS 訂閱來源設定 (這裡提供多個台灣媒體 RSS 確保能抓到資料)
RSS_SOURCES = {
    "科技新報": "https://technews.tw",
    "公視新聞": "https://pts.org.tw",
    "關鍵評論網": "https://feedburner.com"
}

AI_PROMPT = """
你是一個新聞審查員。請分析以下新聞，判斷是否包含「置入性行銷、廠商業配、公關稿、開箱體驗文」，或是由記者「張三、李四」所寫。
如果是業配或該記者的文章，請只回覆: REJECT
如果是正常且客觀的新聞，請只回覆: PASS
新聞內容如下：
"""

# 3. 抓取與 AI 過濾
print("開始抓取新聞...")
today_str = datetime.datetime.now().strftime("%Y-%m-%d")

for source_name, url in RSS_SOURCES.items():
    print(f"正在抓取: {source_name}")
    feed = feedparser.parse(url)
    
    # 防呆：確保 RSS 有抓到內容
    if not feed.entries:
        print(f"警告：無法讀取 {source_name} 的 RSS")
        continue
        
    for entry in feed.entries[:15]:  # 每次抓最新 15 則
        title = entry.title
        summary = entry.get('summary', '')[:200]
        link = entry.link
        
        # 尋找 RSS 中的圖片網址 (相容多種常見標籤)
        img_url = ""
        if 'enclosures' in entry and len(entry.enclosures) > 0:
            img_url = entry.enclosures[0].get('url', '')
        elif 'media_content' in entry and len(entry.media_content) > 0:
            img_url = entry.media_content[0].get('url', '')
            
        # 讓 AI 檢查
        try:
            response = model.generate_content(AI_PROMPT + f"標題:{title}\n摘要:{summary}")
            print(f"新聞: {title[:15]}... -> AI結果: {response.text.strip()}")
            
            if "PASS" in response.text.upper():
                cursor.execute("""
                INSERT OR IGNORE INTO filtered_news (title, summary, source, image_url, link, pub_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (title, summary, source_name, img_url, link, today_str, today_str))
        except Exception as e:
            print(f"AI 檢查失敗: {e}")

conn.commit()

# 4. 生成 HTML 網頁
os.makedirs("archive", exist_ok=True)

# 讀取 Jinja2 模板
with open("templates/index.html", "r", encoding="utf-8") as f:
    template_html = f.read()
tmpl = Template(template_html)

# 撈出資料庫裡所有存在的日期列表
cursor.execute("SELECT DISTINCT created_at FROM filtered_news ORDER BY created_at DESC")
all_dates = [row[0] for row in cursor.fetchall()] # 修正：這裡要取 row[0]

print(f"目前資料庫中擁有的日期：{all_dates}")

# 修正：即使資料庫是空的，今天也要產出一個空的網頁，防止 Git 報錯
if not all_dates:
    all_dates = [today_str]

# 針對每個日期生成專屬的 HTML
for date_str in all_dates:
    cursor.execute("SELECT title, summary, source, image_url, link, pub_date FROM filtered_news WHERE created_at = ? ORDER BY id DESC", (date_str,))
    rows = cursor.fetchall()
    news_list = [{"title": r[0], "summary": r[1], "source": r[2], "image_url": r[3], "link": r[4], "pub_date": r[5]} for r in rows]
    
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
print("網頁與資料庫更新成功！")
