#!/usr/bin/env python3
# 虫控消杀确认平台 —— 云端后台（部署到 Render / Railway / Koyeb 等 PaaS）
# 设计要点：
#  - 端口读环境变量 PORT（PaaS 要求），默认 5000
#  - 监听 0.0.0.0（云环境必须，否则外部访问不到）
#  - 数据库：若设置了 DATABASE_URL（Postgres，云平台自动注入）则用它；
#            否则回退本地/卷 SQLite（DATA_DIR 可配置，默认脚本目录或 /data）
#  - 接口为「逐条追加 / 单日确认」模型，多端（服务商手机、我司电脑）同时写不会互相覆盖
#  - 本机同步脚本用 GET /api/state 拉全量，再写入企微智能表格（按 记录ID 去重）
import os, io, json, datetime, sqlite3, re
from flask import Flask, request, jsonify, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get("DATABASE_URL")
USE_PG = bool(DATABASE_URL)
if USE_PG:
    import psycopg2
    DB_PATH = None
else:
    DATA_DIR = os.environ.get("DATA_DIR") or ("/data" if os.path.isdir("/data") else BASE)
    DB_PATH = os.path.join(DATA_DIR, "xk.db")

app = Flask(__name__, static_folder=None)

@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

@app.after_request
def _no_store_html(resp):
    # HTML 页面禁止缓存：避免手机浏览器长期使用旧版页面（旧版会导致签名上传失败却不报错）
    ct = resp.headers.get("Content-Type", "")
    if "text/html" in ct:
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

def conn():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(DB_PATH)

def cur(c):
    return c.cursor()

def adapt(sql):
    # 把 ? 占位符统一成目标方言（SQLite 用 ?，Postgres 用 %s）
    return sql.replace("?", "%s") if USE_PG else sql

def ensure():
    c = conn(); k = cur(c)
    k.execute(adapt("CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, data TEXT, created_at TEXT)"))
    k.execute(adapt("CREATE TABLE IF NOT EXISTS confirmations(date TEXT PRIMARY KEY, data TEXT)"))
    k.execute(adapt("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)"))
    k.execute(adapt("CREATE TABLE IF NOT EXISTS drugs(id TEXT PRIMARY KEY, data TEXT)"))
    # 现场服务报告：一份报告 = 主表 SR + 9 张子表，整份以 JSON 保存（点位多但只存填过的行）
    k.execute(adapt("CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY, date TEXT, data TEXT, created_at TEXT, updated_at TEXT)"))
    # 现场人员：姓名/岗位/上岗证/健康证（证件图为压缩后 base64 存 JSON）
    k.execute(adapt("CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY, data TEXT, created_at TEXT, updated_at TEXT)"))
    # 杀虫剂清单：13 字段纯文本条目
    k.execute(adapt("CREATE TABLE IF NOT EXISTS insecticides(id TEXT PRIMARY KEY, data TEXT, created_at TEXT, updated_at TEXT)"))
    c.commit(); c.close()

def get_meta(kk, d=None):
    c = conn(); k = cur(c)
    k.execute(adapt("SELECT value FROM meta WHERE key=?"), (kk,))
    r = k.fetchone(); c.close()
    return r[0] if r else d

def set_meta(kk, v):
    c = conn(); k = cur(c)
    k.execute(adapt("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=?"), (kk, v, v))
    c.commit(); c.close()

def report_summary(r):
    """列表只回摘要，不回 9 张子表的大对象"""
    return {"id": r.get("id"), "date": r.get("date", ""),
            "title": r.get("title") or ("现场服务报告 " + str(r.get("date", ""))),
            "client": r.get("client", ""), "progress": r.get("progress", 0),
            "createdAt": r.get("createdAt", ""), "updatedAt": r.get("updatedAt", "")}

def worker_summary(w):
    """人员列表摘要：不带证件图片（base64 大），查看证件单独拉详情"""
    return {"id": w.get("id"), "name": w.get("name", ""), "post": w.get("post", ""),
            "healthExpiry": w.get("healthExpiry", ""),
            "hasUpCert": bool(w.get("certUp")), "hasHealthCert": bool(w.get("certHealth")),
            "createdAt": w.get("createdAt", ""), "updatedAt": w.get("updatedAt", "")}

def read_state():
    c = conn(); k = cur(c)
    k.execute(adapt("SELECT data FROM records ORDER BY created_at"))
    crecs = [json.loads(r[0]) for r in k.fetchall()]
    k.execute(adapt("SELECT date,data FROM confirmations"))
    conf = {d: json.loads(v) for d, v in k.fetchall()}
    k.execute(adapt("SELECT data FROM drugs ORDER BY id"))
    cdr = [json.loads(r[0]) for r in k.fetchall()]
    k.execute(adapt("SELECT data FROM reports ORDER BY created_at DESC"))
    crep = [report_summary(json.loads(r[0])) for r in k.fetchall()]
    k.execute(adapt("SELECT data FROM workers ORDER BY created_at"))
    cwk = [worker_summary(json.loads(r[0])) for r in k.fetchall()]
    k.execute(adapt("SELECT data FROM insecticides ORDER BY created_at DESC"))
    cins = [json.loads(r[0]) for r in k.fetchall()]
    c.close()
    return {"provider": get_meta("provider", "能多洁"), "records": crecs,
            "confirmations": conf, "drugs": cdr, "reports": crep, "workers": cwk,
            "insecticides": cins}

