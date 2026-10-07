#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端测试（使用 test_shim，非交付物）。"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "test_shim"))
os.environ["SIGNBOARD_DB"] = "/tmp/test_signboard.db"
if os.path.exists("/tmp/test_signboard.db"):
    os.remove("/tmp/test_signboard.db")

# 让 app.py 的 `import flask` 与测试用垫片是同一模块对象
import miniflask  # noqa
sys.modules["flask"] = miniflask

from app import app, init_db  # noqa
from http.server import HTTPServer
import miniflask as shim  # noqa

init_db()

PASS, FAIL = 0, 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✔", msg)
    else:
        FAIL += 1
        print("  ✘", msg)


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _handle(self):
            path = self.path.split("?")[0]
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                shim.request._json = json.loads(raw) if raw else None
            except Exception:
                shim.request._json = None
            shim.request.method = self.command
            shim.g._store.clear()
            resp = None
            try:
                for rx, fn in app.routes:
                    m = rx.match(path)
                    if m:
                        kwargs = {k: int(v) for k, v in m.groupdict().items()}
                        out = fn(**kwargs)
                        if isinstance(out, tuple):
                            r, code = out
                            if isinstance(r, tuple):
                                r, code = r
                        else:
                            r, code = out, 200
                        resp = (r, code)
                        break
                if resp is None:
                    resp = (shim._Resp("not found", 404, "text/plain"), 404)
            finally:
                for fn in app.teardown:
                    fn(None)
            r, code = resp
            body = r.body.encode("utf-8") if isinstance(r.body, str) else r.body
            self.send_response(code)
            self.send_header("Content-Type", r.ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _handle
        do_POST = _handle
        do_DELETE = _handle
    return H


server = HTTPServer(("127.0.0.1", 5099), make_handler(app))
threading.Thread(target=server.serve_forever, daemon=True).start()

BASE = "http://127.0.0.1:5099"


def req(method, path, body=None, expect=200):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            code, payload = resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        code, payload = e.code, json.loads(e.read())
    check(code == expect,
          f"{method} {path} -> HTTP {code} (期望 {expect})"
          + ("" if code == expect else f" {payload}"))
    return payload


print("== 1. 示例数据 + 初始核对 ==")
req("POST", "/api/seed")
st = req("GET", "/api/state")
check(st["stage"] == "planning", "载入示例后进入 planning 阶段")
check(len(st["nodes"]) == 6 and len(st["corridors"]) == 6, "实体登记：6 路口 / 6 通道")
check(st["live_check"]["ok"], "初始：所有入口沿旧牌可走到服务台")
sim = st["sim_check"]
check(not sim["ok"], "封路预演：旧牌导向有问题")
check(sim["error"]["type"] == "break", "首个错误类型=断路（旧箭头跨过被封通道）")
check(sim["error"]["junction_name"] == "B展厅", "首个出错路口=B展厅（E1→A→B 后，B→C 通道被封）")

print("== 2. 开始安装后锁定 ==")
req("POST", "/api/plan/start")
req("POST", "/api/nodes", {"name": "X", "kind": "hall"}, 409)
req("DELETE", "/api/plan/items/1", expect=409)
req("DELETE", "/api/plan/closures/5", expect=409)
req("POST", "/api/corridors", {"a_id": 1, "b_id": 2}, 409)
req("DELETE", "/api/boards/1", expect=409)

a_item = next(p["id"] for p in st["plan_items"] if p["node_name"] == "A展厅")
b_item = next(p["id"] for p in st["plan_items"] if p["node_name"] == "B展厅")

print("== 3. 错误顺序安装（先 B→C）：A→B→A 循环，回滚 ==")
out = req("POST", f"/api/install/{b_item}", expect=422)
check(out["rejected"] and out["error"]["type"] == "loop", "报循环，安装不生效")
check(out["error"]["junction_name"] == "A展厅", "循环首个出错路口=A展厅")
check("入口1" in out["message"], "错误信息含路径")
st2 = req("GET", "/api/state")
check(all(not p["installed"] for p in st2["plan_items"]), "回滚：方案项均未安装")
check(all(b["kind"] == "old" for b in st2["boards"]), "回滚：无新牌实物残留")
check(st2["stage"] == "installing", "仍处安装阶段，可继续")

print("== 4. 正确顺序安装 + 封路 ==")
req("POST", f"/api/install/{a_item}")
check(req("GET", "/api/state")["live_check"]["ok"], "装 A→C 后现场仍全部可达")
req("POST", f"/api/install/{b_item}")
st4 = req("GET", "/api/state")
check(all(p["installed"] for p in st4["plan_items"]), "两块牌全部装好")
check(st4["sim_check"]["ok"], "封路预演通过")
# 未装完封路的校验（此处已装完，验证正常成功路径）
req("POST", "/api/close")
st5 = req("GET", "/api/state")
check(st5["stage"] == "closed", "阶段=closed")
check(next(c for c in st5["corridors"] if c["id"] == 5)["status"] == "closed",
      "清单通道已封闭")
check(st5["live_check"]["ok"], "封路后所有入口仍沿牌到达服务台")

# 直接验证：已封闭通道不会出现在现场邻接表中（防止封路后仍被当作可走）
import sqlite3 as _sq
from app import adjacency as _adj
_dbc = _sq.connect(os.environ["SIGNBOARD_DB"])
_dbc.row_factory = _sq.Row
_a = _adj(_dbc, set())
# 通道5（节点4↔5，即 B↔C）已封闭：4 的邻居不含 5，5 的邻居不含 4
check(5 not in _a.get(4, []), "现场邻接表：已封闭的 B→C 不可走")
check(4 not in _a.get(5, []), "现场邻接表：已封闭的 C→B 不可走")
# 模拟封闭集合叠加生效（通道4 为节点3↔5 A↔C，当前开放）
_a2 = _adj(_dbc, {4})
check(5 not in _a2.get(3, []), "模拟封闭集合同样生效：A→C 不可走")
check(5 in _a.get(3, []), "开放通道仍可走：A→C 在场邻接表中")
_dbc.close()

print("== 5. 持久化（另一个连接直接读 SQLite 文件）==")
import sqlite3
con = sqlite3.connect(os.environ["SIGNBOARD_DB"])
check(con.execute("SELECT stage FROM job WHERE id=1").fetchone()[0] == "closed",
      "重启后阶段仍为 closed")
check(con.execute("SELECT COUNT(*) FROM board WHERE kind='new'").fetchone()[0] == 2,
      "新牌仍在")
check(con.execute("SELECT status FROM corridor WHERE id=5").fetchone()[0] == "closed",
      "封闭状态仍在")
con.close()

print("== 6. 封路失败保留（断路）==")
req("POST", "/api/reset")
st = req("GET", "/api/state")
check(st["stage"] == "setup" and all(c["status"] == "open" for c in st["corridors"]),
      "重置后通道全开")
check(not st["plan_items"] and not st["closure_plan"], "方案与清单清空")

n = lambda name: req("POST", "/api/nodes", {"name": name, "kind":
      {"E": "entrance", "S": "service"}.get(name, "hall")})["id"]
e, a, b, sv = n("E"), n("A"), n("B"), n("S")
def cor(x, y): return req("POST", "/api/corridors", {"a_id": x, "b_id": y})["id"]
c_ea, c_ab, c_bs, c_eb = cor(e, a), cor(a, b), cor(b, sv), cor(e, b)
req("POST", "/api/boards", {"node_id": e, "points_to": a})
req("POST", "/api/boards", {"node_id": a, "points_to": b})
req("POST", "/api/boards", {"node_id": b, "points_to": sv})
check(req("GET", "/api/state")["live_check"]["ok"], "新场景初始导向正确")

# 封 E-A 但不换牌：E 的旧牌跨过被封通道 -> 断路；方案项允许为空
req("POST", "/api/plan/closures", {"corridor_id": c_ea})
st = req("GET", "/api/state")
check(st["stage"] == "planning", "加入清单进入 planning")
check(not st["sim_check"]["ok"] and st["sim_check"]["error"]["type"] == "break"
      and st["sim_check"]["error"]["junction_name"] == "E", "预演报 E 断路")
req("POST", "/api/plan/start")
out = req("POST", "/api/close", expect=422)
check(out["failed"] and out["error"]["type"] == "break", "封路失败=断路")
st = req("GET", "/api/state")
check(st["stage"] == "failed", "阶段=failed")
check(all(c["status"] == "open" for c in st["corridors"]), "通道仍全部开放")
check(st["stage_error"].startswith("封路失败"), "记录失败原因")
req("POST", "/api/plan/items", {"node_id": e, "points_to": b}, 409)
req("POST", "/api/install/1", expect=409)

print("== 7. 失败后补方案不可行（锁定），只能重置；重置后成功封路 ==")
req("POST", "/api/reset")
st = req("GET", "/api/state")
# 重新登记方案：封 E-A，同时 E 改指 B
c_ea_id = c_ea
req("POST", "/api/plan/closures", {"corridor_id": c_ea_id})
pi = req("POST", "/api/plan/items", {"node_id": e, "points_to": b})["id"]
req("POST", "/api/plan/start")
req("POST", f"/api/install/{pi}")
req("POST", "/api/close")
st = req("GET", "/api/state")
check(st["stage"] == "closed", "补换牌后封路成功")
check(st["live_check"]["ok"], "封路后 E→B→S 正常")

print("== 8/9. 布展校验：服务台不能有牌、箭头必须指向相邻、缺牌禁开始、空清单禁开始 ==")
req("POST", "/api/reset")
e2 = req("POST", "/api/nodes", {"name": "E2", "kind": "entrance"})["id"]
# setup 阶段的录入校验
req("POST", "/api/boards", {"node_id": sv, "points_to": b}, 400)  # 服务台不能有牌
req("POST", "/api/boards", {"node_id": e2, "points_to": sv}, 400)  # E2 孤立：不相邻
req("POST", "/api/boards", {"node_id": e2, "points_to": a}, 400)   # E2 孤立：不相邻
# setup 阶段不能开始
req("POST", "/api/plan/start", expect=409)
# 删除孤立入口后：只加换牌项（清单仍空）进入 planning
req("DELETE", f"/api/nodes/{e2}")
req("POST", "/api/plan/items", {"node_id": e, "points_to": b})  # E-B 相邻，合法
req("POST", "/api/plan/start", expect=400)  # 空清单不能开始
# 加入清单，现场导向正常 -> 可以开始
req("POST", "/api/plan/closures", {"corridor_id": c_ab})
req("POST", "/api/plan/start")
check(req("GET", "/api/state")["stage"] == "installing", "清单非空且现场有效，可进入安装")
req("POST", "/api/reset")

# 缺牌导致禁止开始
e2 = req("POST", "/api/nodes", {"name": "E2", "kind": "entrance"})["id"]
req("POST", "/api/plan/closures", {"corridor_id": c_ab})  # E2 无牌
out = req("POST", "/api/plan/start", expect=400)
check("缺牌" in out["error"], "初始缺牌时禁止开始安装")
req("POST", "/api/reset")

print("== 10a. 单条已封通道恢复：校验 + 最后一条必须拆完 + 走查回 setup ==")
req("POST", "/api/reset")
# reset 保留已登记路口；清空前面各段登记的路口（级联删除其通道/牌），以便重新载入示例
for nd in req("GET", "/api/state")["nodes"]:
    req("DELETE", f"/api/nodes/{nd['id']}")
req("POST", "/api/seed")
a_item = next(p["id"] for p in req("GET", "/api/state")["plan_items"]
              if p["node_name"] == "A展厅")
b_item = next(p["id"] for p in req("GET", "/api/state")["plan_items"]
              if p["node_name"] == "B展厅")
req("POST", "/api/plan/start")
# 安装阶段恢复接口锁定
req("POST", "/api/restore", {"corridor_id": 5}, 409)
req("POST", f"/api/install/{a_item}")
req("POST", f"/api/install/{b_item}")
req("POST", "/api/close")
stc = req("GET", "/api/state")
check(stc["stage"] == "closed", "封路完成进入 closed")
new_a = next(b["id"] for b in stc["boards"] if b["kind"] == "new" and b["node_name"] == "A展厅")
new_b = next(b["id"] for b in stc["boards"] if b["kind"] == "new" and b["node_name"] == "B展厅")
old_bid = next(b["id"] for b in stc["boards"] if b["kind"] == "old")

# 非法入参
req("POST", "/api/restore", {}, 400)
req("POST", "/api/restore", {"corridor_id": 99999}, 400)          # 通道不存在
req("POST", "/api/restore", {"corridor_id": 1}, 400)              # 通道开放中
req("POST", "/api/restore", {"corridor_id": 5, "board_ids": [old_bid]}, 400)  # 拆旧牌
req("POST", "/api/restore", {"corridor_id": 5, "board_ids": [99999]}, 400)   # 牌不存在
# 最后一条已封通道：必须拆完全部新牌（只拆一块 -> 400，状态不变）
out = req("POST", "/api/restore", {"corridor_id": 5, "board_ids": [new_a]}, 400)
check("最后一条" in out["error"], "最后一条通道未拆完新牌被拒绝")
stx = req("GET", "/api/state")
check(stx["stage"] == "closed", "拒绝后仍为 closed")
check(next(c for c in stx["corridors"] if c["id"] == 5)["status"] == "closed",
      "拒绝后通道仍封闭")
check(len([b for b in stx["boards"] if b["kind"] == "new"]) == 2, "拒绝后新牌都还在")

# 一次拆完两块、开放通道 -> 走查通过 -> setup
out = req("POST", "/api/restore", {"corridor_id": 5, "board_ids": [new_a, new_b]})
check(out["final"] is True, "返回 final=true")
stf = req("GET", "/api/state")
check(stf["stage"] == "setup", "全部恢复后回到布展登记")
check(all(c["status"] == "open" for c in stf["corridors"]), "通道全部开放")
check(all(b["kind"] == "old" for b in stf["boards"]), "新牌全部拆完，旧牌保留")
check(not stf["plan_items"] and not stf["closure_plan"], "本次方案（清单/方案）已清空")
check(stf["live_check"]["ok"], "恢复后现场走查全部入口通过")

print("== 10b. 多条已封通道逐次恢复：失败回滚 + 仅更新所选通道/牌 ==")
req("POST", "/api/reset")
# 上一段（示例馆区）登记的路口仍保留，全部清掉以便搭建新场景
for nd in req("GET", "/api/state")["nodes"]:
    req("DELETE", f"/api/nodes/{nd['id']}")
rn = lambda name: req("POST", "/api/nodes", {"name": name, "kind":
      "entrance" if name == "rE" else ("service" if name == "rS" else "hall")})["id"]
re_, rx, ry, rz, rs = rn("rE"), rn("rX"), rn("rY"), rn("rZ"), rn("rS")
def rc(x, y): return req("POST", "/api/corridors", {"a_id": x, "b_id": y})["id"]
c_ex, c_xy, c_yz, c_zs, c_ey = rc(re_, rx), rc(rx, ry), rc(ry, rz), rc(rz, rs), rc(re_, ry)
for x, y in [(re_, rx), (rx, ry), (ry, rz), (rz, rs)]:
    req("POST", "/api/boards", {"node_id": x, "points_to": y})
check(req("GET", "/api/state")["live_check"]["ok"], "新场景初始导向正确（含示例馆区入口）")
# 封 rE-rX 与 rX-rY：rE 改指 rY；另在 rZ 装一块指向相同的新牌（第二块新牌）
req("POST", "/api/plan/closures", {"corridor_id": c_ex})
req("POST", "/api/plan/closures", {"corridor_id": c_xy})
pi_ey = req("POST", "/api/plan/items", {"node_id": re_, "points_to": ry})["id"]
pi_zs = req("POST", "/api/plan/items", {"node_id": rz, "points_to": rs})["id"]
req("POST", "/api/plan/start")
req("POST", f"/api/install/{pi_ey}")
req("POST", f"/api/install/{pi_zs}")
check(req("GET", "/api/state")["sim_check"]["ok"], "封两条通道的预演通过")
req("POST", "/api/close")
stc = req("GET", "/api/state")
nb_ey = next(b["id"] for b in stc["boards"] if b["kind"] == "new" and b["node_id"] == re_)
nb_zs = next(b["id"] for b in stc["boards"] if b["kind"] == "new" and b["node_id"] == rz)

# 开放 rX-rY 并拆掉 rE→rY：rE-rX 仍封，rE 旧牌跨过封通道 -> 断路，整块回滚
out = req("POST", "/api/restore",
          {"corridor_id": c_xy, "board_ids": [nb_ey]}, expect=422)
check(out["rejected"] and out["error"]["type"] == "break",
      "恢复后断路：报断路且不生效")
check(out["error"]["junction_name"] == "rE", "首个出错路口=rE")
stk = req("GET", "/api/state")
check(stk["stage"] == "closed", "回滚后仍 closed")
check(next(c for c in stk["corridors"] if c["id"] == c_xy)["status"] == "closed"
      and next(c for c in stk["corridors"] if c["id"] == c_ex)["status"] == "closed",
      "回滚：两条通道仍封闭")
check(len([b for b in stk["boards"] if b["kind"] == "new"]) == 2, "回滚：两块新牌都还在")
check(len(stk["closure_plan"]) == 2, "回滚：封路清单仍有两条")
check(all(p["installed"] for p in stk["plan_items"]), "回滚：方案项仍为已安装")

# 先开放 rE-rX（非最后一条），不拆牌：rE 新牌→rY→rZ→rS 可走
out = req("POST", "/api/restore", {"corridor_id": c_ex, "board_ids": []})
check(out["final"] is False, "非最后一条：final=false")
stp = req("GET", "/api/state")
check(stp["stage"] == "closed", "非最后恢复后仍处 closed，可继续操作")
check(next(c for c in stp["corridors"] if c["id"] == c_ex)["status"] == "open"
      and next(c for c in stp["corridors"] if c["id"] == c_xy)["status"] == "closed",
      "仅所选通道开放，另一条仍封闭")
check(len([b for b in stp["boards"] if b["kind"] == "new"]) == 2, "未选新牌保持原状")
check([c["corridor_id"] for c in stp["closure_plan"]] == [c_xy],
      "仅从封路清单移除所选通道")
check(len(stp["plan_items"]) == 2 and all(p["installed"] for p in stp["plan_items"]),
      "换牌方案记录保持原状")
check(stp["live_check"]["ok"], "恢复后现场走查通过")

# 最后一条 rX-rY：只拆一块仍不行（还剩一块）
out = req("POST", "/api/restore",
          {"corridor_id": c_xy, "board_ids": [nb_ey]}, 400)
check("最后一条" in out["error"] and "1" in out["error"], "提示剩 1 块必须拆完")
# 两块一起拆、开放最后一条 -> setup
out = req("POST", "/api/restore",
          {"corridor_id": c_xy, "board_ids": [nb_ey, nb_zs]})
check(out["final"] is True, "最后一条 final=true")
stl = req("GET", "/api/state")
check(stl["stage"] == "setup", "全部恢复回到 setup")
check(all(c["status"] == "open" for c in stl["corridors"]), "通道全部开放")
check(all(b["kind"] == "old" for b in stl["boards"]), "新牌清空、旧牌保留")
check(not stl["plan_items"] and not stl["closure_plan"], "方案与清单已清空")
check(stl["live_check"]["ok"], "旧牌导向恢复正常")

print("== 10c. 无新牌的最终恢复；封路失败阶段恢复被锁定 ==")
req("POST", "/api/reset")
# 仅封旁路 rE-rY（无换牌项）：预演与封路均成立
req("POST", "/api/plan/closures", {"corridor_id": c_ey})
req("POST", "/api/plan/start")
req("POST", "/api/close")
out = req("POST", "/api/restore", {"corridor_id": c_ey, "board_ids": []})
check(out["final"] and req("GET", "/api/state")["stage"] == "setup",
      "无剩余新牌：最后一条通道可直接恢复并回 setup")
# 封路失败阶段恢复锁定（封 rE-rX 不换牌 -> 封路核对断路 -> failed）
req("POST", "/api/reset")
req("POST", "/api/plan/closures", {"corridor_id": c_ex})
req("POST", "/api/plan/start")
req("POST", "/api/close", expect=422)
check(req("GET", "/api/state")["stage"] == "failed", "构造 failed 状态")
req("POST", "/api/restore", {"corridor_id": c_ex}, 409)
# setup 阶段同样不可用
req("POST", "/api/reset")
req("POST", "/api/restore", {"corridor_id": c_ex}, 409)

server.shutdown()
print(f"\n总计：通过 {PASS}，失败 {FAIL}")
sys.exit(1 if FAIL else 0)
