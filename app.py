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
- 【新增】每期随机模拟下注系统：自动生成 1 注真随机号码，对决当期开奖，记录盈亏与中奖
- 【新增】管理员固定号码模拟下注系统：支持配置多注固定心水号，每期自动追投对奖
- 【新增】持久化 SQLite 模拟记账层与统计报表（命中率、ROI回报率、奖项分布统计）
"""

import os
import re
import time
import json
import sqlite3
import secrets
import threading
from datetime import datetime
from contextlib import contextmanager
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
# 1. 六合彩官方波色表与中奖规则定义 (49 码红蓝绿波)
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

def check_prize(
    bet_numbers: List[int],
    draw_numbers: List[int],
    special_num: Optional[int],
    draw_prizes: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """根据香港六合彩官方中奖规则计算中奖等级与奖金"""
    matched_regular = sorted(list(set(bet_numbers) & set(draw_numbers)))
    matched_count = len(matched_regular)
    matched_special = 1 if (special_num is not None and special_num in bet_numbers) else 0

    prize_type = 0
    prize_name = "未中奖"
    dividend = 0.0

    if matched_count == 6:
        prize_type = 1
        prize_name = "头奖"
    elif matched_count == 5 and matched_special:
        prize_type = 2
        prize_name = "二奖"
    elif matched_count == 5:
        prize_type = 3
        prize_name = "三奖"
    elif matched_count == 4 and matched_special:
        prize_type = 4
        prize_name = "四奖"
        dividend = 9600.0
    elif matched_count == 4:
        prize_type = 5
        prize_name = "五奖"
        dividend = 640.0
    elif matched_count == 3 and matched_special:
        prize_type = 6
        prize_name = "六奖"
        dividend = 320.0
    elif matched_count == 3:
        prize_type = 7
        prize_name = "七奖"
        dividend = 40.0

    # 前三奖浮动奖金：优先采用官方开奖当期派彩
    if prize_type in (1, 2, 3):
        if draw_prizes:
            for p in draw_prizes:
                if p.get("type") == prize_type:
                    raw_div = p.get("raw_dividend", 0)
                    if raw_div and raw_div > 0:
                        dividend = float(raw_div)
                        break
        # 若未公布或暂无数据，使用马会基准保障奖金
        if dividend == 0.0:
            benchmark = {1: 8000000.0, 2: 1500000.0, 3: 80000.0}
            dividend = benchmark.get(prize_type, 0.0)

    cost = 10.0  # 每注标准港币 10 元
    net_profit = dividend - cost

    return {
        "matched_numbers": matched_regular,
        "matched_count": matched_count,
        "matched_special": matched_special,
        "prize_type": prize_type,
        "prize_name": prize_name,
        "cost": cost,
        "prize_dividend": dividend,
        "net_profit": net_profit
    }

# ==========================================
# 2. 模拟下注持久化数据层 (SQLite)
# ==========================================
DB_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DB_DIR, "marksix.db")

@contextmanager
def get_db():
    """安全获取 SQLite 连接上下文管理器"""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    """初始化 SQLite 数据库架构"""
    with get_db() as conn:
        cursor = conn.cursor()
        # 1. 模拟设置
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # 2. 管理员设定的固定号码 (支持多注)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fixed_bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                numbers TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)
        # 3. 历史搅珠记录缓存 (供结算和复盘)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS draw_records (
                draw_number TEXT PRIMARY KEY,
                draw_date TEXT NOT NULL,
                numbers TEXT NOT NULL,
                special_number INTEGER,
                total_investment TEXT,
                prizes TEXT,
                created_at TEXT NOT NULL
            )
        """)
        # 4. 模拟下注记录账本 (固定号码 + 随机模拟)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS simulated_bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draw_number TEXT NOT NULL,
                draw_date TEXT NOT NULL,
                bet_type TEXT NOT NULL,  -- 'fixed' or 'random'
                fixed_bet_id INTEGER DEFAULT 0,
                bet_name TEXT NOT NULL,
                numbers TEXT NOT NULL,
                matched_numbers TEXT NOT NULL,
                matched_count INTEGER DEFAULT 0,
                matched_special INTEGER DEFAULT 0,
                prize_type INTEGER DEFAULT 0,
                prize_name TEXT DEFAULT '未中奖',
                cost REAL DEFAULT 10.0,
                prize_dividend REAL DEFAULT 0.0,
                net_profit REAL DEFAULT -10.0,
                created_at TEXT NOT NULL,
                UNIQUE(draw_number, bet_type, fixed_bet_id)
            )
        """)
        # 默认启用每期随机模拟下注
        cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('random_sim_enabled', '1')")
        conn.commit()

init_db()

def sync_draw_records(history_draws: List[Dict[str, Any]]):
    """将抓取到的开奖历史持久化到 draw_records，并自动结算模拟下注"""
    if not history_draws:
        return
    with get_db() as conn:
        cursor = conn.cursor()
        for d in history_draws:
            draw_no = d.get("draw_number")
            if not draw_no:
                continue
            draw_date = d.get("draw_date", "")
            raw_nums = [b["number"] for b in d.get("numbers", []) if "number" in b]
            special_num = d.get("special_number", {}).get("number") if d.get("special_number") else None
            tot_invest = d.get("total_investment", "-")
            prizes_json = json.dumps(d.get("prizes", []))

            cursor.execute("""
                INSERT INTO draw_records (draw_number, draw_date, numbers, special_number, total_investment, prizes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                ON CONFLICT(draw_number) DO UPDATE SET
                    draw_date = excluded.draw_date,
                    numbers = excluded.numbers,
                    special_number = excluded.special_number,
                    total_investment = excluded.total_investment,
                    prizes = excluded.prizes
            """, (draw_no, draw_date, json.dumps(raw_nums), special_num, tot_invest, prizes_json))
        conn.commit()
    settle_simulations()

def settle_simulations():
    """遍历所有已知开奖期数，对所有启用的固定号码及随机模拟进行自动对奖与记账"""
    with get_db() as conn:
        cursor = conn.cursor()

        # 读取配置: 是否启用每期随机模拟下注
        cursor.execute("SELECT value FROM config WHERE key = 'random_sim_enabled'")
        row = cursor.fetchone()
        random_enabled = (row["value"] == "1") if row else True

        # 读取所有开奖记录 (从旧到新结算)
        cursor.execute("SELECT * FROM draw_records ORDER BY draw_number ASC")
        draws = cursor.fetchall()
        if not draws:
            return

        # 读取所有已启用的固定号码
        cursor.execute("SELECT * FROM fixed_bets WHERE is_active = 1")
        fixed_bets = cursor.fetchall()

        sys_rand = secrets.SystemRandom()

        for d in draws:
            draw_no = d["draw_number"]
            draw_date = d["draw_date"]
            try:
                draw_nums = json.loads(d["numbers"])
            except Exception:
                draw_nums = []
            spec_num = d["special_number"]
            try:
                draw_prizes = json.loads(d["prizes"]) if d["prizes"] else []
            except Exception:
                draw_prizes = []

            if not draw_nums:
                continue

            # 1. 结算各个固定号码
            for fb in fixed_bets:
                fb_id = fb["id"]
                fb_name = fb["name"]
                try:
                    fb_nums = json.loads(fb["numbers"])
                except Exception:
                    continue

                cursor.execute("""
                    SELECT id FROM simulated_bets 
                    WHERE draw_number = ? AND bet_type = 'fixed' AND fixed_bet_id = ?
                """, (draw_no, fb_id))
                if not cursor.fetchone():
                    res = check_prize(fb_nums, draw_nums, spec_num, draw_prizes)
                    cursor.execute("""
                        INSERT INTO simulated_bets (
                            draw_number, draw_date, bet_type, fixed_bet_id, bet_name,
                            numbers, matched_numbers, matched_count, matched_special,
                            prize_type, prize_name, cost, prize_dividend, net_profit, created_at
                        ) VALUES (?, ?, 'fixed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                    """, (
                        draw_no, draw_date, fb_id, fb_name,
                        json.dumps(fb_nums), json.dumps(res["matched_numbers"]),
                        res["matched_count"], res["matched_special"],
                        res["prize_type"], res["prize_name"],
                        res["cost"], res["prize_dividend"], res["net_profit"]
                    ))

            # 2. 结算随机模拟下注 (每期真随机生成 1 注)
            if random_enabled:
                cursor.execute("""
                    SELECT id FROM simulated_bets 
                    WHERE draw_number = ? AND bet_type = 'random' AND fixed_bet_id = 0
                """, (draw_no,))
                if not cursor.fetchone():
                    rand_nums = sorted(sys_rand.sample(range(1, 50), 6))
                    res = check_prize(rand_nums, draw_nums, spec_num, draw_prizes)
                    cursor.execute("""
                        INSERT INTO simulated_bets (
                            draw_number, draw_date, bet_type, fixed_bet_id, bet_name,
                            numbers, matched_numbers, matched_count, matched_special,
                            prize_type, prize_name, cost, prize_dividend, net_profit, created_at
                        ) VALUES (?, ?, 'random', 0, '真随机模拟投注', ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                    """, (
                        draw_no, draw_date,
                        json.dumps(rand_nums), json.dumps(res["matched_numbers"]),
                        res["matched_count"], res["matched_special"],
                        res["prize_type"], res["prize_name"],
                        res["cost"], res["prize_dividend"], res["net_profit"]
                    ))

        conn.commit()