# 构建标识：取 index.html 的修改时间，每次部署都会变。
# 用途：页面里内嵌这个值，前端发现「自己带的版本」与「服务器当前版本」不一致时自动刷新一次，
#       根治手机浏览器缓存旧版页面（旧版行为差异会让人以为是功能没生效）。
def _build_id():
    try:
        newest = 0
        for fn in ("index.html", "admin.html", "report.html", "stations.js", "server.py", "portal.html", "staff.html", "insecticides.html", "home.html"):
            p = os.path.join(BASE, fn)
            if os.path.exists(p):
                newest = max(newest, int(os.path.getmtime(p)))
        return str(newest)
    except Exception:
        return "0"
BUILD_ID = _build_id()

def _serve_page(filename):
    """读取页面并把 __BUILD__ 占位替换成当前构建号（不缓存，保证手机每次拿到最新版）"""
    try:
        with open(os.path.join(BASE, filename), "r", encoding="utf-8") as f:
            html = f.read()
    except Exception:
        return jsonify({"ok": False, "msg": "页面读取失败"}), 500
    html = html.replace("__BUILD__", BUILD_ID)
    resp = app.response_class(html, content_type="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/")
def index():
    return _serve_page("portal.html")

@app.route("/daily")
@app.route("/daily.html")
def daily_page():
    return _serve_page("index.html")

@app.route("/xlsx.full.min.js")
def xlsx_js():
    return send_from_directory(BASE, "xlsx.full.min.js")

@app.route("/config.js")
def config_js():
    return send_from_directory(BASE, "config.js")

@app.route("/stations.js")
def stations_js():
    resp = send_from_directory(BASE, "stations.js")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/report")
@app.route("/report.html")
def report_page():
    return _serve_page("report.html")

@app.route("/admin")
@app.route("/admin.html")
def admin_page():
    return _serve_page("admin.html")

@app.route("/staff")
@app.route("/staff.html")
def staff_page():
    return _serve_page("staff.html")

@app.route("/insecticides")
@app.route("/insecticides.html")
def insecticides_page():
    return _serve_page("insecticides.html")

@app.route("/home")
@app.route("/home.html")
def home_page():
    return _serve_page("home.html")

@app.route("/api/version")
def api_version():
    return jsonify({"build": BUILD_ID})

@app.route("/api/health")
def health():
    return jsonify({"ok": True, "db": "postgres" if USE_PG else "sqlite", "build": BUILD_ID})

# ---- 文档解析：把 PDF / Word(.docx) 里的文字抽出来，再按标签识别 13 个字段 ----
# 字段标签（一个字段可有多个别名，按文档常见写法兜底）
_DRUG_LABELS = [
    ("tradeName",  ["商品名称", "商品名", "品名", "产品名称"]),
    ("drugName",   ["药品名称", "通用名", "农药名称"]),
    ("regCert",    ["农药登记证", "登记证", "登记证号", "农药登记证号", "登记号"]),
    ("validUntil", ["有效期限", "有效期", "有效期至", "保质期", "保质期至", "过期日期"]),
    ("ingredients",["有效成分", "有效成分含量", "成分"]),
    ("form",       ["剂型"]),
    ("toxicity",   ["毒性", "毒性级别", "毒性分级", "毒性(级)"]),
    ("maker",      ["生产商", "生产厂", "生产厂家", "生产厂商", "生产企业", "公司名称", "制造厂"]),
    ("usage",      ["使用方法", "使用技术", "使用说明", "使用技术和方法"]),
    ("targets",    ["目标虫害", "防治对象", "靶标", "防治靶标", "适用害虫"]),
    ("mixMethod",  ["配置方法", "稀释方法", "使用剂量", "稀释倍数", "稀释比例", "配药方法", "配制方法"]),
    ("places",     ["使用场所", "使用范围", "适用场所", "适用范围", "施药场所"]),
    ("packSpec",   ["包装规格", "规格", "净含量", "包装"]),
]
_ALL_LABEL_RE = re.compile("(" + "|".join(re.escape(v) for _, vs in _DRUG_LABELS for v in vs) + ")")
def parse_drug_fields(text):
    fields = {}
    def grab(variants):
        # 别名按长度降序，避免「农药登记证」抢先匹配「农药登记证号」
        for line in text.splitlines():
            for v in sorted(variants, key=len, reverse=True):
                m = re.search(re.escape(v) + r"\s*[:：]?\s*(.*)", line)
                if m and m.group(1).strip():
                    val = m.group(1).strip()
                    # 同一行可能紧跟下一个字段标签，截断它
                    val = _ALL_LABEL_RE.split(val)[0].strip(" ：:、-—")
                    if val:
                        return val
        return ""
    for key, variants in _DRUG_LABELS:
        val = grab(variants)
        if key == "validUntil":
            dm = re.search(r"(\d{4})[.\-/年](\d{1,2})[.\-/月](\d{1,2})", val or "")
            if dm:
                val = "%s-%02d-%02d" % (dm.group(1), int(dm.group(2)), int(dm.group(3)))
            else:
                val = ""  # 没提取到日期就不填，避免误填
        fields[key] = val
    return fields

