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
        logger.info(f"🔧 自动安装依赖: {package}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
    except Exception as e:
        logger.warning(f"❌ 安装 {package} 失败: {e}")

try:
    import duckduckgo_search
except ImportError:
    install_package("duckduckgo-search")

try:
    import google.generativeai as genai
except ImportError:
    install_package("google-generativeai")
    import google.generativeai as genai

# ==================== 1. 内置独立 Gemini 客户端 ====================
class DirectGeminiClient:
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            # 尝试从参数或 env 文件读取，或者直接报错
            raise ValueError("未配置 GEMINI_API_KEY")
        
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel('gemini-1.5-flash')
        logger.info("💎 [独立模式] Gemini 客户端初始化成功")

    async def chat(self, prompt):
        try:
            # generate_content 是同步的，但在 asyncio 中通常可以接受
            response = self.model.generate_content(prompt)
            return response.text
        except Exception as e:
            logger.error(f"❌ Gemini API 错误: {e}")
            return None

# ==================== 2. 万能配置适配器 ====================
class ConfigAdapter(dict):
    def __init__(self, original_config):
        self._orig = original_config
        data = {}
        if isinstance(original_config, dict): data = original_config
        elif hasattr(original_config, 'dict'): data = original_config.dict()
        elif hasattr(original_config, '__dict__'): data = vars(original_config)
        super().__init__(data)
        self.__dict__.update(data)

    def __getattr__(self, item):
        val = self.get(item)
        if val is not None: return val
        if hasattr(self._orig, item): return getattr(self._orig, item)
        return None

# ==================== 3. 邮件发送 ====================
def send_email_standalone(subject, html_content):
    sender = os.getenv('EMAIL_SENDER')
    password = os.getenv('EMAIL_PASSWORD')
    receivers_str = os.getenv('EMAIL_RECEIVERS')
    
    if not sender or not password:
        logger.error("❌ 邮件发送失败: 环境变量缺失")
        return False

    receivers = [r.strip() for r in receivers_str.split(',')] if receivers_str else [sender]
    
    # 智能匹配 SMTP
    smtp_server, smtp_port = "smtp.qq.com", 465
    if "@163.com" in sender: smtp_server = "smtp.163.com"
    elif "@gmail.com" in sender: smtp_server, smtp_port = "smtp.gmail.com", 587
    elif "@sina.com" in sender: smtp_server = "smtp.sina.com"

    try:
        msg = MIMEMultipart()
        msg['From'] = Header(f"Daily Market Brief <{sender}>", 'utf-8')
        msg['To'] = Header(",".join(receivers), 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')
        msg.attach(MIMEText(html_content, 'html', 'utf-8'))

        server = smtplib.SMTP_SSL(smtp_server, smtp_port) if smtp_port == 465 else smtplib.SMTP(smtp_server, smtp_port)
        if smtp_port != 465: server.starttls()
            
        server.login(sender, password)
        server.sendmail(sender, receivers, msg.as_string())
        server.quit()
        logger.info(f"✅ 邮件发送成功 ({len(receivers)} 人)")
        return True
    except Exception as e:
        logger.error(f"❌ 邮件发送异常: {e}")
        return False

# ==================== 4. 搜索模块 ====================
async def fallback_search_ddg(query):
    try:
        from duckduckgo_search import DDGS
        logger.info(f"🦆 [DDG] 搜索: {query[:10]}...")
        results = DDGS().text(query, max_results=20)
        text_res = ""
        if not results: return ""
        for r in results:
            if isinstance(r, dict):
                text_res += f"Src: {r.get('title','?')}\nTxt: {r.get('body', r.get('snippet',''))}\n---\n"
            else:
                text_res += f"{str(r)}\n---\n"
        return text_res
    except Exception as e:
        logger.error(f"❌ DDG 搜索失败: {e}")
        return ""

# ==================== 5. 主程序 ====================
async def generate_morning_brief():
    print("="*60)
    logger.info("🚀 任务启动")
    
    # --- 1. 准备 AI 客户端 ---
    llm_client = None
    # 尝试加载项目原有代码
    try:
        from config import Config
        import analyzer
        cfg = Config() if Config else {}
        # 寻找 Analyzer 类
        AnalyzerCls = None
        for name in ['GeminiAnalyzer', 'GoogleGeminiAnalyzer', 'Analyzer']:
            if hasattr(analyzer, name):
                AnalyzerCls = getattr(analyzer, name)
                break
        if not AnalyzerCls:
             for name, cls in inspect.getmembers(analyzer, inspect.isclass):
                if 'Analyzer' in name: AnalyzerCls = cls; break
        
        if AnalyzerCls:
            try: llm_client = AnalyzerCls(ConfigAdapter(cfg))
            except: llm_client = AnalyzerCls(cfg)
            logger.info("✅ 使用项目原生 AI 分析器")
    except Exception as e:
        logger.warning(f"⚠️ 项目模块加载受限: {e}")

    # 兜底：使用独立 Gemini 客户端
    if not llm_client:
        try:
            llm_client = DirectGeminiClient()
        except Exception as e:
            logger.error(f"❌ 无法初始化任何 AI 客户端: {e}")
            sys.exit(0)

    # --- 2. 执行搜索 ---
    queries = [
        "过去24小时 中国股市 A股 港股 重大财经新闻 利好利空",
        "latest China stock market rumors and insider news last 24 hours",
        "A股 市场小作文 传闻 24小时内 热门",
        "新浪财经 东方财富 财联社 头条新闻 24小时"
    ]
    
    raw_context = ""
    for q in queries:
        # 这里简化逻辑，直接使用稳定的 DDG，避免项目 SearchService 的兼容性地狱
        # 除非确定项目 SearchService 可用，否则 DDG 足够且更稳定
        res = await fallback_search_ddg(q)
        if res:
            raw_context += f"\nQuery: {q}\nResults:\n{res[:3000]}\n"

    logger.info(f"📊 资料长度: {len(raw_context)}")
    if len(raw_context) < 50:
        logger.error("❌ 搜索无结果")
        sys.exit(0)

    # --- 3. 生成报告 ---
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

    logger.info("🧠 AI 正在生成...")
    html_content = ""
    
    # 独立的 try-except 块处理 AI 生成，防止语法错误
    try:
        res = None
        if hasattr(llm_client, 'chat'):
            if inspect.iscoroutinefunction(llm_client.chat):
                res = await llm_client.chat(prompt)
            else:
                res = llm_client.chat(prompt)
        elif hasattr(llm_client, 'analyze'):
            if inspect.iscoroutinefunction(llm_client.analyze):
                try: res = await llm_client.analyze(prompt)
                except: res = await llm_client.analyze("000001", prompt)
            else:
                res = llm_client.analyze(prompt)
        elif hasattr(llm_client, 'generate_content'): # 原生 model 对象
            res = llm_client.generate_content(prompt).text
        
        # 统一处理结果
        if res:
            html_content = res if isinstance(res, str) else str(res)
            
    except Exception as e:
        logger.error(f"❌ 生成失败: {e}")
        traceback.print_exc()
        sys.exit(0)

    if not html_content:
        logger.error("❌ AI 返回内容为空")
        sys.exit(0)

    # 清洗
    html_content = html_content.replace("```html", "").replace("```", "").strip()

    # --- 4. 发送 ---
    subject = f"【市场晨报】{current_date}"
    if send_email_standalone(subject, html_content):
        logger.info("🎉 流程结束")
    else:
        logger.warning("⚠️ 邮件发送失败")

if __name__ == "__main__":
    try:
        asyncio.run(generate_morning_brief())
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ 运行异常: {e}")
        sys.exit(0)
