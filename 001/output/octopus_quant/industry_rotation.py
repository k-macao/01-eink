"""中国行业指数周度评分 / 月度纯多头轮动（申万一级行业 · 东方财富板块指数日线）。

2026-10-04 起迁入 ``output/octopus_quant/``（与港股量化引擎同层）：取数仍由本模块自带
适配器完成（可注入 ``fetch_json``，离线可测），排版与拼版由 ``pipeline`` 负责；
存档键为 ``industry_rotation`` / ``industry_rotation_diag``，与港股「板块轮动」
（``sector_rotation*``，A股概念→港股篮子）**互不覆盖**。

每次展示截至最近完整收盘日的全行业评分；持仓仅在新月份首次成功运行时
更新（首次启用为建仓，并非回填月初）。纯规则研究口径，不含交易成本或收益承诺。

行业名单（2026-09-29 修正）：
  · 旧版在运行时向东方财富 ``clist/get?fs=m:90+t:2`` 拉「完整行业板块列表」。线上核查
    （``news_history.json`` 的 ``sector_rotation_probe``，GitHub Actions 真实出口）证明这一步
    是整栏缺席的唯一原因：``pz=100 / 500`` 的列表请求被 nginx 以 **502** 拒绝（5 种参数变体
    全部 502），而同一分钟内 ``pz=5`` 的小页请求（全景「板块热力」）与板块日 K
    （``push2his …/kline/get``）都是 200。
  · 同时东方财富已把「行业板块」改成申万式三级结构：``m:90+t:2`` 现返回 **496** 个板块
    （一级 / 二级 / 三级混排，如「商用车」与其子板块「商用载货车」并列）。把 496 个嵌套板块
    直接当轮动股票池，前五持仓会被同一条产业链的父子板块占满，不再是「行业」轮动。
  · 因此改为 GitHub 公开量化实践通用的股票池：**申万一级行业 31 个**，用东方财富对应的
    一级行业板块指数（``90.BKxxxx``）取日线。代码表来自公开仓库
    xp13465/trade-data-signal（``SW_EM_MAP``，2026-07 按名称匹配 clist 得到），并于 2026-09-29
    逐个用东财 kline / 行情页核对名称（全部 31 个一致）。运行时仍以 kline 返回的 ``name``
    复核，名称漂移写进诊断，不会静默用错板块。
  · 独立备用源：申万宏源研究所官方指数发布接口（``swsresearch.com …/index_publish/trend/``，
    801xxx 代码；akshare ``index_hist_sw`` 与 morewnutri/a-share-mainline-scanner 同一入口）。
    单个行业在东财三台主机都取不到日线时才走它；官方源通常滞后一个交易日，滞后行业按
    「锚点不一致」自动不评分，绝不与东财数据混用同一行业。

取数与判死口径（任一环节不达标 → 整栏缺席，并记录原因，绝不补造行情）：
  1. 股票池：31 个申万一级行业（固定表，不再依赖 clist 列表接口）；
  2. 行业日 K：板块指数在「前复权 / 不复权」两种口径下可用性不同，先用样本行业探测
     能返回数据的口径，再按该口径取全部行业；东财整体不可用时探测申万官方源，
     两边都取不到 → 判定接口不可用，不再逐个重试；
  3. 锚点日：各行业最后收盘日的众数，且必须落在最近完整收盘日（北京时间
     15:30 前视为前一日收盘），超过 14 个自然日视为过期；
  4. 评分：需 ≥66 根日线（当前 5 日 + 12 个不重叠 5 日窗口），零赢或零亏的
     行业不评分；可评分行业少于 max(5, 60% × 股票池) → 整栏缺席（覆盖不足时的
     「前五」没有意义）；
  5. 月度持仓：写入存档失败同样整栏缺席（不冒充调仓成功）。

每次运行（成功或失败）都把诊断写入存档的 `industry_rotation_diag` 字段，日报
据此在日志里点名失败原因；该存档（news_history.json）由自动/手动工作流随日报
提交，便于隔日核查「栏目为什么缺席」。
"""
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# 每条数据线 1 主源 + 2 备用源（同格式镜像；注册表见 output/backup_sources.py）
LIST_URLS = (LIST_URL, "https://82.push2.eastmoney.com/api/qt/clist/get",
             "https://72.push2.eastmoney.com/api/qt/clist/get")
