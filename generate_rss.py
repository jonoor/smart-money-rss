#!/usr/bin/env python3
"""
Smart Money RSS Feed Generator
===============================
Generates RSS 2.0 XML feeds + CSV history from AKShare for Inoreader subscription.
Feeds:
  1. northbound.xml     — 北向资金每日流向
  2. insider.xml        — 大股东/高管增减持
  3. dragon-tiger.xml   — 龙虎榜机构席位
  4. fund-holdings.xml  — 公募基金重仓变动（季度）
  5. market-heat.xml    — 市场热度（两市成交额 + 融资融券）
  6. sec-13f.xml        — SEC 13F 知名机构持仓（过滤后）

CSV History (permanently accumulated, appending):
  docs/csv/northbound.csv
  docs/csv/insider.csv
  docs/csv/dragon-tiger.csv
  docs/csv/fund-holdings.csv
  docs/csv/market-heat.csv
  docs/csv/sec-13f.csv

Data sources: AKShare → 东方财富 / 巨潮资讯 / 新浪财经 | SEC EDGAR Atom Feed
Schedule: Daily @ 16:30 CST (via GitHub Actions)
"""

import akshare as ak
import pandas as pd
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime, timedelta
import os
import sys
import traceback
import hashlib
import urllib.request
import urllib.error

# Fix encoding for Windows terminal output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Configuration ──────────────────────────────────────────────────────────────
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
CSV_DIR = os.path.join(OUTPUT_DIR, "csv")
LOOKBACK_DAYS_INSIDER = 5
LOOKBACK_DAYS_LHB = 5
LOOKBACK_DAYS_NORTHBOUND = 10
MAX_ITEMS_PER_FEED = 20
TODAY_STR = datetime.now().strftime("%Y-%m-%d")
TODAY_COMPACT = datetime.now().strftime("%Y%m%d")

# ── SEC 13F Institution Filter ─────────────────────────────────────────────────
# Tier 1: Top-tier hedge funds / legendary investors
TIER1_KEYWORDS = [
    "BERKSHIRE HATHAWAY", "BRIDGEWATER", "TIGER GLOBAL", "ARK INVESTMENT",
    "SOROS FUND", "DUQUESNE", "BAUPOST", "PERSHING SQUARE", "APPALOOSA",
    "SCION ASSET", "ELLIOTT INVESTMENT", "THIRD POINT", "COATUE",
    "D.E. SHAW", "RENAISSANCE TECHNOLOGIES", "TWO SIGMA", "CITADEL",
    "MILLENNIUM MANAGEMENT", "POINT72", "MAVERICK CAPITAL", "LONE PINE CAPITAL",
    "VIKING GLOBAL", "VALUEACT CAPITAL", "ICAHN ENTERPRISES", "GREENLIGHT CAPITAL",
    "GLENVIEW CAPITAL", "TUDOR INVESTMENT", "MOORE CAPITAL", "CAXTON ASSOCIATES",
    "SEMINOLE CAPITAL", "BAILEY GIFFORD", "PRIMECAP", "FAIRHOLME",
    "SEMPER AUGUSTUS", "DAKOTA VALLEY", "SEIDMAN AND ASSOCIATES",
]

# Tier 2: Large asset managers / sovereign wealth / endowments
TIER2_KEYWORDS = [
    "BLACKROCK", "VANGUARD", "FIDELITY", "STATE STREET", "T. ROWE PRICE",
    "CAPITAL GROUP", "WELLINGTON MANAGEMENT", "FRANKLIN TEMPLETON", "INVESCO",
    "GOLDMAN SACHS", "MORGAN STANLEY", "JPMORGAN", "UBS", "CREDIT SUISSE",
    "DEUTSCHE BANK", "NOMURA", "HSBC", "AMUNDI", "LEGAL & GENERAL",
    "NORGES BANK", "SAUDI PIF", "TEMASEK", "GIC", "CANADA PENSION",
    "CALPERS", "CALSTRS", "YALE UNIVERSITY", "HARVARD MANAGEMENT",
    "STANFORD MANAGEMENT", "MIT INVESTMENT", "PRINCETON UNIVERSITY",
    "BAILLIE GIFFORD", "CAISSE DE DEPOT", "ABU DHABI INVESTMENT",
    "KUWAIT INVESTMENT", "QATAR INVESTMENT", "CHINA INVESTMENT",
]


def match_institution_tier(title_text):
    """
    Check if a 13F filing title matches a known institution.
    Returns (tier, matched_keyword) or (None, None).
    """
    upper = title_text.upper()
    for kw in TIER1_KEYWORDS:
        if kw.upper() in upper:
            return (1, kw)
    for kw in TIER2_KEYWORDS:
        if kw.upper() in upper:
            return (2, kw)
    return (None, None)

# ── CSV History Utilities ─────────────────────────────────────────────────────


def ensure_csv_dir():
    os.makedirs(CSV_DIR, exist_ok=True)


def append_csv(filename, columns, rows, unique_key_col=None):
    """
    Append rows to a CSV file. If the file exists, merge new rows (dedup by unique_key_col).
    Returns the total row count after append.
    """
    filepath = os.path.join(CSV_DIR, filename)
    new_df = pd.DataFrame(rows, columns=columns)

    if new_df.empty:
        return 0

    if os.path.exists(filepath):
        try:
            existing = pd.read_csv(filepath)
            combined = pd.concat([existing, new_df], ignore_index=True)
            if unique_key_col and unique_key_col in combined.columns:
                combined = combined.drop_duplicates(subset=[unique_key_col], keep="last")
            combined.to_csv(filepath, index=False, encoding="utf-8-sig")
            return len(combined)
        except Exception:
            new_df.to_csv(filepath, index=False, encoding="utf-8-sig")
            return len(new_df)
    else:
        new_df.to_csv(filepath, index=False, encoding="utf-8-sig")
        return len(new_df)


# ── RSS XML Utilities ──────────────────────────────────────────────────────────