@app.route("/api/extract-text", methods=["POST"])
def extract_text():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "msg": "未收到文件"}), 400
    name = (f.filename or "").lower()
    raw = f.read()
    try:
        if name.endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
        elif name.endswith(".docx"):
            import docx
            d = docx.Document(io.BytesIO(raw))
            text = "\n".join(p.text for p in d.paragraphs)
        elif name.endswith(".doc"):
            return jsonify({"ok": False, "msg": "暂不支持旧版 .doc，请在 Word 里另存为 .docx 或导出 PDF 后再导入"}), 400
        else:
            return jsonify({"ok": False, "msg": "仅支持 PDF 或 Word(.docx) 文件"}), 400
    except Exception as e:
        return jsonify({"ok": False, "msg": "文档解析失败：" + str(e)[:80]}), 400
    text = (text or "").strip()
    if not text:
        return jsonify({"ok": False, "msg": "未能从文档提取到文字（可能是扫描图片版 PDF，请用文字版）"}), 400
    return jsonify({"ok": True, "text": text[:4000], "fields": parse_drug_fields(text)})


# ===== 现场服务报告 xlsx 一键导入 =====
# 9 张表按 stations.js 的 SH/ST 结构映射；签名区一律跳过
RPT_CHECK = ["E", "H", "G", "F-1", "F-2"]
RPT_FREE = ["消杀记录", "物料进出厂清单", "CAPA"]
# 各表列键（与 stations.js 的 SH[n].cols 顺序一致，由前端 schema 传入为准）
RPT_DEFAULT_COLS = {
    "E": ["a", "b", "c", "d", "e"],
    "H": ["a", "b", "c", "d"],
    "G": ["a", "b", "c", "d", "e", "f"],
    "F-1": ["a", "b", "c", "d", "e", "f", "g"],
    "F-2": ["a", "b", "c", "d", "e", "f", "g"],
    "消杀记录": ["date", "place", "drug", "target", "method", "regNo", "ratio", "operator", "note"],
    "物料进出厂清单": ["name", "bringIn", "used", "waste", "remain", "note"],
    "CAPA": ["no", "date", "place", "missing", "reason", "action", "due", "owner", "before", "after", "done", "closed"],
}
RPT_MATERIAL_HEAD = ["产品名称", "批号", "靶标生物", "药品登记号", "具体使用地点", "使用方法", "浓度剂量", "用量"]
# 计数列（stations.js 里 type==="01"）：只保留数字，原表的 "-" / "·" / "/" 视为无异常 -> 0
RPT_NUM_KEYS = {
    "E": ["a", "b"], "H": ["a"], "G": ["a", "b", "c"],
    "F-1": ["a", "b", "c", "d", "e"], "F-2": ["a", "b", "c", "d", "e"],
}
RPT_FREE_ROWS = {"消杀记录": 30, "物料进出厂清单": 34, "CAPA": 18}
# SR 主表复选框字段的候选项（与 stations.js opts 一致）
RPT_SR_OPTS = {
    "pests": ["鼠类", "蟑螂", "蚂蚁", "蝇", "蚊", "其他"],
    "visitType": ["首次", "常规", "评估", "紧急", "跟进", "一次性"],
    "q1Pests": ["鼠", "蟑螂", "飞虫", "其他"],
    "attachments": ["图片简报", "风险清单", "检查记录", "消杀记录"],
    "risk": ["高", "中", "低"],
}


def RPT_cstr(v):
    """单元格值 -> 字符串（时间/日期/数字统一格式化）"""
    if v is None:
        return ""
    if isinstance(v, datetime.datetime):
        if v.year <= 1900:  # Excel 里的时间常存成 1900-01-02 08:30:00
            return v.strftime("%H:%M")
        if v.hour == 0 and v.minute == 0 and v.second == 0:
            return v.strftime("%Y-%m-%d")
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, datetime.date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, datetime.time):
        return v.strftime("%H:%M")
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else str(v)
    if isinstance(v, bool):
        return "是" if v else "不是"
    return str(v).strip()


def RPT_norm_date(s, year_hint=None):
    """把 '2026年/Y11月/M 2日/D' / '11月2日' / '2025-08-18' 统一成 YYYY-MM-DD"""
    if not s:
        return ""
    s = str(s)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    nums = re.findall(r"\d{1,4}", re.sub(r"[年月日YMD/\s:]+", "-", s))
    if len(nums) >= 3 and len(nums[0]) == 4:
        return "%s-%02d-%02d" % (nums[0], int(nums[1]), int(nums[2]))
    if len(nums) >= 2 and year_hint:
        return "%s-%02d-%02d" % (year_hint, int(nums[0]), int(nums[1]))
    return ""


RPT_SIGN_WORDS = ("签字", "签名", "signature", "盖章")
def RPT_is_sign_row(row):
    """签名区 / 说明行：一律跳过，绝不进入导入数据"""
    line = " ".join(v for v in row if v)
    if any(w in line.lower() for w in RPT_SIGN_WORDS):
        return True
    first = (row[0] if row else "").strip()
    return first.startswith("备注：") or first.startswith("备注:")


