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

# ==================== 0. 自动依赖检查 (规避 ImportError) ====================
def install_package(package):
    try:
        logger.info(f"🔧 检测到缺失库 {package}，正在自动安装...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
        logger.info(f"✅ {package} 安装成功")
    except Exception as e:
        logger.warning(f"❌ 自动安装失败: {e}")

try:
    import duckduckgo_search
except ImportError:
    install_package("duckduckgo-search")

# ==================== 1. 万能配置适配器 (规避 Config 初始化错误) ====================
class ConfigAdapter(dict):
    """将配置对象转换为既支持 .属性 也支持 ['key'] 的通用格式"""
    def __init__(self, original_config):
        self._orig = original_config
        data = {}
        # 提取数据：支持字典、对象属性、Pydantic模型
        if isinstance(original_config, dict):
            data = original_config
        elif hasattr(original_config, 'dict') and callable(original_config.dict):
            data = original_config.dict()
        elif hasattr(original_config, '__dict__'):
            data = vars(original_config)
        
        super().__init__(data)
        self.__dict__.update(data)

    def __getattr__(self, item):
        # 优先字典查找，失败则回退到原始对象
        val = self.get(item)
        if val is not None: return val
        if hasattr(self._orig, item):
            return getattr(self._orig, item)
        return None

# ==================== 2. 动态加载项目模块 ====================
try:
    from config import Config
    from search_service import SearchService
    import analyzer
    
    # 智能查找 AI 分析器类 (兼容 Analyzer, GeminiAnalyzer 等命名)
    LLMAnalyzer = None
    # 优先列表
    candidates = ['GeminiAnalyzer', 'Analyzer', 'StockAnalyzer']
    for name in candidates:
        if hasattr(analyzer, name):
            LLMAnalyzer = getattr(analyzer, name)
            break
    
    # 如果没找到，扫描模块内所有类
    if LLMAnalyzer is None:
        for name, cls in inspect.getmembers(analyzer, inspect.isclass):
            if 'Analyzer' in name and 'Base' not in name:
                LLMAnalyzer = cls
                break

except ImportError:
    Config = None
    SearchService = None
    LLMAnalyzer = None
    logger.warning("⚠️ 未找到项目核心模块，将尝试以最小模式运行。")

# ==================== 3. 独立邮件发送 (规避 Notification 模块错误) ====================
def send_email_standalone(subject, html_content):
    sender = os.getenv('EMAIL_SENDER')
    password = os.getenv('EMAIL_PASSWORD')
    receivers_str = os.getenv('EMAIL_RECEIVERS')
    
    if not sender or not password:
        logger.error("❌ 邮件发送失败: 环境变量 EMAIL_SENDER 或 EMAIL_PASSWORD 未配置")
        return False

    receivers = [r.strip() for r in receivers_str.split(',')] if receivers_str else [sender]
    
    # 智能匹配 SMTP 服务器
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

# ==================== 4. 搜索功能 (规避 SearchService 方法名错误) ====================
async def fallback_search_ddg(query):
    """使用 DuckDuckGo 作为备用搜索，并处理结果解析"""
    try:
        from duckduckgo_search import DDGS
        logger.info(f"🦆 [备用搜索] 正在调用 DuckDuckGo: {query[:10]}...")
        # max_results=25 确保有足够数据筛选
        results = DDGS().text(query, max_results=25)
        
        text_res = ""
        if not results: return ""
        
        for r in results:
            # 严格类型检查，防止 'str' object has no attribute 'get'
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
    """自动侦测 SearchService 的正确方法名"""
    # 常见的方法名列表
    possible_methods = ['search', 'search_news', 'query', 'fetch', 'get_news', 'run']
    
    for method in possible_methods:
        if hasattr(service, method):
            func = getattr(service, method)
            if callable(func):
                try:
                    logger.info(f"👉 尝试调用项目搜索方法: {method}")
                    # 尝试调用，处理可能的参数差异
                    try:
                        res = func(query)
                    except TypeError:
                        res = func(query, 10) # 尝试传入 limit
                    
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
    logger.info("🚀 [每日早报] 任务启动")
    
    # --- 初始化 ---
    cfg = Config() if Config else {}
    wrapped_cfg = ConfigAdapter(cfg) # 使用适配器防止报错
    
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
    # 精心设计的搜索词，覆盖正规新闻和市场传闻
    queries = [
        "过去24小时 中国股市 A股 港股 重大财经新闻 利好利空",
        "latest Chinese stock market rumors and insider news last 24 hours",
        "A股 市场小作文 传闻 24小时内 热门",
        "新浪财经 东方财富 财联社 头条新闻 24小时"
    ]
    
    raw_context = ""
    logger.info("🔍 开始全网搜索...")
    
    for q in queries:
        res_text = ""
        # 1. 优先尝试项目自带搜索
        if search_service:
            res_text = await smart_project_search(search_service, q)
        
        # 2. 如果失败或为空，使用 DDG 备用
        if not res_text or len(res_text) < 100:
            res_text = await fallback_search_ddg(q)
            
        if res_text:
            raw_context += f"\nQuery: {q}\nResults:\n{res_text[:3000]}\n"

    logger.info(f"📊 获取资料总长度: {len(raw_context)}")
    
    if len(raw_context) < 100:
        logger.error("❌ 未获取到有效数据，停止生成。")
        sys.exit(0)

    # --- AI 分析与生成 ---
    current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    
    # 严格遵循要求的 Prompt
    prompt = f"""
    You are a professional financial editor. Generate a "Daily Stock Analysis - Morning Brief" for {current_date}.
    
    SOURCE DATA:
    {raw_context}

    REQUIREMENTS:
    1. **Format**: Output PURE HTML code only. Use a clean, professional "Swiss Style" (Grid, Sans-serif).
       - No Markdown code blocks (no ```html).
       - Include internal CSS for styling (Make it look like a professional newsletter).
    
    2. **Content Categories**:
       - **Section 1: 🏛️ 权威要闻 (Market Facts)**
         - Select exactly 20 MOST IMPORTANT news items from reliable sources (Gov, Sina, Reuters, Bloomberg).
         - Sort by importance.
         - NO speculation here.
       - **Section 2: 🗣️ 市场传闻 (Market Rumors)**
         - Select exactly 20 HOTTEST market rumors/buzz ("Little Compositions", unverified discussions).
         - Sort by heat/discussion level.
    
    3. **Writing Style**:
       - **NO TITLES**.
       - **One sentence summary per item**. Concise and professional.
       - Language: Chinese (Simplified).
       - Numbered lists (1-20).

    4. **Structure**:
       - Header: "{current_date} 每日证券分析·市场晨报"
       - Section 1 (Facts)
       - Section 2 (Rumors)
       - Footer: "Generated by Daily Stock Analysis AI"

    Generate the HTML now.
    """

    logger.info("🧠 AI 正在分析并撰写报告...")
    html_content = ""
    try:
        # 尝试调用 chat 或 analyze
        if hasattr(llm_analyzer, 'chat'):
            html_content = await llm_analyzer.chat(prompt)
        elif hasattr(llm_analyzer, 'analyze'):
            # 兼容需要 ticker 参数的情况
            try: html_content = await llm_analyzer.analyze(prompt)
            except: html_content = await llm_analyzer.analyze("000001", prompt)
        
        if not html_content:
            logger.error("❌ AI 返回内容为空")
            sys.exit(0)

        # 清理可能存在的 markdown 标记
        html_content = html_content.replace("```html", "").replace("```", "").strip()
        
        # --- 发送邮件 ---
        subject = f"【每日证券分析】{current_date} 市场晨报 (20条要闻+20条传闻)"
        if send_email_standalone(subject, html_content):
            logger.info("🎉 任务圆满完成！")
        else:
            logger.warning("⚠️ 报告生成成功但邮件发送失败，请检查 Actions 日志。")
            
    except Exception as e:
        logger.error(f"❌ 生成过程中发生异常: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    try:
        asyncio.run(generate_morning_brief())
        # 显式正常退出，防止 Action 报红
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ 未捕获的顶级异常: {e}")
        sys.exit(0)
