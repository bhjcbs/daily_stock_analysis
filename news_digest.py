import os
import asyncio
import logging
import smtplib
import json
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
import pytz

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== 1. 动态导入与环境检查 ====================
try:
    from config import Config
    # 尝试导入 Tavily (项目依赖中通常有)
    try:
        from tavily import TavilyClient
    except ImportError:
        TavilyClient = None
    
    # 尝试导入 AI 分析器
    import analyzer
    LLMAnalyzer = None
    if hasattr(analyzer, 'GeminiAnalyzer'):
        LLMAnalyzer = getattr(analyzer, 'GeminiAnalyzer')
    elif hasattr(analyzer, 'Analyzer'):
        LLMAnalyzer = getattr(analyzer, 'Analyzer')
    else:
        # 暴力查找
        import inspect
        for name, cls in inspect.getmembers(analyzer, inspect.isclass):
            if 'Analyzer' in name and 'Base' not in name:
                LLMAnalyzer = cls
                break

except ImportError as e:
    logger.error(f"❌ 依赖导入失败: {e}")
    exit(1)

# ==================== 2. 独立搜索函数 (直连 API) ====================
async def direct_search(query):
    """
    直接调用 API 搜索，不经过项目内部逻辑封装，防止被过滤
    """
    results_text = ""
    
    # --- 优先尝试 Tavily (效果最好) ---
    tavily_key = os.getenv("TAVILY_API_KEYS") or os.getenv("TAVILY_API_KEY")
    if tavily_key and TavilyClient:
        try:
            logger.info("   -> 正在使用 Tavily 直连搜索...")
            # 处理多个 key 的情况，取第一个
            if "," in tavily_key: tavily_key = tavily_key.split(",")[0]
            
            client = TavilyClient(api_key=tavily_key)
            # advanced 模式适合搜新闻
            response = client.search(
                query=query, 
                search_depth="advanced", 
                topic="news", 
                days=1, 
                max_results=10
            )
            # 解析 Tavily 响应
            if isinstance(response, dict) and 'results' in response:
                for item in response['results']:
                    title = item.get('title', 'No Title')
                    content = item.get('content', '')
                    url = item.get('url', '')
                    results_text += f"- [{title}]({url}): {content}\n"
            logger.info(f"   -> Tavily 返回了 {len(results_text)} 字符")
            return results_text
        except Exception as e:
            logger.error(f"   -> Tavily 搜索失败: {e}")

    # --- 备选尝试: Bocha (博查) ---
    bocha_key = os.getenv("BOCHA_API_KEYS")
    if bocha_key and not results_text:
        try:
            logger.info("   -> 正在使用 Bocha 直连搜索...")
            import requests
            if "," in bocha_key: bocha_key = bocha_key.split(",")[0]
            
            headers = {"Authorization": f"Bearer {bocha_key}", "Content-Type": "application/json"}
            payload = {"query": query, "freshness": "oneDay", "count": 10}
            resp = requests.post("https://api.bochaai.com/v1/web-search", json=payload, headers=headers, timeout=10)
            
            if resp.status_code == 200:
                data = resp.json()
                if 'data' in data and 'webPages' in data['data']:
                    for item in data['data']['webPages']['value']:
                        results_text += f"- {item.get('name')} : {item.get('snippet')}\n"
            logger.info(f"   -> Bocha 返回了 {len(results_text)} 字符")
            return results_text
        except Exception as e:
            logger.error(f"   -> Bocha 搜索失败: {e}")

    return results_text

# ==================== 3. 智能初始化 ====================
def smart_init(cls, config_obj):
    try:
        return cls(config_obj)
    except:
        try:
            # 尝试传 dict
            cfg_dict = vars(config_obj) if hasattr(config_obj, '__dict__') else {}
            return cls(cfg_dict)
        except:
            return cls()

