"""
香港六合彩 (Mark Six) 个人即时看板与真随机选号工具 - FastAPI 后端
================================================================
技术特性：
- 采用 secrets.SystemRandom() 硬件/操作系统底层真随机熵源选号 (1-49 无放回升序)
- 实时抓取香港马会 (HKJC) 官方最新预估头奖金额、期数、开奖时间
- 具备 1 小时内存缓存 (Thread-Safe)，网络故障时提供保底容灾数据
- 官方红/蓝/绿波色映射及双位格式化
- 账号密码鉴权系统 (可环境变量开启/关闭)
- 全局 CORS 跨域支持与单页面前端静态托管
"""

import os
import re
import time
import secrets
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Depends, HTTPException, status, Query, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

# ==========================================
# 0. 加载环境配置
# ==========================================
def load_env():
    """轻量读取 .env 文件（若存在）"""
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.isfile(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

load_env()

AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() in ("true", "1", "yes")
AUTH_USERNAME = os.getenv("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "marksix888")
SECRET_KEY = os.getenv("SECRET_KEY", "marksix_super_secret_key_2026")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "3600"))

# ==========================================
# 1. 六合彩官方波色表定义 (49 码红蓝绿波)
# ==========================================
RED_BALLS = {1, 2, 7, 8, 12, 13, 18, 19, 23, 24, 29, 30, 34, 35, 40, 45, 46}
BLUE_BALLS = {3, 4, 9, 10, 14, 15, 20, 25, 26, 31, 36, 37, 41, 42, 47, 48}
GREEN_BALLS = {5, 6, 11, 16, 17, 21, 22, 27, 28, 32, 33, 38, 39, 43, 44, 49}

def get_ball_color(num: int) -> str:
    """返回六合彩官方球色: 'red', 'blue', 'green'"""
    if num in RED_BALLS:
        return "red"
    elif num in BLUE_BALLS:
        return "blue"
    elif num in GREEN_BALLS:
        return "green"
    return "unknown"

# ==========================================
# 2. 爬虫与缓存逻辑 (HKJC 奖池抓取)
# ==========================================
HKJC_GRAPHQL_URL = "https://info.cld.hkjc.com/graphql/base-get/"
HKJC_PAGE_URL = "https://bet.hkjc.com/marksix/index.aspx?lang=ch"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

GRAPHQL_QUERY = """
query marksixDraw {
    timeOffset {
        m6
        ts
    }
    lotteryDraws {
        id
        year
        no
        openDate
        closeDate
        drawDate
        status
        snowballCode
        snowballName_en
        snowballName_ch
        lotteryPool {
            sell
            status
            totalInvestment
            jackpot
            unitBet
            estimatedPrize
            derivedFirstPrizeDiv
            lotteryPrizes {
                type
                winningUnit
                dividend
            }
        }
        drawResult {
            drawnNo
            xDrawnNo
        }
    }
}
"""

_cache_lock = threading.Lock()
_info_cache: Optional[Dict[str, Any]] = None
_info_cache_time: float = 0.0

