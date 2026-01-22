# -*- coding: utf-8 -*-
import os
import sys
import asyncio
import logging
import smtplib
import subprocess
import json
import ssl
import time
import requests # 必须引入 requests
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
        subprocess.check_call([sys.executable, "-m", "pip", "install", package], stdout=subprocess.DEVNULL)
        logger.info(f"✅ 依赖 {package} 就绪")
    except Exception: pass

# 安装必要的库
pkgs = ["feedparser", "duckduckgo-search>=6.0.0", "google-generativeai", "openai", "requests"]
for pkg in pkgs: install_package(pkg)

import feedparser
from duckduckgo_search import DDGS
import google.generativeai as genai

# ==================== 1. 万能 AI 客户端 ====================
class UniversalAIClient:
    def __init__(self):
        self.client_type = None
        self.client = None
        self.model_name = None

        # 1. OpenAI 兼容接口 (优先)
        openai_key = os.getenv("OPENAI_API_KEY")
        openai_base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        if openai_key and openai_key.strip():
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=openai_key, base_url=openai_base)
                self.model_name = openai_model
                self.client_type = "openai"
                logger.info(f"🤖 [模式] OpenAI 兼容接口 ({openai_model})")
                return
            except Exception as e:
                logger.warning(f"⚠️ OpenAI 初始化失败: {e}")

        # 2. Google Gemini
        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key:
            genai.configure(api_key=gemini_key)
            self.client_type = "gemini"
            self.gemini_candidates = ['gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-pro']
            logger.info("💎 [模式] Google Gemini")
            return

        raise ValueError("❌ 未配置有效的 API Key")

    async def chat(self, prompt):
        if self.client_type == "openai":
            try:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": "You are a financial analyst."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.7
                )
                return response.choices[0].message.content
            except Exception as e:
                logger.error(f"❌ OpenAI 接口报错: {e}")
                return None

        elif self.client_type == "gemini":
            for model in self.gemini_candidates:
                try:
                    m = genai.GenerativeModel(model)
                    resp = m.generate_content(prompt)
                    return resp.text
                except: continue
            return None

# ==================== 2. 数据获取 (Tavily + RSS + DDG) ====================
def fetch_tavily_data():
    """使用 Tavily API (最稳)"""
    key = os.getenv("TAVILY_API_KEYS")
    if not key: return ""
    
    logger.info("🕵️ 正在调用 Tavily 搜索 (高可靠)...")
    try:
        # 搜索事实和传闻
        query = "China stock market news rumors last 24 hours A股 市场传闻"
        resp = requests.post(
            "https://api.tavily.com/search",
            json={"query": query, "api_key": key, "search_depth": "basic", "max_results": 10},
            timeout=10
        )
        data = resp.json()
        text = ""
        for r in data.get("results", []):
            text += f"Src: {r['title']}\nTxt: {r['content']}\n---\n"
        logger.info(f"✅ Tavily 获取到 {len(text)} 字符")
        return text
    except Exception as e:
        logger.warning(f"⚠️ Tavily 搜索失败: {e}")
        return ""