def RPT__grid(ws):
    return [[RPT_cstr(c) for c in row] for row in ws.iter_rows(values_only=True)]


def RPT__row_nonempty(g, i):
    return [j for j, v in enumerate(g[i]) if v] if i < len(g) else []


def RPT_parse_report_xlsx(raw, schema=None):
    """
    返回 (report, summary, error)
    report = {"date": "YYYY-MM-DD", "sheets": {...}}
    签名区（操作人员签字 / 客户签字）完全跳过，不产生任何签名字段。
    """
    schema = schema or {}
    ids_map = schema.get("ids") or {}
    cols_map = schema.get("cols") or RPT_DEFAULT_COLS
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as e:
        return None, None, "Excel 打开失败：%s" % str(e)[:80]

    names = set(wb.sheetnames)
    missing = [n for n in (["SR"] + RPT_CHECK + RPT_FREE) if n not in names]
    if "SR" not in names:
        return None, None, "这不是现场服务报告（缺少 SR 工作表）"

    sheets = {}
    summary = {"sheets": {}, "missing": missing, "signSkipped": True}

    # ---------- SR 主表 ----------
    g = RPT__grid(wb["SR"])
    fields = {}

    def find_label(word, start=0, end=None):
        end = end or len(g)
        for i in range(start, min(end, len(g))):
            for j, v in enumerate(g[i]):
                if word in v:
                    return i, j
        return None

    def right_value(i, j, maxgap=4):
        for k in range(j + 1, min(j + 1 + maxgap, len(g[i]))):
            if g[i][k]:
                return g[i][k]
        return ""

    # 日期 / 时间
    date_iso = ""
    loc = find_label("服务日期")
    if loc:
        date_iso = RPT_norm_date(right_value(*loc))
    loc = find_label("开始时间")
    if loc:
        v = right_value(*loc)
        m = re.search(r"(\d{1,2}):(\d{2})", v)
        if m:
            fields["startTime"] = "%02d:%s" % (int(m.group(1)), m.group(2))
    loc = find_label("完成时间")
    if loc:
        v = right_value(*loc)
        m = re.search(r"(\d{1,2}):(\d{2})", v)
        if m:
            fields["finishTime"] = "%02d:%s" % (int(m.group(1)), m.group(2))
    # 天气（单元格形如「天气：晴」）
    for row in g[:6]:
        for v in row:
            m = re.search(r"天气\s*[:：]\s*(\S+)", v)
            if m:
                fields["weather"] = m.group(1)
                break
        if fields.get("weather"):
            break

    # 区域定位：目标有害生物 -> q1 -> 补充附件
    r_target = (find_label("目标有害生物") or [None])[0]
    r_q1 = (find_label("1.检查中是否发现害虫") or find_label("1. 检查中是否发现害虫") or [None])[0]
    r_attach = (find_label("可能包含补充附件") or [None])[0]
    r_material = (find_label("物料使用记录") or [None])[0]
    r_service = (find_label("服务内容") or [None])[0]

    def checked_in(row_from, row_to, opt):
        """在指定行区间里找 ■+选项 的单元格"""
        for i in range(max(0, row_from or 0), min(row_to or len(g), len(g))):
            for v in g[i]:
                if v.startswith("■"):
                    body = v[1:].split("/")[0].strip()
                    if body == opt:
                        return True
        return False

    if r_target is not None and r_q1 is not None:
        fields["pests"] = [o for o in RPT_SR_OPTS["pests"] if checked_in(r_target, r_q1, o)]
        fields["visitType"] = [o for o in RPT_SR_OPTS["visitType"] if checked_in(r_target, r_q1, o)]
    if r_q1 is not None:
        fields["q1Pests"] = [o for o in RPT_SR_OPTS["q1Pests"] if checked_in(r_q1, r_q1 + 1, o)]
    if r_attach is not None:
        fields["attachments"] = [o for o in RPT_SR_OPTS["attachments"] if checked_in(r_attach, r_attach + 3, o)]
        for o in RPT_SR_OPTS["risk"]:
            if checked_in(r_attach, r_attach + 3, o):
                fields["risk"] = o
                break

    # 四个是/否问题：按问题文字定位，在该行找 ■是 / ■不是
    q_labels = [
        ("q1", "1.检查中是否发现害虫"), ("q2", "2.检查中是否发现建筑结构漏洞"),
        ("q3", "3.检查中是否发现卫生清洁问题"), ("q4", "4.上一次"),
    ]
    for key, label in q_labels:
        loc = find_label(label)
        if not loc:
            continue
        line = " ".join(g[loc[0]])
        cells = [v for v in g[loc[0]] if v]
        if any(v.startswith("■") and "不是" in v for v in cells):
            fields[key] = "不是"
        elif any(v.startswith("■") and re.search(r"是(?!非)", v) and "不是" not in v for v in cells):
            fields[key] = "是"

    # 服务内容 / 建议：按列收集多行文本
    if r_service is not None and r_material is not None:
        def collect(col):
            parts = []
            for i in range(r_service + 1, r_material):
                v = g[i][col] if col < len(g[i]) else ""
                if v and "服务内容" not in v and "Service Details" not in v:
                    parts.append(v)
            return "\n".join(parts).strip()
        a = collect(0)
        if a:
            fields["serviceDetail"] = a
        # 建议列：找 r_service 行里含「服务措施」的列
        hj = next((j for j, v in enumerate(g[r_service]) if "服务措施" in v), 7)
        b = collect(hj)
        if b:
            fields["recommend"] = b

    # 物料使用记录（SR 内）：按表头文字定位列，再取数据行
    materials = []
    if r_material is not None:
        hdr_i = None
        col_idx = {}
        for i in range(r_material, min(r_material + 4, len(g))):
            row = g[i]
            if "产品名称" in " ".join(row):
                hdr_i = i
                for h in RPT_MATERIAL_HEAD:
                    col_idx[h] = next((j for j, v in enumerate(row) if h in v), None)
                break
        if hdr_i is not None:
            stop = r_attach if r_attach is not None else len(g)
            for i in range(hdr_i + 1, stop):
                row = g[i]
                if not any(row):
                    continue
                rec = {}
                for x, h in enumerate(RPT_MATERIAL_HEAD):
                    j = col_idx.get(h)
                    v = row[j] if (j is not None and j < len(row)) else ""
                    if v:
                        rec["m" + str(x)] = v
                if rec and rec.get("m0"):
                    materials.append(rec)

    # 服务日期同时写进 SR.fields.date，供填单页显示
    if date_iso:
        fields["date"] = date_iso
    sheets["SR"] = {"fields": fields, "materials": materials}
    summary["sheets"]["SR"] = {"materialRows": len(materials), "fields": len(fields)}

    # ---------- 点位检查表 ----------
    year_hint = date_iso[:4] if date_iso else None
    for n in RPT_CHECK:
        if n not in names:
            sheets[n] = {}
            continue
        g = RPT__grid(wb[n])
        keys = cols_map.get(n) or RPT_DEFAULT_COLS[n]
        allowed = set(str(x) for x in ids_map.get(n, []))
        # 找表头行（A 列含「编号」），数据从其后开始；也支持直接按编号正则扫全表
        # 兼容 A3-01 / 2E35 / LW001 / CY101 等带横杠或前导数字的编号
        id_re = re.compile(r"^[0-9]?[A-Za-z]{1,3}[0-9]{0,2}-?\d{1,4}$")
        data = {}
        for row in g:
            if not row or not row[0]:
                continue
            sid = row[0].strip()
            if allowed:
                if sid not in allowed:
                    continue
            elif not id_re.match(sid):
                continue
            rec = {}
            numkeys = RPT_NUM_KEYS.get(n, [])
            for x, k in enumerate(keys):
                j = x + 1  # B 列起
                v = row[j] if j < len(row) else ""
                if v:
                    if k in numkeys:
                        # 计数格：只留数字；"-"/"·"/"/" 等占位视为无异常 0
                        digits = re.sub(r"[^\d]", "", v)
                        v = digits if digits else "0"
                    rec[k] = v
            if rec:
                data[sid] = rec
        sheets[n] = data
        summary["sheets"][n] = {"filled": len(data), "expected": len(allowed) or None}

    # ---------- 自由行表 ----------
    for n in RPT_FREE:
        if n not in names:
            sheets[n] = []
            continue
        g = RPT__grid(wb[n])
        keys = cols_map.get(n) or RPT_DEFAULT_COLS[n]
        # 表头行 = 含该表第一个列名最多者；数据行从其后，排除双层表头
        head_titles = {n: []}
        skip_words = ("消杀记录", "日期", "物料名称", "带入数量", "使用量", "废弃物料",
                      "剩余数量", "备注", "药品及物料进出工厂记录清单", "序号", "位置")
        rows = []
        for row in g:
            if not row or not row[0]:
                continue
            # 签名区（操作人员签字 / 客户签字）与表尾说明一律跳过
            if RPT_is_sign_row(row):
                continue
            first = row[0].strip()
            if first in skip_words or first.startswith("带入") or first.startswith("带出"):
                continue
            rec = {}
            for x, k in enumerate(keys):
                v = row[x] if x < len(row) else ""
                if not v:
                    continue
                if k in ("date", "due", "done"):
                    v = RPT_norm_date(v, year_hint)
                    if not v:
                        continue
                rec[k] = v
            if rec:
                rows.append(rec)
        # 限制在平台各表最大行数内
        maxrows = RPT_FREE_ROWS.get(n, 30)
        sheets[n] = rows[:maxrows]
        summary["sheets"][n] = {"rows": len(sheets[n])}

    return {"date": date_iso, "sheets": sheets}, summary, None