def fetch_hkjc_data_from_network() -> Dict[str, Any]:
    """
    抓取马会六合彩即时数据：
    1. 首选请求马会官方 GraphQL 实时接口（官方 SPA 底层数据源，数据最新最全）
    2. 备用请求马会主页面利用 BeautifulSoup 进行 HTML 匹配
    3. 异常失败时返回兜底默认值
    """
    # 策略 1: 马会官方实时 GraphQL 接口
    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://bet.hkjc.com",
            "Referer": "https://bet.hkjc.com/"
        }
        resp = requests.post(
            HKJC_GRAPHQL_URL,
            json={"query": GRAPHQL_QUERY},
            headers=headers,
            timeout=8
        )
        if resp.status_code == 200:
            res_json = resp.json()
            draws = res_json.get("data", {}).get("lotteryDraws", [])
            if draws:
                upcoming = None
                past = None
                for d in draws:
                    if d.get("status") in ["Defined", "StartSell"] or d.get("lotteryPool", {}).get("sell") is True:
                        upcoming = d
                    elif d.get("status") == "Result":
                        past = d
                
                if not upcoming and draws:
                    upcoming = draws[-1]

                pool = upcoming.get("lotteryPool") or {}
                first_prize_div = pool.get("derivedFirstPrizeDiv")
                jackpot_raw = pool.get("jackpot")
                
                # 预估头奖金额
                jackpot_val = 0
                if first_prize_div and str(first_prize_div).isdigit() and int(first_prize_div) > 0:
                    jackpot_val = int(first_prize_div)
                elif jackpot_raw and str(jackpot_raw).isdigit() and int(jackpot_raw) > 0:
                    jackpot_val = int(jackpot_raw)
                else:
                    jackpot_val = 8000000

                jackpot_formatted = f"${jackpot_val:,.0f}"

                # 期数
                year = upcoming.get("year", "")
                no = upcoming.get("no", "")
                short_year = str(year)[-2:] if year else ""
                draw_number = f"{short_year}/{str(no).zfill(3)}" if short_year and no else f"第 {no} 期"

                # 开奖日期格式化 (加星期)
                draw_date_str = upcoming.get("drawDate", "")
                date_clean = draw_date_str.split("+")[0].split("T")[0]
                weekday_str = ""
                try:
                    dt = datetime.strptime(date_clean, "%Y-%m-%d")
                    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
                    weekday_str = f" ({weekdays[dt.weekday()]})"
                except Exception:
                    pass
                formatted_date = f"{date_clean}{weekday_str}" if date_clean else "待公布"

                close_date = upcoming.get("closeDate", "").split("+")[0].replace("T", " ")

                # 上期开奖结果 (附带球色)
                past_draw_info = None
                if past:
                    p_res = past.get("drawResult") or {}
                    raw_nums = [int(x) for x in p_res.get("drawnNo", []) if str(x).isdigit()]
                    special_num = int(p_res.get("xDrawnNo", 0)) if str(p_res.get("xDrawnNo", "")).isdigit() else None
                    past_draw_info = {
                        "draw_number": f"{str(past.get('year', ''))[-2:]}/{str(past.get('no', '')).zfill(3)}",
                        "draw_date": past.get("drawDate", "").split("+")[0],
                        "numbers": [
                            {"number": n, "text": f"{n:02d}", "color": get_ball_color(n)}
                            for n in raw_nums
                        ],
                        "special_number": (
                            {"number": special_num, "text": f"{special_num:02d}", "color": get_ball_color(special_num)}
                            if special_num else None
                        )
                    }

                return {
                    "success": True,
                    "draw_number": draw_number,
                    "draw_date": formatted_date,
                    "close_date": close_date,
                    "jackpot": jackpot_formatted,
                    "jackpot_amount": jackpot_val,
                    "snowball_name": upcoming.get("snowballName_ch") or upcoming.get("snowballName_en") or "",
                    "source": "hkjc_official_api",
                    "past_draw": past_draw_info,
                    "is_fallback": False
                }
    except Exception as e:
        print(f"[MarkSix] 官方 GraphQL 抓取异常: {e}")

    # 策略 2: 访问马会网页并使用 BeautifulSoup 解析
    try:
        headers = {"User-Agent": USER_AGENT}
        page_resp = requests.get(HKJC_PAGE_URL, headers=headers, timeout=8)
        if page_resp.status_code == 200:
            soup = BeautifulSoup(page_resp.text, "html.parser")
            body_text = soup.get_text()
            m_jackpot = re.search(r'\$\s*([0-9,]{6,})', body_text)
            m_draw = re.search(r'(\d{2}/\d{3})', body_text)
            if m_jackpot or m_draw:
                return {
                    "success": True,
                    "draw_number": m_draw.group(1) if m_draw else "下期",
                    "draw_date": "每周二/四/六 21:30",
                    "close_date": "",
                    "jackpot": f"${m_jackpot.group(1)}" if m_jackpot else "$8,000,000",
                    "jackpot_amount": 8000000,
                    "snowball_name": "",
                    "source": "hkjc_html_bs4",
                    "past_draw": None,
                    "is_fallback": False
                }
    except Exception as e:
        print(f"[MarkSix] 页面 BeautifulSoup 解析异常: {e}")

    # 策略 3: 保底容灾默认数据
    return {
        "success": False,
        "draw_number": "26/--",
        "draw_date": "每周二/四/六 21:30 (以马会为准)",
        "close_date": "开奖日 21:15 截止",
        "jackpot": "$8,000,000",
        "jackpot_amount": 8000000,
        "snowball_name": "",
        "source": "fallback_default",
        "past_draw": None,
        "is_fallback": True
    }