# ==========================================
# 3. 爬虫与缓存逻辑 (HKJC 奖池与往期抓取)
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
            if history_draws:
                past_draw_data = history_draws[0]
    except Exception as e:
        print(f"[MarkSix] 往期历史 GraphQL 请求异常: {e}")

    # 同步到本地数据库并自动触发对奖模拟
    if history_draws:
        try:
            sync_draw_records(history_draws)
        except Exception as e:
            print(f"[MarkSix] 同步模拟开奖数据库异常: {e}")

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
# 4. 登录与身份鉴权 (Token / Session)
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
# 5. FastAPI 应用主体初始化
# ==========================================
app = FastAPI(
    title="香港六合彩 (Mark Six) 个人即时看板与真随机选号工具",
    description="包含马会实时大盘、往期中奖派彩明细、Python secrets 物理真随机选号、固定号码与随机模拟下注及统计系统。",
    version="1.2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 6. 业务 API 路由
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
# 7. 模拟下注系统 API 路由 (固定号码 + 随机模拟 + 战报)
# ==========================================

class FixedBetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, description="固定号码名称或备注")
    numbers: List[int] = Field(..., description="6个不重复的号码 (范围 1~49)")

@app.get("/api/simulation/summary", summary="获取模拟下注整体统计看板与ROI")
def api_simulation_summary(current_user: str = Depends(verify_token)):
    """获取所有模拟下注汇总信息（总投注、中奖、净盈亏、ROI、各奖项分布）"""
    settle_simulations()
    with get_db() as conn:
        cursor = conn.cursor()

        # 整体统计
        cursor.execute("""
            SELECT 
                COUNT(*) as total_bets,
                COUNT(DISTINCT draw_number) as total_draws,
                COALESCE(SUM(cost), 0) as total_cost,
                COALESCE(SUM(prize_dividend), 0) as total_won,
                COALESCE(SUM(net_profit), 0) as net_profit,
                COUNT(CASE WHEN prize_type > 0 THEN 1 END) as win_count
            FROM simulated_bets
        """)
        row = cursor.fetchone()
        total_bets = row["total_bets"]
        total_draws = row["total_draws"]
        total_cost = row["total_cost"]
        total_won = row["total_won"]
        net_profit = row["net_profit"]
        win_count = row["win_count"]
        roi = (net_profit / total_cost * 100.0) if total_cost > 0 else 0.0
        win_rate = (win_count / total_bets * 100.0) if total_bets > 0 else 0.0

        # 分奖项分布
        cursor.execute("SELECT prize_type, COUNT(*) as count FROM simulated_bets GROUP BY prize_type")
        prize_dist = {i: 0 for i in range(8)}
        for p_row in cursor.fetchall():
            prize_dist[p_row["prize_type"]] = p_row["count"]

        # 固定号码分组统计
        cursor.execute("""
            SELECT 
                COUNT(*) as count,
                COALESCE(SUM(cost), 0) as cost,
                COALESCE(SUM(prize_dividend), 0) as won,
                COALESCE(SUM(net_profit), 0) as net,
                COUNT(CASE WHEN prize_type > 0 THEN 1 END) as win_count
            FROM simulated_bets WHERE bet_type = 'fixed'
        """)
        f_row = cursor.fetchone()

        # 随机模拟分组统计
        cursor.execute("""
            SELECT 
                COUNT(*) as count,
                COALESCE(SUM(cost), 0) as cost,
                COALESCE(SUM(prize_dividend), 0) as won,
                COALESCE(SUM(net_profit), 0) as net,
                COUNT(CASE WHEN prize_type > 0 THEN 1 END) as win_count
            FROM simulated_bets WHERE bet_type = 'random'
        """)
        r_row = cursor.fetchone()

        # 随机模拟配置状态
        cursor.execute("SELECT value FROM config WHERE key = 'random_sim_enabled'")
        cfg_row = cursor.fetchone()
        random_enabled = (cfg_row["value"] == "1") if cfg_row else True

        # 固定号码数
        cursor.execute("SELECT COUNT(*) as cnt FROM fixed_bets WHERE is_active = 1")
        active_fixed_cnt = cursor.fetchone()["cnt"]

        return {
            "code": 200,
            "status": "success",
            "data": {
                "total_bets": total_bets,
                "total_draws": total_draws,
                "total_cost": total_cost,
                "total_won": total_won,
                "net_profit": net_profit,
                "roi_percent": round(roi, 2),
                "win_count": win_count,
                "win_rate_percent": round(win_rate, 2),
                "prize_distribution": prize_dist,
                "fixed_summary": {
                    "total_bets": f_row["count"],
                    "total_cost": f_row["cost"],
                    "total_won": f_row["won"],
                    "net_profit": f_row["net"],
                    "win_count": f_row["win_count"],
                    "active_count": active_fixed_cnt
                },
                "random_summary": {
                    "total_bets": r_row["count"],
                    "total_cost": r_row["cost"],
                    "total_won": r_row["won"],
                    "net_profit": r_row["net"],
                    "win_count": r_row["win_count"],
                    "is_enabled": random_enabled
                }
            }
        }

