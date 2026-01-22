import os
import asyncio
import logging
import smtplib
import traceback
import inspect
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
import pytz

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== 1. 动态导入模块 ====================
try:
    from config import Config
    from search_service import SearchService
    
    # 尝试多种方式导入 AI 分析器
    LLMAnalyzer = None
    import analyzer
    # 优先找 GeminiAnalyzer (项目默认)
    if hasattr(analyzer, 'GeminiAnalyzer'):
        LLMAnalyzer = getattr(analyzer, 'GeminiAnalyzer')
    # 其次找 Analyzer
    elif hasattr(analyzer, 'Analyzer'):
        LLMAnalyzer = getattr(analyzer, 'Analyzer')
    else:
        # 最后通过检查类名查找
        clsmembers = inspect.getmembers(analyzer, inspect.isclass)
        for name, cls in clsmembers:
            if 'Analyzer' in name and 'Base' not in name:
                LLMAnalyzer = cls
                break
    
    if LLMAnalyzer is None:
        raise ImportError("未找到合适的 Analyzer 类")

except ImportError as e:
    logger.error(f"❌ 导入项目模块失败: {e}")
    logger.error("请确保 news_digest.py 位于项目根目录")
    exit(1)

# ==================== 2. 邮件发送逻辑 ====================
def send_email_debug(subject, html_content):
    """
    带详细调试信息的邮件发送函数
    """
    sender = os.getenv('EMAIL_SENDER')
    password = os.getenv('EMAIL_PASSWORD')
    receivers_str = os.getenv('EMAIL_RECEIVERS')
    
    logger.info("📧 [邮件调试] 准备发送邮件...")
    
    if not sender or not password:
        logger.error("❌ [邮件调试] 失败: 环境变量 EMAIL_SENDER 或 EMAIL_PASSWORD 为空！")
        return False

    if not receivers_str:
        receivers = [sender]
    else:
        receivers = [r.strip() for r in receivers_str.split(',')]

    # 智能匹配 SMTP 服务器
    smtp_server = "smtp.qq.com"
    smtp_port = 465 
    
    if "@163.com" in sender:
        smtp_server = "smtp.163.com"
    elif "@gmail.com" in sender:
        smtp_server = "smtp.gmail.com"
        smtp_port = 587
    elif "@sina.com" in sender:
        smtp_server = "smtp.sina.com"
    
    try:
        message = MIMEMultipart()
        message['From'] = Header(sender, 'utf-8')
        message['To'] = Header(",".join(receivers), 'utf-8')
        message['Subject'] = Header(subject, 'utf-8')
        message.attach(MIMEText(html_content, 'html', 'utf-8'))

        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port)
            server.starttls()
            
        server.login(sender, password)
        server.sendmail(sender, receivers, message.as_string())
        server.quit()
        logger.info("✅ [邮件调试] 邮件发送成功！")
        return True
    except Exception as e:
        logger.error(f"❌ [邮件调试] 发送异常: {e}")
        return False

# ==================== 3. 核心修复：智能初始化 ====================
def smart_init(cls, config_obj, name="Unknown"):
    """
    尝试多种方式初始化类，解决 'Config object is not iterable' 问题
    """
    # 尝试 1: 直接传递 Config 对象 (标准做法)
    try:
        instance = cls(config_obj)
        logger.info(f"✅ {name} 初始化成功 (Method: Object)")
        return instance
    except Exception as e:
        # 忽略非类型错误，继续尝试
        pass

    # 尝试 2: 传递 Config 的字典形式 (vars 或 __dict__)
    # 解决 'not iterable' 错误的核心尝试
    try:
        config_dict = vars(config_obj) if hasattr(config_obj, '__dict__') else {}
        if not config_dict and hasattr(config_obj, 'dict'): # 兼容 Pydantic
             config_dict = config_obj.dict()
             
        instance = cls(config_dict)
        logger.info(f"✅ {name} 初始化成功 (Method: Dict)")
        return instance
    except Exception as e:
        pass

    # 尝试 3: 不传参数 (有些类会自动读取环境变量)
    try:
        instance = cls()
        logger.info(f"✅ {name} 初始化成功 (Method: No Args)")
        return instance
    except Exception as e:
        logger.error(f"❌ {name} 初始化失败，所有方法均尝试无效。")
        logger.error(f"   最后一次报错: {e}")
        raise e

# ==================== 4. 主程序 ====================
async def generate_morning_brief():
    print("="*50)
    logger.info("🚀 任务开始")
    
    # --- 初始化阶段 ---
    try:
        cfg = Config()
        # 使用智能初始化修复报错
        search_service = smart_init(SearchService, cfg, "SearchService")
        llm_analyzer = smart_init(LLMAnalyzer, cfg, "LLMAnalyzer")
    except Exception as e:
        logger.error(f"❌ 服务初始化致命错误: {e}")
        return

    # --- 搜索阶段 ---
    search_queries = [
        "24小时内 中国股市 A股 港股 重大利好利空新闻",
        "latest China stock market news rumors last 24 hours",
        "权威财经媒体头条 24小时内 新浪财经 财联社",
    ]
    
    logger.info("🔍 开始搜索...")
    raw_context = ""
    for query in search_queries:
        try:
            # 兼容 search 方法可能需要不同参数的情况
            try:
                results = await search_service.search(query)
            except TypeError:
                # 假如 search 需要其他参数，这里做一个最简单的降级
                results = await search_service.search(query, 10) # 假设需要 limit 参数

            if results:
                raw_context += f"\nQuery: {query}\nResults: {str(results)[:1500]}...\n"
        except Exception as e:
            logger.warning(f"   - 搜索 '{query}' 失败: {e}")

    logger.info(f"   - 搜索数据长度: {len(raw_context)} 字符")
    if len(raw_context) < 50:
        logger.error("❌ 搜索结果过少，停止生成。")
        return

    # --- 生成阶段 ---
    current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    prompt = f"""
    Generate a "Morning Market Brief" for {current_date} based on:
    {raw_context}
    
    Task:
    1. Select 20 Facts (Reliable Sources) and 20 Rumors (Market Buzz).
    2. Format as RAW HTML ONLY (No markdown blocks like ```html).
    3. Style: Swiss Design (Minimalist, Grid, Sans-serif), suitable for email.
    4. Sections: "🏛️ 市场要闻", "🗣️ 市场传闻".
    """

    logger.info("🧠 正在生成内容...")
    html_content = ""
    try:
        # 智能调用 analyze 或 chat
        if hasattr(llm_analyzer, 'chat'):
            html_content = await llm_analyzer.chat(prompt)
        elif hasattr(llm_analyzer, 'analyze'):
             # 有些 analyze 方法需要 ticker 参数，我们尝试只传 prompt
            try:
                html_content = await llm_analyzer.analyze(prompt)
            except TypeError:
                 # 如果必须传 ticker，传一个假的
                html_content = await llm_analyzer.analyze("000001", prompt)
        else:
             logger.error("❌ AI 类没有找到 chat 或 analyze 方法")
             return
    except Exception as e:
        logger.error(f"❌ AI 生成失败: {e}")
        return

    if not html_content: return
    html_content = html_content.replace("```html", "").replace("```", "").strip()

    # --- 发送阶段 ---
    subject = f"【市场晨报】{current_date}"
    success = send_email_debug(subject, html_content)
    
    if not success:
        logger.warning("请检查 Actions 日志中的[邮件调试]部分")

if __name__ == "__main__":
    asyncio.run(generate_morning_brief())
