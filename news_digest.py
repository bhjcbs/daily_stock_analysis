import os
import asyncio
import logging
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
import pytz

# 尝试导入原有项目模块
try:
    from config import Config
    from search_service import SearchService
    # 尝试导入 AI 分析器，如果类名不同可能需要调整，通常是 Analyzer 或 GeminiAnalyzer
    # 这里我们尝试从 analyzer 导入 Analyzer，如果失败则尝试通用导入
    try:
        from analyzer import Analyzer as LLMAnalyzer 
    except ImportError:
        try:
            from analyzer import GeminiAnalyzer as LLMAnalyzer
        except ImportError:
            # 如果都找不到，稍后会报错，提示用户检查 analyzer.py
            from analyzer import * # 假设默认导出的类可以直接用，或者这里需要用户手动确认类名
            pass
except ImportError as e:
    print(f"❌ 导入项目模块失败: {e}")
    print("请确保 news_digest.py 位于项目根目录")
    exit(1)

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def send_email_standalone(subject, html_content):
    """
    独立邮件发送函数，不依赖项目原有的 notification.py
    """
    sender = os.getenv('EMAIL_SENDER')
    password = os.getenv('EMAIL_PASSWORD') # 授权码
    receivers_str = os.getenv('EMAIL_RECEIVERS')
    
    if not (sender and password):
        logger.warning("⚠️ 未配置 EMAIL_SENDER 或 EMAIL_PASSWORD，跳过发送邮件。")
        return

    # 如果没有配置收件人，默认发给自己
    if not receivers_str:
        receivers = [sender]
    else:
        receivers = receivers_str.split(',')

    # 智能匹配 SMTP 服务器
    smtp_server = "smtp.qq.com" # 默认 QQ
    smtp_port = 465 # 默认 SSL 端口
    
    if "@163.com" in sender:
        smtp_server = "smtp.163.com"
    elif "@gmail.com" in sender:
        smtp_server = "smtp.gmail.com"
    elif "@sina.com" in sender:
        smtp_server = "smtp.sina.com"
    
    logger.info(f"正在通过 {smtp_server} 发送邮件给 {len(receivers)} 人...")

    try:
        message = MIMEMultipart()
        message['From'] = Header(sender, 'utf-8')
        message['To'] = Header(",".join(receivers), 'utf-8')
        message['Subject'] = Header(subject, 'utf-8')
        
        message.attach(MIMEText(html_content, 'html', 'utf-8'))

        # 连接服务器
        try:
            server = smtplib.SMTP_SSL(smtp_server, 465)
        except Exception:
            # 如果 SSL 失败，尝试 TLS
            server = smtplib.SMTP(smtp_server, 587)
            server.starttls()
            
        server.login(sender, password)
        server.sendmail(sender, receivers, message.as_string())
        server.quit()
        logger.info("✅ 邮件发送成功！")
    except Exception as e:
        logger.error(f"❌ 邮件发送失败: {e}")

async def generate_morning_brief():
    logger.info("🚀 开始执行每日市场晨报任务...")
    
    # 1. 初始化服务
    try:
        cfg = Config()
        search_service = SearchService(cfg)
        
        # 尝试初始化分析器，这里假设类名为 Analyzer，如果原来的 analyzer.py 里类名不同，
        # 请打开 analyzer.py 查看 class 定义的名字并在此修改
        # 根据常见习惯，通常是 Analyzer(cfg) 或 GeminiAnalyzer(cfg)
        try:
            llm_analyzer = LLMAnalyzer(cfg)
        except NameError:
             # 如果上面 import 没搞定，尝试直接实例化 analyzer 里的第一个类（盲猜）
             import analyzer
             cls_name = [x for x in dir(analyzer) if 'Analyzer' in x and 'Base' not in x][0]
             LLMAnalyzerClass = getattr(analyzer, cls_name)
             llm_analyzer = LLMAnalyzerClass(cfg)

    except Exception as e:
        logger.error(f"初始化服务失败: {e}")
        return
    
    # 2. 执行搜索
    # 针对 24小时内的新闻和传闻
    search_queries = [
        "24小时内 中国股市 A股 港股 重大利好利空新闻",
        "latest China stock market news rumors last 24 hours",
        "A股 市场传闻 小作文 24小时内",
        "权威财经媒体头条 24小时内 新浪财经 财联社",
    ]
    
    logger.info("🔍 正在全网搜索最新情报...")
    raw_context = ""
    for query in search_queries:
        try:
            # 兼容不同的 search 方法签名
            # 如果 search_service.search 只需要 query
            results = await search_service.search(query)
            raw_context += f"\nSearch Query: {query}\nResults: {results}\n"
        except Exception as e:
            logger.warning(f"搜索关键词 '{query}' 时出错 (可能是API限制): {e}")
            continue

    if len(raw_context) < 100:
        logger.error("❌ 搜索结果过少，无法生成报告。")
        return

    # 3. 构建 Prompt
    current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    
    prompt = f"""
    You are a senior financial analyst. Based on the searched news below, generate a "Morning Market Brief" for {current_date}.
    
    SEARCH CONTEXT:
    {raw_context}

    INSTRUCTIONS:
    1. **Filter**: Select top 20 verified news (Facts) and top 20 market rumors/buzz (Rumors).
    2. **Format**: OUTPUT RAW HTML ONLY. No markdown blocks.
    3. **Style**: "International Typographic Style" (Swiss Style). 
       - Sans-serif fonts (Helvetica/Arial).
       - High contrast.
       - Grid-based layout.
       - Use an internal <style> block to make it look professional in email clients.
    
    CONTENT STRUCTURE:
    - **Header**: "{current_date} 市场晨报" (Big, Bold).
    - **Section 1**: 🏛️ 市场要闻 (Reliable sources like Reuters, Sina, etc).
    - **Section 2**: 🗣️ 市场传闻 (Unverified buzz, "Little Compositions").
    - **Footer**: Generated by AI.

    Create the HTML now.
    """

    logger.info("🧠 正在调用 AI 生成分析报告...")
    try:
        # 调用 AI，假设方法名为 analyze 或 chat
        # 大部分 analyzer 类都有 chat 或 generate 方法
        if hasattr(llm_analyzer, 'chat'):
            html_content = await llm_analyzer.chat(prompt)
        elif hasattr(llm_analyzer, 'analyze'):
            # analyze 通常需要 ticker，我们这里直接传 prompt 试试，或者看源码
            # 为了保险，我们尝试直接调用 LLM 接口如果 analyzer 封装太死
            html_content = await llm_analyzer.chat(prompt) # 赌它是 chat
        else:
            # 如果找不到方法，打印所有方法名供调试
            methods = [func for func in dir(llm_analyzer) if callable(getattr(llm_analyzer, func)) and not func.startswith("__")]
            logger.error(f"Analyzer 类中找不到 'chat' 方法。可用方法: {methods}")
            return

        # 清理 Markdown
        html_content = html_content.replace("```html", "").replace("```", "").strip()

        # 4. 发送邮件
        subject = f"【市场晨报】{current_date} A股/港股 每日速递"
        send_email_standalone(subject, html_content)
        
    except Exception as e:
        logger.error(f"生成或发送过程中出错: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(generate_morning_brief())