@app.get("/api/simulation/fixed-bets", summary="获取所有固定号码及模拟表现")
def api_get_fixed_bets(current_user: str = Depends(verify_token)):
    """获取所有管理员设定的固定号码，包含各注累计投入、中奖与收益"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                fb.id, fb.name, fb.numbers, fb.is_active, fb.created_at,
                COUNT(sb.id) as total_bets,
                COALESCE(SUM(sb.cost), 0) as total_cost,
                COALESCE(SUM(sb.prize_dividend), 0) as total_won,
                COALESCE(SUM(sb.net_profit), 0) as net_profit,
                COUNT(CASE WHEN sb.prize_type > 0 THEN 1 END) as win_count,
                MIN(CASE WHEN sb.prize_type > 0 THEN sb.prize_type ELSE 99 END) as best_prize_type
            FROM fixed_bets fb
            LEFT JOIN simulated_bets sb ON fb.id = sb.fixed_bet_id AND sb.bet_type = 'fixed'
            GROUP BY fb.id
            ORDER BY fb.id DESC
        """)
        rows = cursor.fetchall()
        result = []
        for r in rows:
            nums = json.loads(r["numbers"])
            balls = [{"number": n, "text": f"{n:02d}", "color": get_ball_color(n)} for n in nums]
            best_type = r["best_prize_type"]
            best_prize_name = PRIZE_NAMES.get(best_type, "未中奖") if best_type != 99 else "未中奖"

            result.append({
                "id": r["id"],
                "name": r["name"],
                "numbers": nums,
                "balls": balls,
                "is_active": bool(r["is_active"]),
                "created_at": r["created_at"],
                "stats": {
                    "total_bets": r["total_bets"],
                    "total_cost": r["total_cost"],
                    "total_won": r["total_won"],
                    "net_profit": r["net_profit"],
                    "win_count": r["win_count"],
                    "best_prize": best_prize_name
                }
            })

        return {"code": 200, "status": "success", "data": result}

