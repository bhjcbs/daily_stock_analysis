import os
import asyncio
import logging
import smtplib
import traceback
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
import pytz

# 尝试导入原有项目模块
try:
    from config import Config
    from search_service import SearchService
    try:
        from analyzer import Analyzer as LLMAnalyzer 
    except ImportError:
        try:
            from analyzer import GeminiAnalyzer as LLMAnalyzer
        except ImportError:
            # 最后的尝试：导入 analyzer 模块中的任意 Analyzer 类
            import analyzer
            import inspect
            clsmembers = inspect.getmembers(analyzer, inspect.isclass)
            # 找名字里带 Analyzer 的类
            found = False
            for name, cls in clsmembers:
                if 'Analyzer' in name and 'Base' not in name:
                    LLMAnalyzer = cls
                    found = True
                    break
            if not found:
                raise ImportError("无法找到 Analyzer 类")
except ImportError as e:
    print(f"❌ 导入项目模块失败: {e}")
    exit(1)

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def send_email_debug(subject, html_content):
    """
    带详细调试信息的邮件发送函数
    """
    sender = os.getenv('EMAIL_SENDER')
    password = os.getenv('EMAIL_PASSWORD')
    receivers_str = os.getenv('EMAIL_RECEIVERS')
    
    logger.info("📧 [邮件调试] 准备发送邮件...")
    logger.info(f"   - 发件人: {sender}")
    logger.info(f"   - 收件人设置: {receivers_str}")
    
    if not sender or not password:
        logger.error("❌ [邮件调试] 失败: 环境变量 EMAIL_SENDER 或 EMAIL_PASSWORD 为空！")
        return False

    if not receivers_str:
        receivers = [sender]
        logger.info("   - 未指定收件人，默认发给发件人自己")
    else:
        receivers = [r.strip() for r in receivers_str.split(',')]

    # 智能匹配 SMTP 服务器
    smtp_server = "smtp.qq.com"
    smtp_port = 465 # SSL
    
    if "@163.com" in sender:
        smtp_server = "smtp.163.com"
    elif "@gmail.com" in sender:
        smtp_server = "smtp.gmail.com"
        smtp_port = 587 # Gmail 通常用 TLS
    elif "@sina.com" in sender:
        smtp_server = "smtp.sina.com"
    
    logger.info(f"   - SMTP服务器: {smtp_server}:{smtp_port}")

    try:
        message = MIMEMultipart()
        message['From'] = Header(sender, 'utf-8')
        message['To'] = Header(",".join(receivers), 'utf-8')
        message['Subject'] = Header(subject, 'utf-8')
        message.attach(MIMEText(html_content, 'html', 'utf-8'))

        logger.info("   - 正在连接 SMTP 服务器...")
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port)
            server.starttls()
        
        logger.info("   - 正在登录...")
        server.login(sender, password)
        
        logger.info("   - 正在发送数据...")
        server.sendmail(sender, receivers, message.as_string())
        server.quit()
        logger.info("✅ [邮件调试] 邮件发送成功！请检查收件箱和垃圾箱。")
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("❌ [邮件调试] 认证失败：请检查邮箱授权码（不是登录密码）是否正确，或是否开启了 SMTP 服务。")
    except Exception as e:
        logger.error(f"❌ [邮件调试] 发送异常: {e}")
        traceback.print_exc()
    return False

async def generate_morning_brief():
    print("="*50)
    logger.info("🚀 任务开始")
    
    # 1. 初始化
    try:
        cfg = Config()
        search_service = SearchService(cfg)
        llm_analyzer = LLMAnalyzer(cfg)
        logger.info("✅ 服务初始化完成")
    except Exception as e:
        logger.error(f"❌ 初始化失败: {e}")
        return

    # 2. 搜索
    search_queries = [
        "24小时内 中国股市 A股 港股 重大利好利空新闻",
        "latest China stock market news rumors last 24 hours",
        "权威财经媒体头条 24小时内 新浪财经 财联社",
    ]
    
    logger.info("🔍 开始搜索...")
    raw_context = ""
    for query in search_queries:
        try:
            # 尝试调用 search
            results = await search_service.search(query)
            # 简单检查结果是否有效
            if results:
                raw_context += f"\nQuery: {query}\nResults: {str(results)[:2000]}...\n" # 截断防止日志过长
        except Exception as e:
            logger.warning(f"   - 搜索 '{query}' 失败: {e}")

    logger.info(f"   - 搜索数据长度: {len(raw_context)} 字符")
    if len(raw_context) < 100:
        logger.error("❌ 搜索结果过少，停止生成。可能原因：API 额度耗尽或网络问题。")
        return

    # 3. AI 生成
    current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    prompt = f"""
    Generate a "Morning Market Brief" for {current_date} based on:
    {raw_context}
    
    Required:
    - 20 Facts (Reliable Sources)
    - 20 Rumors (Market Buzz)
    - Output RAW HTML code only (No markdown blocks).
    - Style: Swiss Design (Minimalist, Grid, Sans-serif).
    """

    logger.info("🧠 正在生成内容 (这可能需要 30 秒)...")
    html_content = ""
    try:
        # 兼容性调用
        if hasattr(llm_analyzer, 'chat'):
            html_content = await llm_analyzer.chat(prompt)
        elif hasattr(llm_analyzer, 'analyze'):
            html_content = await llm_analyzer.analyze(prompt)
        else:
             logger.error("❌ 无法找到 AI 分析方法 (chat 或 analyze)")
             return
    except Exception as e:
        logger.error(f"❌ AI 生成失败: {e}")
        return

    if not html_content:
        logger.error("❌ AI 返回内容为空")
        return
        
    html_content = html_content.replace("```html", "").replace("```", "").strip()
    logger.info(f"✅ 内容生成成功 (长度: {len(html_content)})")

    # 4. 发送邮件
    subject = f"【市场晨报】{current_date}"
    success = send_email_debug(subject, html_content)

    if not success:
        print("\n" + "!"*20 + " 邮件发送失败，备份内容如下 " + "!"*20)
        print(html_content)
        print("!"*60 + "\n")

if __name__ == "__main__":
    asyncio.run(generate_morning_brief())