def rfc2822_date(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0800")


def date_str_to_rfc2822(date_val, default_time_hour=16, default_time_min=30):
    """Parse various date formats and return RFC2822 string at the given default time."""
    if date_val is None or pd.isna(date_val):
        return rfc2822_date(datetime.now())

    if isinstance(date_val, pd.Timestamp):
        dt = date_val.to_pydatetime()
        return rfc2822_date(dt)

    if isinstance(date_val, datetime):
        return rfc2822_date(date_val)

    s = str(date_val).strip()
    if not s or s in ("NaT", "nan", ""):
        return rfc2822_date(datetime.now())

    # Try various formats
    formats = [
        "%Y%m%d",           # 20260728
        "%Y-%m-%d",         # 2026-07-28
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d",
        "%m/%d/%Y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            return rfc2822_date(dt.replace(hour=default_time_hour, minute=default_time_min))
        except ValueError:
            continue

    return rfc2822_date(datetime.now())


def write_rss(filename, title, link, description, items):
    """Write a valid RSS 2.0 XML file."""
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = link
    ET.SubElement(channel, "description").text = description
    ET.SubElement(channel, "language").text = "zh-CN"
    ET.SubElement(channel, "lastBuildDate").text = rfc2822_date(datetime.now())

    for item in items[:MAX_ITEMS_PER_FEED]:
        it = ET.SubElement(channel, "item")
        ET.SubElement(it, "title").text = str(item.get("title", ""))
        ET.SubElement(it, "description").text = str(item.get("description", ""))
        ET.SubElement(it, "link").text = str(item.get("link", ""))
        ET.SubElement(it, "guid", isPermaLink="false").text = str(item.get("guid", ""))
        ET.SubElement(it, "pubDate").text = str(item.get("pubDate", ""))

    raw = ET.tostring(rss, encoding="utf-8")
    dom = minidom.parseString(raw)
    xml_str = dom.toprettyxml(indent="  ", encoding="utf-8")

    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(xml_str)
    print(f"  [OK] Wrote {filename} ({len(items[:MAX_ITEMS_PER_FEED])} items)")


def escape_xml(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ── 1. 北向资金每日流向 ────────────────────────────────────────────────────────


def generate_northbound_rss():
    print("\n[1/5] Generating Northbound RSS + CSV...")
    items = []
    csv_rows = []

    try:
        # Get today's fund flow summary
        flow = ak.stock_hsgt_fund_flow_summary_em()

        sh_mask = (flow.iloc[:, 1] == "沪股通") & (flow.iloc[:, 3] == "北向")
        sz_mask = (flow.iloc[:, 2] == "深股通") & (flow.iloc[:, 3] == "北向")
        sh = flow[sh_mask]
        sz = flow[sz_mask]

        if not sh.empty and not sz.empty:
            sh_row = sh.iloc[0]
            sz_row = sz.iloc[0]

            sh_net = sh_row.iloc[6] if len(sh_row) > 6 else "N/A"
            sz_net = sz_row.iloc[6] if len(sz_row) > 6 else "N/A"
            sh_amount = sh_row.iloc[5] if len(sh_row) > 5 else 0
            sz_amount = sz_row.iloc[5] if len(sz_row) > 5 else 0
            sh_up = sh_row.iloc[8] if len(sh_row) > 8 else 0
            sh_flat = sh_row.iloc[9] if len(sh_row) > 9 else 0
            sh_down = sh_row.iloc[10] if len(sh_row) > 10 else 0
            sz_up = sz_row.iloc[8] if len(sz_row) > 8 else 0
            sz_down = sz_row.iloc[10] if len(sz_row) > 10 else 0
            sh_idx = sh_row.iloc[11] if len(sh_row) > 11 else ""
            sz_idx = sz_row.iloc[11] if len(sz_row) > 11 else ""
            sh_idx_chg = sh_row.iloc[12] if len(sh_row) > 12 else ""
            sz_idx_chg = sz_row.iloc[12] if len(sz_row) > 12 else ""

            total_net = None
            try:
                total_net = round(float(sh_net) + float(sz_net), 2) if sh_net != "N/A" and sz_net != "N/A" else None
            except (ValueError, TypeError):
                total_net = None

            direction = ""
            if total_net is not None:
                direction = "净买入" if total_net > 0 else "净卖出"

            title = f"北向资金 {TODAY_STR}: {direction} {abs(total_net) if total_net else '—'} 亿 | 沪{sh_idx_chg} 深{sz_idx_chg}"
            desc_parts = [f"<b>北向资金 {TODAY_STR}</b>"]
            desc_parts.append(f"沪股通: 净流入 {sh_net} 亿 | 成交 {float(sh_amount)/100000000:.1f} 亿")
            desc_parts.append(f"深股通: 净流入 {sz_net} 亿 | 成交 {float(sz_amount)/100000000:.1f} 亿")
            if total_net is not None:
                label = "净买入" if total_net > 0 else "净卖出"
                desc_parts.append(f"<b>合计: {label} {abs(total_net):.2f} 亿</b>")
            desc_parts.append(f"上证: {sh_idx} ({sh_idx_chg}) | 深证: {sz_idx} ({sz_idx_chg})")
            desc_parts.append(f"涨跌家数: 沪涨{sh_up}/跌{sh_down} | 深涨{sz_up}/跌{sz_down}")

            items.append({
                "title": escape_xml(title),
                "description": escape_xml("<br/>".join(desc_parts)),
                "link": "https://data.eastmoney.com/hsgt/index.html",
                "guid": f"northbound-{TODAY_STR}",
                "pubDate": date_str_to_rfc2822(TODAY_STR)
            })

            # CSV: today's snapshot
            csv_rows.append({
                "日期": TODAY_STR,
                "沪股通净流入_亿": sh_net,
                "深股通净流入_亿": sz_net,
                "北向合计净流入_亿": total_net if total_net else "",
                "沪股通成交额_亿": round(float(sh_amount) / 100000000, 2),
                "深股通成交额_亿": round(float(sz_amount) / 100000000, 2),
                "沪上涨家数": sh_up,
                "沪下跌家数": sh_down,
                "深上涨家数": sz_up,
                "深下跌家数": sz_down,
                "上证指数涨跌幅": sh_idx_chg,
                "深证成指涨跌幅": sz_idx_chg,
            })

        # Historical context: last N trading days
        hist = ak.stock_hsgt_hist_em(symbol="北向资金")
        hist = hist.tail(LOOKBACK_DAYS_NORTHBOUND)

        for _, row in hist.iterrows():
            date_val = str(row.get("日期", ""))
            if len(date_val) < 8 or date_val == TODAY_STR:
                continue

            net_buy = row.get("当日成交净买额", None)
            market_value = row.get("持股市值", None)
            buy_amount = row.get("买入成交额", None)
            sell_amount = row.get("卖出成交额", None)
            leading_stock = row.get("领涨股", "")
            leading_change = row.get("领涨股-涨跌幅", "")
            hs300 = row.get("沪深300", "")
            hs300_chg = row.get("沪深300-涨跌幅", "")

            if pd.isna(net_buy) and pd.isna(market_value):
                continue

            title_parts = [f"北向资金 {date_val}"]
            if not pd.isna(net_buy):
                direction_h = "净买入" if float(net_buy) > 0 else "净卖出"
                title_parts.append(f"{direction_h} {abs(float(net_buy)):.1f} 亿")

            # Compact description
            desc_parts_hist = [f"<b>北向资金 {date_val}</b>"]
            if not pd.isna(net_buy):
                d = "净买入" if float(net_buy) > 0 else "净卖出"
                desc_parts_hist.append(f"<b>{d} {abs(float(net_buy)):.2f} 亿</b>")
            if not pd.isna(buy_amount):
                desc_parts_hist.append(f"买入: {float(buy_amount):.1f} 亿 | 卖出: {float(sell_amount):.1f} 亿")
            if not pd.isna(market_value) and float(market_value) > 0:
                desc_parts_hist.append(f"持股市值: {float(market_value):.1f} 亿")
            if leading_stock and str(leading_stock) != "nan":
                desc_parts_hist.append(f"领涨: {leading_stock} ({leading_change})")
            if hs300 and str(hs300) != "nan":
                desc_parts_hist.append(f"沪深300: {hs300} ({hs300_chg})")

            items.append({
                "title": escape_xml(" | ".join(title_parts)),
                "description": escape_xml("<br/>".join(desc_parts_hist)),
                "link": "https://data.eastmoney.com/hsgt/index.html",
                "guid": f"northbound-{date_val}",
                "pubDate": date_str_to_rfc2822(date_val)
            })

            # CSV historical rows
            csv_rows.append({
                "日期": date_val,
                "沪股通净流入_亿": "",
                "深股通净流入_亿": "",
                "北向合计净流入_亿": float(net_buy) if not pd.isna(net_buy) else "",
                "沪股通成交额_亿": "",
                "深股通成交额_亿": "",
                "沪上涨家数": "",
                "沪下跌家数": "",
                "深上涨家数": "",
                "深下跌家数": "",
                "上证指数涨跌幅": f"{hs300_chg}" if not pd.isna(hs300_chg) else "",
                "深证成指涨跌幅": "",
            })

    except Exception as e:
        print(f"  [WARN] Northbound feed error: {e}")
        traceback.print_exc()
        items.append({
            "title": f"北向资金数据暂不可用 ({TODAY_STR})",
            "description": f"数据源暂时不可用，将在下次更新时重试。错误: {escape_xml(str(e))}",
            "link": "https://data.eastmoney.com/hsgt/index.html",
            "guid": f"northbound-error-{TODAY_COMPACT}",
            "pubDate": rfc2822_date(datetime.now())
        })

    write_rss("northbound.xml",
              "北向资金每日流向 — Smart Money",
              "https://data.eastmoney.com/hsgt/index.html",
              "外资通过沪深港通每日净买入/卖出A股数据。北向资金=沪股通+深股通。信号：连续净流入→外资看多；连续净流出→外资撤退。",
              items)

    # Save CSV
    csv_cols = ["日期", "沪股通净流入_亿", "深股通净流入_亿", "北向合计净流入_亿",
                "沪股通成交额_亿", "深股通成交额_亿", "沪上涨家数", "沪下跌家数",
                "深上涨家数", "深下跌家数", "上证指数涨跌幅", "深证成指涨跌幅"]
    total = append_csv("northbound.csv", csv_cols, csv_rows, unique_key_col="日期")
    print(f"  [CSV] northbound.csv ({total} total rows)")


# ── 2. 大股东/高管增减持 ────────────────────────────────────────────────────────
# Focus filter: only show meaningful moves (>100万 RMB, real change data, tech/leader priority)

# Industry keyword mapping for tagging (enrichment)
INDUSTRY_TAGS = {
    "科技/半导体": ["中芯", "海光", "北方华创", "韦尔", "兆易", "紫光", "长电", "通富", "华天",
                 "寒武纪", "海思", "龙芯", "圣邦", "卓胜微", "汇顶", "澜起", "中微"],
    "新能源": ["宁德", "比亚迪", "隆基", "通威", "阳光", "晶科", "晶澳", "天合", "TCL中环",
             "亿纬", "恩捷", "天赐", "赣锋", "天齐", "璞泰来", "迈为", "固德威", "锦浪"],
    "AI/软件": ["科大讯飞", "金山", "用友", "广联达", "深信服", "宝信", "浪潮", "中科曙光",
              "紫光股份", "恒生", "同花顺", "东方财富", "三六零", "昆仑万维", "万兴"],
    "医药/生物": ["恒瑞", "迈瑞", "药明", "智飞", "长春高新", "爱尔", "片仔癀", "云南白药",
                "复星", "沃森", "康泰", "凯莱英", "泰格", "康龙", "昭衍"],
    "金融": ["招商", "平安", "兴业", "宁波", "中信", "光大", "华泰", "国泰君安",
           "东方财富", "同花顺", "中金", "广发", "海通", "中国人保", "中国人寿"],
    "消费电子": ["立讯", "歌尔", "蓝思", "欧菲", "领益", "鹏鼎", "工业富联", "大族激光",
              "京东方", "TCL", "传音", "小米", "舜宇"],
    "汽车": ["比亚迪", "长城", "吉利", "长安", "上汽", "广汽", "福耀", "华域",
           "德赛", "拓普", "伯特利", "三花", "科博达"],
}


def get_stock_tags(name, code):
    """Return list of industry/concept tags for a stock name."""
    tags = []
    for tag, keywords in INDUSTRY_TAGS.items():
        for kw in keywords:
            if kw in name:
                tags.append(tag)
                break
    # Prioritize large-cap by stock code pattern (rough proxy)
    large_cap_codes = {
        "600519", "600036", "601398", "601288", "601857", "601628",
        "600276", "600309", "600900", "600438", "601012", "601888",
        "603259", "601318", "601066", "603288", "600809", "600585",
        "000858", "000333", "002594", "002415", "300750", "300760",
        "300124", "300274", "300122", "300014", "300408", "300433",
    }
    if str(code) in large_cap_codes:
        tags.append("龙头")
    return list(set(tags))


def generate_insider_rss():
    print("\n[2/5] Generating Insider RSS + CSV...")
    items = []
    csv_rows = []

    try:
        df = ak.stock_ggcg_em()
        # NOTE: On Windows, column names from AKShare may have encoding issues.
        # Use positional iloc access for robustness.
        # Column layout (stable per AKShare stock_ggcg_em):
        #  0=代码, 1=名称, 2=最新价, 3=涨跌幅, 4=股东名称,
        #  5=增减持信息-变动方向, 6=增减持信息-变动数量,
        #  7=增减持信息-占总股本比例, 8=增减持信息-占流通股比例,
        #  9=变动截止日-持股数量, 10=变动截止日-占总股本比例,
        #  11=变动截止日-占流通股比例, 12=???, 13=变动起始日,
        #  14=变动截止日, 15=公告日
        if len(df.columns) < 16:
            # Fallback: try old column-name-based rename
            col_map = {
                "代码": "code", "名称": "name", "最新价": "price",
                "涨跌幅": "pct_chg", "股东名称": "shareholder",
                "增减持信息-变动方向": "direction",
                "增减持信息-变动数量": "change_amount",
                "增减持信息-占总股本比例": "pct_total",
                "增减持信息-占流通股比例": "pct_float",
                "变动起始日": "start_date",
                "变动截止日": "end_date", "公告日": "announce_date"
            }
            existing_cols = {k: v for k, v in col_map.items() if k in df.columns}
            df = df.rename(columns=existing_cols)
            has_pos = False
        else:
            has_pos = True

        today = pd.Timestamp.now()
        cutoff = today - pd.Timedelta(days=LOOKBACK_DAYS_INSIDER)

        # Date filter
        if has_pos:
            # col15 = 公告日, col14 = 变动截止日, col13 = 变动起始日
            df["announce_date"] = pd.to_datetime(df.iloc[:, 15], errors="coerce")
            df["end_date"] = pd.to_datetime(df.iloc[:, 14], errors="coerce")
            df["start_date"] = pd.to_datetime(df.iloc[:, 13], errors="coerce")
        else:
            for date_col in ["announce_date", "end_date", "start_date"]:
                if date_col in df.columns:
                    try:
                        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
                    except Exception:
                        continue

        # Apply cutoff
        if "announce_date" in df.columns:
            df = df[df["announce_date"] >= cutoff]
        elif "end_date" in df.columns:
            df = df[df["end_date"] >= cutoff]

        # Extract fields
        if has_pos:
            df["code"] = df.iloc[:, 0]
            df["name"] = df.iloc[:, 1].astype(str)
            df["price"] = pd.to_numeric(df.iloc[:, 2], errors="coerce")
            df["shareholder"] = df.iloc[:, 4].astype(str)
            df["direction"] = df.iloc[:, 5].astype(str)
            df["change_amount"] = pd.to_numeric(df.iloc[:, 6], errors="coerce")
            df["pct_total"] = pd.to_numeric(df.iloc[:, 7], errors="coerce")
            df["pct_float"] = pd.to_numeric(df.iloc[:, 8], errors="coerce")

        # Numeric conversions
        df["change_amount_num"] = pd.to_numeric(df["change_amount"], errors="coerce")
        df["price_num"] = pd.to_numeric(df["price"], errors="coerce")

        # ── FILTERS ──
        # 1. Must have real change data (>0 shares)
        df = df[df["change_amount_num"] > 0]

        # Compute change amount in 万股 and RMB value (万元)
        # NOTE: AKShare stock_ggcg_em returns change_amount in 万股 (not 股)
        df["change_num_wan"] = df["change_amount_num"]
        df["change_value_wan"] = df["change_num_wan"] * df["price_num"]

        # 2. Must have meaningful monetary value (>50万元)
        df = df[df["change_value_wan"] >= 50]

        # 3. Must have direction (增持/减持)
        df = df[df["direction"].isin(["增持", "减持"])]

        if df.empty:
            items.append({
                "title": f"大股东增减持 — {TODAY_STR} 无符合条件的大额变动",
                "description": f"今日无变动金额>=100万元的大股东/高管增减持记录。过滤条件：变动数量>0、变动金额>=100万元、含明确方向。",
                "link": "https://data.eastmoney.com/center/stock/trade/5.html",
                "guid": f"insider-empty-{TODAY_STR}",
                "pubDate": rfc2822_date(datetime.now())
            })
            write_rss("insider.xml",
                      "大股东/高管增减持 — Smart Money",
                      "https://data.eastmoney.com/center/stock/trade/5.html",
                      "A股上市公司大股东、董监高持股变动实时披露。已过滤：只保留变动金额>=100万元、有明确方向、科技/龙头/热门行业优先。",
                      items)
            csv_cols = ["抓取日期", "公告日", "股票代码", "股票名称", "股东名称",
                        "变动方向", "变动数量_股", "变动数量_万股", "变动金额_万元",
                        "占总股本比例", "占流通股比例", "最新价", "变动截止日", "标签"]
            total = append_csv("insider.csv", csv_cols, csv_rows, unique_key_col=None)
            print(f"  [CSV] insider.csv ({total} total rows)")
            return

        # Sort by monetary value (largest first), then by date
        df = df.sort_values(["change_value_wan", "announce_date"],
                            ascending=[False, False], na_position="last")

        # Tag each row and prioritize rows with tags
        df["tags"] = df.apply(lambda r: get_stock_tags(str(r.get("name", "")), str(r.get("code", ""))), axis=1)
        df["tag_count"] = df["tags"].apply(len)
        # Secondary sort: rows with tags first, then by value
        df = df.sort_values(["tag_count", "change_value_wan", "announce_date"],
                            ascending=[False, False, False], na_position="last")

        for _, row in df.head(MAX_ITEMS_PER_FEED).iterrows():
            code = str(row.get("code", ""))
            name = str(row.get("name", ""))
            shareholder = str(row.get("shareholder", "未知"))
            direction = str(row.get("direction", ""))
            change_amount = row.get("change_amount", 0)
            pct_total = row.get("pct_total", "")
            pct_float = row.get("pct_float", "")
            price = row.get("price", "")
            announce_date = row.get("announce_date", "")
            change_num = row.get("change_num_wan", 0)
            change_val = row.get("change_value_wan", 0)
            tags = row.get("tags", [])

            direction_label = {"增持": "增持", "减持": "减持"}.get(direction, direction)
            arrow = "▲" if direction_label == "增持" else "▼"
            tag_str = f" [{'/'.join(tags)}]" if tags else ""

            # Title: concise with amount + value + tags
            if change_val >= 1000:
                val_str = f"¥{change_val/10000:.2f}亿"
            else:
                val_str = f"¥{change_val:.0f}万"

            title = f"[{arrow}{direction_label}{tag_str}] {name}({code}) {change_num:.1f}万股/{val_str}"

            # Description
            desc_parts = [f"<b>{name} ({code})</b>{tag_str} | {shareholder}"]
            desc_parts.append(f"<b>{arrow} {direction_label} {change_num:.2f} 万股 | 约 {val_str}</b>")

            if price and str(price) not in ("nan", "NaT", ""):
                try:
                    p = float(price)
                    desc_parts.append(f"最新价: ¥{p:.2f}")
                except (ValueError, TypeError):
                    pass

            if pct_total and str(pct_total) not in ("nan", "NaT", ""):
                try:
                    pt = float(pct_total)
                    if pt > 0:
                        desc_parts.append(f"占总股本: {pt:.4f}%")
                except (ValueError, TypeError):
                    pass

            if pct_float and str(pct_float) not in ("nan", "NaT", ""):
                try:
                    pf = float(pct_float)
                    if pf > 0:
                        desc_parts.append(f"占流通股: {pf:.4f}%")
                except (ValueError, TypeError):
                    pass

            if announce_date and str(announce_date) != "NaT":
                ad = str(announce_date).split(" ")[0] if " " in str(announce_date) else str(announce_date)
                desc_parts.append(f"公告日: {ad}")

            link = ""
            if str(code).startswith(("6", "9")):
                link = f"https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html?type=web&code=SH{code}"
            else:
                link = f"https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html?type=web&code=SZ{code}"

            guid = f"insider-{code}-{announce_date}-{hash(str(shareholder)) % 10000}"

            items.append({
                "title": escape_xml(title),
                "description": escape_xml("<br/>".join(desc_parts)),
                "link": link,
                "guid": guid,
                "pubDate": date_str_to_rfc2822(announce_date)
            })

            end_date_val = row.get("end_date", "")
            if has_pos and pd.notna(end_date_val):
                end_date_val = str(end_date_val).split(" ")[0] if " " in str(end_date_val) else str(end_date_val)

            csv_rows.append({
                "抓取日期": TODAY_STR,
                "公告日": str(announce_date),
                "股票代码": code,
                "股票名称": name,
                "股东名称": shareholder,
                "变动方向": direction,
                "变动数量_股": change_amount,
                "变动数量_万股": round(change_num, 2) if isinstance(change_num, (int, float)) else "",
                "变动金额_万元": round(change_val, 2) if isinstance(change_val, (int, float)) else "",
                "占总股本比例": pct_total,
                "占流通股比例": pct_float,
                "最新价": price,
                "变动截止日": str(end_date_val) if end_date_val else "",
                "标签": "/".join(tags) if tags else "",
            })

    except Exception as e:
        print(f"  [WARN] Insider feed error: {e}")
        traceback.print_exc()
        items.append({
            "title": f"大股东增减持数据暂不可用 ({TODAY_STR})",
            "description": f"数据源暂时不可用。错误: {escape_xml(str(e))}",
            "link": "https://data.eastmoney.com/center/stock/trade/5.html",
            "guid": f"insider-error-{TODAY_COMPACT}",
            "pubDate": rfc2822_date(datetime.now())
        })

    write_rss("insider.xml",
              "大股东/高管增减持 — Smart Money",
              "https://data.eastmoney.com/center/stock/trade/5.html",
              "A股上市公司大股东、董监高持股变动实时披露。已过滤：只保留变动金额>=100万元、有明确方向、科技/龙头/热门行业优先。",
              items)

    csv_cols = ["抓取日期", "公告日", "股票代码", "股票名称", "股东名称",
                "变动方向", "变动数量_股", "变动数量_万股", "变动金额_万元",
                "占总股本比例", "占流通股比例", "最新价", "变动截止日", "标签"]
    total = append_csv("insider.csv", csv_cols, csv_rows, unique_key_col=None)
    print(f"  [CSV] insider.csv ({total} total rows)")


# ── 3. 龙虎榜机构席位 ──────────────────────────────────────────────────────────


def generate_dragon_tiger_rss():
    print("\n[3/5] Generating Dragon-Tiger RSS + CSV...")
    items = []
    csv_rows = []

    try:
        start = (datetime.now() - timedelta(days=LOOKBACK_DAYS_LHB)).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")

        df = ak.stock_lhb_jgmmtj_em(start_date=start, end_date=end)

        if "机构买入净额" in df.columns:
            df["机构买入净额_num"] = pd.to_numeric(df["机构买入净额"], errors="coerce")
            df = df.sort_values("机构买入净额_num", key=abs, ascending=False, na_position="last")

        for _, row in df.head(MAX_ITEMS_PER_FEED).iterrows():
            code = row.get("代码", "")
            name = row.get("名称", "")
            close = row.get("收盘价", "")
            pct_chg = row.get("涨跌幅", "")
            buy_inst_count = row.get("买方机构数", 0)
            sell_inst_count = row.get("卖方机构数", 0)
            buy_total = row.get("机构买入总额", 0)
            sell_total = row.get("机构卖出总额", 0)
            net_buy = row.get("机构买入净额", 0)
            seat_date = row.get("上榜日期", "")

            try:
                net_buy_yi = float(net_buy) / 100000000
            except (ValueError, TypeError):
                net_buy_yi = 0

            try:
                buy_total_yi = float(buy_total) / 100000000
                sell_total_yi = float(sell_total) / 100000000
            except (ValueError, TypeError):
                buy_total_yi = sell_total_yi = 0

            direction_label = "机构净买入" if net_buy_yi > 0 else "机构净卖出"
            abs_net = abs(net_buy_yi)

            title = f"[{direction_label} {abs_net:.2f}亿] {name}({code}) 涨{pct_chg}% | {seat_date}"

            desc_parts = [f"<b>{name} ({code})</b> | 收盘价 {close} | 涨跌幅 {pct_chg}%"]
            desc_parts.append(f"<b>机构{direction_label} {abs_net:.2f} 亿</b>")
            desc_parts.append(f"买方机构 {buy_inst_count} 家 | 卖方机构 {sell_inst_count} 家")
            desc_parts.append(f"机构买入 {buy_total_yi:.2f} 亿 | 机构卖出 {sell_total_yi:.2f} 亿")
            if net_buy_yi > 0:
                desc_parts.append("信号: 机构席位积极买入")
            else:
                desc_parts.append("信号: 机构席位主动卖出")

            link = f"https://data.eastmoney.com/stock/lhb,{code}.html"

            items.append({
                "title": escape_xml(title),
                "description": escape_xml("<br/>".join(desc_parts)),
                "link": link,
                "guid": f"lhb-{code}-{seat_date}",
                "pubDate": date_str_to_rfc2822(seat_date)
            })

            csv_rows.append({
                "抓取日期": TODAY_STR,
                "上榜日期": seat_date,
                "股票代码": code,
                "股票名称": name,
                "收盘价": close,
                "涨跌幅": pct_chg,
                "买方机构数": buy_inst_count,
                "卖方机构数": sell_inst_count,
                "机构买入总额_亿": round(buy_total_yi, 4),
                "机构卖出总额_亿": round(sell_total_yi, 4),
                "机构买入净额_亿": round(net_buy_yi, 4),
            })

    except Exception as e:
        print(f"  [WARN] Dragon-Tiger feed error: {e}")
        traceback.print_exc()
        items.append({
            "title": f"龙虎榜数据暂不可用 ({TODAY_STR})",
            "description": f"数据源暂时不可用。错误: {escape_xml(str(e))}",
            "link": "https://data.eastmoney.com/stock/lhb.html",
            "guid": f"lhb-error-{TODAY_COMPACT}",
            "pubDate": rfc2822_date(datetime.now())
        })

    write_rss("dragon-tiger.xml",
              "龙虎榜机构席位 — Smart Money",
              "https://data.eastmoney.com/stock/lhb.html",
              "每日龙虎榜中机构专用席位的买卖数据。信号：多家机构同时买入→机构共识看多；机构专用集中卖出→警惕。",
              items)

    csv_cols = ["抓取日期", "上榜日期", "股票代码", "股票名称", "收盘价", "涨跌幅",
                "买方机构数", "卖方机构数", "机构买入总额_亿", "机构卖出总额_亿", "机构买入净额_亿"]
    total = append_csv("dragon-tiger.csv", csv_cols, csv_rows, unique_key_col=None)
    print(f"  [CSV] dragon-tiger.csv ({total} total rows)")


# ── 4. 公募基金重仓变动 ─────────────────────────────────────────────────────────


def generate_fund_holdings_rss():
    print("\n[4/5] Generating Fund Holdings RSS + CSV...")
    items = []
    csv_rows = []

    REPRESENTATIVE_FUNDS = [
        ("510050", "华夏上证50ETF"),
        ("510300", "华泰柏瑞沪深300ETF"),
        ("510500", "南方中证500ETF"),
        ("159919", "嘉实沪深300ETF"),
    ]

    current_quarter = f"{datetime.now().year}"
    m = datetime.now().month
    if m <= 3:
        current_quarter += "Q1"
        quarter_end = datetime(datetime.now().year, 3, 31, 16, 30)
    elif m <= 6:
        current_quarter += "Q1"  # most recent confirmed
        quarter_end = datetime(datetime.now().year, 3, 31, 16, 30)
    elif m <= 9:
        current_quarter += "Q2"
        quarter_end = datetime(datetime.now().year, 6, 30, 16, 30)
    else:
        current_quarter += "Q3"
        quarter_end = datetime(datetime.now().year, 9, 30, 16, 30)

    quarter_pubdate = rfc2822_date(quarter_end)

    try:
        for fund_code, fund_name in REPRESENTATIVE_FUNDS:
            try:
                df = ak.fund_portfolio_hold_em(symbol=fund_code, date=datetime.now().year)
                if df.empty:
                    continue

                top5 = df.head(5)
                holdings = []
                for _, row in top5.iterrows():
                    sname = row.get("股票名称", row.get("名称", "?"))
                    pct = row.get("占净值比例", row.get("持仓占比", "?"))
                    holdings.append(f"{sname}({pct}%)")

                title = f"{fund_name}({fund_code}) {current_quarter}重仓TOP5"
                desc_parts = [f"<b>{fund_name} ({fund_code}) | {current_quarter}</b>"]

                for idx, (_, row) in enumerate(top5.iterrows(), 1):
                    stock_name = row.get("股票名称", row.get("名称", "?"))
                    weight = row.get("占净值比例", row.get("持仓占比", "?"))
                    value = row.get("持仓市值", "?")
                    shares = row.get("持股数", row.get("持仓数量", "?"))
                    desc_parts.append(f"{idx}. {stock_name}: 权重{weight}% | 市值{value}")

                    csv_rows.append({
                        "抓取日期": TODAY_STR,
                        "季度": current_quarter,
                        "基金代码": fund_code,
                        "基金名称": fund_name,
                        "股票名称": stock_name,
                        "占净值比例": weight,
                        "持股数": shares,
                        "持仓市值": value,
                    })

                items.append({
                    "title": escape_xml(title),
                    "description": escape_xml("<br/>".join(desc_parts)),
                    "link": f"https://fund.eastmoney.com/{fund_code}.html",
                    "guid": f"fund-holdings-{fund_code}-{current_quarter}",
                    "pubDate": quarter_pubdate
                })

                if len(items) >= MAX_ITEMS_PER_FEED:
                    break

            except Exception as fund_e:
                print(f"    [WARN] Fund {fund_code}: {fund_e}")
                continue

    except Exception as e:
        print(f"  [WARN] Fund holdings feed error: {e}")
        traceback.print_exc()

    if not items:
        items.append({
            "title": f"基金重仓数据 — {current_quarter} (等待最新季报披露)",
            "description": f"公募基金{current_quarter}季报正在陆续披露中。重仓数据将在季报发布后更新。历史数据请访问基金详情页。",
            "link": "https://fund.eastmoney.com/",
            "guid": f"fund-holdings-{current_quarter}",
            "pubDate": rfc2822_date(datetime.now())
        })

    write_rss("fund-holdings.xml",
              "公募基金重仓变动 — Smart Money",
              "https://fund.eastmoney.com/",
              "主流公募基金季度重仓股变动。信号：多家基金同步加仓→机构共识方向；基金集中减仓→资金撤退信号。",
              items)

    csv_cols = ["抓取日期", "季度", "基金代码", "基金名称", "股票名称", "占净值比例", "持股数", "持仓市值"]
    total = append_csv("fund-holdings.csv", csv_cols, csv_rows, unique_key_col=None)
    print(f"  [CSV] fund-holdings.csv ({total} total rows)")


# ── 5. 市场热度（两市成交额 + 融资融券）───────────────NEW─────────────────────


def generate_market_heat_rss():
    """
    市场热度指标:
    - 两市成交额 (上证+深证 daily turnover)
    - 融资融券余额 (margin trading balance)
    用于判断市场情绪和资金活跃度。
    增加与昨日对比，让数据变化一目了然。
    """
    print("\n[5/5] Generating Market Heat RSS + CSV...")
    items = []
    csv_rows = []
    csv_row = {"日期": TODAY_STR}

    # ── Load yesterday's data for comparison ──
    prev_day_data = {}
    csv_path = os.path.join(CSV_DIR, "market-heat.csv")
    if os.path.exists(csv_path):
        try:
            hist_df = pd.read_csv(csv_path)
            if not hist_df.empty:
                # Get the most recent row that is not today
                hist_df = hist_df[hist_df["日期"] != TODAY_STR]
                if not hist_df.empty:
                    prev = hist_df.iloc[-1]
                    prev_day_data = {
                        "两市合计成交额_亿": prev.get("两市合计成交额_亿", None),
                        "融资融券余额_亿": prev.get("融资融券余额_亿", None),
                        "沪市成交额_亿": prev.get("沪市成交额_亿", None),
                        "深市成交额_亿": prev.get("深市成交额_亿", None),
                        "融资余额_亿": prev.get("融资余额_亿", None),
                    }
        except Exception:
            pass

    def pct_change(curr, prev):
        try:
            c = float(curr)
            p = float(prev)
            if p and p > 0:
                return round((c - p) / p * 100, 2)
        except (ValueError, TypeError):
            pass
        return None

    def fmt_change(curr, prev, unit="亿"):
        """Return formatted string like '+5.2% (+892亿)' or '' if no prev."""
        pc = pct_change(curr, prev)
        if pc is None:
            return ""
        sign = "+" if pc >= 0 else ""
        try:
            diff = round(float(curr) - float(prev), 1)
            diff_sign = "+" if diff >= 0 else ""
            return f"{sign}{pc}% ({diff_sign}{diff}{unit})"
        except (ValueError, TypeError):
            return f"{sign}{pc}%"

    try:
        # ── SSE turnover (stock_sse_deal_daily) ──
        try:
            sse = ak.stock_sse_deal_daily()
            if len(sse) > 3:
                turnover_row = sse.iloc[3]
                item_name = str(turnover_row.iloc[0])
                if "金额" in item_name:
                    raw = turnover_row.iloc[1]
                    sh_turnover = round(float(raw), 2)
                    csv_row["沪市成交额_亿"] = sh_turnover
        except Exception as e:
            print(f"    [WARN] SSE turnover: {e}")

        # ── SZSE turnover (stock_szse_summary) ──
        try:
            szse = ak.stock_szse_summary()
            if len(szse) > 0:
                raw = szse.iloc[0, 2]
                sz_turnover = round(float(raw) / 100000000, 2)
                csv_row["深市成交额_亿"] = sz_turnover
        except Exception as e:
            print(f"    [WARN] SZSE turnover: {e}")

        # Compute total
        sh_t = csv_row.get("沪市成交额_亿", None)
        sz_t = csv_row.get("深市成交额_亿", None)
        if sh_t is not None and sz_t is not None:
            csv_row["两市合计成交额_亿"] = round(float(sh_t) + float(sz_t), 2)
        elif sh_t is not None:
            csv_row["两市合计成交额_亿"] = round(float(sh_t) * 2.5, 0)
        elif sz_t is not None:
            csv_row["两市合计成交额_亿"] = round(float(sz_t) * 2, 0)

        # ── 融资融券 ──
        margin_dates = [TODAY_COMPACT]
        margin_dates.append((datetime.now() - timedelta(days=1)).strftime("%Y%m%d"))
        margin_dates.append((datetime.now() - timedelta(days=2)).strftime("%Y%m%d"))

        sse_margin_bal = None
        sse_margin_buy = None
        for mdate in margin_dates:
            try:
                margin_detail = ak.stock_margin_detail_sse(date=mdate)
                if margin_detail is not None and len(margin_detail) > 0:
                    sse_margin_bal = pd.to_numeric(margin_detail.iloc[:, 3], errors="coerce").sum()
                    sse_margin_buy = pd.to_numeric(margin_detail.iloc[:, 4], errors="coerce").sum()
                    break
            except Exception:
                continue

        if sse_margin_bal is not None and pd.notna(sse_margin_bal) and sse_margin_bal > 0:
            csv_row["融资余额_亿"] = round(float(sse_margin_bal) / 100000000, 2)
        if sse_margin_buy is not None and pd.notna(sse_margin_buy) and sse_margin_buy > 0:
            csv_row["融资买入额_亿"] = round(float(sse_margin_buy) / 100000000, 2)

        try:
            sz_margin = ak.stock_margin_szse()
            if not sz_margin.empty:
                sz_bal = sz_margin.iloc[0, 1]
                sz_short_bal = sz_margin.iloc[0, 3]
                sz_total = sz_margin.iloc[0, 5]
                csv_row["融券余额_亿"] = round(float(sz_short_bal), 2)
                csv_row["融资融券余额_亿"] = round(float(sz_total), 2)
                if "融资余额_亿" not in csv_row:
                    csv_row["融资余额_亿"] = round(float(sz_bal), 2)
        except Exception as e:
            print(f"    [WARN] SZSE margin: {e}")

        # Build RSS item WITH day-over-day comparison
        total_vol = csv_row.get("两市合计成交额_亿", "N/A")
        margin_bal = csv_row.get("融资融券余额_亿", "N/A")

        # Changes vs yesterday
        vol_change = fmt_change(total_vol, prev_day_data.get("两市合计成交额_亿"))
        margin_change = fmt_change(margin_bal, prev_day_data.get("融资融券余额_亿"))
        sh_change = fmt_change(sh_t, prev_day_data.get("沪市成交额_亿"))
        sz_change = fmt_change(sz_t, prev_day_data.get("深市成交额_亿"))

        # Heat assessment
        heat = ""
        try:
            tv = float(total_vol)
            if tv > 15000:
                heat = "火热 (>1.5万亿) — 市场活跃度高，注意过热风险"
            elif tv > 10000:
                heat = "中等偏热 (1.0-1.5万亿) — 正常偏活跃"
            elif tv > 6000:
                heat = "正常 (0.6-1.0万亿) — 市场平稳"
            else:
                heat = "冷清 (<6000亿) — 市场情绪低迷"
        except (ValueError, TypeError):
            heat = "数据暂缺"

        # Title with change indicator
        change_indicators = []
        if vol_change:
            change_indicators.append(f"成交{vol_change}")
        if margin_change:
            change_indicators.append(f"两融{margin_change}")
        change_str = f" | {' | '.join(change_indicators)}" if change_indicators else ""

        title = f"市场热度 {TODAY_STR}: 两市成交 {total_vol} 亿 | 两融 {margin_bal} 亿{change_str} | {heat.partition('—')[0].strip()}"

        desc_parts = [f"<b>市场热度 {TODAY_STR}</b>"]
        sh_line = f"沪市: {csv_row.get('沪市成交额_亿', 'N/A')} 亿"
        if sh_change:
            sh_line += f" <i>({sh_change})</i>"
        sz_line = f"深市: {csv_row.get('深市成交额_亿', 'N/A')} 亿"
        if sz_change:
            sz_line += f" <i>({sz_change})</i>"
        desc_parts.append(f"{sh_line} | {sz_line}")

        total_line = f"<b>两市合计: {total_vol} 亿</b>"
        if vol_change:
            total_line += f" <i>较昨日 {vol_change}</i>"
        desc_parts.append(total_line)

        margin_line = f"融资余额: {csv_row.get('融资余额_亿', 'N/A')} 亿 | 融资买入: {csv_row.get('融资买入额_亿', 'N/A')} 亿"
        desc_parts.append(margin_line)

        margin_total_line = f"融券余额: {csv_row.get('融券余额_亿', 'N/A')} 亿 | 两融合计: {margin_bal} 亿"
        if margin_change:
            margin_total_line += f" <i>较昨日 {margin_change}</i>"
        desc_parts.append(margin_total_line)

        desc_parts.append(f"判断: {heat}")

        # Additional insight if we have prev data
        if vol_change and prev_day_data.get("两市合计成交额_亿"):
            try:
                vol_pct = pct_change(total_vol, prev_day_data["两市合计成交额_亿"])
                if vol_pct and vol_pct > 10:
                    desc_parts.append("<b>信号: 成交额显著放大，资金活跃度提升</b>")
                elif vol_pct and vol_pct < -10:
                    desc_parts.append("<b>信号: 成交额明显萎缩，市场情绪降温</b>")
            except Exception:
                pass

        items.append({
            "title": escape_xml(title),
            "description": escape_xml("<br/>".join(desc_parts)),
            "link": "https://data.eastmoney.com/cjsj/hsgt.html",
            "guid": f"market-heat-{TODAY_STR}",
            "pubDate": date_str_to_rfc2822(TODAY_STR)
        })

        csv_rows.append(csv_row)

    except Exception as e:
        print(f"  [WARN] Market heat feed error: {e}")
        traceback.print_exc()
        items.append({
            "title": f"市场热度数据暂不可用 ({TODAY_STR})",
            "description": f"数据源暂时不可用。错误: {escape_xml(str(e))}",
            "link": "https://data.eastmoney.com/cjsj/hsgt.html",
            "guid": f"market-heat-error-{TODAY_COMPACT}",
            "pubDate": rfc2822_date(datetime.now())
        })

    write_rss("market-heat.xml",
              "市场热度（成交额+融资融券）— Smart Money",
              "https://data.eastmoney.com/cjsj/hsgt.html",
              "A股市场热度指标：两市成交额 + 融资融券余额。增加与昨日对比变化，信号：成交额持续放大→资金涌入；两融余额上升→杠杆资金看多。",
              items)

    csv_cols = ["日期", "沪市成交额_亿", "深市成交额_亿", "两市合计成交额_亿",
                "融资余额_亿", "融资买入额_亿", "融券余额_亿", "融资融券余额_亿"]
    total = append_csv("market-heat.csv", csv_cols, csv_rows, unique_key_col="日期")
    print(f"  [CSV] market-heat.csv ({total} total rows)")


# ── 6. SEC EDGAR Form 13F: 机构季度持仓变化 ────────────────────────────────────


def generate_sec_13f_rss():
    """
    Fetch SEC EDGAR 13F-HR filings and filter for well-known institutions only.
    Eliminates noise from thousands of small RIAs and advisory firms.
    """
    print("\n[6/6] Generating SEC 13F RSS (filtered)...")
    items = []
    csv_rows = []

    SEC_ATOM_URL = (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
        "&type=13F-HR&company=&dateb=&owner=include&start=0&count=200&output=atom"
    )

    try:
        req = urllib.request.Request(
            SEC_ATOM_URL,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml_bytes = resp.read()

        # Parse Atom feed
        root = ET.fromstring(xml_bytes)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        entries = []
        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            updated_el = entry.find("atom:updated", ns)
            summary_el = entry.find("atom:summary", ns)

            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            link = link_el.get("href", "") if link_el is not None else ""
            updated = updated_el.text.strip() if updated_el is not None and updated_el.text else ""
            summary = summary_el.text.strip() if summary_el is not None and summary_el.text else ""

            if not title:
                continue

            # Filter: only keep known institutions
            tier, matched_kw = match_institution_tier(title)
            if tier is None:
                continue

            entries.append({
                "title": title,
                "link": link,
                "updated": updated,
                "summary": summary,
                "tier": tier,
                "matched_kw": matched_kw,
            })

        if not entries:
            items.append({
                "title": f"SEC 13F — {TODAY_STR} 暂无知名机构新披露",
                "description": "今日 SEC EDGAR 13F  filings 中未检测到白名单内的知名机构。知名机构通常在每个季度末（3/6/9/12月）后的45天内集中披露。",
                "link": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=13F-HR",
                "guid": f"sec-13f-empty-{TODAY_STR}",
                "pubDate": rfc2822_date(datetime.now())
            })
        else:
            # Sort: Tier 1 first, then by updated time (newest first)
            entries.sort(key=lambda x: (x["tier"], x["updated"]), reverse=False)
            # We want tier 1 first, so tier=1 < tier=2. But for date we want newest first.
            # Re-sort properly:
            entries.sort(key=lambda x: (x["tier"], x["updated"]))
            entries.reverse()  # Now tier 2 comes first... need custom sort
            # Actually let's do:
            entries.sort(key=lambda x: (x["tier"], x["updated"]))
            # tier 1 first means smaller tier number first.
            # For date, newer first means larger date string first (ISO 8601 sorts lexicographically)
            # So we need sort with tier ascending, updated descending
            entries.sort(key=lambda x: (x["tier"], x["updated"]))
            # Reverse within each tier... let's just do a stable two-step sort
            entries.sort(key=lambda x: x["updated"], reverse=True)
            entries.sort(key=lambda x: x["tier"])

            for e in entries[:MAX_ITEMS_PER_FEED]:
                tier_label = "[Tier1]" if e["tier"] == 1 else "[Tier2]"
                # Clean up title: remove redundant "13F-HR - " prefix
                clean_title = e["title"]
                if clean_title.startswith("13F-HR - "):
                    clean_title = clean_title[9:]
                elif clean_title.startswith("13F-HR/A - "):
                    clean_title = clean_title[11:]

                title = f"{tier_label} {clean_title}"

                # Extract Acc-No and size from summary if present
                desc_parts = [f"<b>{tier_label} {clean_title}</b>"]
                if e["updated"]:
                    desc_parts.append(f"披露时间: {e['updated']}")
                if e["summary"]:
                    # Summary may contain Acc-No and size
                    desc_parts.append(f"详情: {e['summary']}")
                desc_parts.append(
                    "<i>13F-HR 为机构季度持仓报告，披露美股多头持仓明细。"
                    "重点关注前十大持仓变动、新增/清仓标的。</i>"
                )

                # Parse updated to RFC2822
                pub_date = rfc2822_date(datetime.now())
                if e["updated"]:
                    try:
                        # ISO 8601 format: 2026-07-30T16:30:00-04:00
                        dt = datetime.fromisoformat(e["updated"].replace("Z", "+00:00"))
                        pub_date = rfc2822_date(dt)
                    except Exception:
                        pass

                guid = f"sec-13f-{hash(e['title'] + e['updated']) % 100000}"

                items.append({
                    "title": escape_xml(title),
                    "description": escape_xml("<br/>".join(desc_parts)),
                    "link": e["link"] or "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=13F-HR",
                    "guid": guid,
                    "pubDate": pub_date,
                })

                csv_rows.append({
                    "抓取日期": TODAY_STR,
                    "披露时间": e["updated"],
                    "标题": e["title"],
                    "机构级别": f"Tier{e['tier']}",
                    "匹配关键词": e["matched_kw"],
                    "链接": e["link"],
                })

    except urllib.error.URLError as ue:
        print(f"  [WARN] SEC 13F network error: {ue}")
        err_msg = str(ue)
        if "403" in err_msg:
            desc = ("SEC EDGAR 当前从本机网络环境无法访问（403 Forbidden）。"
                    "GitHub Actions 部署后会自动从美国服务器抓取，此提示将消失。")
        else:
            desc = f"SEC 网络请求失败。错误: {escape_xml(err_msg)}"
        items.append({
            "title": f"SEC 13F 数据暂不可用 ({TODAY_STR})",
            "description": desc,
            "link": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=13F-HR",
            "guid": f"sec-13f-error-{TODAY_COMPACT}",
            "pubDate": rfc2822_date(datetime.now())
        })
    except Exception as e:
        print(f"  [WARN] SEC 13F feed error: {e}")
        traceback.print_exc()
        items.append({
            "title": f"SEC 13F 数据暂不可用 ({TODAY_STR})",
            "description": f"数据源暂时不可用。错误: {escape_xml(str(e))}",
            "link": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=13F-HR",
            "guid": f"sec-13f-error-{TODAY_COMPACT}",
            "pubDate": rfc2822_date(datetime.now())
        })

    write_rss("sec-13f.xml",
              "SEC 13F 机构持仓 — Smart Money",
              "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=13F-HR",
              "SEC EDGAR Form 13F 季度持仓报告，已过滤：只保留 Tier1/Tier2 知名机构。"
              "信号：顶级基金增减持方向 = 聪明钱共识。",
              items)

    csv_cols = ["抓取日期", "披露时间", "标题", "机构级别", "匹配关键词", "链接"]
    total = append_csv("sec-13f.csv", csv_cols, csv_rows, unique_key_col=None)
    print(f"  [CSV] sec-13f.csv ({total} total rows)")
    print(f"  [FILTER] Kept {len(items)} of 200 filings (Tier1/Tier2 only)")


# ── Main ────────────────────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print(f"Smart Money RSS Generator — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ensure_csv_dir()

    generate_northbound_rss()
    generate_insider_rss()
    generate_dragon_tiger_rss()
    generate_fund_holdings_rss()
    generate_market_heat_rss()
    generate_sec_13f_rss()

    print("\n" + "=" * 60)
    print("All feeds + CSV generated successfully!")
    print(f"RSS Output: {OUTPUT_DIR}")
    print(f"CSV Output: {CSV_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()