KLINE_URLS = (KLINE_URL, "https://91.push2his.eastmoney.com/api/qt/stock/kline/get",
              "https://63.push2his.eastmoney.com/api/qt/stock/kline/get")
# 申万宏源研究所官方指数发布（独立源；返回自 2014 年起的全部日线，只在东财缺席时调用）
SW_TREND_URL = "https://www.swsresearch.com/institute-sw/api/index_publish/trend/"
SW_HEADERS = {"Referer": "https://www.swsresearch.com/"}
# 存档与日报同目录（output/news_history.json）：本模块在 output/octopus_quant/ 下一层，
# 所以往上走两级。密钥沿用「行业轮动」独立命名，绝不与港股「板块轮动」（sector_rotation*）混用。
STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "news_history.json")
MIN_WEEKS = 12
MIN_SCORED = 5            # 纯多头组合固定五只，可评分行业至少五个
MIN_COVERAGE = 0.6        # 且不低于股票池的 60%（31 个 → 至少 19 个），否则「前五」无意义
KLINE_LIMIT = 180         # 日线根数：评分需 66 根，60 日动量 / 月度持仓跟踪都在窗口内
# 行业列表逐页抓取（仅供研究 / 探测脚本使用，主流程不再依赖）：单页 100，最多 6 页。
LIST_PAGE_SIZE = 100
LIST_MAX_PAGES = 6
# 日 K 复权口径候选：先试前复权，再试不复权（板块指数两种口径可用性不同）。
FQT_CANDIDATES = ("1", "0")
# 东财行情接口按来源页校验，带上 Referer 更接近站点真实请求。
EM_HEADERS = {"Referer": "https://quote.eastmoney.com/"}

# 申万一级行业 31 个 → 东方财富一级行业板块指数代码（2021 版申万分类命名）。
# (东财板块代码, 行业名, 申万一级指数代码)。名称已于 2026-09-29 逐个按东财 kline 返回的
# data.name 核对；运行时再次核对，漂移写入诊断 renamed 字段。
SW_L1_SECTORS = (
    ("BK0433", "农林牧渔", "801010"),
    ("BK1206", "基础化工", "801030"),
    ("BK0479", "钢铁", "801040"),
    ("BK0478", "有色金属", "801050"),
    ("BK1201", "电子", "801080"),
    ("BK0456", "家用电器", "801110"),
    ("BK0438", "食品饮料", "801120"),
    ("BK0436", "纺织服饰", "801130"),
    ("BK1212", "轻工制造", "801140"),
    ("BK1216", "医药生物", "801150"),
    ("BK0427", "公用事业", "801160"),
    ("BK1210", "交通运输", "801170"),
    ("BK1202", "房地产", "801180"),
    ("BK1213", "商贸零售", "801200"),
    ("BK1214", "社会服务", "801210"),
    ("BK1217", "综合", "801230"),
    ("BK1208", "建筑材料", "801710"),
    ("BK1209", "建筑装饰", "801720"),
    ("BK1200", "电力设备", "801730"),
    ("BK1204", "国防军工", "801740"),
    ("BK1207", "计算机", "801750"),
    ("BK0486", "传媒", "801760"),
    ("BK1215", "通信", "801770"),
    ("BK1283", "银行", "801780"),
    ("BK1203", "非银金融", "801790"),
    ("BK1211", "汽车", "801880"),
    ("BK1205", "机械设备", "801890"),
    ("BK0437", "煤炭", "801950"),
    ("BK0464", "石油石化", "801960"),
    ("BK0728", "环保", "801970"),
    ("BK1035", "美容护理", "801980"),
)
UNIVERSE_LABEL = "申万一级行业 31 个（东方财富板块指数）"


