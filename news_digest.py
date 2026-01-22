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
import requests 
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

pkgs = ["feedparser", "duckduckgo-search>=6.0.0", "google-generativeai", "openai", "requests"]
for pkg in pkgs: install_package(pkg)

import feedparser
from duckduckgo_search import DDGS
import google.generativeai as genai

# ==================== 1. 具有自动灾备能力的 AI 客户端 ====================
class ResilienceAIClient:
    """
    自动灾备客户端：
    1. 优先尝试 OpenAI 兼容接口 (Grok/ChatGPT/Nvidia)
    2. 如果失败 (403/500/Timeout)，自动切换到 Gemini
    """
    def __init__(self):
        self.primary_client = None
        self.backup_client = None
        self.model_name = "gpt-4o-mini" # 默认

        # 1. 配置 Primary (OpenAI Compatible)
        openai_key = os.getenv("OPENAI_API_KEY")
        openai_base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        if openai_key and openai_key.strip():
            try:
                from openai import OpenAI
                self.primary_client = OpenAI(api_key=openai_key, base_url=openai_base)
                logger.info(f"🤖 [首选] OpenAI 兼容接口已就绪 ({self.openai_model})")
            except Exception as e:
                logger.warning(f"⚠️ OpenAI 初始化异常: {e}")

        # 2. 配置 Backup (Gemini)
        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key:
            try:
                genai.configure(api_key=gemini_key)
                self.gemini_candidates = ['gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-pro']
                self.backup_client = True
                logger.info("💎 [备用] Gemini 接口已就绪 (随时待命)")
            except Exception as e:
                logger.warning(f"⚠️ Gemini 初始化异常: {e}")

        if not self.primary_client and not self.backup_client:
            raise ValueError("❌ 未找到任何可用的 API Key (OPENAI 或 GEMINI)")

    async def chat(self, prompt):
        # --- 尝试 Primary (Grok/OpenAI) ---
        if self.primary_client:
            try:
                logger.info(f"🚀 正在调用首选模型: {self.openai_model}...")
                response = self.primary_client.chat.completions.create(
                    model=self.openai_model,
                    messages=[
                        {"role": "system", "content": "You are a professional financial analyst."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.7
                )
                return response.choices[0].message.content
            except Exception as e:
                logger.error(f"❌ 首选模型调用失败: {e}")
                logger.warning("🔄 正在触发故障转移 (Failover) -> 切换到 Gemini...")
                # 失败后，不返回，直接向下执行 Gemini 逻辑

        # --- 尝试 Backup (Gemini) ---
        if self.backup_client:
            return await self._call_gemini_fallback(prompt)
        
        return None

    async def _call_gemini_fallback(self, prompt):
        logger.info("💎 正在调用备用模型 (Gemini)...")
        for model in self.gemini_candidates:
            try:
                m = genai.GenerativeModel(model)
                # generate_content 是同步的，但在 fallback 场景下可以直接用
                resp = m.generate_content(prompt)
                if resp.text:
                    logger.info(f"✅ Gemini ({model}) 调用成功")
                    return resp.text
            except Exception as e:
                logger.warning(f"   - Gemini {model} 失败: {e}")
                continue
        return None

# ==================== 2. 数据获取模块 (Tavily + RSS) ====================
def fetch_tavily_data():
    key = os.getenv("TAVILY_API_KEYS")
    if not key: return ""
    
    logger.info("🕵️ [Level 1] 调用 Tavily 搜索...")
    try:
        # 搜索最近 24 小时新闻
        query = "China stock market news rumors last 24 hours A股 市场传闻 利好利空"
        resp = requests.post(
            "https://api.tavily.com/search",
            json={"query": query, "api_key": key, "search_depth": "basic", "max_results": 15},
            timeout=15
        )
        data = resp.json()
        text = ""
        for r in data.get("results", []):
            text += f"Source: {r['title']}\nContent: {r['content']}\n---\n"
        logger.info(f"✅ Tavily 获取成功 ({len(text)} 字符)")
        return text
    except Exception as e:
        logger.warning(f"⚠️ Tavily 失败: {e}")
        return ""

def fetch_rss_data():
    logger.info("📡 [Level 2] 读取 RSS 源...")
    text = ""
    sources = [
        "https://rss.sina.com.cn/roll/finance/hot_roll.xml",
        "http://www.eastmoney.com/rss/msg.xml",
        "https://www.solidot.org/index.rss"
    ]
    if hasattr(ssl, '_create_unverified_context'):
        ssl._create_default_https_context = ssl._create_unverified_context

    for url in sources:
        try:
            d = feedparser.parse(url, agent="Mozilla/5.0")
            for e in d.entries[:5]:
                t = e.get('title', '')
                s = e.get('summary', e.get('description', ''))[:200].replace('<p>', '')
                text += f"Source: RSS\nTitle: {t}\nSummary: {s}\n---\n"
        except: pass
    return text

def fetch_ddg_data():
    logger.info("🦆 [Level 3] DDG 补充搜索...")
    text = ""
    try:
        ddgs = DDGS()
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
    logger.info("🚀 任务启动 (Resilience Mode)")

    # 1. 初始化 AI (双引擎)
    try:
        ai = ResilienceAIClient()
    except Exception as e:
        logger.error(f"❌ AI 初始化失败: {e}")
        sys.exit(0)

    # 2. 获取数据
    raw_data = ""
    raw_data += fetch_tavily_data() # 优先 Tavily
    
    if len(raw_data) < 2000: # 数据不够才读 RSS
        raw_data += fetch_rss_data()
        
    if len(raw_data) < 3000: # 还没够就读 DDG
        raw_data += fetch_ddg_data()

    logger.info(f"📊 最终数据长度: {len(raw_data)}")
    
    if len(raw_data) < 50:
        logger.error("❌ 无有效数据")
        sys.exit(0)

    # 3. 生成报告
    today = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    prompt = f"""
    Current Date: {today}. 
    Based on the provided financial news data, generate a 'Morning Market Brief' HTML report.
    
    DATA START:
    {raw_data[:12000]}
    DATA END.

    REQUIREMENTS:
    1. **Format**: Output pure HTML code ONLY. No Markdown blocks.
    2. **Structure**:
       - Header: "{today} 市场晨报"
       - **Section 1: 🏛️ 权威要闻 (Facts)**
         - List 20 verified news items (Policy, Earnings, Global).
         - Source must be reliable.
       - **Section 2: 🗣️ 市场传闻 (Rumors)**
         - List 20 unverified rumors/buzz from the market.
         - Rank by heat.
    3. **Style**:
       - Swiss Design (Grid, Clean, Sans-serif).
       - One sentence per item.
       - Language: Chinese (Simplified).
    """

    logger.info("🧠 正在请求 AI 生成...")
    try:
        res = await ai.chat(prompt)
        if not res: raise ValueError("所有 AI 模型均未返回内容")
        
        html = res.replace("```html", "").replace("```", "").strip()
        send_email(f"【市场晨报】{today}", html)
    except Exception as e:
        logger.error(f"❌ 最终失败: {e}")

if __name__ == "__main__":
    asyncio.run(main())
