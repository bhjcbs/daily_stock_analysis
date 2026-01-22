# -*- coding: utf-8 -*-
import os
import sys
import asyncio
import logging
import smtplib
import subprocess
import inspect
import traceback
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
import pytz

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== 0. 自动依赖检查 ====================
def install_package(package):
    try:
        logger.info(f"🔧 [Gemini优先] 检测到缺失库 {package}，正在自动安装...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
        logger.info(f"✅ {package} 安装成功")
    except Exception as e:
        logger.warning(f"❌ 自动安装失败: {e}")

try:
    import duckduckgo_search
except ImportError:
    install_package("duckduckgo-search")

# ==================== 1. 万能配置适配器 ====================
class ConfigAdapter(dict):
    """将配置对象转换为通用格式"""
    def __init__(self, original_config):
        self._orig = original_config
        data = {}
        if isinstance(original_config, dict):
            data = original_config
        elif hasattr(original_config, 'dict') and callable(original_config.dict):
            data = original_config.dict()
        elif hasattr(original_config, '__dict__'):
            data = vars(original_config)
        
        super().__init__(data)
        self.__dict__.update(data)

    def __getattr__(self, item):
        val = self.get(item)
        if val is not None: return val
        if hasattr(self._orig, item):
            return getattr(self._orig, item)
        return None

# ==================== 2. 动态加载 (强制 Gemini 3 优先) ====================
try:
    from config import Config
    from search_service import SearchService
    import analyzer
    
    # 智能查找 AI 分析器类
    LLMAnalyzer = None
    
    # [优先策略] 显式寻找 Gemini 相关类
    gemini_candidates = ['GeminiAnalyzer', 'GoogleGeminiAnalyzer', 'GeminiProAnalyzer']
    other_candidates = ['Analyzer', 'StockAnalyzer']
    
    # 1. 优先尝试 Gemini
    for name in gemini_candidates:
        if hasattr(analyzer, name):
            LLMAnalyzer = getattr(analyzer, name)
            logger.info(f"💎 已锁定 Gemini 分析器: {name}")
            break
            
    # 2. 如果没有 Gemini，才尝试其他
    if LLMAnalyzer is None:
        for name in other_candidates:
            if hasattr(analyzer, name):
                LLMAnalyzer = getattr(analyzer, name)
                logger.info(f"⚠️ 未找到 Gemini 专用类，降级使用: {name}")
                break
    
    # 3. 最后的兜底
    if LLMAnalyzer is None:
        for name, cls in inspect.getmembers(analyzer, inspect.isclass):
            if 'Analyzer' in name and 'Base' not in name:
                LLMAnalyzer = cls
                break

except ImportError:
    Config = None
    SearchService = None
    LLMAnalyzer = None
    logger.warning("⚠️ 未找到项目核心模块，进入备用模式。")

# ==================== 3. 独立邮件发送 ====================
def send_email_standalone(subject, html_content):
    sender = os.getenv('EMAIL_SENDER')
    password = os.getenv('EMAIL_PASSWORD')
    receivers_str = os.getenv('EMAIL_RECEIVERS')
    
    if not sender or not password:
        logger.error("❌ 邮件发送失败: 环境变量 EMAIL_SENDER 或 EMAIL_PASSWORD 未配置")
        return False

    receivers = [r.strip() for r in receivers_str.split(',')] if receivers_str else [sender]
    
    smtp_server = "smtp.qq.com"
    smtp_port = 465
    if "@163.com" in sender: smtp_server = "smtp.163.com"
    elif "@gmail.com" in sender: 
        smtp_server = "smtp.gmail.com"
        smtp_port = 587
    elif "@sina.com" in sender: smtp_server = "smtp.sina.com"

    try:
        msg = MIMEMultipart()
        msg['From'] = Header(f"Daily Stock Analysis <{sender}>", 'utf-8')
        msg['To'] = Header(",".join(receivers), 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')
        msg.attach(MIMEText(html_content, 'html', 'utf-8'))

        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port)
            server.starttls()
            
        server.login(sender, password)
        server.sendmail(sender, receivers, msg.as_string())
        server.quit()
        logger.info(f"✅ 邮件已发送给: {len(receivers)} 位收件人")
        return True
    except Exception as e:
        logger.error(f"❌ 邮件发送异常: {e}")
        return False

# ==================== 4. 搜索功能 (智能侦测) ====================
async def fallback_search_ddg(query):
    """DuckDuckGo 备用搜索"""
    try:
        from duckduckgo_search import DDGS
        logger.info(f"🦆 [备用] 调用 DuckDuckGo 搜索: {query[:10]}...")
        results = DDGS().text(query, max_results=25)
        
        text_res = ""
        if not results: return ""
        
        for r in results:
            if isinstance(r, dict):
                title = r.get('title', 'No Title')
                body = r.get('body', r.get('snippet', ''))
                text_res += f"Source: {title}\nContent: {body}\n---\n"
            else:
                text_res += f"{str(r)}\n---\n"
        return text_res
    except Exception as e:
        logger.error(f"❌ DuckDuckGo 搜索失败: {e}")
        return ""

async def smart_project_search(service, query):
    """
    自动侦测 SearchService 的正确方法名
    优先寻找可能利用 AI 增强的搜索方法
    """
    # 优先级列表：优先尝试可能包含 'gemini' 或 'smart' 的方法，然后是标准方法
    possible_methods = ['search_with_gemini', 'smart_search', 'search_news', 'search', 'query', 'fetch', 'run']
    
    for method in possible_methods:
        if hasattr(service, method):
            func = getattr(service, method)
            if callable(func):
                try:
                    logger.info(f"👉 [Gemini流程] 尝试调用项目搜索方法: {method}")
                    try:
                        res = func(query)
                    except TypeError:
                        res = func(query, 10) 
                    
                    if inspect.iscoroutine(res):
                        res = await res
                    
                    if res: return str(res)
                except Exception as e:
                    logger.warning(f"   调用 {method} 失败: {e}")
                    continue
    return None

# ==================== 5. 主流程 ====================
async def generate_morning_brief():
    print("="*60)
    logger.info("🚀 [每日早报] 任务启动 (Gemini 3 Enhanced)")
    
    # --- 初始化 ---
    cfg = Config() if Config else {}
    wrapped_cfg = ConfigAdapter(cfg)
    
    search_service = None
    llm_analyzer = None

    if SearchService:
        try: search_service = SearchService(wrapped_cfg)
        except: 
            try: search_service = SearchService(cfg)
            except: pass
            
    if LLMAnalyzer:
        try: llm_analyzer = LLMAnalyzer(wrapped_cfg)
        except: 
            try: llm_analyzer = LLMAnalyzer(cfg)
            except: pass
            
    if not llm_analyzer:
        logger.error("❌ 无法初始化 AI 分析器，任务终止。")
        sys.exit(0)

    # --- 执行搜索 ---
    queries = [
        "过去24小时 中国股市 A股 港股 重大财经新闻 利好利空",
        "latest Chinese stock market rumors and insider news last 24 hours",
        "A股 市场小作文 传闻 24小时内 热门",
        "新浪财经 东方财富 财联社 头条新闻 24小时"
    ]
    
    raw_context = ""
    logger.info("🔍 开始全网搜索 (优先使用项目内置源)...")
    
    for q in queries:
        res_text = ""
        # 1. 优先尝试项目自带搜索
        if search_service:
            res_text = await smart_project_search(search_service, q)
        
        # 2. 备用
        if not res_text or len(res_text) < 100:
            res_text = await fallback_search_ddg(q)
            
        if res_text:
            raw_context += f"\nQuery: {q}\nResults:\n{res_text[:3000]}\n"

    logger.info(f"📊 获取资料总长度: {len(raw_context)}")
    
    if len(raw_context) < 100:
        logger.error("❌ 未获取到有效数据，停止生成。")
        sys.exit(0)

    # --- AI 分析与生成 (Gemini 3 Prompt) ---
    current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    
    # 针对 Gemini 3 优化的 Prompt
    prompt = f"""
    You are an expert financial analyst using the Gemini 3 model capabilities. 
    Analyze the raw search data below to create a "Daily Stock Analysis - Morning Brief" for {current_date}.

    SOURCE DATA:
    {raw_context}

    INSTRUCTIONS:
    1. **Format**: Output PURE HTML code. "Swiss Style" design (Minimalist, Grid, Sans-serif).
       - NO Markdown code blocks.
       - Include internal CSS.
    
    2. **Content Extraction (Gemini Reasoning)**:
       - **Section 1: 🏛️ 权威要闻 (Market Facts)**
         - Filter for the 20 MOST IMPACTFUL news items from reliable sources (Gov, Sina, Reuters).
         - Focus on policy changes, earnings, and major market moves.
         - NO speculation.
       - **Section 2: 🗣️ 市场传闻 (Market Rumors)**
         - Filter for the 20 HOTTEST market rumors ("Little Compositions", unverified buzz) currently driving sentiment.
         - Rank by heat/controversy.
    
    3. **Writing Style**:
       - NO TITLES. One sentence summary per item.
       - Language: Chinese (Simplified).
       - Numbered lists (1-20).

    4. **Structure**:
       - Header: "{current_date} 市场晨报 (Powered by Gemini 3)"
       - Section 1 (Facts)
       - Section 2 (Rumors)
       - Footer: "Generated by Daily Stock Analysis AI"

    Generate the HTML now.
    """

    logger.info("🧠 Gemini 3 正在分析并撰写报告...")
    html_content = ""
    try:
        # 尝试调用 chat 或 analyze
        if hasattr(llm_analyzer, 'chat'):
            html_content = await llm_analyzer.chat(prompt)
        elif hasattr(llm_analyzer, 'analyze'):
            try: html_content = await llm_analyzer.analyze(prompt)
            except: html_content = await llm_analyzer.analyze("000001", prompt)
        
        if not html_content:
            logger.error("❌ AI 返回内容为空")
            sys.exit(0)

        html_content = html_content.replace("```html", "").replace("```", "").strip()
        
        subject = f"【每日证券分析】{current_date} 市场晨报 (Gemini 3版)"
        if send_email_standalone(subject, html_content):
            logger.info("🎉 任务完成！")
        else:
            logger.warning("⚠️ 邮件发送失败")
            
    except Exception as e:
        logger.error(f"❌ 异常: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    try:
        asyncio.run(generate_morning_brief())
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ 顶级异常: {e}")
        sys.exit(0)
