"""
香港六合彩 (Mark Six) 个人即时看板与真随机选号工具 - FastAPI 后端
================================================================
技术特性：
- 采用 secrets.SystemRandom() 操作系统底层真随机熵源选号 (1-49 无放回升序)
- 实时抓取香港马会 (HKJC) 官方最新预估头奖、多宝金额、当前投注额、期数与开奖时间
- 完整抓取上期搅珠详情：总投注额、中奖球号、头奖至七奖派彩明细 (浮动/固定奖金与中奖注数)
- 抓取往期 10 期搅珠历史记录（期数、开奖号码、特别号、头奖分配）
- 具备 1 小时内存缓存 (Thread-Safe)，网络故障时提供保底容灾数据
- 官方红/蓝/绿波色映射及双位格式化
- 账号密码鉴权系统 (可由环境变量开启/关闭)
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

PRIZE_NAMES = {
    1: "头奖",
    2: "二奖",
    3: "三奖",
    4: "四奖",
    5: "五奖",
    6: "六奖",
    7: "七奖",
}

FIXED_PRIZES = {
    4: 9600,
    5: 640,
    6: 320,
    7: 40
}

def format_prize(p: Dict[str, Any], unit_bet: int = 10) -> Dict[str, Any]:
    """格式化单项奖金及中奖注数"""
    p_type = p.get('type')
    div = p.get('dividend', '0')
    w_unit = p.get('winningUnit', 0)
    
    # 官方中奖注数计算: winningUnit / unitBet (例如 95/10 = 9.5 注)
    units_float = float(w_unit) / float(unit_bet) if unit_bet else float(w_unit) / 10.0
    units_str = f"{units_float:,.1f} 注" if units_float > 0 else "-"
    
    # 官方每注奖金
    div_int = int(div) if str(div).isdigit() else 0
    if div_int > 0:
        div_str = f"${div_int:,.0f}"
    elif p_type in FIXED_PRIZES:
        div_str = f"${FIXED_PRIZES[p_type]:,.0f}"
    else:
        div_str = "-"
        
    return {
        "type": p_type,
        "name": PRIZE_NAMES.get(p_type, f"{p_type}奖"),
        "is_floating": p_type in (1, 2, 3),
        "dividend": div_str,
        "winning_units": units_str,
        "raw_dividend": div_int,
        "raw_units": units_float
    }

# ==========================================
# 2. 爬虫与缓存逻辑 (HKJC 奖池与往期抓取)
# ==========================================
HKJC_GRAPHQL_URL = "https://info.cld.hkjc.com/graphql/base-get/"
HKJC_PAGE_URL = "https://bet.hkjc.com/marksix/index.aspx?lang=ch"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# 官方白名单：即期大盘接口
QUERY_CURRENT = """
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

# 官方白名单：往期历史开奖结果与奖项明细接口
QUERY_HISTORY = """
fragment lotteryDrawsFragment on LotteryDraw {
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

query marksixResult($lastNDraw: Int, $startDate: String, $endDate: String, $drawType: LotteryDrawType) {
    lotteryDraws(lastNDraw: $lastNDraw, startDate: $startDate, endDate: $endDate, drawType: $drawType) {
      ...lotteryDrawsFragment
    }
}
"""

_cache_lock = threading.Lock()
_info_cache: Optional[Dict[str, Any]] = None
_info_cache_time: float = 0.0

def format_date_with_weekday(date_str: str) -> str:
    """提取日期并格式化星期"""
    if not date_str:
        return "待公布"
    date_clean = date_str.split("+")[0].split("T")[0]
    weekday_str = ""
    try:
        dt = datetime.strptime(date_clean, "%Y-%m-%d")
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        weekday_str = f" ({weekdays[dt.weekday()]})"
    except Exception:
        pass
    return f"{date_clean}{weekday_str}"