@app.route("/api/parse-report-xlsx", methods=["POST"])
def api_parse_report_xlsx():
    """现场服务报告 xlsx -> 平台报告数据结构（签名一律留空，不解析签字区）"""
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "msg": "未收到文件"}), 400
    name = (f.filename or "").lower()
    if not name.endswith(".xlsx"):
        return jsonify({"ok": False, "msg": "请上传 .xlsx 格式的 Excel 报告（旧版 .xls 请先用 Excel 另存为 .xlsx）"}), 400
    try:
        import openpyxl  # noqa: F401  (函数内部使用)
    except ImportError:
        return jsonify({"ok": False, "msg": "服务器缺少 Excel 组件，请联系管理员"}), 500
    try:
        raw_schema = request.form.get("schema")
        if not raw_schema:
            # 兼容个别浏览器把 schema 当文件字段提交的情况
            sf = request.files.get("schema")
            if sf:
                raw_schema = sf.read().decode("utf-8", "ignore")
        schema = json.loads(raw_schema) if raw_schema else None
    except Exception:
        schema = None
    rep, summ, err = RPT_parse_report_xlsx(f.read(), schema)
    if err:
        return jsonify({"ok": False, "msg": err}), 400
    # 二次保险：彻底剔除任何签名相关字段，确保导入后签名栏留空
    def strip_sign(o):
        if isinstance(o, dict):
            return {k: strip_sign(v) for k, v in o.items()
                    if not any(w in str(k).lower() for w in ("sign", "签名", "签字"))}
        if isinstance(o, list):
            return [strip_sign(x) for x in o]
        return o
    rep = strip_sign(rep)
    return jsonify({"ok": True, "report": rep, "summary": summ})

