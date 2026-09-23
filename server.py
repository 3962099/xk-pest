#!/usr/bin/env python3
# 虫控消杀确认平台 —— 云端后台（部署到 Render / Railway / Koyeb 等 PaaS）
# 设计要点：
#  - 端口读环境变量 PORT（PaaS 要求），默认 5000
#  - 监听 0.0.0.0（云环境必须，否则外部访问不到）
#  - 数据库：若设置了 DATABASE_URL（Postgres，云平台自动注入）则用它；
#            否则回退本地/卷 SQLite（DATA_DIR 可配置，默认脚本目录或 /data）
#  - 接口为「逐条追加 / 单日确认」模型，多端（服务商手机、我司电脑）同时写不会互相覆盖
#  - 本机同步脚本用 GET /api/state 拉全量，再写入企微智能表格（按 记录ID 去重）
import os, json, datetime, sqlite3
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
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
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

def read_state():
    c = conn(); k = cur(c)
    k.execute(adapt("SELECT data FROM records ORDER BY created_at"))
    crecs = [json.loads(r[0]) for r in k.fetchall()]
    k.execute(adapt("SELECT date,data FROM confirmations"))
    conf = {d: json.loads(v) for d, v in k.fetchall()}
    k.execute(adapt("SELECT data FROM drugs ORDER BY id"))
    cdr = [json.loads(r[0]) for r in k.fetchall()]
    c.close()
    return {"provider": get_meta("provider", "能多洁"), "records": crecs, "confirmations": conf, "drugs": cdr}

@app.route("/")
def index():
    return send_from_directory(BASE, "index.html")

@app.route("/xlsx.full.min.js")
def xlsx_js():
    return send_from_directory(BASE, "xlsx.full.min.js")

@app.route("/config.js")
def config_js():
    return send_from_directory(BASE, "config.js")

@app.route("/admin")
@app.route("/admin.html")
def admin_page():
    return send_from_directory(BASE, "admin.html")

@app.route("/api/health")
def health():
    return jsonify({"ok": True, "db": "postgres" if USE_PG else "sqlite"})

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
    k.execute(adapt("INSERT INTO confirmations(date,data) VALUES(?,?) ON CONFLICT(date) DO UPDATE SET data=?"),
              (d, json.dumps(cc, ensure_ascii=False), json.dumps(cc, ensure_ascii=False)))
    c.commit(); c.close()
    return jsonify({"ok": True})

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

# 清空全部数据（管理用，需谨慎）
@app.route("/api/state", methods=["DELETE"])
def reset_state():
    c = conn(); k = cur(c)
    k.execute(adapt("DELETE FROM records")); k.execute(adapt("DELETE FROM confirmations"))
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
