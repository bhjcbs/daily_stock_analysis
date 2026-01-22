import os
import asyncio
import logging
import smtplib
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

# ==================== 1. 万能配置适配器 ====================
class ConfigAdapter(dict):
    def __init__(self, original_config):
        self._orig = original_config
        data = {}
        if isinstance(original_config, dict): data = original_config
        elif hasattr(original_config, '__dict__'): data = vars(original_config)
        super().__init__(data)
        self.__dict__.update(data)
    def __getattr__(self, item):
        val = self.get(item)
        if val is not None: return val
        if hasattr(self._orig, item): return getattr(self._orig, item)
        return None

# ==================== 2. 动态导入 ====================
try:
    from config import Config
    from search_service import SearchService
    import analyzer
    
    # 智能查找 Analyzer 类
    LLMAnalyzer = None
    if hasattr(analyzer, 'GeminiAnalyzer'): LLMAnalyzer = getattr(analyzer, 'GeminiAnalyzer')
    elif hasattr(analyzer, 'Analyzer'): LLMAnalyzer = getattr(analyzer, 'Analyzer')
    else:
        for name, cls in inspect.getmembers(analyzer, inspect.isclass):
            if 'Analyzer' in name and 'Base' not in name:
                LLMAnalyzer = cls; break
except ImportError:
    Config = None; SearchService = None; LLMAnalyzer = None
    logger.warning("⚠️ 运行在独立模式 (未找到项目模块)")

