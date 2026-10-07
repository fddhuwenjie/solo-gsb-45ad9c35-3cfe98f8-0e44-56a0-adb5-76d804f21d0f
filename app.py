#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""展馆临时封路导向牌工作台 —— Flask + SQLite 后端。"""
import os
import sqlite3
from flask import Flask, request, jsonify, g, render_template

DB_PATH = os.environ.get("SIGNBOARD_DB", "/data/signboard.db")

app = Flask(__name__, template_folder="templates", static_folder="static")


# ---------------------------------------------------------------- 数据库
def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS job (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            stage TEXT NOT NULL DEFAULT 'setup',
            error TEXT
        );
        INSERT OR IGNORE INTO job(id, stage) VALUES (1, 'setup');

        CREATE TABLE IF NOT EXISTS node (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL CHECK (kind IN ('hall','entrance','service')),
            x INTEGER NOT NULL DEFAULT 300,
            y INTEGER NOT NULL DEFAULT 200
        );

        CREATE TABLE IF NOT EXISTS corridor (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            a_id INTEGER NOT NULL REFERENCES node(id) ON DELETE CASCADE,
            b_id INTEGER NOT NULL REFERENCES node(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','closed')),
            CHECK (a_id < b_id)
        );

        -- 实物箭头牌：每块牌固定在 node_id 路口，指向相邻的 points_to 展厅
        -- kind='old' 为原有旧牌；kind='new' 为本次方案新牌（安装后出现）
        CREATE TABLE IF NOT EXISTS board (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER NOT NULL REFERENCES node(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('old','new')),
            points_to INTEGER NOT NULL REFERENCES node(id) ON DELETE CASCADE,
            installed_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(node_id, kind)
        );

        -- 待封通道清单
        CREATE TABLE IF NOT EXISTS closure_plan (
            corridor_id INTEGER PRIMARY KEY
                REFERENCES corridor(id) ON DELETE CASCADE
        );

        -- 换牌方案：待安装的新牌（在某路口指向某相邻展厅）
        CREATE TABLE IF NOT EXISTS plan_item (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER NOT NULL REFERENCES node(id) ON DELETE CASCADE,
            points_to INTEGER NOT NULL REFERENCES node(id) ON DELETE CASCADE,
            installed INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------- 工具
def get_stage(db):
    return db.execute("SELECT stage, error FROM job WHERE id=1").fetchone()


def set_stage(db, stage, error=None):
    db.execute("UPDATE job SET stage=?, error=? WHERE id=1", (stage, error))


def node_name(db, nid):
    row = db.execute("SELECT name FROM node WHERE id=?", (nid,)).fetchone()
    return row["name"] if row else f"#{nid}"


def adjacency(db, closed_ids, force_open_ids=()):
    """构造邻接表；closed_ids 中或已实际封闭的通道视为断路；
    force_open_ids 中的通道即使状态为 closed 也视为通路（恢复开放模拟用）。"""
    adj = {}
    for r in db.execute("SELECT id, a_id, b_id, status FROM corridor"):
        if r["id"] in force_open_ids:
            pass  # 强制视为通路
        elif r["id"] in closed_ids or r["status"] == "closed":
            continue
        adj.setdefault(r["a_id"], []).append(r["b_id"])
        adj.setdefault(r["b_id"], []).append(r["a_id"])
    return adj


def effective_arrow(db, node_id, removed_boards=()):
    """该路口当前生效的箭头：新牌优先，否则旧牌；无牌返回 None。
    removed_boards 中的 board id 视为已拆除（仅用于恢复开放时的模拟走查）。"""
    row = db.execute(
        "SELECT id, points_to FROM board WHERE node_id=? AND kind='new'",
        (node_id,),
    ).fetchone()
    if row and row["id"] not in removed_boards:
        return ("new", row["points_to"])
    row = db.execute(
        "SELECT points_to FROM board WHERE node_id=? AND kind='old'",
        (node_id,),
    ).fetchone()
    if row:
        return ("old", row["points_to"])
    return None


def walk_check(db, closed_ids, removed_boards=(), force_open_ids=()):
    """
    从所有入口依实物箭头逐步走到服务台。
    closed_ids: 本次检查视为已封闭的通道集合。
    removed_boards: 本次检查视为已拆除的实物牌 id 集合（拆新牌后露出旧牌）。
    force_open_ids: 即使状态为 closed 也强制视为通路的通道（恢复开放模拟用）。
    返回 dict: ok / blocked (通道封路状态) / entrances(逐个结果)
    任一入口出错时，error 为首个（按登记顺序）出错入口的首个出错路口。
    """
    nodes = db.execute("SELECT id, kind FROM node ORDER BY id").fetchall()
    adj = adjacency(db, closed_ids, force_open_ids)
    blocked = {
        r["id"]: (r["status"] == "closed")
        for r in db.execute("SELECT id, status FROM corridor")
    }
    results = []
    first_error = None
    for n in nodes:
        if n["kind"] != "entrance":
            continue
        cur, trail, seen = n["id"], [n["id"]], set()
        err = None
        while True:
            kind = db.execute(
                "SELECT kind FROM node WHERE id=?", (cur,)
            ).fetchone()["kind"]
            if kind == "service":
                break
            seen.add(cur)
            arrow = effective_arrow(db, cur, removed_boards)
            if arrow is None:
                err = {
                    "type": "missing",
                    "junction": cur,
                    "junction_name": node_name(db, cur),
                    "trail": list(trail),
                    "detail": f"路口「{node_name(db, cur)}」缺牌：没有任何实物箭头",
                }
                break
            via, nxt = arrow
            if nxt not in adj.get(cur, []):
                # 箭头指向的相邻通道不通
                err = {
                    "type": "break",
                    "junction": cur,
                    "junction_name": node_name(db, cur),
                    "trail": list(trail),
                    "detail": (
                        f"路口「{node_name(db, cur)}」断路："
                        f"箭头指向「{node_name(db, nxt)}」，但该通道不通"
                    ),
                }
                break
            trail.append(nxt)
            if nxt in seen:
                err = {
                    "type": "loop",
                    "junction": nxt,
                    "junction_name": node_name(db, nxt),
                    "trail": list(trail),
                    "detail": (
                        f"在路口「{node_name(db, nxt)}」陷入循环："
                        + " → ".join(node_name(db, x) for x in trail)
                    ),
                }
                break
            cur = nxt
        res = {
            "entrance_id": n["id"],
            "entrance_name": node_name(db, n["id"]),
            "ok": err is None,
            "path": [node_name(db, x) for x in trail],
            "error": err,
        }
        results.append(res)
        if err and first_error is None:
            first_error = err
    return {"ok": first_error is None, "error": first_error, "entrances": results,
            "blocked": blocked}


def lock_error(stage):
    return {
        "setup": "尚未进入方案阶段，请先确认初始导向正确后「拟定方案」。",
        "planning": "方案尚未开始安装。",
        "installing": "安装已开始：封路清单与换牌方案已锁定，不能再改动。",
        "closed": "通道已封闭：施工结束后请在「已封路」页面逐次恢复开放。",
        "failed": "封路失败，本次作业已终止：只能查看或重置。",
    }[stage]


def bad(msg, code=400):
    return jsonify({"error": msg}), code


# ---------------------------------------------------------------- 页面
@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------- 状态
@app.route("/api/state")
def api_state():
    db = get_db()
    st = get_stage(db)
    nodes = [dict(r) for r in db.execute("SELECT * FROM node ORDER BY id")]
    corridors = [
        dict(r)
        for r in db.execute(
            """SELECT c.*, na.name AS a_name, nb.name AS b_name
               FROM corridor c
               JOIN node na ON na.id = c.a_id
               JOIN node nb ON nb.id = c.b_id ORDER BY c.id"""
        )
    ]
    boards = [
        dict(r)
        for r in db.execute(
            """SELECT b.*, n.name AS node_name, t.name AS points_to_name
               FROM board b
               JOIN node n ON n.id = b.node_id
               JOIN node t ON t.id = b.points_to ORDER BY b.id"""
        )
    ]
    closure = [
        dict(r)
        for r in db.execute(
            """SELECT cp.corridor_id, c.a_id, c.b_id, c.status,
                      na.name || ' ↔ ' || nb.name AS label
               FROM closure_plan cp
               JOIN corridor c ON c.id = cp.corridor_id
               JOIN node na ON na.id = c.a_id
               JOIN node nb ON nb.id = c.b_id
               ORDER BY cp.corridor_id"""
        )
    ]
    items = [
        dict(r)
        for r in db.execute(
            """SELECT p.*, n.name AS node_name, t.name AS points_to_name
               FROM plan_item p
               JOIN node n ON n.id = p.node_id
               JOIN node t ON t.id = p.points_to ORDER BY p.id"""
        )
    ]

    # 生效箭头（新牌优先，否则旧牌）
    eff = {}
    for n in nodes:
        a = effective_arrow(db, n["id"])
        if a:
            eff[n["id"]] = {"via": a[0], "points_to": a[1]}

    live = walk_check(db, set())  # 现状（按已封闭通道）
    sim = walk_check(db, {c["corridor_id"] for c in closure})  # 模拟封路后

    # 已封路阶段：模拟「全部已封通道开放 + 全部新牌拆除」后的最终恢复状态
    closed_now = [
        r["id"] for r in db.execute("SELECT id FROM corridor WHERE status='closed'")
    ]
    new_board_ids = {
        r["id"] for r in db.execute("SELECT id FROM board WHERE kind='new'")
    }
    reopen_check = walk_check(db, set(), new_board_ids, set(closed_now))

    return jsonify(
        {
            "stage": st["stage"],
            "stage_error": st["error"],
            "nodes": nodes,
            "corridors": corridors,
            "boards": boards,
            "closure_plan": closure,
            "plan_items": items,
            "effective": eff,
            "live_check": live,
            "sim_check": sim,
            "reopen_check": reopen_check,
        }
    )


# ---------------------------------------------------------------- 布展登记
@app.route("/api/nodes", methods=["POST"])
def add_node():
    db = get_db()
    st = get_stage(db)
    if st["stage"] != "setup":
        return bad(lock_error(st["stage"]), 409)
    d = request.get_json(force=True)
    name, kind = (d.get("name") or "").strip(), d.get("kind")
    x, y = int(d.get("x", 300)), int(d.get("y", 200))
    if not name:
        return bad("名称不能为空")
    if kind not in ("hall", "entrance", "service"):
        return bad("类型必须是 hall / entrance / service")
    try:
        cur = db.execute(
            "INSERT INTO node(name, kind, x, y) VALUES (?,?,?,?)",
            (name, kind, x, y),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return bad(f"已存在同名路口「{name}」")
    return jsonify({"id": cur.lastrowid})


@app.route("/api/nodes/<int:nid>", methods=["DELETE"])
def del_node(nid):
    db = get_db()
    st = get_stage(db)
    if st["stage"] != "setup":
        return bad(lock_error(st["stage"]), 409)
    db.execute("DELETE FROM node WHERE id=?", (nid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/corridors", methods=["POST"])
def add_corridor():
    db = get_db()
    st = get_stage(db)
    if st["stage"] != "setup":
        return bad(lock_error(st["stage"]), 409)
    d = request.get_json(force=True)
    try:
        a, b = int(d["a_id"]), int(d["b_id"])
    except (KeyError, TypeError, ValueError):
        return bad("请选择通道两端的展厅")
    if a == b:
        return bad("通道必须连接两个不同的路口")
    a, b = sorted((a, b))
    if not db.execute("SELECT 1 FROM node WHERE id=?", (a,)).fetchone():
        return bad("路口不存在")
    if db.execute(
        "SELECT 1 FROM corridor WHERE a_id=? AND b_id=?", (a, b)
    ).fetchone():
        return bad("该双向通道已登记")
    cur = db.execute("INSERT INTO corridor(a_id,b_id) VALUES (?,?)", (a, b))
    db.commit()
    return jsonify({"id": cur.lastrowid})


@app.route("/api/corridors/<int:cid>", methods=["DELETE"])
def del_corridor(cid):
    db = get_db()
    st = get_stage(db)
    if st["stage"] != "setup":
        return bad(lock_error(st["stage"]), 409)
    db.execute("DELETE FROM corridor WHERE id=?", (cid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/boards", methods=["POST"])
def add_board():
    """登记布展时的原有旧牌。"""
    db = get_db()
    st = get_stage(db)
    if st["stage"] != "setup":
        return bad(lock_error(st["stage"]), 409)
    d = request.get_json(force=True)
    try:
        node_id, points_to = int(d["node_id"]), int(d["points_to"])
    except (KeyError, TypeError, ValueError):
        return bad("请选择路口与箭头指向的相邻展厅")
    n = db.execute("SELECT kind FROM node WHERE id=?", (node_id,)).fetchone()
    if not n:
        return bad("路口不存在")
    if n["kind"] == "service":
        return bad("服务台是终点，不需要箭头牌")
    if not db.execute("SELECT 1 FROM node WHERE id=?", (points_to,)).fetchone():
        return bad("指向的展厅不存在")
    if not db.execute(
        "SELECT 1 FROM corridor WHERE (a_id=? AND b_id=?) OR (a_id=? AND b_id=?)",
        (node_id, points_to, points_to, node_id),
    ).fetchone():
        return bad("箭头只能指向与该路口有双向通道直接相连的相邻展厅")
    try:
        cur = db.execute(
            "INSERT INTO board(node_id, kind, points_to) VALUES (?,'old',?)",
            (node_id, points_to),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return bad("该路口已有一块旧牌（每路口仅一块生效箭头）")
    return jsonify({"id": cur.lastrowid})


@app.route("/api/boards/<int:bid>", methods=["DELETE"])
def del_board(bid):
    db = get_db()
    st = get_stage(db)
    if st["stage"] != "setup":
        return bad(lock_error(st["stage"]), 409)
    db.execute("DELETE FROM board WHERE id=? AND kind='old'", (bid,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 方案
@app.route("/api/plan/closures", methods=["POST"])
def plan_add_closure():
    db = get_db()
    st = get_stage(db)
    if st["stage"] not in ("setup", "planning"):
        return bad(lock_error(st["stage"]), 409)
    d = request.get_json(force=True)
    cid = int(d.get("corridor_id"))
    if not db.execute("SELECT 1 FROM corridor WHERE id=?", (cid,)).fetchone():
        return bad("通道不存在")
    db.execute("INSERT OR IGNORE INTO closure_plan(corridor_id) VALUES (?)", (cid,))
    if st["stage"] == "setup":
        set_stage(db, "planning")
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/plan/closures/<int:cid>", methods=["DELETE"])
def plan_del_closure(cid):
    db = get_db()
    st = get_stage(db)
    if st["stage"] not in ("setup", "planning"):
        return bad(lock_error(st["stage"]), 409)
    db.execute("DELETE FROM closure_plan WHERE corridor_id=?", (cid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/plan/items", methods=["POST"])
def plan_add_item():
    db = get_db()
    st = get_stage(db)
    if st["stage"] not in ("setup", "planning"):
        return bad(lock_error(st["stage"]), 409)
    d = request.get_json(force=True)
    try:
        node_id, points_to = int(d["node_id"]), int(d["points_to"])
    except (KeyError, TypeError, ValueError):
        return bad("请选择换牌路口与新箭头指向")
    n = db.execute("SELECT kind FROM node WHERE id=?", (node_id,)).fetchone()
    if not n:
        return bad("路口不存在")
    if n["kind"] == "service":
        return bad("服务台不需要箭头牌")
    if not db.execute("SELECT 1 FROM node WHERE id=?", (points_to,)).fetchone():
        return bad("指向的展厅不存在")
    if not db.execute(
        "SELECT 1 FROM corridor WHERE (a_id=? AND b_id=?) OR (a_id=? AND b_id=?)",
        (node_id, points_to, points_to, node_id),
    ).fetchone():
        return bad("新箭头只能指向相邻展厅（须有双向通道）")
    if db.execute(
        "SELECT 1 FROM plan_item WHERE node_id=? AND installed=0", (node_id,)
    ).fetchone():
        return bad("该路口在方案中已有一块待装新牌")
    cur = db.execute(
        "INSERT INTO plan_item(node_id, points_to) VALUES (?,?)",
        (node_id, points_to),
    )
    if st["stage"] == "setup":
        set_stage(db, "planning")
    db.commit()
    return jsonify({"id": cur.lastrowid})


@app.route("/api/plan/items/<int:pid>", methods=["DELETE"])
def plan_del_item(pid):
    db = get_db()
    st = get_stage(db)
    if st["stage"] not in ("setup", "planning"):
        return bad(lock_error(st["stage"]), 409)
    db.execute("DELETE FROM plan_item WHERE id=? AND installed=0", (pid,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 流程
@app.route("/api/plan/start", methods=["POST"])
def plan_start():
    """确认方案，进入安装阶段（此后清单与方案锁定）。"""
    db = get_db()
    st = get_stage(db)
    if st["stage"] != "planning":
        return bad(lock_error(st["stage"]), 409)
    base = walk_check(db, set())
    if not base["ok"]:
        return bad("当前现场导向本身就不满足要求：" + base["error"]["detail"])
    if not db.execute("SELECT 1 FROM closure_plan").fetchone():
        return bad("封路清单为空，请先勾选待封通道")
    set_stage(db, "installing")
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/install/<int:pid>", methods=["POST"])
def install_one(pid):
    """逐块安装一块方案中的牌；安装后立即按全部未封闭通道+新旧混用牌走查。
    出错则整块回滚，本次安装不生效。"""
    db = get_db()
    st = get_stage(db)
    if st["stage"] != "installing":
        return bad(lock_error(st["stage"]), 409)
    item = db.execute("SELECT * FROM plan_item WHERE id=?", (pid,)).fetchone()
    if not item:
        return bad("方案中没有这块牌")
    if item["installed"]:
        return bad("这块牌已经安装过了")

    try:
        db.execute(
            "INSERT INTO board(node_id, kind, points_to) VALUES (?,'new',?)",
            (item["node_id"], item["points_to"]),
        )
    except sqlite3.IntegrityError:
        return bad("该路口已存在一块新牌")

    # 安装期间通道一律尚未封闭，按全部开放通道核对
    chk = walk_check(db, set())
    if not chk["ok"]:
        db.rollback()
        e = chk["error"]
        return jsonify({
            "ok": False,
            "rejected": True,
            "error": e,
            "message": f"安装不生效：{e['detail']}（路径："
                       + " → ".join(node_name(db, x) for x in e["trail"]) + "）",
        }), 422

    db.execute("UPDATE plan_item SET installed=1 WHERE id=?", (pid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/close", methods=["POST"])
def close_corridors():
    """方案中的牌全部装好后，封闭清单中的通道并最终核对。"""
    db = get_db()
    st = get_stage(db)
    if st["stage"] != "installing":
        return bad(lock_error(st["stage"]), 409)
    pending = db.execute(
        "SELECT COUNT(*) c FROM plan_item WHERE installed=0"
    ).fetchone()["c"]
    if pending:
        return bad(f"还有 {pending} 块方案中的牌未安装，不能封路")

    ids = [r["corridor_id"] for r in db.execute("SELECT corridor_id FROM closure_plan")]
    chk = walk_check(db, set(ids))
    if not chk["ok"]:
        # 封路失败：保留已安装的牌，通道维持开放，作业终止
        e = chk["error"]
        set_stage(db, "failed", f"封路失败：{e['detail']}")
        db.commit()
        return jsonify({
            "ok": False,
            "failed": True,
            "error": e,
            "message": f"封路失败：{e['detail']}（路径："
                       + " → ".join(node_name(db, x) for x in e["trail"])
                       + "）。已装新牌与开放通道均已保留。",
        }), 422

    db.executemany(
        "UPDATE corridor SET status='closed' WHERE id=?",
        [(i,) for i in ids],
    )
    set_stage(db, "closed")
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/reopen", methods=["POST"])
def reopen_corridor():
    """施工结束后逐次恢复开放：选择一条已封通道 + 本次要拆的零块或多块新牌。
    按「该通道开放 + 选中新牌拆除」后的状态从所有入口重新走查；
    出现断路 / 缺牌 / 循环则指出首个出错路口，本次开放与拆牌均不生效。
    成功后仅更新所选通道、被拆实物牌及其封路清单/换牌方案记录。
    恢复最后一条已封通道时必须同时拆完剩余新牌；走查通过后清空本次方案，
    回到布展登记阶段（展厅、通道与旧牌保留，通道全部开放）。"""
    db = get_db()
    st = get_stage(db)
    if st["stage"] != "closed":
        return bad(lock_error(st["stage"]), 409)
    d = request.get_json(force=True) or {}
    try:
        corridor_id = int(d.get("corridor_id"))
    except (TypeError, ValueError):
        return bad("请选择本次要恢复开放的已封通道")
    corr = db.execute(
        "SELECT * FROM corridor WHERE id=?", (corridor_id,)
    ).fetchone()
    if not corr:
        return bad("通道不存在")
    if corr["status"] != "closed":
        return bad("只能恢复已封闭的通道")

    raw_boards = d.get("board_ids", [])
    if not isinstance(raw_boards, list):
        return bad("board_ids 必须是数组（可为空）")
    try:
        board_ids = [int(b) for b in raw_boards]
    except (TypeError, ValueError):
        return bad("拆除牌编号不合法")
    if len(set(board_ids)) != len(board_ids):
        return bad("拆除清单中有重复的牌")

    removed = set()
    for bid in board_ids:
        b = db.execute(
            "SELECT id, node_id, points_to FROM board WHERE id=? AND kind='new'",
            (bid,),
        ).fetchone()
        if not b:
            return bad(f"编号 {bid} 不是可拆除的已装新牌")
        removed.add(b["id"])

    other_closed = [
        r["id"]
        for r in db.execute("SELECT id FROM corridor WHERE status='closed'")
        if r["id"] != corridor_id
    ]
    all_new_ids = {
        r["id"] for r in db.execute("SELECT id FROM board WHERE kind='new'")
    }
    # 恢复最后一条已封通道时，必须同时拆完全部剩余新牌
    if not other_closed and all_new_ids - removed:
        return bad("这是最后一条待恢复通道，必须同时拆除全部剩余新牌")

    # 模拟：本次通道强制开放，其余已封通道仍视为断路；选中新牌视为已拆。
    still_closed = set(other_closed)
    chk = walk_check(db, still_closed, removed, force_open_ids={corridor_id})
    if not chk["ok"]:
        e = chk["error"]
        return jsonify({
            "ok": False,
            "rejected": True,
            "error": e,
            "message": f"恢复不生效：{e['detail']}（路径："
                       + " → ".join(node_name(db, x) for x in e["trail"]) + "）",
        }), 422

    # ---- 走查通过：落地本次开放与拆牌 ----
    # 拆牌前先收集 (node_id, points_to)，用于同步删除换牌方案记录
    removed_items = []
    for bid in removed:
        b = db.execute(
            "SELECT node_id, points_to FROM board WHERE id=?", (bid,)
        ).fetchone()
        removed_items.append((b["node_id"], b["points_to"]))

    db.execute("UPDATE corridor SET status='open' WHERE id=?", (corridor_id,))
    if removed:
        db.executemany(
            "DELETE FROM board WHERE id=? AND kind='new'",
            [(bid,) for bid in removed],
        )
        # 同步删除对应的换牌方案记录
        db.executemany(
            "DELETE FROM plan_item WHERE node_id=? AND points_to=?",
            removed_items,
        )
    db.execute("DELETE FROM closure_plan WHERE corridor_id=?", (corridor_id,))

    remaining_closed = db.execute(
        "SELECT COUNT(*) c FROM corridor WHERE status='closed'"
    ).fetchone()["c"]
    remaining_new = db.execute(
        "SELECT COUNT(*) c FROM board WHERE kind='new'"
    ).fetchone()["c"]

    finished = remaining_closed == 0
    if finished:
        if remaining_new:
            # 不应发生：最后一条通道的拆牌约束在下方校验，此处兜底回滚
            db.rollback()
            return bad("恢复最后一条通道时必须同时拆完剩余新牌")
        # 全部恢复：清空本次方案（plan_item / closure_plan 已随操作清空，
        # 此处幂等兜底），回到布展登记；展厅、通道、旧牌保留。
        db.execute("DELETE FROM plan_item")
        db.execute("DELETE FROM closure_plan")
        set_stage(db, "setup")
    db.commit()
    return jsonify({"ok": True, "finished": finished})


@app.route("/api/reset", methods=["POST"])
def reset_job():
    """重置作业：清除封路清单/方案/新牌，通道全部开放，回到方案阶段重拟。
    已登记的展厅、通道、原有旧牌保留。"""
    db = get_db()
    db.execute("DELETE FROM plan_item")
    db.execute("DELETE FROM closure_plan")
    db.execute("DELETE FROM board WHERE kind='new'")
    db.execute("UPDATE corridor SET status='open'")
    set_stage(db, "setup")
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 示例
@app.route("/api/seed", methods=["POST"])
def seed_demo():
    db = get_db()
    st = get_stage(db)
    if st["stage"] != "setup":
        return bad("只有布展阶段可以载入示例（可先重置）", 409)
    if db.execute("SELECT COUNT(*) c FROM node").fetchone()["c"]:
        return bad("已有登记数据，不能覆盖")

    db.executescript(
        """
        DELETE FROM plan_item; DELETE FROM closure_plan;
        DELETE FROM board; DELETE FROM corridor; DELETE FROM node;
        DELETE FROM sqlite_sequence;
        """
    )
    # 节点（id 由插入顺序决定：1..6）
    nodes = [
        ("入口1", "entrance", 80, 120),
        ("入口2", "entrance", 80, 360),
        ("A展厅", "hall", 320, 120),
        ("B展厅", "hall", 320, 360),
        ("C展厅", "hall", 560, 240),
        ("服务台", "service", 800, 240),
    ]
    db.executemany("INSERT INTO node(name,kind,x,y) VALUES (?,?,?,?)", nodes)

    corridors = [(1, 3), (2, 4), (3, 4), (3, 5), (4, 5), (5, 6)]
    db.executemany("INSERT INTO corridor(a_id,b_id) VALUES (?,?)", corridors)

    # 旧牌：E1→A→B→C→服务台，E2→B→C→服务台（封路前全部可达）
    old = [(1, 3), (2, 4), (3, 4), (4, 5), (5, 6)]
    db.executemany(
        "INSERT INTO board(node_id,kind,points_to) VALUES (?,'old',?)", old
    )

    # 预设方案：封 B↔C（通道5），A 改指 C、B 改指 A。
    # 注意顺序：先装 B→A 会造成 A→B→A 循环；必须先装 A→C。
    db.execute("INSERT INTO closure_plan(corridor_id) VALUES (5)")
    db.execute("INSERT INTO plan_item(node_id,points_to) VALUES (3,5)")
    db.execute("INSERT INTO plan_item(node_id,points_to) VALUES (4,3)")
    set_stage(db, "planning")
    db.commit()
    return jsonify({"ok": True})


# WSGI（gunicorn）启动时同样确保表结构存在（幂等）
init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
