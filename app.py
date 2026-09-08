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
from string import Template

warnings.filterwarnings("ignore", category=FutureWarning)
import google.generativeai as genai

# 🌟 核心修正：首先讀取外部 app.config，判定是否啟動強制抹除資料庫
config_path = "app.config"
force_reset_db = False

if os.path.exists(config_path):
    with open(config_path, "r", encoding="utf-8") as cfg_f:
        config_data = json.load(cfg_f)
    RSS_SOURCES = config_data.get("RSS_SOURCES", {})
    AI_PROMPT = config_data.get("AI_PROMPT", "")
    force_reset_db = config_data.get("FORCE_RESET_DB", False)
    print("📁 成功自建入外部 app.config 核心配置參數！")
else:
    # 保底防呆機制
    RSS_SOURCES = {"ETtoday 新聞雲": "https://ettoday.net"}
    AI_PROMPT = "分析新聞並輸出 JSON：\n{\"label\": \"葉配/網軍/正常\"}\n內容："
    print("⚠️ 警告：未找到 app.config，已自動發動內建保底參數。")

# 🌟 核心修正：依據 config 參數判定是否在雲端直接炸掉舊資料庫
if force_reset_db and os.path.exists("news.db"):
    try:
        os.remove("news.db")
        print("💥 FORCE_RESET_DB 已開啟！已強制抹除舊的 news.db 資料庫，啟動全新乾淨大抓取！")
    except Exception as e:
        print(f"抹除資料庫失敗: {e}")
elif not force_reset_db:
    print("🔒 FORCE_RESET_DB 已關閉！進入正常累積模式（僅抓取並新增未重複之最新新聞）。")

# 1. 初始化 AI 客戶端
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

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
ssl_context = ssl._create_unverified_context()

for source_name, url in RSS_SOURCES.items():
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
        
    for entry in feed.entries[:15]:
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
        
        # 🤖 呼叫 Gemini AI 進行 JSON 判讀
        ai_label = "正常"
        try:
            time.sleep(3.5) # 防止觸發免費版頻率限制
            response = model.generate_content(AI_PROMPT + f"標題:{title}\n摘要:{summary}")
            raw_text = response.text.strip()
            raw_text = re.sub(r'^```json\s*|\s*```$', '', raw_text, flags=re.MULTILINE)
            
            ai_data = json.loads(raw_text)
            ai_label = ai_data.get("label", "正常")
            if "網群" in ai_label:
                ai_label = "網軍"
        except Exception as e:
            print(f"   ⚠️ AI 判讀受限，已發動異常保底機制: {title[:12]}...")
            ai_label = "AI異常"

        # 寫入或更新
        try:
            cursor.execute("""
            INSERT INTO filtered_news (title, summary, source, image_url, link, pub_date, ai_label, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (title, summary, source_name, img_url, link, pub_date_str, ai_label, today_str))
            inserted_count += 1
        except sqlite3.IntegrityError:
            cursor.execute("""
            UPDATE filtered_news SET ai_label = ? WHERE title = ? AND ai_label = '正常'
            """, (ai_label, title))
            if cursor.rowcount > 0:
                updated_count += 1

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

for date_str in all_dates:
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
        elif label_text == "網軍":
            label_badge = '<span class="badge-label label-wj">🔴 網軍風向</span>'
        else:
            label_badge = '<span class="badge-label label-err">⚪ AI異常</span>'

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
