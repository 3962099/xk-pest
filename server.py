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
        for fn in ("index.html", "admin.html", "report.html", "stations.js", "server.py", "portal.html", "staff.html", "insecticides.html"):
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
