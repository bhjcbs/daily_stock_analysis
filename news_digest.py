# -*- coding: utf-8 -*-
import os
import sys
import asyncio
import logging
import smtplib
import subprocess
import inspect
import traceback
import json
import ssl
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
import pytz

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== 0. 自动依赖检查 (增加 feedparser) ====================
def install_package(package):
    try:
        logger.info(f"🔧 自动安装依赖: {package}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
    except Exception as e:
        logger.warning(f"❌ 安装 {package} 失败: {e}")

# 1. RSS 解析库 (最稳的兜底)
try:
    import feedparser
except ImportError:
    install_package("feedparser")
    import feedparser

# 2. 搜索库 (新版)
try:
    from duckduckgo_search import DDGS
except ImportError:
    install_package("duckduckgo-search>=6.0.0")
    from duckduckgo_search import DDGS

# 3. Gemini SDK
try:
    import google.generativeai as genai
except ImportError:
    install_package("google-generativeai")
    import google.generativeai as genai

# ==================== 1. RSS 硬兜底 (杀手锏) ====================
# 当搜索挂掉时，直接读取这些官方源，100% 可用
RSS_SOURCES = {
    "Sina_Global": "https://rss.sina.com.cn/news/world/focus15.xml",
    "Sina_Finance": "https://rss.sina.com.cn/roll/finance/hot_roll.xml",
    "EastMoney": "http://www.eastmoney.com/rss/msg.xml",
    "WallstreetCN": "https://wallstreetcn.com/rss/live.xml" 
}

def fetch_rss_news():
    """读取 RSS 源获取最新财经新闻 (不受反爬虫影响)"""
    logger.info("📡 [RSS] 启动硬兜底模式，正在读取官方新闻源...")
    combined_text = ""
    
    # 忽略 SSL 验证，防止 Actions 环境下的证书问题
    if hasattr(ssl, '_create_unverified_context'):
        ssl._create_default_https_context = ssl._create_unverified_context

    for name, url in RSS_SOURCES.items():
        try:
            feed = feedparser.parse(url)
            logger.info(f"   - 读取 {name}: 获取到 {len(feed.entries)} 条")
            
            # 只取前 10 条，避免 token 爆炸
            for entry in feed.entries[:10]:
                title = entry.get('title', '')
                summary = entry.get('summary', entry.get('description', ''))
                # 清洗 HTML 标签
                summary = summary.replace('<p>', '').replace('</p>', '').replace('<br>', '')
                combined_text += f"Source: {name} (RSS)\nTitle: {title}\nSummary: {summary[:200]}\n---\n"
        except Exception as e:
            logger.warning(f"   - 读取 {name} 失败: {e}")
            
    return combined_text

# ==================== 2. 独立 Gemini 客户端 ====================
class DirectGeminiClient:
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("环境变量 GEMINI_API_KEY 未配置")
        
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel('gemini-1.5-flash')
        logger.info("💎 [独立模式] Gemini 客户端就绪")

    async def chat(self, prompt):
        try:
            response = self.model.generate_content(prompt)
            return response.text
        except Exception as e:
            logger.error(f"❌ Gemini API 调用失败: {e}")
            return None

# ==================== 3. 邮件发送 ====================
def send_email_standalone(subject, html_content):
    sender = os.getenv('EMAIL_SENDER')
    password = os.getenv('EMAIL_PASSWORD')
    receivers_str = os.getenv('EMAIL_RECEIVERS')
    
    if not sender or not password:
        logger.error("❌ 邮件失败: 缺少发件人或密码环境变量")
        return False

    receivers = [r.strip() for r in receivers_str.split(',')] if receivers_str else [sender]
    
    smtp_server, smtp_port = "smtp.qq.com", 465
    if "@163.com" in sender: smtp_server = "smtp.163.com"
    elif "@gmail.com" in sender: smtp_server, smtp_port = "smtp.gmail.com", 587
    elif "@sina.com" in sender: smtp_server = "smtp.sina.com"

    try:
        msg = MIMEMultipart()
        msg['From'] = Header(f"Daily Market Brief <{sender}>", 'utf-8')
        msg['To'] = Header(",".join(receivers), 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')
        msg.attach(MIMEText(html_content, 'html', 'utf-8'))

        server = smtplib.SMTP_SSL(smtp_server, smtp_port) if smtp_port == 465 else smtplib.SMTP(smtp_server, smtp_port)
        if smtp_port != 465: server.starttls()
            
        server.login(sender, password)
        server.sendmail(sender, receivers, msg.as_string())
        server.quit()
        logger.info(f"✅ 邮件发送成功 ({len(receivers)} 人)")
        return True
    except Exception as e:
        logger.error(f"❌ 邮件发送异常: {e}")
        return False

# ==================== 4. 混合搜索模块 ====================
async def robust_search(query):
    """尝试 API 搜索 -> DDG 搜索"""
    text_res = ""
    
    # 1. 尝试 Tavily API (如果你配置了)
    tavily_key = os.getenv("TAVILY_API_KEYS")
    if tavily_key:
        try:
            logger.info("🕵️ 尝试 Tavily API 搜索...")
            # 简单的 HTTP 请求模拟，避免依赖 tavily-python 库
            import urllib.request
            req_data = json.dumps({"query": query, "api_key": tavily_key, "search_depth": "basic", "max_results": 10}).encode('utf-8')
            req = urllib.request.Request("https://api.tavily.com/search", data=req_data, headers={'content-type': 'application/json'})
            with urllib.request.urlopen(req) as f:
                resp = json.loads(f.read().decode('utf-8'))
                for r in resp.get('results', []):
                    text_res += f"Src: {r['title']}\nTxt: {r['content']}\n---\n"
            logger.info("✅ Tavily 搜索成功")
            return text_res
        except Exception as e:
            logger.warning(f"⚠️ Tavily 搜索失败: {e}")

    # 2. 尝试 DDG
    try:
        logger.info(f"🦆 [DDG] 搜索: {query[:10]}...")
        results = DDGS().text(query, max_results=15)
        if results:
            for r in results:
                if isinstance(r, dict):
                    text_res += f"Src: {r.get('title','?')}\nTxt: {r.get('body', r.get('snippet',''))}\n---\n"
            return text_res
    except Exception as e:
        logger.error(f"❌ DDG 搜索失败: {e}")
    
    return ""

# ==================== 5. 主程序 ====================
async def generate_morning_brief():
    print("="*60)
    logger.info("🚀 每日早报任务启动")
    
    # --- 1. 初始化 AI ---
    try:
        llm_client = DirectGeminiClient()
    except Exception as e:
        logger.error(f"❌ 无法初始化 AI: {e}")
        sys.exit(0)

    # --- 2. 获取数据 (三级保障) ---
    raw_context = ""
    
    # A. 尝试主动搜索 (针对传闻和小作文)
    queries = [
        "A股 市场小作文 传闻 24小时内 热门",
        "latest China stock market rumors last 24 hours"
    ]
    for q in queries:
        res = await robust_search(q)
        if res:
            raw_context += f"\nQuery: {q}\nResults:\n{res[:2000]}\n"

    # B. 必须执行：RSS 硬兜底 (确保有权威新闻)
    # 如果搜索结果太少，或者为了保证权威性，我们强制加载 RSS
    rss_data = fetch_rss_news()
    if rss_data:
        raw_context += f"\n=== AUTHORITATIVE NEWS (RSS) ===\n{rss_data}\n"

    logger.info(f"📊 最终资料长度: {len(raw_context)}")
    
    if len(raw_context) < 100:
        logger.error("❌ 无法获取任何有效新闻 (搜索和RSS均失败)")
        sys.exit(0)

    # --- 3. 生成报告 ---
    current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    
    prompt = f"""
    You are an expert financial analyst. Create a "Morning Market Brief" for {current_date} based on the data below.

    DATA SOURCE:
    {raw_context}

    INSTRUCTIONS:
    1. Output PURE HTML code only. NO markdown.
    2. Style: Swiss Design (Minimalist, Grid, Sans-serif).
    3. Content:
       - **🏛️ 权威要闻 (Facts)**: Select 20 verified news items (Prioritize RSS data from Sina/EastMoney).
       - **🗣️ 市场传闻 (Rumors)**: Select 20 market buzz/rumors (From search data).
    4. Format: One sentence per item. Numbered lists (1-20). Language: Chinese.
    5. Footer: "Generated by AI Analysis".
    """

    logger.info("🧠 AI 正在生成...")
    html_content = ""

    try:
        res = None
        if hasattr(llm_client, 'chat'):
            if inspect.iscoroutinefunction(llm_client.chat): res = await llm_client.chat(prompt)
            else: res = llm_client.chat(prompt)
        
        if res: html_content = res if isinstance(res, str) else str(res)
            
    except Exception as e:
        logger.error(f"❌ 生成异常: {e}")
        sys.exit(0)

    if not html_content:
        logger.error("❌ AI 返回空")
        sys.exit(0)

    html_content = html_content.replace("```html", "").replace("```", "").strip()

    # --- 4. 发送邮件 ---
    subject = f"【市场晨报】{current_date}"
    if send_email_standalone(subject, html_content):
        logger.info("🎉 任务完成")
    else:
        logger.warning("⚠️ 邮件发送失败")

if __name__ == "__main__":
    try:
        asyncio.run(generate_morning_brief())
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ 运行异常: {e}")
        sys.exit(0)