def parse_draw_item(d: Dict[str, Any]) -> Dict[str, Any]:
    """解析单期开奖记录"""
    pool = d.get("lotteryPool") or {}
    res = d.get("drawResult") or {}
    year = d.get("year", "")
    no = d.get("no", "")
    short_year = str(year)[-2:] if year else ""
    draw_number = f"{short_year}/{str(no).zfill(3)}" if short_year and no else f"第 {no} 期"

    raw_nums = [int(x) for x in res.get("drawnNo", []) if str(x).isdigit()]
    special_num = int(res.get("xDrawnNo", 0)) if str(res.get("xDrawnNo", "")).isdigit() else None

    # 投注额
    tot_invest = pool.get("totalInvestment")
    tot_invest_val = int(tot_invest) if tot_invest and str(tot_invest).isdigit() else 0
    tot_invest_str = f"${tot_invest_val:,.0f}" if tot_invest_val > 0 else "-"

    # 奖项派彩
    unit_bet = pool.get("unitBet", 10)
    prizes = [format_prize(p, unit_bet) for p in pool.get("lotteryPrizes", [])]

    # 头奖中奖摘要
    first_prize = next((p for p in prizes if p["type"] == 1), None)
    fp_summary = "-"
    if first_prize and first_prize["winning_units"] != "-":
        fp_summary = f"{first_prize['dividend']} ({first_prize['winning_units']})"

    return {
        "id": d.get("id"),
        "draw_number": draw_number,
        "draw_date": format_date_with_weekday(d.get("drawDate", "")),
        "total_investment": tot_invest_str,
        "raw_investment": tot_invest_val,
        "numbers": [
            {"number": n, "text": f"{n:02d}", "color": get_ball_color(n)}
            for n in raw_nums
        ],
        "special_number": (
            {"number": special_num, "text": f"{special_num:02d}", "color": get_ball_color(special_num)}
            if special_num else None
        ),
        "prizes": prizes,
        "first_prize_summary": fp_summary
    }

