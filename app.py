import os
import sqlite3
import datetime
import feedparser
import re
import urllib.request
import ssl
import json
import warnings
from string import Template  # 🌟 採用內建 Template，不依賴任何外部套件

# 強制隱藏 Google 官方的 Deprecated 升級警告，保持排程日誌乾淨
warnings.filterwarnings("ignore", category=FutureWarning)
import google.generativeai as genai

# 1. 初始化 AI 客戶端
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# 2. 初始化資料庫並啟動「自動補欄位防護」
conn = sqlite3.connect("news.db")
conn.row_factory = sqlite3.Row  # 依欄位名稱返回資料，確保資料組裝對齊不混亂
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
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/xml,text/xml,application/xhtml+xml,text/html;q=0.9',
                'Accept-Language': 'zh-TW,zh;q=0.9'
            }
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
            img_url = entry.enclosures[0].get('url', '')
        elif 'media_content' in entry and len(entry.media_content) > 0:
            img_url = entry.media_content[0].get('url', '')
        elif 'image' in entry and len(entry.image) > 0:
            img_url = entry.image[0].get('url', '')
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
            pass

        try:
            cursor.execute("""
            INSERT INTO filtered_news (title, summary, source, image_url, link, pub_date, reporter, ai_label, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (title, summary, source_name, img_url, link, today_str, reporter, ai_label, today_str))
            inserted_count += 1
        except sqlite3.IntegrityError:
            cursor.execute("""
            UPDATE filtered_news 
            SET reporter = ?, ai_label = ? 
            WHERE title = ? AND (reporter = '編輯台' AND ai_label = '正常')
            """, (reporter, ai_label, title))
            if cursor.rowcount > 0:
                updated_count += 1

conn.commit()
print(f"掃描結束！新增了 {inserted_count} 則新聞，回頭幫 {updated_count} 則舊新聞補上了 AI 標籤。")

# 4. 🌟 核心渲染：載入外部 template.html 並生成獨立的原生 CSS 網頁
# 讀取剛剛建立的純樣式模板
with open("template.html", "r", encoding="utf-8") as f:
    template_content = f.read()
html_template = Template(template_content)

# 撈出日期清單
cursor.execute("SELECT DISTINCT created_at FROM filtered_news ORDER BY created_at DESC")
all_dates = [row['created_at'] for row in cursor.fetchall()]

if not all_dates:
    all_dates = [today_str]

# 拼接歷史日期按鈕
date_buttons = ""
for d in all_dates:
    date_buttons += f'<a href="/archive/{d}.html" class="btn-date">{d}</a>'

# 針對每個日期組裝卡片與輸出網頁
for date_str in all_dates:
    cursor.execute("""
        SELECT title, summary, source, image_url, link, pub_date, reporter, ai_label 
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
                        <div class="meta-left">
                            <span class="badge-source">{r['source']}</span>
                            <span class="badge-reporter">✍️ {r['reporter']}</span>
                        </div>
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

    # 使用內建 string.Template 進行安全替換，完全不受外部 CDN 與雜訊干擾
    full_webpage = html_template.substitute(
        date_buttons=date_buttons,
        news_cards=cards_html
    )

    # 建立目錄並寫入
    os.makedirs("archive", exist_ok=True)
    with open(f"archive/{date_str}.html", "w", encoding="utf-8") as f:
        f.write(full_webpage)
        
    if date_str == today_str:
        with open("index.html", "w", encoding="utf-8") as f_index:
            f_index.write(full_webpage)

# 自動清理 90 天前的舊資料
cursor.execute("DELETE FROM filtered_news WHERE date(created_at) < date('now', '-90 days')")
conn.commit()
conn.close()
print("🎉 程式與網頁分離式架構更新成功！0 外部 CDN 依賴新聞牆已上線。")