# ==================== 3. 工具函数 ====================
def send_email_debug(subject, html_content):
    sender = os.getenv('EMAIL_SENDER')
    password = os.getenv('EMAIL_PASSWORD')
    receivers_str = os.getenv('EMAIL_RECEIVERS')
    
    if not sender or not password:
        logger.error("❌ [邮件] 失败: 未配置 EMAIL_SENDER 或 EMAIL_PASSWORD")
        return False
    receivers = [r.strip() for r in receivers_str.split(',')] if receivers_str else [sender]
    
    smtp_server, smtp_port = "smtp.qq.com", 465
    if "@163.com" in sender: smtp_server = "smtp.163.com"
    elif "@gmail.com" in sender: smtp_server, smtp_port = "smtp.gmail.com", 587
    elif "@sina.com" in sender: smtp_server = "smtp.sina.com"

    try:
        msg = MIMEMultipart()
        msg['From'] = Header(sender, 'utf-8'); msg['To'] = Header(",".join(receivers), 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')
        msg.attach(MIMEText(html_content, 'html', 'utf-8'))
        
        s = smtplib.SMTP_SSL(smtp_server, smtp_port) if smtp_port == 465 else smtplib.SMTP(smtp_server, smtp_port)
        if smtp_port != 465: s.starttls()
        s.login(sender, password); s.sendmail(sender, receivers, msg.as_string()); s.quit()
        logger.info("✅ 邮件发送成功")
        return True
    except Exception as e:
        logger.error(f"❌ 邮件发送异常: {e}"); return False

async def fallback_search(query):
    try:
        from duckduckgo_search import DDGS
        logger.info(f"🦆 [备用] DuckDuckGo 搜索: {query[:10]}...")
        results = DDGS().text(query, max_results=10)
        text_res = ""
        if not results: return ""
        for r in results:
            if isinstance(r, dict):
                text_res += f"- Title: {r.get('title','')} \n  Snippet: {r.get('body', r.get('snippet',''))}\n"
            else: text_res += f"- {str(r)}\n"
        return text_res
    except ImportError:
        logger.error("❌ 未安装 duckduckgo-search (请运行 pip install duckduckgo-search)")
        return ""
    except Exception as e:
        logger.error(f"❌ DuckDuckGo 搜索失败: {e}"); return ""

# ==================== 4. 核心修复：自动侦测搜索方法 ====================
async def smart_search(service_instance, query):
    """
    自动侦测并调用 SearchService 中真正的搜索方法
    """
    # 1. 打印所有方法名供调试
    methods = [func for func in dir(service_instance) if callable(getattr(service_instance, func)) and not func.startswith("__")]
    logger.info(f"🔍 SearchService 可用方法: {methods}")

    # 2. 定义可能的搜索方法名优先级
    candidates = ['search_news', 'search', 'query', 'get_news', 'fetch', 'run']
    
    # 3. 尝试调用
    for method_name in candidates:
        if hasattr(service_instance, method_name):
            try:
                func = getattr(service_instance, method_name)
                logger.info(f"👉 尝试调用方法: {method_name}")
                
                # 检查参数数量，防止传参报错
                sig = inspect.signature(func)
                params = sig.parameters
                
                # 简单判断参数个数进行调用
                if len(params) == 1: # 只有一个参数 (self 除外)
                    res = func(query)
                elif len(params) >= 2: # 可能有 limit 或其他参数
                    try: res = func(query, 10) # 尝试传 limit
                    except: res = func(query)  # 失败则回退
                else:
                    continue # 无参数方法跳过

                if inspect.iscoroutine(res): res = await res
                if res: return str(res)
            except Exception as e:
                logger.warning(f"   调用 {method_name} 失败: {e}")
                
    logger.error("❌ 未能通过任何已知方法名成功调用搜索服务")
    return None

# ==================== 5. 主程序 ====================
async def generate_morning_brief():
    print("="*50)
    logger.info("🚀 任务开始")
    
    cfg = Config() if Config else {}
    
    # 初始化搜索服务
    search_service = None
    if SearchService:
        try: search_service = SearchService(ConfigAdapter(cfg))
        except: 
            try: search_service = SearchService(cfg)
            except: pass
    
    # 初始化 AI
    llm_analyzer = None
    if LLMAnalyzer:
        try: llm_analyzer = LLMAnalyzer(ConfigAdapter(cfg))
        except: llm_analyzer = LLMAnalyzer(cfg)

    # 搜索流程
    queries = [
        "24小时内 中国股市 A股 港股 重大利好利空新闻",
        "latest China stock market news rumors last 24 hours",
    ]
    
    raw_context = ""
    for query in queries:
        logger.info(f"Testing Query: {query}")
        result_text = ""
        
        # 1. 尝试原项目搜索 (带自适应侦测)
        if search_service:
            try:
                result_text = await smart_search(search_service, query)
            except Exception as e:
                logger.warning(f"智能搜索尝试失败: {e}")

        # 2. 备用搜索
        if not result_text or len(result_text) < 50:
            result_text = await fallback_search(query)
            
        if result_text:
            raw_context += f"\nQuery: {query}\nResults: {result_text[:2000]}\n"

    logger.info(f"📊 最终资料长度: {len(raw_context)}")
    if len(raw_context) < 50:
        logger.error("❌ 资料不足，停止生成。请先解决 '未安装 duckduckgo-search' 或检查 API Key。")
        return

    # 生成流程
    current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    prompt = f"""
    Generate a "Morning Market Brief" for {current_date} based on:
    {raw_context}
    Task: Select 20 Facts & 20 Rumors. Output RAW HTML. Swiss Design style.
    Sections: "🏛️ 市场要闻", "🗣️ 市场传闻".
    """

    logger.info("🧠 AI 正在生成...")
    try:
        html = ""
        if hasattr(llm_analyzer, 'chat'): html = await llm_analyzer.chat(prompt)
        elif hasattr(llm_analyzer, 'analyze'): 
             try: html = await llm_analyzer.analyze(prompt)
             except: html = await llm_analyzer.analyze("000001", prompt)
        
        if html:
            html = html.replace("```html", "").replace("```", "").strip()
            send_email_debug(f"【市场晨报】{current_date}", html)
        else:
            logger.error("❌ AI 返回空内容")
    except Exception as e:
        logger.error(f"❌ 生成失败: {e}")
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    asyncio.run(generate_morning_brief())