def fetch_hkjc_data_from_network() -> Dict[str, Any]:
    """抓取马会六合彩即期大盘及往期中奖历史"""
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://bet.hkjc.com",
        "Referer": "https://bet.hkjc.com/"
    }

    upcoming_data = None
    past_draw_data = None
    history_draws = []

    # 1. 抓取下期即期数据 (marksixDraw)
    try:
        resp = requests.post(
            HKJC_GRAPHQL_URL,
            json={"query": QUERY_CURRENT},
            headers=headers,
            timeout=8
        )
        if resp.status_code == 200:
            res_json = resp.json()
            draws = res_json.get("data", {}).get("lotteryDraws", [])
            for d in draws:
                if d.get("status") in ["Defined", "StartSell"] or d.get("lotteryPool", {}).get("sell") is True:
                    upcoming_data = d
                elif d.get("status") == "Result" and not past_draw_data:
                    past_draw_data = d
            if not upcoming_data and draws:
                upcoming_data = draws[-1]
    except Exception as e:
        print(f"[MarkSix] 即期大盘 GraphQL 请求异常: {e}")

    # 2. 抓取往期 10 期历史中奖及派彩明细 (marksixResult)
    try:
        resp_hist = requests.post(
            HKJC_GRAPHQL_URL,
            json={"query": QUERY_HISTORY, "variables": {"lastNDraw": 10}},
            headers=headers,
            timeout=8
        )
        if resp_hist.status_code == 200:
            hist_json = resp_hist.json()
            raw_hist_draws = hist_json.get("data", {}).get("lotteryDraws", [])
            for d in raw_hist_draws:
                parsed = parse_draw_item(d)
                history_draws.append(parsed)
            # 若历史列表中有最新一期结果，更新 past_draw_data
            if history_draws:
                past_draw_data = history_draws[0]
    except Exception as e:
        print(f"[MarkSix] 往期历史 GraphQL 请求异常: {e}")

    # 如果 GraphQL 抓取到了下期大盘
    if upcoming_data:
        pool = upcoming_data.get("lotteryPool") or {}
        first_prize_div = pool.get("derivedFirstPrizeDiv")
        jackpot_raw = pool.get("jackpot")
        curr_invest_raw = pool.get("totalInvestment")
        
        # 预估头奖金额
        jackpot_val = 0
        if first_prize_div and str(first_prize_div).isdigit() and int(first_prize_div) > 0:
            jackpot_val = int(first_prize_div)
        elif jackpot_raw and str(jackpot_raw).isdigit() and int(jackpot_raw) > 0:
            jackpot_val = int(jackpot_raw)
        else:
            jackpot_val = 8000000

        # 多宝 / 金多宝金额
        snowball_val = int(jackpot_raw) if jackpot_raw and str(jackpot_raw).isdigit() else 0
        snowball_str = f"${snowball_val:,.0f}" if snowball_val > 0 else "-"

        # 当前已累计投注额
        curr_invest_val = int(curr_invest_raw) if curr_invest_raw and str(curr_invest_raw).isdigit() else 0
        curr_invest_str = f"${curr_invest_val:,.0f}" if curr_invest_val > 0 else "-"

        # 期数与日期
        year = upcoming_data.get("year", "")
        no = upcoming_data.get("no", "")
        short_year = str(year)[-2:] if year else ""
        draw_number = f"{short_year}/{str(no).zfill(3)}" if short_year and no else f"第 {no} 期"

        formatted_date = format_date_with_weekday(upcoming_data.get("drawDate", ""))
        close_date = upcoming_data.get("closeDate", "").split("+")[0].replace("T", " ")

        return {
            "success": True,
            "draw_number": draw_number,
            "draw_date": formatted_date,
            "close_date": close_date,
            "jackpot": f"${jackpot_val:,.0f}",
            "jackpot_amount": jackpot_val,
            "snowball_pool": snowball_str,
            "current_investment": curr_invest_str,
            "snowball_name": upcoming_data.get("snowballName_ch") or upcoming_data.get("snowballName_en") or "",
            "source": "hkjc_official_api",
            "past_draw": past_draw_data,
            "history": history_draws,
            "is_fallback": False
        }

    # 兜底默认值
    return {
        "success": False,
        "draw_number": "26/--",
        "draw_date": "每周二/四/六 21:30 (以马会为准)",
        "close_date": "开奖日 21:15 截止",
        "jackpot": "$8,000,000",
        "jackpot_amount": 8000000,
        "snowball_pool": "-",
        "current_investment": "-",
        "snowball_name": "",
        "source": "fallback_default",
        "past_draw": None,
        "history": [],
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

class LoginRequest(BaseModel):
    username: str = Field(..., description="管理员账号")
    password: str = Field(..., description="管理员密码")

def verify_token(
    authorization: Optional[str] = Header(None),
    x_token: Optional[str] = Header(None, alias="X-Token")
) -> Optional[str]:
    """检查用户 Token 鉴权"""
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
        sess["last_active"] = time.time()
        return sess["username"]

# ==========================================
# 4. FastAPI 应用主体初始化
# ==========================================
app = FastAPI(
    title="香港六合彩 (Mark Six) 个人即时看板与真随机选号工具",
    description="包含马会实时大盘、往期中奖派彩明细、Python secrets 物理真随机选号及鉴权服务。",
    version="1.1.0"
)

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
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_token:
        token = x_token.strip()

    if token:
        with SESSION_LOCK:
            ACTIVE_SESSIONS.pop(token, None)
    return {"success": True, "message": "已成功退出"}

@app.get("/api/info", summary="获取最新预估头奖、上期开奖派彩及往期历史")
def api_info(
    refresh: bool = Query(False, description="是否强制穿透缓存刷新"),
    current_user: str = Depends(verify_token)
):
    """
    抓取马会六合彩大盘信息：
    - 下期预估头奖、多宝金额、当前投注额、期数与开奖日期
    - 上期总投注额、中奖号码、头奖至七奖派彩明细 (每注奖金与中奖注数)
    - 往期历史 10 期开奖汇总
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
    """采用 secrets.SystemRandom() 从 1-49 中无放回抽取 6 个不重复号码并升序排列"""
    sys_rand = secrets.SystemRandom()
    pool = list(range(1, 50))

    bets = []
    for idx in range(1, count + 1):
        drawn = sys_rand.sample(pool, 6)
        drawn.sort()

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