# 全量状态（同步脚本 / 前端加载用）
@app.route("/api/state", methods=["GET"])
def get_state():
    return jsonify(read_state())

# 追加一条消杀记录（服务商填完提交）
@app.route("/api/record", methods=["POST"])
def add_record():
    try:
        r = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "msg": "格式错误"}), 400
    rid = r.get("id") or (datetime.datetime.now().strftime("%Y%m%d%H%M%S") + os.urandom(3).hex())
    r["id"] = rid
    c = conn(); k = cur(c)
    k.execute(adapt("INSERT INTO records(id,data,created_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=?"),
              (rid, json.dumps(r, ensure_ascii=False), datetime.datetime.now().isoformat(), json.dumps(r, ensure_ascii=False)))
    c.commit(); c.close()
    return jsonify({"ok": True, "id": rid})

# 删除一条记录
@app.route("/api/record/<rid>", methods=["DELETE"])
def del_record(rid):
    c = conn(); k = cur(c)
    k.execute(adapt("DELETE FROM records WHERE id=?"), (rid,))
    c.commit(); c.close()
    return jsonify({"ok": True})

# 设置某日双方签名确认
@app.route("/api/confirm", methods=["POST"])
def set_confirm():
    try:
        cc = request.get_json(force=True)
        d = cc.get("date")
    except Exception:
        return jsonify({"ok": False, "msg": "格式错误"}), 400
    if not d:
        return jsonify({"ok": False, "msg": "缺少 date"}), 400
    c = conn(); k = cur(c)
    # 读取旧数据做「合并」：支持服务商与我司分设备、分先后各自签名，互不覆盖
    k.execute(adapt("SELECT data FROM confirmations WHERE date=?"), (d,))
    row = k.fetchone()
    cur_data = {}
    if row:
        try:
            cur_data = json.loads(row[0]) or {}
        except Exception:
            cur_data = {}
    for kk, vv in (cc or {}).items():
        if kk in ("provider", "company") and isinstance(vv, dict) and isinstance(cur_data.get(kk), dict):
            m = dict(cur_data[kk]); m.update(vv); cur_data[kk] = m
        else:
            cur_data[kk] = vv
    cur_data["date"] = d
    # 双方签名都齐了才算确认完成
    cur_data["confirmed"] = bool(cur_data.get("provider") and cur_data.get("company"))
    if cur_data["confirmed"] and not cur_data.get("confirmTs"):
        cur_data["confirmTs"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = json.dumps(cur_data, ensure_ascii=False)
    k.execute(adapt("INSERT INTO confirmations(date,data) VALUES(?,?) ON CONFLICT(date) DO UPDATE SET data=?"),
              (d, payload, payload))
    c.commit(); c.close()
    return jsonify({"ok": True, "confirmed": cur_data["confirmed"],
                    "hasProvider": bool(cur_data.get("provider")),
                    "hasCompany": bool(cur_data.get("company"))})

@app.route("/api/provider", methods=["GET", "POST"])
def provider():
    if request.method == "POST":
        try:
            set_meta("provider", request.get_json(force=True).get("name", "能多洁"))
        except Exception:
            pass
    return jsonify({"provider": get_meta("provider", "能多洁")})

# ===== 药品库 CRUD（多款药品独立实体） =====
def list_drugs():
    c = conn(); k = cur(c)
    k.execute(adapt("SELECT data FROM drugs ORDER BY id"))
    rows = [json.loads(r[0]) for r in k.fetchall()]; c.close()
    return rows

@app.route("/api/drugs", methods=["GET"])
def get_drugs():
    return jsonify(list_drugs())

@app.route("/api/drugs", methods=["POST"])
def create_drug():
    try:
        d = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "msg": "格式错误"}), 400
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "药品名称不能为空"}), 400
    for ex in list_drugs():
        if ex.get("name", "").strip().lower() == name.lower():
            return jsonify({"ok": False, "msg": "药品名称已存在，不可重复"}), 400
    did = d.get("id") or ("D" + datetime.datetime.now().strftime("%Y%m%d%H%M%S") + os.urandom(2).hex())
    d["id"] = did; d["name"] = name
    c = conn(); k = cur(c)
    k.execute(adapt("INSERT INTO drugs(id,data) VALUES(?,?)"), (did, json.dumps(d, ensure_ascii=False)))
    c.commit(); c.close()
    return jsonify({"ok": True, "drug": d})