def fetch_rss_data():
    """RSS 兜底"""
    logger.info("📡 正在读取 RSS 源...")
    # 更多样化的源，防止单一源挂掉
    sources = [
        "https://rss.sina.com.cn/roll/finance/hot_roll.xml", # 新浪财经
        "http://www.eastmoney.com/rss/msg.xml",             # 东方财富
        "https://feedx.net/rss/36kr.xml",                   # 36氪
        "https://www.solidot.org/index.rss"                 # 科技
    ]
    text = ""
    if hasattr(ssl, '_create_unverified_context'):
        ssl._create_default_https_context = ssl._create_unverified_context

    for url in sources:
        try:
            d = feedparser.parse(url, agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
            for e in d.entries[:5]:
                t = e.get('title', '')
                s = e.get('summary', e.get('description', ''))[:150].replace('<p>', '')
                text += f"Src: RSS\nTitle: {t}\nTxt: {s}\n---\n"
        except: pass
    return text

def fetch_ddg_data():
    """DDG 补充"""
    logger.info("🦆 正在尝试 DDG 补充搜索...")
    text = ""
    try:
        ddgs = DDGS()
        # 针对传闻搜索
        res = ddgs.text("A股 小作文 传闻 最新", max_results=5)
        if res:
            for r in res:
                text += f"Src: {r.get('title')}\nTxt: {r.get('body')}\n---\n"
    except: pass
    return text

# ==================== 3. 邮件发送 ====================
def send_email(subject, html):
    sender = os.getenv('EMAIL_SENDER')
    pwd = os.getenv('EMAIL_PASSWORD')
    to = os.getenv('EMAIL_RECEIVERS')
    
    if not sender or not pwd: return False
    
    receivers = to.split(',') if to else [sender]
    smtp_server = "smtp.qq.com"
    if "@163.com" in sender: smtp_server = "smtp.163.com"
    elif "@gmail.com" in sender: smtp_server, port = "smtp.gmail.com", 587
    else: smtp_server, port = "smtp.qq.com", 465
    
    msg = MIMEMultipart()
    msg['From'] = Header(f"Daily Brief <{sender}>", 'utf-8')
    msg['To'] = Header(",".join(receivers), 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    try:
        s = smtplib.SMTP_SSL(smtp_server, 465)
        s.login(sender, pwd)
        s.sendmail(sender, receivers, msg.as_string())
        s.quit()
        logger.info("✅ 邮件发送成功")
        return True
    except Exception as e:
        logger.error(f"❌ 邮件发送失败: {e}")
        return False

# ==================== 4. 主流程 ====================
async def main():
    print("="*60)
    logger.info("🚀 任务启动")

    # 1. 初始化 AI
    try:
        ai = UniversalAIClient()
    except Exception as e:
        logger.error(f"❌ AI 初始化失败: {e}")
        sys.exit(0)

    # 2. 获取数据 (Tavily > RSS > DDG)
    raw_data = ""
    
    # 优先尝试 Tavily (最稳)
    raw_data += fetch_tavily_data()
    
    # 如果数据不够，叠加 RSS
    if len(raw_data) < 1000:
        raw_data += fetch_rss_data()
    
    # 最后叠加 DDG
    if len(raw_data) < 2000:
        raw_data += fetch_ddg_data()

    logger.info(f"📊 最终数据长度: {len(raw_data)}")
    
    if len(raw_data) < 50:
        logger.error("❌ 所有渠道均未获取到有效数据，任务终止")
        sys.exit(0)

    # 3. 生成报告
    today = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    prompt = f"""
    Current Date: {today}. 
    Based on the following news data, generate a 'Morning Market Brief' HTML report.
    
    DATA START:
    {raw_data[:10000]}
    DATA END.

    REQUIREMENTS:
    1. Output **PURE HTML ONLY**. No Markdown code blocks.
    2. **Section 1: 🏛️ 市场要闻 (Facts)**
       - List 20 verified news items from reliable sources.
       - Focus on policy, earnings, and global markets.
    3. **Section 2: 🗣️ 市场传闻 (Rumors)**
       - List 20 unverified rumors/buzz ("Little Compositions").
       - Prioritize items with high discussion heat.
    4. **Style**: 
       - Minimalist Swiss Design. 
       - Use internal CSS for styling.
       - Language: Chinese (Simplified).
       - One sentence summary per item.
    """

    logger.info("🧠 AI 正在分析生成的报告...")
    try:
        res = await ai.chat(prompt)
        if not res: raise ValueError("AI 返回空")
        
        # 清理可能存在的 Markdown
        html = res.replace("```html", "").replace("```", "").strip()
        
        send_email(f"【市场晨报】{today}", html)
    except Exception as e:
        logger.error(f"❌ 生成失败: {e}")

if __name__ == "__main__":
    asyncio.run(main())