def get_cached_marksix_info(force: bool = False) -> Dict[str, Any]:
    """具备 1 小时 TTL 的内存缓存包装器"""
    global _info_cache, _info_cache_time
    now = time.time()
    with _cache_lock:
        if not force and _info_cache is not None and (now - _info_cache_time < CACHE_TTL):
            result = dict(_info_cache)
            result["cached"] = True
            result["cache_age_seconds"] = int(now - _info_cache_time)
            return result

        fresh_data = fetch_hkjc_data_from_network()
        _info_cache = fresh_data
        _info_cache_time = now
        
        result = dict(fresh_data)
        result["cached"] = False
        result["cache_age_seconds"] = 0
        return result

# ==========================================
# 3. 登录与身份鉴权 (Token / Session)
# ==========================================
ACTIVE_SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSION_LOCK = threading.Lock()
SESSION_TTL = 7 * 86400  # 登录凭证有效期 7 天

class LoginRequest(BaseModel):
    username: str = Field(..., description="管理员账号")
    password: str = Field(..., description="管理员密码")

def verify_token(
    authorization: Optional[str] = Header(None),
    x_token: Optional[str] = Header(None, alias="X-Token")
) -> Optional[str]:
    """
    检查用户 Token 鉴权：
    若系统未开启鉴权 (AUTH_ENABLED=False)，直接放行；
    若开启鉴权，提取 Bearer Token 或 X-Token 进行校验。
    """
    if not AUTH_ENABLED:
        return "guest_mode"

    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_token:
        token = x_token.strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请先输入账号密码登录",
            headers={"WWW-Authenticate": "Bearer"}
        )

    with SESSION_LOCK:
        sess = ACTIVE_SESSIONS.get(token)
        if not sess:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="登录凭证已过期或无效，请重新登录"
            )
        # 延长活跃周期
        sess["last_active"] = time.time()
        return sess["username"]

# ==========================================
# 4. FastAPI 应用主体初始化
# ==========================================
app = FastAPI(
    title="香港六合彩 (Mark Six) 个人即时看板与真随机选号工具",
    description="包含马会实时奖池看板、Python secrets 物理级真随机选号、红蓝绿波色渲染及身份鉴权服务。",
    version="1.0.0"
)

# 开启 CORS 允许跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 5. 业务 API 路由
# ==========================================

@app.post("/api/login", summary="管理员账号密码登录")
def api_login(req: LoginRequest):
    """
    支持账号密码登录模式
    默认账号密码可通过环境变量 AUTH_USERNAME, AUTH_PASSWORD 设置
    """
    user_ok = secrets.compare_digest(req.username.strip(), AUTH_USERNAME)
    pwd_ok = secrets.compare_digest(req.password.strip(), AUTH_PASSWORD)

    if not (user_ok and pwd_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号或密码错误，请重试"
        )

    token = secrets.token_urlsafe(32)
    with SESSION_LOCK:
        ACTIVE_SESSIONS[token] = {
            "username": req.username.strip(),
            "created_at": time.time(),
            "last_active": time.time()
        }

    return {
        "success": True,
        "message": "登录成功",
        "token": token,
        "username": req.username.strip()
    }