def _finite(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def _get_json(fetch_json, url, params, timeout=15, headers=None):
    """调用注入的取数函数；老版取数函数不接受 headers 时自动降级。"""
    if headers is None:
        return fetch_json(url, params=params, timeout=timeout)
    try:
        return fetch_json(url, params=params, timeout=timeout, headers=headers)
    except TypeError:
        return fetch_json(url, params=params, timeout=timeout)


def _get_json_chain_indexed(fetch_json, urls, params, ok, timeout=15, headers=None):
    """主源 → 备用源依次尝试，返回 (第一个通过 ok(data) 的响应, 命中的序号)；全部失败 (None, None)。"""
    for index, url in enumerate(urls):
        try:
            data = _get_json(fetch_json, url, params, timeout=timeout, headers=headers)
        except Exception:
            data = None
        if data is None:
            continue
        try:
            if ok(data):
                return data, index
        except Exception:
            continue
    return None, None


def _get_json_chain(fetch_json, urls, params, ok, timeout=15, headers=None):
    """主源 → 备用源依次尝试，返回第一个通过 ok(data) 的响应；全部失败返回 None。"""
    return _get_json_chain_indexed(fetch_json, urls, params, ok, timeout=timeout, headers=headers)[0]


def normalize_universe(universe=None):
    """把股票池统一成 [{code, name, sw}]；缺省用 31 个申万一级行业固定表。"""
    rows = []
    seen = set()
    for item in (SW_L1_SECTORS if universe is None else universe):
        if isinstance(item, dict):
            code, name, sw = item.get("code"), item.get("name"), item.get("sw")
        else:
            parts = tuple(item) + (None, None)
            code, name, sw = parts[0], parts[1], parts[2]
        code = str(code or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        rows.append({"code": code, "name": str(name or code).strip(),
                     "sw": str(sw).strip() if sw else None})
    return rows


def fetch_universe(fetch_json):
    """逐页抓取完整行业板块列表（研究 / 探测用；主流程改用 SW_L1_SECTORS 固定表）。

    任一一页取不到数据、或已知总数大于抓到的条数 → 返回空列表：
    不完整的列表绝不能冒充全行业。注意：东方财富 2026 年起该列表为一级 / 二级 / 三级
    混排（约 496 个），且大页请求在境外出口常被 502 拒绝，不适合作为每日任务的门槛。
    """
    collected = {}
    total = None
    for page in range(1, LIST_MAX_PAGES + 1):
        data = _get_json_chain(fetch_json, LIST_URLS,
                               {"pn": str(page), "pz": str(LIST_PAGE_SIZE), "po": "1", "np": "1",
                                "fltt": "2", "invt": "2", "fid": "f12", "fs": "m:90+t:2",
                                "fields": "f12,f14"},
                               ok=lambda d: bool(((d or {}).get("data") or {}).get("diff")),
                               timeout=15, headers=EM_HEADERS)
        root = (data or {}).get("data") or {}
        diff = root.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not isinstance(diff, list) or not diff:
            return []          # 首页空=接口不可用/参数被拒；中间页空=列表不完整
        for it in diff:
            if isinstance(it, dict) and it.get("f12") and it.get("f14"):
                collected[str(it["f12"])] = str(it["f14"]).strip()
        raw_total = root.get("total")
        if raw_total not in (None, "", "-"):
            try:
                total = int(raw_total)
            except (TypeError, ValueError):
                total = None
        if total is not None and len(collected) >= total:
            break
    if total is not None and len(collected) < total:
        return []              # 抓到的条数少于接口声明的总数 → 判定列表不完整
    return sorted(collected.items())


def _parse_klines(raw):
    rows = {}
    for line in raw or []:
        parts = str(line).split(",")
        if len(parts) < 3:
            continue
        try:
            d = datetime.strptime(parts[0], "%Y-%m-%d").date().isoformat()
        except ValueError:
            continue
        close = _finite(parts[2])
        if close is not None and close > 0:
            rows[d] = close
    return sorted(rows.items())


def fetch_series(fetch_json, code, fqt="1"):
    """单个板块指数的日线：{"name": 东财返回的板块名或 None, "bars": [(date, close)], "via": 命中主机序号}。

    via = 0 主源、1/2 同格式镜像（KLINE_URLS 顺序）、None 三台都没取到。
    """
    data, via = _get_json_chain_indexed(
        fetch_json, KLINE_URLS,
        {"secid": "90." + code, "klt": "101", "fqt": fqt,
         "lmt": str(KLINE_LIMIT), "end": "20500101",
         "fields1": "f1,f2,f3,f4,f5,f6",
         "fields2": "f51,f52,f53,f54,f55,f56"},
        ok=lambda d: bool(((d or {}).get("data") or {}).get("klines")),
        timeout=15, headers=EM_HEADERS)
    root = (data or {}).get("data") or {}
    name = str(root.get("name") or "").strip() or None
    return {"name": name, "bars": _parse_klines(root.get("klines")), "via": via}


def fetch_closes(fetch_json, code, fqt="1"):
    """单个板块指数的日线（date, close）列表；取不到返回空列表。"""
    return fetch_series(fetch_json, code, fqt=fqt)["bars"]


def fetch_sw_series(fetch_json, sw_code):
    """申万官方指数发布接口的日线（独立备用源）；取不到返回空列表。

    响应形如 {"code": "200", "data": [{"bargaindate": "2026-09-24", "closeindex": 4140.14, …}]}，
    一次返回全部历史（2014 年起）。只取收盘价；官方源通常滞后一个交易日，由锚点规则处理。
    """
    if not sw_code:
        return []
    data = _get_json_chain(fetch_json, (SW_TREND_URL,),
                           {"swindexcode": str(sw_code), "period": "DAY"},
                           ok=lambda d: isinstance((d or {}).get("data"), list) and bool(d["data"]),
                           timeout=20, headers=SW_HEADERS)
    rows = {}
    for it in ((data or {}).get("data") or []):
        if not isinstance(it, dict):
            continue
        try:
            d = datetime.strptime(str(it.get("bargaindate"))[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            continue
        close = _finite(it.get("closeindex"))
        if close is not None and close > 0:
            rows[d] = close
    return sorted(rows.items())


def negotiate_fqt(fetch_json, samples):
    """用 1~2 个样本行业确定能返回日线的复权口径，返回 (fqt, 样本日线)。

    样本行业在两种口径下都取不到数据 → 返回 (None, [])：判定东财日 K 不可用，
    不再对余下行业逐个重试（否则网络异常时会把每日任务拖到超时）。
    """
    for code, _name in samples:
        for fqt in FQT_CANDIDATES:
            bars = fetch_closes(fetch_json, code, fqt=fqt)
            if bars:
                return fqt, bars
    return None, []


def _ret(closes, end, n):
    return closes[end] / closes[end - n] - 1 if end - n >= 0 and closes[end - n] > 0 else None


def score_sector(code, name, bars, asof):
    """截至 asof 的 5 日收益；前 12 个不重叠已结算 5 日窗口算胜率与赔率。

    另附 GitHub 公开行业轮动策略常用的动量 / 均线背景因子（20 日、60 日收益、
    偏离 MA20），只作展示与研判语境，不参与综合分排序。
    """
    valid = [(d, _finite(c)) for d, c in bars if d <= asof]
    valid = [(d, c) for d, c in valid if c is not None and c > 0]
    if len(valid) < 5 * (MIN_WEEKS + 1) + 1 or valid[-1][0] != asof:
        return None
    closes = [c for _, c in valid]
    end = len(closes) - 1
    week_return = closes[end] / closes[end - 5] - 1
    # 训练窗口截止于当前 5 日之前；当前一周不参与历史胜率估计。
    returns = [closes[end - 5 - 5 * j] / closes[end - 10 - 5 * j] - 1
               for j in range(MIN_WEEKS)]
    wins = [x for x in returns if x > 0]
    losses = [x for x in returns if x < 0]
    # 无赢或无亏时赔率没有定义：不以零或无穷伪装为正常赔率。
    if not wins or not losses:
        return None
    win_rate = len(wins) / MIN_WEEKS
    odds = (sum(wins) / len(wins)) / (-sum(losses) / len(losses))
    odds_score = odds / (1 + odds)  # 单调有界 0~1，避免极端赔率主导
    composite = 100 * (0.8 * win_rate + 0.2 * odds_score)
    ma20 = sum(closes[end - 19:end + 1]) / 20 if end >= 19 else None
    return {"code": code, "name": name, "asof": asof, "week_return": week_return,
            "win_rate": win_rate, "odds": odds, "score": composite, "weeks": MIN_WEEKS,
            "ret20": _ret(closes, end, 20), "ret60": _ret(closes, end, 60),
            "ma20_gap": (closes[end] / ma20 - 1) if ma20 else None}


def build_scores(series, asof):
    scores = [score_sector(code, name, bars, asof) for code, name, bars in series]
    return sorted((s for s in scores if s), key=lambda s: (-s["score"], s["code"]))


def breadth_summary(scores):
    """行业宽度：近 5 日上涨行业数、站上 MA20 行业数、全行业近 5 日等权均值。"""
    if not scores:
        return None
    weeks = [s["week_return"] for s in scores if s.get("week_return") is not None]
    gaps = [s["ma20_gap"] for s in scores if s.get("ma20_gap") is not None]
    return {"total": len(scores),
            "up_week": sum(1 for x in weeks if x > 0),
            "above_ma20": sum(1 for x in gaps if x > 0) if gaps else None,
            "avg_week": (sum(weeks) / len(weeks)) if weeks else None}


def allocate(scores):
    """前五权重与综合得分成正比；不得做空或虚构不满五只的组合。"""
    if len(scores) < 5:
        return []
    top = scores[:5]
    total = sum(s["score"] for s in top)
    if total <= 0:
        return []
    return [{"code": s["code"], "name": s["name"], "score": s["score"],
             "weight": s["score"] / total} for s in top]


def _load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            previous = json.load(f)
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    previous.setdefault("version", 1)
    previous.setdefault("items", [])
    return previous


def _persist(path, mutate):
    """读-改-写存档（原子替换），保持 items 等其它字段不被覆盖。"""
    previous = _load_state(path)
    mutate(previous)
    temp = path + ".tmp"
    try:
        # 与 pipeline._save_news_corpus 同一格式（ensure_ascii=False / indent=1 / 无尾换行）：
        # 两个写入方共用 news_history.json，格式不一致会让每轮运行产生整文件 diff
        # （tools/merge_generated.py 的并集合并按该格式归一化，字节级不一致会被判成改动）。
        with open(temp, "w", encoding="utf-8") as f:
            f.write(json.dumps(previous, ensure_ascii=False, indent=1))
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save_diag(path, diag):
    """把本次运行的诊断写进存档，便于隔日核查栏目缺席原因（失败也写）。"""
    try:
        def _set(state):
            state["industry_rotation_diag"] = diag
        _persist(path, _set)
    except OSError:
        pass                   # 诊断写不进去不影响本次日报


def monthly_holdings(scores, asof, path=None):
    """同一月保留原持仓；先原子写入新月快照再返回。失败则不冒充调仓成功。"""
    path = path or STATE_FILE
    month = asof[:7]
    previous = _load_state(path)
    saved = previous.get("industry_rotation") or {}
    if len(saved.get("holdings") or []) == 5 and saved.get("month", "") >= month:
        return saved
    holdings = allocate(scores)
    if not holdings:
        return None
    state = {"month": month, "rebalance_date": asof, "holdings": holdings}
    previous["industry_rotation"] = state

    def _set(target):
        target["industry_rotation"] = state
    _persist(path, _set)
    return state


def _since_exec(bars, rebalance_date, asof):
    """执行日（调仓信号日之后第一个交易日）收盘 → asof 收盘的收益；未到执行日返回 None。"""
    exec_bar = next(((d, c) for d, c in bars if d > rebalance_date), None)
    if not exec_bar or exec_bar[0] > asof or not bars or bars[-1][0] != asof:
        return None, exec_bar[0] if exec_bar else None
    if exec_bar[0] == asof:
        return 0.0, exec_bar[0]
    return bars[-1][1] / exec_bar[1] - 1, exec_bar[0]


def track_holdings(state, bars_by_code, asof):
    """月度持仓自执行日以来的表现（反馈闭环）：逐只、组合加权、全行业等权基准。

    信号在 rebalance_date 收盘产生、下一交易日执行，因此统一从执行日收盘起算，
    不把信号日当天的涨跌算进组合（避免高估）。任一持仓算不出就不给组合合计。
    """
    holdings = (state or {}).get("holdings") or []
    reb = (state or {}).get("rebalance_date") or ""
    if not holdings or not reb:
        return None
    rows, exec_dates = [], set()
    for h in holdings:
        ret, exec_date = _since_exec(bars_by_code.get(h["code"]) or [], reb, asof)
        if exec_date:
            exec_dates.add(exec_date)
        rows.append({"code": h["code"], "name": h["name"], "weight": h["weight"],
                     "since_exec": ret})
    complete = all(r["since_exec"] is not None for r in rows)
    portfolio = sum(r["weight"] * r["since_exec"] for r in rows) if complete else None
    bench_rets = [r for r in (_since_exec(bars, reb, asof)[0] for bars in bars_by_code.values())
                  if r is not None]
    benchmark = (sum(bench_rets) / len(bench_rets)) if (complete and bench_rets) else None
    exec_date = min(exec_dates) if exec_dates else None
    return {"rebalance_date": reb, "exec_date": exec_date, "pending": not complete,
            "holdings": rows, "portfolio": portfolio, "benchmark": benchmark,
            "benchmark_n": len(bench_rets) if complete else 0}


def _fetch_one(fetch_json, sector, fqt, cutoff, em_ok=True):
    """单个行业：东财三主机 → 申万官方源；返回 (sector, live_name, bars, source)。

    source："eastmoney"（主源）/ "eastmoney_mirror"（同格式镜像）/ "sw_official" / None。
    """
    live_name, bars, source = None, [], None
    if em_ok and fqt:
        got = fetch_series(fetch_json, sector["code"], fqt)
        live_name, bars = got["name"], got["bars"]
        if bars:
            source = "eastmoney" if not got.get("via") else "eastmoney_mirror"
    if not bars and sector.get("sw"):
        bars = fetch_sw_series(fetch_json, sector["sw"])
        if bars:
            source = "sw_official"
    # 当天盘中不含当日未收盘 K 线（北京时间 15:30 后视为完整）。
    bars = [(d, c) for d, c in bars if d <= cutoff]
    return sector, live_name, bars, (source if bars else None)


def run(fetch_json, *, state_path=None, now=None, universe=None):
    """只有足够且同步的完整收盘数据才产出栏目；单行业失败只剔除该行业。

    返回值：成功 {"available": True, ...}；失败 {"available": False,
    "reason": "...", "diag": {...}}。诊断同时落盘到存档的
    ``industry_rotation_diag``，供日报日志与隔日核查使用。

    state_path 缺省用 STATE_FILE（运行时可整体替换，便于离线测试隔离）；
    universe 缺省用 SW_L1_SECTORS（31 个申万一级行业），可注入自定义名单。
    """
    state_path = state_path or STATE_FILE
    now = now or datetime.now(CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    local_now = now.astimezone(CST)
    today = local_now.date()
    diag = {"ran_at": local_now.strftime("%Y-%m-%d %H:%M:%S"),
            "cutoff": (today if (local_now.hour, local_now.minute) >= (15, 30)
                       else today - timedelta(days=1)).isoformat()}

    def _fail(reason):
        diag["reason"] = reason
        save_diag(state_path, diag)
        return {"available": False, "reason": reason, "diag": diag}

    sectors = normalize_universe(universe)
    diag["universe"] = len(sectors)
    diag["universe_kind"] = "sw_l1_fixed" if universe is None else "custom"
    if len(sectors) < MIN_SCORED:
        return _fail(f"行业名单不足五个（{len(sectors)} 个）")

    # 口径探测样本：名单首尾各半取一个（两个都失败才认定东财整体不可用）
    samples = [sectors[0], sectors[len(sectors) // 2]]
    fqt, _sample = negotiate_fqt(fetch_json, [(s["code"], s["name"]) for s in samples])
    diag["fqt"] = fqt
    em_ok = bool(fqt)
    if not em_ok:
        # 东财日 K 整体不可用：只有独立源也能给出样本日线时才继续（全走申万官方）
        sw_probe = next((s["sw"] for s in sectors if s.get("sw")), None)
        if not sw_probe or not fetch_sw_series(fetch_json, sw_probe):
            return _fail("行业日K接口未返回数据（东财前复权/不复权两种口径均无有效日线"
                         + ("，申万官方源亦无数据）" if sw_probe else "）"))
        diag["fallback"] = "sw_official"

    series, bars_by_code, sources, missing, renamed = [], {}, {}, [], {}
    served_by = {}                                   # 行业代码 -> 实际供数的源

    def _accept(sector, live_name, bars, source):
        if live_name and live_name != sector["name"]:
            renamed[sector["code"]] = {"expected": sector["name"], "live": live_name}
        series.append((sector["code"], live_name or sector["name"], bars))
        bars_by_code[sector["code"]] = bars
        sources[source] = sources.get(source, 0) + 1
        served_by[sector["code"]] = source

    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = [pool.submit(_fetch_one, fetch_json, s, fqt, diag["cutoff"], em_ok)
                for s in sectors]
        for job in as_completed(jobs):
            try:
                sector, live_name, bars, source = job.result()
            except Exception:
                continue
            if not bars:
                missing.append(sector["code"])
                continue
            _accept(sector, live_name, bars, source)
    # 首轮失败的行业再补一次（串行）：镜像偶发 5xx 不该让整个行业消失
    retry_codes = list(missing)
    for sector in (s for s in sectors if s["code"] in retry_codes):
        try:
            sector, live_name, bars, source = _fetch_one(fetch_json, sector, fqt,
                                                         diag["cutoff"], em_ok)
        except Exception:
            continue
        if bars:
            missing.remove(sector["code"])
            _accept(sector, live_name, bars, source)
    series.sort(key=lambda row: row[0])
    diag["kline_series"] = len(series)
    diag["sources"] = sources
    diag["missing"] = sorted(missing)
    if renamed:
        diag["renamed"] = renamed
    mirror = sorted(c for c, src in served_by.items() if src == "eastmoney_mirror")
    sw_fallback = sorted(c for c, src in served_by.items() if src == "sw_official")
    if mirror:
        diag["em_mirror"] = mirror
    if sw_fallback:
        diag["sw_fallback"] = sw_fallback
    if len(series) < MIN_SCORED:
        return _fail(f"可用行业日线不足五个（{len(series)}/{len(sectors)} 个行业取到日线）")
    # 取各行业最后收盘日的众数，避免不同日期价格混用；允许个别行业缺失。
    dates = [bars[-1][0] for _, _, bars in series]
    asof = sorted(set(dates), key=lambda d: (-dates.count(d), d))[0]
    diag["asof"] = asof
    diag["asof_hits"] = dates.count(asof)
    if (today - datetime.strptime(asof, "%Y-%m-%d").date()).days > 14:
        return _fail(f"行业日线已过期（最新共同收盘日 {asof}，距今超过 14 个自然日）")
    scores = build_scores(series, asof)
    diag["scored"] = len(scores)
    need = max(MIN_SCORED, math.ceil(MIN_COVERAGE * len(sectors)))
    diag["required"] = need
    if len(scores) < need:
        return _fail(f"可评分行业覆盖不足（{len(scores)}/{len(sectors)} 个行业满足评分条件，"
                     f"至少需要 {need} 个）")
    try:
        before = _load_state(state_path).get("industry_rotation") or {}
        state = monthly_holdings(scores, asof, state_path)
    except OSError as exc:
        return _fail(f"月度持仓无法存档：{exc}")
    diag["state"] = "reused" if before and before == state else "built"
    diag["holdings"] = len((state or {}).get("holdings") or [])
    if not state or len(state.get("holdings") or []) != 5:
        return _fail("月度持仓未能生成五只组合（评分或权重计算未通过）")
    tracking = track_holdings(state, bars_by_code, asof)
    save_diag(state_path, diag)
    return {"available": True, "asof": asof, "scores": scores, "state": state,
            "universe_count": len(sectors), "scored_count": len(scores),
            "universe_label": UNIVERSE_LABEL if universe is None else f"自定义名单 {len(sectors)} 个",
            "breadth": breadth_summary(scores), "tracking": tracking,
            "sources": sources, "missing": sorted(missing), "diag": diag}