@app.post("/api/simulation/fixed-bets", summary="添加固定号码模拟下注")
def api_create_fixed_bet(req: FixedBetCreate, current_user: str = Depends(verify_token)):
    """添加一注新的固定号码，自动进行 1-49 校验并对决现有开奖记录"""
    nums = sorted(list(set(req.numbers)))
    if len(nums) != 6:
        raise HTTPException(status_code=400, detail="必须提供 6 个不重复的号码")
    for n in nums:
        if not (1 <= n <= 49):
            raise HTTPException(status_code=400, detail="号码必须介于 1 到 49 之间")

    name = req.name.strip()
    if not name:
        name = "心水号码"

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO fixed_bets (name, numbers, is_active, created_at) VALUES (?, ?, 1, datetime('now', 'localtime'))",
            (name, json.dumps(nums))
        )
        new_id = cursor.lastrowid
        conn.commit()

    # 立即结算该注号码在历史开奖中的战绩
    settle_simulations()

    return {
        "code": 200,
        "status": "success",
        "message": f"成功添加固定模拟号码「{name}」",
        "bet_id": new_id,
        "numbers": nums
    }

@app.post("/api/simulation/fixed-bets/{bet_id}/toggle", summary="启用/暂停某注固定号码")
def api_toggle_fixed_bet(bet_id: int, current_user: str = Depends(verify_token)):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, is_active FROM fixed_bets WHERE id = ?", (bet_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="找不到该固定号码")
        new_status = 0 if row["is_active"] else 1
        cursor.execute("UPDATE fixed_bets SET is_active = ? WHERE id = ?", (new_status, bet_id))
        conn.commit()

    if new_status == 1:
        settle_simulations()

    return {
        "code": 200,
        "status": "success",
        "message": "已开启" if new_status else "已暂停",
        "is_active": bool(new_status)
    }

