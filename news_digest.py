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
        logger.info(f"🔧 正在自动安装依赖: {package}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
        logger.info(f"✅ {package} 安装成功")
    except Exception as e:
        logger.warning(f"❌ 安装 {package} 失败: {e}")

# 检查必要的库
try:
    import duckduckgo_search
except ImportError:
    install_package("duckduckgo-search")

try:
    import google.generativeai as genai
except ImportError:
    install_package("google-generativeai")
    import google.generativeai as genai

# ==================== 1. 内置独立 Gemini 客户端 (兜底神器) ====================
class DirectGeminiClient:
    """
    当原项目分析器无法加载时，直接使用此客户端连接 Gemini。
    不依赖项目任何文件，只要有 API Key 就能跑。
    """
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("未找到 GEMINI_API_KEY 环境变量")
        
        genai.configure(api_key=api_key)
        # 优先尝试新版 Flash 模型，速度快效果好
        self.model = genai.GenerativeModel('gemini-1.5-flash')
        logger.info("💎 [独立模式] 已初始化内置 Gemini 客户端 (gemini-1.5-flash)")

    async def chat(self, prompt):
        try:
            # 这里的 generate_content 是同步调用，但在 async 函数中没问题
            response = self.model.generate_content(prompt)
            return response.text
        except Exception as e:
            logger.error(f"❌ Gemini API 调用失败: {e}")
            return None

# ==================== 2. 万能配置适配器 ====================
class ConfigAdapter(dict):
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
        if hasattr(self._orig, item): return getattr(self._orig, item)
        return None

# ==================== 3. 动态加载项目模块 ====================
try:
    from config import Config
    from search_service import SearchService
    import analyzer
    
    # 尝试查找项目中的 Analyzer 类
    ProjectAnalyzerClass = None
    candidates = ['GeminiAnalyzer', 'GoogleGeminiAnalyzer', 'Analyzer', 'StockAnalyzer']
    for name in candidates:
        if hasattr(analyzer, name):
            ProjectAnalyzerClass = getattr(analyzer, name)
            break
            
    if ProjectAnalyzerClass is None:
        # 扫描所有类
        for name, cls in inspect.getmembers(analyzer, inspect.isclass):
            if 'Analyzer' in name and 'Base' not in name:
                ProjectAnalyzerClass = cls
                break
except ImportError:
    Config = None
    SearchService = None
    ProjectAnalyzerClass = None
    logger.warning("⚠️ 未找到项目核心模块，将使用纯独立模式运行。")

# ==================== 4. 邮件发送模块 ====================
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
        msg['From'] = Header(f"Daily Market Brief <{sender}>", 'utf-8')
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
        logger.info(f"✅ 邮件发送成功 ({len(receivers)} 人)")
        return True
    except Exception as e:
        logger.error(f"❌ 邮件发送异常: {e}")
        return False

# ==================== 5. 搜索功能 (混合模式) ====================
async def fallback_search_ddg(query):
    try:
        from duckduckgo_search import DDGS
        logger.info(f"🦆 [DuckDuckGo] 搜索: {query[:15]}...")
        # 尝试使用 v4+ 新版 API
        results = DDGS().text(query, max_results=20)
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
    """尝试调用项目原有的搜索功能"""
    possible_methods = ['search', 'search_news', 'query', 'fetch', 'get_news', 'run']
    for method in possible_methods:
        if hasattr(service, method):
            func = getattr(service, method)
            if callable(func):
                try:
                    logger.info(f"👉 [项目内置] 尝试调用 {method}...")
                    try: res = func(query)
                    except TypeError: res = func(query, 10)
                    
                    if inspect.iscoroutine(res): res = await res
                    if res: return str(res)
                except Exception as e:
                    logger.warning(f"   调用 {method} 失败: {e}")
    return None

# ==================== 6. 主程序 ====================
async def generate_morning_brief():
    print("="*60)
    logger.info("🚀 每日早报任务启动")
    
    # 1. 初始化 AI 分析器 (双重保障)
    llm_client = None
    
    # A计划：尝试加载项目原有的 Analyzer
    if ProjectAnalyzerClass:
        try:
            cfg = Config() if Config else {}
            wrapped_cfg = ConfigAdapter(cfg)
            try: llm_client = ProjectAnalyzerClass(wrapped_cfg)
            except: llm_client = ProjectAnalyzerClass(cfg)
            logger.info("✅ 成功加载项目原有 AI 分析器")
        except Exception as e:
            logger.warning(f"⚠️ 项目 Analyzer 加载失败 ({e})，切换到 B 计划...")
    
    # B计划：加载内置独立 Gemini 客户端
    if not llm_client:
        try:
            llm_client = DirectGeminiClient()
        except Exception as e:
            logger.error(f"❌ 致命错误: 无法初始化任何 AI 客户端。原因: {e}")
            logger.error("👉 请检查 GitHub Secrets 中是否配置了 GEMINI_API_KEY")
            sys.exit(0)

    # 2. 执行搜索
    # 搜索词旨在覆盖 24小时内的“事实”与“传闻”
    queries = [
        "过去24小时 中国股市 A股 港股 重大财经新闻 利好利空",
        "latest China stock market rumors and insider news last 24 hours",
        "A股 市场小作文 传闻 24小时内 热门",
        "新浪财经 东方财富 财联社 头条新闻 24小时"
    ]
    
    raw_context = ""
    
    # 初始化搜索服务 (如果有)
    project_search = None
    if SearchService:
        try:
            cfg = Config() if Config else {}
            project_search = SearchService(ConfigAdapter(cfg))
        except: pass

    for q in queries:
        res_text = ""
        # 优先用项目搜索
        if project_search:
            res_text = await smart_project_search(project_search, q)
        
        # 兜底用 DDG
        if not res_text or len(res_text) < 100:
            res_text = await fallback_search_ddg(q)
            
        if res_text:
            raw_context += f"\nQuery: {q}\nResults:\n{res_text[:3000]}\n"

    logger.info(f"📊 资料总长度: {len(raw_context)}")
    
    if len(raw_context) < 100:
        logger.error("❌ 搜索无结果，停止生成。")
        sys.exit(0)

    # 3. 生成报告
    current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    
    prompt = f"""
    You are an expert financial analyst. Analyze the provided search data to create a "Morning Market Brief" for {current_date}.

    SOURCE DATA:
    {raw_context}

    INSTRUCTIONS:
    1. **Format**: Output PURE HTML code. "Swiss Style" design (Minimalist, Grid, Sans-serif).
       - NO Markdown code blocks (do not start with ```html).
       - Include internal CSS for clean styling.
    
    2. **Content Extraction**:
       - **Section 1: 🏛️ 权威要闻 (Market Facts)**
         - Select 20 verified news items from reliable sources (Gov, Sina, Reuters).
         - Focus on facts, policy, and earnings.
       - **Section 2: 🗣️ 市场传闻 (Market Rumors)**
         - Select 20 unverified rumors ("Little Compositions", market buzz).
         - Rank by discussion heat.
    
    3. **Writing Style**:
       - NO TITLES for items.
       - One sentence summary per item.
       - Language: Chinese (Simplified).
       - Numbered lists (1-20).

    4. **Structure**:
       - Header: "{current_date} 市场晨报"
       - Section 1 (Facts)
       - Section 2 (Rumors)
       - Footer: "Generated by AI Analysis"

    Generate the HTML now.
    """

    logger.info("🧠 AI 正在生成报告...")
    html_content = ""
    try:
        # 兼容不同的调用方法
        if hasattr(llm_client, 'chat'):
            # 标准 Gemini 库通常没有 chat 方法直接返回文本，而是返回对象，但我们的 wrapper 或者是项目 analyzer 可能有
            res = await llm_client.chat(prompt) if inspect.iscoroutinefunction(llm_client.chat) else llm_client.chat(prompt)
            # 处理返回值可能是对象的情况
            html_content = res if isinstance(res, str) else str(res)
        elif hasattr(llm_client, 'analyze'):
             # 项目可能的 analyze 方法
             try: res = await llm_client.analyze(prompt)
             except: res = await llm_client.analyze("000001", prompt) # 假 ticker
             html_
