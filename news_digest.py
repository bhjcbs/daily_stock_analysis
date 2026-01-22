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
        # 避免重复安装 output 干扰日志
        subprocess.check_call([sys.executable, "-m", "pip", "install", package], stdout=subprocess.DEVNULL)
        logger.info(f"✅ 依赖 {package} 就绪")
    except Exception:
        pass

# 安装基础库
for pkg in ["feedparser", "duckduckgo-search>=6.0.0", "google-generativeai", "openai"]:
    install_package(pkg)

import feedparser
from duckduckgo_search import DDGS
import google.generativeai as genai

# ==================== 1. 万能 AI 客户端 (支持 Grok/GPT/Nvidia/Gemini) ====================
class UniversalAIClient:
    """
    自动适配所有主流模型的客户端。
    优先级: OpenAI兼容接口 (Grok/Nvidia/GPT) > Google Gemini
    """
    def __init__(self):
        self.client_type = None
        self.client = None
        self.model_name = None

        # 1. 优先检查 OpenAI 兼容配置 (支持 Grok, Nvidia, DeepSeek, ChatGPT)
        openai_key = os.getenv("OPENAI_API_KEY")
        openai_base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        if openai_key and openai_key.strip():
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=openai_key, base_url=openai_base)
                self.model_name = openai_model
                self.client_type = "openai"
                logger.info(f"🤖 [万能模式] 已连接 OpenAI 兼容接口")
                logger.info(f"   - URL: {openai_base}")
                logger.info(f"   - Model: {self.model_name}")
                return
            except Exception as e:
                logger.warning(f"⚠️ OpenAI 配置存在但初始化失败: {e}")

        # 2. 回退到 Gemini
        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key:
            genai.configure(api_key=gemini_key)
            self.client_type = "gemini"
            # 自动轮询 Gemini 模型列表
            self.gemini_candidates = ['gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-pro']
            logger.info("💎 [万能模式] 切换至 Google Gemini")
            return

        raise ValueError("❌ 未找到任何有效的 API Key (OPENAI_API_KEY 或 GEMINI_API_KEY)")

    async def chat(self, prompt):
        # A. OpenAI 兼容通道 (Grok, Nvidia, GPT)
        if self.client_type == "openai":
            try:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": "You are a professional financial analyst."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.7
                )
                return response.choices[0].message.content
            except Exception as e:
                logger.error(f"❌ OpenAI/Grok 接口调用失败: {e}")
                return None

        # B. Gemini 通道
        elif self.client_type == "gemini":
            for model in self.gemini_candidates:
                try:
                    m = genai.GenerativeModel(model)
                    resp = m.generate_content(prompt)
                    return resp.text
                except Exception as e:
                    logger.warning(f"⚠️ Gemini {model} 失败: {e}")
                    time.sleep(1)
            logger.error("❌ 所有 Gemini 模型均失败")
            return None

# ==================== 2. 数据源获取 (RSS + 搜索) ====================
RSS_SOURCES = {
    "Sina_Roll": "https://rss.sina.com.cn/roll/finance/hot_roll.xml",
    "EastMoney": "http://www.eastmoney.com/rss/msg.xml",
    "WallstreetCN": "https://wallstreetcn.com/rss/live.xml"
}

def fetch_data():
    raw_text = ""
    
    # 1. 优先：权威 RSS (不受反爬影响)
    if hasattr(ssl, '_create_unverified_context'):
        ssl._create_default_https_context = ssl._create_unverified_context
    
    logger.info("📡 读取 RSS 新闻源...")
    for name, url in RSS_SOURCES.items():
        try:
            feed = feedparser.parse(url, agent="Mozilla/5.0")
            for entry in feed.entries[:5]:
                title = entry.get('title', '')
                summary = entry.get('summary', '').replace('<p>', '')[:100]
                raw_text += f"Src: {name}\nTitle: {title}\nTxt: {summary}\n---\n"
        except: pass

    # 2. 补充：DuckDuckGo 搜索 (传闻/小作文)
    queries = ["A股 市场传闻 小作文 24小时", "China stock market rumors today"]
    logger.info("🦆 执行补充搜索...")
    try:
        ddgs = DDGS()
        for q in queries:
            results = ddgs.text(q, max_results=8)
            if results:
                for r in results:
                    if isinstance(r, dict):
                        raw_text += f"Src: {r.get('title')}\nTxt: {r.get('body')}\n---\n"
    except Exception as e:
        logger.warning(f"DDG 搜索波动: {e}")

    return raw_text

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
    else: smtp_server, port = "smtp.qq.com", 465 # Default
    
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

    # 初始化 AI
    try:
        ai = UniversalAIClient()
    except Exception as e:
        logger.error(f"❌ AI 初始化失败: {e}")
        sys.exit(0)

    # 获取数据
    data = fetch_data()
    if len(data) < 50:
        logger.error("❌ 数据不足，无法生成")
        sys.exit(0)
    logger.info(f"📊 数据长度: {len(data)}")

    # 生成报告
    today = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    prompt = f"""
    Time: {today}. 
    Analyze this financial data and create a HTML Morning Brief.
    
    DATA:
    {data[:6000]}

    REQUIREMENTS:
    1. Output pure HTML. No Markdown.
    2. Section 1: 🏛️ Facts (20 items from verified sources).
    3. Section 2: 🗣️ Rumors (20 items from buzz/rumors).
    4. Language: Chinese. One sentence per item.
    5. Style: Minimalist, Grid layout.
    """

    logger.info(f"🧠 {ai.client_type.upper()} 正在生成...")
    try:
        res = await ai.chat(prompt)
        if not res: raise ValueError("AI 返回空")
        
        html = res.replace("```html", "").replace("```", "").strip()
        send_email(f"【市场晨报】{today}", html)
    except Exception as e:
        logger.error(f"❌ 生成失败: {e}")

if __name__ == "__main__":
    asyncio.run(main())