@app.delete("/api/simulation/fixed-bets/{bet_id}", summary="删除某注固定号码及下注记录")
def api_delete_fixed_bet(bet_id: int, current_user: str = Depends(verify_token)):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM fixed_bets WHERE id = ?", (bet_id,))
        cursor.execute("DELETE FROM simulated_bets WHERE fixed_bet_id = ? AND bet_type = 'fixed'", (bet_id,))
        conn.commit()

    return {"code": 200, "status": "success", "message": "已删除该固定号码及对应模拟下注记录"}

@app.post("/api/simulation/random/toggle", summary="开启/关闭每期随机模拟下注")
def api_toggle_random_sim(current_user: str = Depends(verify_token)):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = 'random_sim_enabled'")
        row = cursor.fetchone()
        curr = (row["value"] == "1") if row else True
        new_val = "0" if curr else "1"
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('random_sim_enabled', ?)", (new_val,))
        conn.commit()

    if new_val == "1":
        settle_simulations()

    return {
        "code": 200,
        "status": "success",
        "message": "已开启每期随机模拟下注" if new_val == "1" else "已暂停每期随机模拟下注",
        "is_enabled": new_val == "1"
    }

@app.post("/api/simulation/random/re-run", summary="重新生成并结算历史随机模拟下注")
def api_rerun_random_sim(current_user: str = Depends(verify_token)):
    """重置现有的随机模拟记录，并为已知开奖期数重新真随机生成 1 注对奖"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM simulated_bets WHERE bet_type = 'random'")
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('random_sim_enabled', '1')")
        conn.commit()

    settle_simulations()
    return {"code": 200, "status": "success", "message": "已重新完成所有期数真随机模拟下注与对奖！"}

@app.get("/api/simulation/records", summary="获取模拟下注明细流水账本")
def api_get_simulation_records(
    bet_type: Optional[str] = Query("all", description="筛选类型: all / fixed / random"),
    prize_filter: Optional[str] = Query("all", description="中奖筛选: all / won / miss"),
    fixed_bet_id: Optional[int] = Query(None, description="按指定固定号码 ID 筛选"),
    limit: int = Query(100, ge=1, le=500),
    current_user: str = Depends(verify_token)
):
    """查询模拟下注流水明细，包含投注号码、当期官方开奖号码、命中球标记与盈亏"""
    settle_simulations()
    with get_db() as conn:
        cursor = conn.cursor()
        query = """
            SELECT 
                sb.id, sb.draw_number, sb.draw_date, sb.bet_type, sb.fixed_bet_id,
                sb.bet_name, sb.numbers, sb.matched_numbers, sb.matched_count,
                sb.matched_special, sb.prize_type, sb.prize_name, sb.cost,
                sb.prize_dividend, sb.net_profit, sb.created_at,
                dr.numbers as draw_numbers, dr.special_number as draw_special
            FROM simulated_bets sb
            LEFT JOIN draw_records dr ON sb.draw_number = dr.draw_number
            WHERE 1=1
        """
        params = []
        if bet_type and bet_type != "all":
            query += " AND sb.bet_type = ?"
            params.append(bet_type)

        if fixed_bet_id is not None:
            query += " AND sb.fixed_bet_id = ?"
            params.append(fixed_bet_id)

        if prize_filter == "won":
            query += " AND sb.prize_type > 0"
        elif prize_filter == "miss":
            query += " AND sb.prize_type = 0"

        query += " ORDER BY sb.draw_number DESC, sb.id DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        records = []
        for r in rows:
            bet_nums = json.loads(r["numbers"])
            matched_nums = json.loads(r["matched_numbers"])
            matched_set = set(matched_nums)

            # 解析当期开奖官方号码
            try:
                draw_nums = json.loads(r["draw_numbers"]) if r["draw_numbers"] else []
            except Exception:
                draw_nums = []
            spec_num = r["draw_special"]

            # 生成立体球，命中高亮标注
            balls = []
            for n in bet_nums:
                is_hit = (n in matched_set) or (spec_num is not None and n == spec_num)
                is_special_hit = (spec_num is not None and n == spec_num)
                balls.append({
                    "number": n,
                    "text": f"{n:02d}",
                    "color": get_ball_color(n),
                    "is_hit": is_hit,
                    "is_special_hit": is_special_hit
                })

            draw_balls = [
                {"number": n, "text": f"{n:02d}", "color": get_ball_color(n)}
                for n in draw_nums
            ]
            special_ball = (
                {"number": spec_num, "text": f"{spec_num:02d}", "color": get_ball_color(spec_num)}
                if spec_num else None
            )

            records.append({
                "id": r["id"],
                "draw_number": r["draw_number"],
                "draw_date": r["draw_date"],
                "bet_type": r["bet_type"],
                "fixed_bet_id": r["fixed_bet_id"],
                "bet_name": r["bet_name"],
                "balls": balls,
                "draw_balls": draw_balls,
                "special_ball": special_ball,
                "matched_count": r["matched_count"],
                "matched_special": bool(r["matched_special"]),
                "prize_type": r["prize_type"],
                "prize_name": r["prize_name"],
                "cost": r["cost"],
                "prize_dividend": r["prize_dividend"],
                "net_profit": r["net_profit"],
                "created_at": r["created_at"]
            })

        return {"code": 200, "status": "success", "count": len(records), "data": records}

@app.post("/api/simulation/reset", summary="清空模拟下注记录")
def api_reset_simulations(current_user: str = Depends(verify_token)):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM simulated_bets")
        conn.commit()
    settle_simulations()
    return {"code": 200, "status": "success", "message": "模拟下注流水账本已重置！"}

# ==========================================
# 8. 单页面前端静态文件托管
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
# 9. 本地直接执行入口
# ==========================================
if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    print(f"[*] 启动香港六合彩看板服务: http://{host}:{port}")
    uvicorn.run("app:app", host=host, port=port, reload=True)
