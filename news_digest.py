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
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
import pytz

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== 0. 自动依赖安装 ====================
def install_package(package):
    try:
        logger.info(f"🔧 自动安装依赖: {package}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
    except Exception as e:
        logger.warning(f"❌ 安装 {package} 失败: {e}")

try:
    import feedparser
except ImportError:
    install_package("feedparser")
    import feedparser

try:
    from duckduckgo_search import DDGS
except ImportError:
    install_package("duckduckgo-search>=6.0.0")
    from duckduckgo_search import DDGS

try:
    import google.generativeai as genai
except ImportError:
    install_package("google-generativeai")
    import google.generativeai as genai

# ==================== 1. RSS 硬兜底 ====================
RSS_SOURCES = {
    "Sina_Roll": "https://rss.sina.com.cn/roll/finance/hot_roll.xml",
    "Sina_Focus": "https://rss.sina.com.cn/news/china/focus15.xml",
    "EastMoney": "http://www.eastmoney.com/rss/msg.xml",
    "WallstreetCN": "https://wallstreetcn.com/rss/live.xml" 
}

def fetch_rss_news():
    logger.info("📡 [RSS] 读取官方新闻源...")
    combined_text = ""
    # 忽略 SSL
    if hasattr(ssl, '_create_unverified_context'):
        ssl._create_default_https_context = ssl._create_unverified_context

    for name, url in RSS_SOURCES.items():
        try:
            # 增加 User-Agent 防止被拒
            feed = feedparser.parse(url, agent="Mozilla/5.0")
            logger.info(f"   - {name}: 获取到 {len(feed.entries)} 条")
            
            for entry in feed.entries[:8]:
                title = entry.get('title', '')
                summary = entry.get('summary', entry.get('description', ''))
                summary = summary.replace('<p>', '').replace('</p>', '').replace('<br>', '')
                combined_text += f"Src: {name} (RSS)\nTitle: {title}\nSum: {summary[:150]}\n---\n"
        except Exception as e:
            logger.warning(f"   - {name} 失败: {e}")
            
    return combined_text

# ==================== 2. 独立 Gemini 客户端 (自动换模型版) ====================
class DirectGeminiClient:
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY 未配置")
        
        genai.configure(api_key=api_key)
        
        # 候选模型列表 (按优先级排序)
        # 如果 flash 404，会自动尝试 pro，再尝试旧版 pro
        self.candidate_models = [
            'gemini-1.5-flash',
            'gemini-1.5-flash-latest',
            'gemini-1.5-pro',
            'gemini-1.5-pro-latest',
            'gemini-pro',       # 经典版
            'gemini-1.0-pro'    # 兼容版
        ]
        logger.info("💎 [独立模式] Gemini 客户端初始化完成")

    async def chat(self, prompt):
        last_error = None
        
        for model_name in self.candidate_models:
            try:
                logger.info(f"🤖 尝试调用模型: {model_name} ...")
                model = genai.GenerativeModel(model_name)
                # generate_content 是同步方法，但在 async 中运行通常没问题
                response = model.generate_content(prompt)
                
                if response and response.text:
                    logger.info(f"✅ 模型 {model_name} 调用成功！")
                    return response.text
                    
            except Exception as e:
                error_str = str(e)
                # 过滤常见错误
                if "404" in error_str or "not found" in error_str.lower():
                    logger.warning(f"⚠️ 模型 {model_name} 不存在或不可用，切换下一个...")
                elif "429" in error_str:
                    logger.warning(f"⚠️ 模型 {model_name} 请求过多 (429)，休息2秒后切换...")
                    time.sleep(2)
                else:
                    logger.warning(f"❌ 模型 {model_name} 报错: {e}")
                
                last_error = e
                continue
        
        logger.error("❌ 所有候选模型均失败，无法生成报告。")
        raise last_error

# ==================== 3. 邮件发送 ====================
def send_email_standalone(subject, html_content):
    sender = os.getenv('EMAIL_SENDER')
    password = os.getenv('EMAIL_PASSWORD')
    receivers_str = os.getenv('EMAIL_RECEIVERS')
    
    if not sender or not password:
        logger.error("❌ 邮件失败: 环境变量不足")
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

# ==================== 4. 混合搜索 ====================
async def robust_search(query):
    text_res = ""
    # 1. Tavily API (已验证你配置了Key，优先用它)
    tavily_key = os.getenv("TAVILY_API_KEYS")
    if tavily_key:
        try:
            # 手动 request 避免安装库
            import urllib.request
            data = json.dumps({"query": query, "api_key": tavily_key, "max_results": 10}).encode()
            req = urllib.request.Request("https://api.tavily.com/search", data=data, headers={'content-type': 'application/json'})
            with urllib.request.urlopen(req) as f:
                resp = json.loads(f.read().decode())
                for r in resp.get('results', []):
                    text_res += f"Src: {r['title']}\nTxt: {r['content']}\n---\n"
            return text_res
        except Exception as e:
            logger.warning(f"⚠️ Tavily 搜索异常: {e}")

    # 2. DDG (备用)
    try:
        results = DDGS().text(query, max_results=10)
        for r in results:
            if isinstance(r, dict):
                text_res += f"Src: {r.get('title','?')}\nTxt: {r.get('body', r.get('snippet',''))}\n---\n"
    except Exception:
        pass
    
    return text_res

# ==================== 5. 主流程 ====================
async def generate_morning_brief():
    print("="*60)
    logger.info("🚀 任务启动")
    
    # 1. 初始化 AI
    try:
        llm_client = DirectGeminiClient()
    except Exception as e:
        logger.error(f"❌ 初始化失败: {e}")
        sys.exit(0)

    # 2. 获取数据
    raw_context = ""
    # A. 搜索 (Tavily/DDG)
    queries = ["A股 市场小作文 传闻 24小时内", "China stock market news rumors"]
    for q in queries:
        res = await robust_search(q)
        if res: raw_context += f"\nQuery: {q}\nResults:\n{res[:2000]}\n"

    # B. RSS (权威源)
    rss_data = fetch_rss_news()
    if rss_data: raw_context += f"\n=== RSS DATA ===\n{rss_data}\n"

    if len(raw_context) < 100:
        logger.error("❌ 无有效数据")
        sys.exit(0)
    
    logger.info(f"📊 资料长度: {len(raw_context)}")

    # 3. 生成报告
    current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    prompt = f"""
    You are an expert financial analyst. Create a "Morning Market Brief" for {current_date} based on the data below.

    DATA:
    {raw_context}

    INSTRUCTIONS:
    1. Output PURE HTML code only. NO markdown.
    2. Style: Swiss Design (Minimalist, Grid, Sans-serif).
    3. Sections:
       - **🏛️ 权威要闻 (Facts)**: Top 20 verified news.
       - **🗣️ 市场传闻 (Rumors)**: Top 20 market buzz.
    4. Format: One sentence per item. Numbered lists (1-20). Language: Chinese.
    """

    logger.info("🧠 AI 正在生成...")
    html_content = ""
    try:
        res = await llm_client.chat(prompt)
        if res: html_content = str(res)
    except Exception as e:
        logger.error(f"❌ 生成最终失败: {e}")
        sys.exit(0)

    if not html_content:
        logger.error("❌ AI 返回空")
        sys.exit(0)

    html_content = html_content.replace("```html", "").replace("```", "").strip()

    # 4. 发送
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