# ==================== 4. 邮件发送 ====================
def send_email_debug(subject, html_content):
    sender = os.getenv('EMAIL_SENDER')
    password = os.getenv('EMAIL_PASSWORD')
    receivers_str = os.getenv('EMAIL_RECEIVERS')
    
    if not sender or not password:
        logger.error("❌ 未配置邮箱 Secrets (EMAIL_SENDER/EMAIL_PASSWORD)")
        return False

    receivers = receivers_str.split(',') if receivers_str else [sender]
    
    # 自动识别 SMTP
    smtp_map = {
        "qq.com": ("smtp.qq.com", 465),
        "163.com": ("smtp.163.com", 465),
        "gmail.com": ("smtp.gmail.com", 587),
        "sina.com": ("smtp.sina.com", 465)
    }
    
    smtp_server, smtp_port = ("smtp.qq.com", 465) # 默认
    for domain, (server, port) in smtp_map.items():
        if domain in sender:
            smtp_server, smtp_port = server, port
            break

    try:
        msg = MIMEMultipart()
        msg['From'] = Header(sender, 'utf-8')
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
        logger.info(f"✅ 邮件已发送至: {receivers}")
        return True
    except Exception as e:
        logger.error(f"❌ 邮件发送失败: {e}")
        return False

# ==================== 5. 主程序 ====================
async def generate_morning_brief():
    logger.info("🚀 启动晨报生成任务...")
    
    # 初始化 AI 分析器
    cfg = Config()
    llm = smart_init(LLMAnalyzer, cfg)
    
    # 执行搜索 (使用直连模式)
    queries = [
        "A股 港股 昨夜今晨 重大财经新闻 政策利好",
        "China stock market rumors and insider news last 24h",
        "财联社 证券时报 头条新闻 摘要",
    ]
    
    raw_context = ""
    for q in queries:
        res = await direct_search(q)
        if res:
            raw_context += f"\n=== {q} ===\n{res}\n"
    
    # 检查搜索结果
    if len(raw_context) < 50:
        logger.error("❌ 搜索结果依然过少。原因分析：")
        logger.error("1. 请检查 GitHub Secrets 中是否配置了 TAVILY_API_KEYS")
        logger.error("2. 检查 Tavily 是否有额度")
        logger.error("3. 如果没有 Key，脚本无法获取新闻。")
        
        # 兜底：如果没有搜索结果，尝试让 AI 仅凭自身知识库生成（虽然不推荐，但比报错好）
        logger.warning("⚠️ 尝试使用 AI 自身知识库进行兜底生成...")
        raw_context = "System: Search failed. Please generate a general market outlook based on your internal knowledge cutoff, explicitly stating data might be outdated."

    # 生成内容
    current_date = datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d')
    prompt = f"""
    You are a professional financial editor. Generate a "Morning Market Brief" for {current_date}.
    
    SOURCE DATA:
    {raw_context[:10000]} 

    TASK:
    Create a clean HTML email newsletter.
    
    STRUCTURE & CONTENT:
    1. **Heading**: "{current_date} 市场晨报"
    2. **Section 1: 🏛️ 市场要闻 (Facts)**
       - List 15-20 verified news items from the source data.
       - Focus on regulations, major company moves, and macroeconomics.
    3. **Section 2: 🗣️ 市场传闻 (Rumors)**
       - List 15-20 buzz/rumors/speculations ("小作文").
       - If source data is thin, generalize common market sentiments.
    
    STYLE (CRITICAL):
    - **Format**: RAW HTML only (no markdown code blocks).
    - **Design**: "Swiss Style" (International Typographic Style).
    - **CSS**: Use internal <style>. Font: Helvetica/Arial. Minimalist borders. High contrast black/white.
    - **Items**: Use numbered lists <ol>. One sentence per item.
    
    Generate the HTML now.
    """

    logger.info("🧠 正在生成分析报告...")
    try:
        # 兼容调用
        if hasattr(llm, 'chat'):
            content = await llm.chat(prompt)
        elif hasattr(llm, 'analyze'):
            try:
                content = await llm.analyze(prompt)
            except:
                content = await llm.analyze("MARKET_BRIEF", prompt)
        else:
            logger.error("❌ 无法调用 AI 方法")
            return

        # 清洗结果
        content = content.replace("```html", "").replace("```", "").strip()
        
        # 发送
        send_email_debug(f"【市场晨报】{current_date}", content)
        
    except Exception as e:
        logger.error(f"❌ 生成过程异常: {e}")

if __name__ == "__main__":
    asyncio.run(generate_morning_brief())