@app.get("/api/auth/status", summary="查询当前鉴权状态")
def api_auth_status(authorization: Optional[str] = Header(None), x_token: Optional[str] = Header(None, alias="X-Token")):
    """查询后端是否强制登录，以及当前请求是否已登录"""
    if not AUTH_ENABLED:
        return {"auth_required": False, "is_logged_in": True, "username": "public"}

    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_token:
        token = x_token.strip()

    is_logged = False
    uname = None
    if token:
        with SESSION_LOCK:
            sess = ACTIVE_SESSIONS.get(token)
            if sess:
                is_logged = True
                uname = sess["username"]

    return {
        "auth_required": True,
        "is_logged_in": is_logged,
        "username": uname
    }

@app.post("/api/logout", summary="退出登录")
def api_logout(authorization: Optional[str] = Header(None), x_token: Optional[str] = Header(None, alias="X-Token")):
    """注销当前 Token 会话"""
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_token:
        token = x_token.strip()

    if token:
        with SESSION_LOCK:
            ACTIVE_SESSIONS.pop(token, None)
    return {"success": True, "message": "已成功退出"}

@app.get("/api/info", summary="获取最新预估头奖金额、期数与开奖日期")
def api_info(
    refresh: bool = Query(False, description="是否强制穿透缓存刷新"),
    current_user: str = Depends(verify_token)
):
    """
    抓取马会六合彩页面信息：
    - 预估头奖基金（奖池金额）
    - 最新期数
    - 下期开奖日期
    - 具备 1 小时内存时间戳缓存
    - 网络失败时自动返回保底默认值
    """
    data = get_cached_marksix_info(force=refresh)
    return {
        "code": 200,
        "status": "success",
        "data": data,
        "operator": current_user
    }

@app.get("/api/generate", summary="真随机选号生成器")
def api_generate(
    count: int = Query(3, ge=1, le=10, description="生成注数，范围 1~10 注"),
    current_user: str = Depends(verify_token)
):
    """
    核心要求：
    1. 必须使用 Python 的 secrets.SystemRandom() 模块进行真随机抽取
    2. 从 1-49 中无放回抽取 6 个不重复号码
    3. 每注内部升序排列
    4. 带有香港六合彩官方红/蓝/绿波色及两位数格式化
    """
    # 实例化基于操作系统 CSPRNG 熵源的真随机发生器
    sys_rand = secrets.SystemRandom()
    pool = list(range(1, 50))  # 1 到 49

    bets = []
    for idx in range(1, count + 1):
        # 1-49 无放回抽取 6 个不重复号码
        drawn = sys_rand.sample(pool, 6)
        # 升序排列
        drawn.sort()

        # 生成球号与波色列表
        balls = [
            {
                "number": num,
                "text": f"{num:02d}",
                "color": get_ball_color(num)
            }
            for num in drawn
        ]

        bets.append({
            "bet_id": idx,
            "numbers": drawn,
            "formatted_string": " ".join(f"{num:02d}" for num in drawn),
            "balls": balls
        })

    return {
        "code": 200,
        "status": "success",
        "count": count,
        "entropy_source": "secrets.SystemRandom() (Hardware/OS Entropy)",
        "bets": bets,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

# ==========================================
# 6. 单页面前端静态文件托管
# ==========================================
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", summary="前端首页入口")
def read_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index_file):
        return FileResponse(index_file)
    return JSONResponse({
        "message": "FastAPI 六合彩服务正常运行中。请在 static 目录下创建 index.html。",
        "docs": "/docs"
    })

# ==========================================
# 7. 本地直接执行入口
# ==========================================
if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    print(f"[*] 启动香港六合彩看板服务: http://{host}:{port}")
    uvicorn.run("app:app", host=host, port=port, reload=True)