@app.route("/api/drugs/<did>", methods=["PUT"])
def update_drug(did):
    try:
        d = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "msg": "格式错误"}), 400
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "药品名称不能为空"}), 400
    for ex in list_drugs():
        if ex.get("id") != did and ex.get("name", "").strip().lower() == name.lower():
            return jsonify({"ok": False, "msg": "药品名称已存在，不可重复"}), 400
    d["id"] = did; d["name"] = name
    c = conn(); k = cur(c)
    k.execute(adapt("UPDATE drugs SET data=? WHERE id=?"), (json.dumps(d, ensure_ascii=False), did))
    c.commit(); c.close()
    return jsonify({"ok": True, "drug": d})

@app.route("/api/drugs/<did>", methods=["DELETE"])
def delete_drug(did):
    c = conn(); k = cur(c)
    k.execute(adapt("DELETE FROM drugs WHERE id=?"), (did,))
    c.commit(); c.close()
    return jsonify({"ok": True})

# ===== 现场服务报告：一份报告 = SR 主表 + 9 张子表 =====
def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

@app.route("/api/reports", methods=["GET"])
def list_reports():
    c = conn(); k = cur(c)
    k.execute(adapt("SELECT data FROM reports ORDER BY created_at DESC"))
    rows = [report_summary(json.loads(r[0])) for r in k.fetchall()]
    c.close()
    return jsonify(rows)

@app.route("/api/reports/<rid>", methods=["GET"])
def get_report(rid):
    c = conn(); k = cur(c)
    k.execute(adapt("SELECT data FROM reports WHERE id=?"), (rid,))
    row = k.fetchone(); c.close()
    if not row:
        return jsonify({"ok": False, "msg": "报告不存在"}), 404
    return jsonify(json.loads(row[0]))

@app.route("/api/reports", methods=["POST"])
def save_report():
    try:
        r = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "msg": "格式错误"}), 400
    if not isinstance(r, dict):
        return jsonify({"ok": False, "msg": "格式错误"}), 400
    rid = r.get("id") or ("R" + datetime.datetime.now().strftime("%Y%m%d%H%M%S") + os.urandom(3).hex())
    r["id"] = rid
    now = _now()
    r.setdefault("createdAt", now)
    r["updatedAt"] = now
    r.setdefault("title", "现场服务报告 " + str(r.get("date") or ""))
    payload = json.dumps(r, ensure_ascii=False)
    c = conn(); k = cur(c)
    k.execute(adapt("INSERT INTO reports(id,date,data,created_at,updated_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET date=?, data=?, updated_at=?"),
              (rid, r.get("date") or "", payload, r["createdAt"], now,
               r.get("date") or "", payload, now))
    c.commit(); c.close()
    return jsonify({"ok": True, "id": rid, "report": r})

@app.route("/api/reports/<rid>", methods=["DELETE"])
def delete_report(rid):
    c = conn(); k = cur(c)
    k.execute(adapt("DELETE FROM reports WHERE id=?"), (rid,))
    c.commit(); c.close()
    return jsonify({"ok": True})

# ===== 现场人员：姓名/岗位/上岗证/健康证（证件图为 base64） =====
def list_workers():
    c = conn(); k = cur(c)
    k.execute(adapt("SELECT data FROM workers ORDER BY created_at"))
    rows = [json.loads(r[0]) for r in k.fetchall()]; c.close()
    return rows

@app.route("/api/workers", methods=["GET"])
def get_workers():
    return jsonify([worker_summary(w) for w in list_workers()])

@app.route("/api/workers", methods=["POST"])
def save_worker():
    try:
        w = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "msg": "格式错误"}), 400
    if not isinstance(w, dict):
        return jsonify({"ok": False, "msg": "格式错误"}), 400
    name = (w.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "姓名不能为空"}), 400
    # 重名拦截（同一人不重复建档）
    for ex in list_workers():
        if ex.get("id") != w.get("id") and ex.get("name", "").strip() == name:
            return jsonify({"ok": False, "msg": "该姓名已存在，请直接编辑原人员"}), 400
    # 证件大小限制：图片（压缩后 base64）约 800KB；PDF 文件允许到 8MB（base64 后约 11MB）
    for key in ("certUp", "certHealth"):
        v = w.get(key)
        if v and isinstance(v, str):
            limit = 11 * 1024 * 1024 if v.startswith("data:application/pdf") else 1200 * 1024
            if len(v) > limit:
                kind = "PDF 文件" if limit > 1200 * 1024 else "证件图片"
                return jsonify({"ok": False, "msg": kind + "过大，请压缩后再上传"}), 400
    wid = w.get("id") or ("W" + datetime.datetime.now().strftime("%Y%m%d%H%M%S") + os.urandom(2).hex())
    w["id"] = wid; w["name"] = name
    now = _now()
    w.setdefault("createdAt", now)
    w["updatedAt"] = now
    payload = json.dumps(w, ensure_ascii=False)
    c = conn(); k = cur(c)
    k.execute(adapt("INSERT INTO workers(id,data,created_at,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET data=?, updated_at=?"),
              (wid, payload, w["createdAt"], now, payload, now))
    c.commit(); c.close()
    return jsonify({"ok": True, "id": wid, "worker": worker_summary(w)})

