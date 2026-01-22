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

# ==================== 1. 万能配置适配器 (核心修复) ====================
class ConfigAdapter(dict):
    """
    将任何配置对象包装成既支持 .key 也支持 ['key'] 和 .get('key') 的万能容器
    解决 'str object has no attribute get' 或 'Config object is not iterable' 等兼容性问题
    """
    def __init__(self, original_config):
        self._orig = original_config
        # 尝试将原始配置转换为字典数据
        data = {}
        if isinstance(original_config, dict):
            data = original_config
        elif hasattr(original_config, '__dict__'):
            data = vars(original_config)
        
        # 初始化字典父类
        super().__init__(data)
        # 同时支持属性访问
        self.__dict__.update(data)

    def __getattr__(self, item):
        # 优先从字典取，如果没有，尝试从原始对象取
        val = self.get(item)
        if val is not None: return val
        if hasattr(self._orig, item):
            return getattr(self._orig, item)
        return None

# ==================== 2. 动态导入 ====================
try:
    from config import Config
    from search_service import SearchService
    import analyzer
    
    # 智能查找 Analyzer 类
    LLMAnalyzer = None
    # 1. 优先找 GeminiAnalyzer
    if hasattr(analyzer, 'GeminiAnalyzer'):
        LLMAnalyzer = getattr(analyzer, 'GeminiAnalyzer')
    # 2. 其次找 Analyzer
    elif hasattr(analyzer, 'Analyzer'):
        LLMAnalyzer = getattr(analyzer, 'Analyzer')
    # 3. 暴力查找
    else:
        for name, cls in inspect.getmembers(analyzer, inspect.isclass):
            if 'Analyzer' in name and 'Base' not in name:
                LLMAnalyzer = cls
                break
except ImportError:
    Config = None
    SearchService = None
    LLMAnalyzer = None
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
    
    # 智能匹配 SMTP
    smtp_server, smtp_port = "smtp.qq.com", 465
    if "@163.com" in sender: smtp_server = "smtp.163.com"
    elif "@gmail.com" in sender: smtp_server, smtp_port = "smtp.gmail.com", 587
    elif "@sina.com" in sender: smtp_server = "smtp.sina.com"

    try:
        msg = MIMEMultipart()
        msg['From'] = Header(sender, 'utf-8')
        msg['To'] = Header(",".join(receivers), 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')
        msg.attach(MIMEText(html_content, 'html', 'utf-8'))

        if smtp_port == 465:
            s = smtplib.SMTP_SSL(smtp_server, smtp_port)
        else:
            s = smtplib.SMTP(smtp_server, smtp_port)
            s.starttls()
            
        s.login(sender, password)
        s.sendmail(sender, receivers, msg.as_string())
        s.quit()
        logger.info("✅ 邮件发送成功")
        return True
    except Exception as e:
        logger.error(f"❌ 邮件发送异常: {e}")
        return False

async def fallback_search(query):
    """
    使用 DuckDuckGo 进行免费备用搜索 (增强健壮性)
    """
    try:
        from duckduckgo_search import DDGS
        logger.info(f"🦆 [备用] DuckDuckGo 搜索: {query[:10]}...")
        # v4+ 版本 text() 返回的是 list[dict]
        results = DDGS().text(query, max_results=10)
        text_res = ""
        
        if not results:
            return ""

        for r in results:
            # 修复 'str' object has no attribute 'get'
            if isinstance(r, dict):
                title = r.get('title', 'No Title')
                body = r.get('body', r.get('snippet', ''))
                text_res += f"- Title: {title}\n  Snippet: {body}\n"
            elif isinstance(r, str):
                text_res += f"- {r}\n"
            else:
                text_res += f"- {str(r)}\n"
                
        return text_res
    except ImportError:
        logger.error("❌ 未安装 duckduckgo-search")
        return ""
    except Exception as e:
        logger.error(f"❌ DuckDuckGo 搜索失败: {e}")
        # 打印详细堆栈以便调试
        logger.error(traceback.format_exc())
        return ""

def init_analyzer_safely(config_obj):
    """
    使用适配器尝试初始化分析器
    """
    if LLMAnalyzer is None: return None
    
    # 1. 使用万能适配器 (ConfigAdapter)
    try:
        adapter = ConfigAdapter(config_obj)
        return LLMAnalyzer(adapter)
    except Exception:
        pass
        
    # 2. 尝试原始对象
    try:
        return LLMAnalyzer(config_obj)
    except Exception:
        pass

    # 3. 尝试空参
    try:
        return LLMAnalyzer()
    except Exception:
        pass
        
    return None

# ==================== 4. 主程序 ====================
async def generate_morning_brief():
    print("="*50)
    logger.info("🚀 任务开始")
    
    cfg = Config() if Config else {}
    
    # 初始化
    search_service = None
    if SearchService:
        try: search_service = SearchService(ConfigAdapter(cfg))
        except: 
            try: search_service = SearchService(cfg)
            except: pass
            
    llm_analyzer = init_analyzer_safely(cfg)
    
    if not llm_analyzer:
        logger.error("❌ 无法初始化 AI 分析器 (Analyzer)，任务终止。")
        return

    # 搜索
    search_queries = [
        "24小时内 中国股市 A股 港股 重大利好利空新闻",
        "latest China stock market news rumors last 24 hours",
        "权威财经媒体头条 24小时内 新浪财经 财联社",
    ]
    
    raw_context = ""
    
    for query in search_queries:
        logger.info(f"🔍 搜索: {query}")
        result_text = ""
        
        # 1. 原项目搜索
        if search_service:
            try:
                res = await search_service.search(query)
                if res: result_text = str(res)
            except Exception as e:
                logger.warning(f"   原项目搜索报错 (正常现象，切换备用): {e}")

        # 2. DuckDuckGo 备用
        if not result_text or len(result_text) < 50:
            result_text = await fallback_search(query)
            
        if result_text:
            raw_context += f"\nQuery: {query}\nResults: {result_text[:2000]}\n"

    logger.info(f"📊 搜索资料长度: {len(raw_context)}")
    
    if len(raw_context) < 50:
        logger.error("❌ 资料严重不足，停止生成。")
        return

    # 生成
    current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    prompt = f"""
    Generate a "Morning Market Brief" for {current_date} based on:
    {raw_context}
    
    Task:
    1. Select 20 Facts (Reliable Sources) and 20 Rumors (Market Buzz).
    2. Format as RAW HTML ONLY (No markdown blocks).
    3. Style: Swiss Design (Minimalist, Grid, Sans-serif).
    4. Sections: "🏛️ 市场要闻", "🗣️ 市场传闻".
    """

    logger.info("🧠 AI 正在生成...")
    try:
        html_content = ""
        # 尝试调用
        if hasattr(llm_analyzer, 'chat'):
            html_content = await llm_analyzer.chat(prompt)
        elif hasattr(llm_analyzer, 'analyze'):
            # 某些 analyze 可能需要 ticker 参数，做个假参数兼容
            try:
                html_content = await llm_analyzer.analyze(prompt)
            except TypeError:
                html_content = await llm_analyzer.analyze("000001", prompt)
        
        if html_content:
            html_content = html_content.replace("```html", "").replace("```", "").strip()
            subject = f"【市场晨报】{current_date}"
            send_email_debug(subject, html_content)
        else:
            logger.error("❌ AI 返回空内容")
            
    except Exception as e:
        logger.error(f"❌ 生成过程异常: {e}")
        # 打印详细堆栈，这行能帮你看到到底是哪行代码出的错
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    asyncio.run(generate_morning_brief())