@app.route("/api/workers/<wid>", methods=["GET"])
def get_worker(wid):
    c = conn(); k = cur(c)
    k.execute(adapt("SELECT data FROM workers WHERE id=?"), (wid,))
    row = k.fetchone(); c.close()
    if not row:
        return jsonify({"ok": False, "msg": "人员不存在"}), 404
    return jsonify(json.loads(row[0]))

@app.route("/api/workers/<wid>", methods=["DELETE"])
def delete_worker(wid):
    c = conn(); k = cur(c)
    k.execute(adapt("DELETE FROM workers WHERE id=?"), (wid,))
    c.commit(); c.close()
    return jsonify({"ok": True})

# 清空全部数据（管理用，需谨慎）
@app.route("/api/state", methods=["DELETE"])
def reset_state():
    c = conn(); k = cur(c)
    k.execute(adapt("DELETE FROM records")); k.execute(adapt("DELETE FROM confirmations"))
    k.execute(adapt("DELETE FROM reports")); k.execute(adapt("DELETE FROM workers"))
    k.execute(adapt("DELETE FROM insecticides"))
    c.commit(); c.close()
    return jsonify({"ok": True})

# ===== 杀虫剂清单：13 字段（商品名称/药品名称/农药登记证/有效期限/有效成分/剂型/毒性/生产商/使用方法/目标虫害/配置方法/使用场所/包装规格） =====
INS_FIELDS = ["tradeName", "drugName", "regCert", "validUntil", "ingredients", "form",
              "toxicity", "maker", "usage", "targets", "mixMethod", "places", "packSpec"]

def list_insecticides():
    c = conn(); k = cur(c)
    k.execute(adapt("SELECT data FROM insecticides ORDER BY created_at DESC"))
    rows = [json.loads(r[0]) for r in k.fetchall()]; c.close()
    return rows

@app.route("/api/insecticides", methods=["GET"])
def get_insecticides():
    return jsonify(list_insecticides())

@app.route("/api/insecticides", methods=["POST"])
def save_insecticide():
    try:
        d = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "msg": "格式错误"}), 400
    if not isinstance(d, dict):
        return jsonify({"ok": False, "msg": "格式错误"}), 400
    trade = (d.get("tradeName") or "").strip()
    drug = (d.get("drugName") or "").strip()
    if not trade and not drug:
        return jsonify({"ok": False, "msg": "商品名称与药品名称至少填一项"}), 400
    # 同名拦截：商品名称+药品名称 都相同视为重复（编辑自身除外）
    for ex in list_insecticides():
        if ex.get("id") != d.get("id") and (ex.get("tradeName", "").strip() == trade
                and ex.get("drugName", "").strip() == drug and trade and drug):
            return jsonify({"ok": False, "msg": "相同商品名称+药品名称的条目已存在，请直接编辑原条目"}), 400
    iid = d.get("id") or ("I" + datetime.datetime.now().strftime("%Y%m%d%H%M%S") + os.urandom(2).hex())
    d["id"] = iid
    if trade: d["tradeName"] = trade
    if drug: d["drugName"] = drug
    for kk in INS_FIELDS:
        if d.get(kk) is not None: d[kk] = str(d[kk]).strip()
    now = _now()
    d.setdefault("createdAt", now)
    d["updatedAt"] = now
    payload = json.dumps(d, ensure_ascii=False)
    c = conn(); k = cur(c)
    k.execute(adapt("INSERT INTO insecticides(id,data,created_at,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET data=?, updated_at=?"),
              (iid, payload, d["createdAt"], now, payload, now))
    c.commit(); c.close()
    return jsonify({"ok": True, "id": iid, "item": d})

@app.route("/api/insecticides/<iid>", methods=["GET"])
def get_insecticide(iid):
    c = conn(); k = cur(c)
    k.execute(adapt("SELECT data FROM insecticides WHERE id=?"), (iid,))
    row = k.fetchone(); c.close()
    if not row:
        return jsonify({"ok": False, "msg": "条目不存在"}), 404
    return jsonify(json.loads(row[0]))

@app.route("/api/insecticides/<iid>", methods=["DELETE"])
def delete_insecticide(iid):
    c = conn(); k = cur(c)
    k.execute(adapt("DELETE FROM insecticides WHERE id=?"), (iid,))
    c.commit(); c.close()
    return jsonify({"ok": True})

# gunicorn 方式启动时不会执行 __main__，建表必须在模块加载时完成
ensure()

if __name__ == "__main__":
    ensure()
    port = int(os.environ.get("PORT", 5000))
    print("==================================================")
    print(" 虫控消杀确认平台（云端版）已启动，端口", port)
    print(" 服务商填表网址： http://localhost:%d" % port)
    print("==================================================")
    app.run(host="0.0.0.0", port=port, debug=False)
