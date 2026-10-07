"""测试用最小 Flask 垫片：仅实现 app.py 用到的子集，非交付物。"""
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer


class G:
    def __init__(self):
        self.__dict__["_store"] = {}

    def __contains__(self, k):
        return k in self._store

    def pop(self, k, default=None):
        return self._store.pop(k, default)

    def __getattr__(self, k):
        try:
            return self._store[k]
        except KeyError:
            raise AttributeError(k)

    def __setattr__(self, k, v):
        self._store[k] = v


class _App:
    def __init__(self, *a, **kw):
        self.routes = []
        self.before = []
        self.teardown = []

    def route(self, path, **kw):
        def deco(fn):
            rx = re.compile(r"^" + re.sub(r"<(?:int:)?(\w+)>", r"(?P<\1>[^/]+)", path) + r"$")
            self.routes.append((rx, fn))
            return fn
        return deco

    def teardown_appcontext(self, fn):
        self.teardown.append(fn)

    def run(self, **kw):
        serve(self, kw.get("port", 5000))


Flask = _App


def jsonify(obj=None, **kw):
    if obj is None:
        obj = kw
    return _Resp(json.dumps(obj, ensure_ascii=False), 200, "application/json")


class _Resp:
    def __init__(self, body, status, ctype):
        self.body, self.status, self.ctype = body, status, ctype


def render_template(name, **kw):
    with open("templates/" + name, encoding="utf-8") as f:
        return _Resp(f.read(), 200, "text/html; charset=utf-8")


class _Request:
    def __init__(self):
        self.method = "GET"
        self._json = None

    @property
    def json(self):
        return self._json

    def get_json(self, force=False):
        return self._json


request = _Request()
g = G()


def serve(app, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _handle(self):
            from app import DB_PATH  # noqa
            path = self.path.split("?")[0]
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                request._json = json.loads(raw) if raw else None
            except Exception:
                request._json = None
            request.method = self.command
            g.__dict__.clear()
            resp = None
            try:
                for rx, fn in app.routes:
                    m = rx.match(path)
                    if m:
                        kwargs = {k: (int(v) if k.isalnum() else v) for k, v in m.groupdict().items()}
                        out = fn(**kwargs)
                        resp = out if isinstance(out, tuple) else (out, 200)
                        if isinstance(resp, tuple):
                            r, code = resp
                            resp = (r, code)
                        else:
                            resp = (resp, 200)
                        break
                if resp is None:
                    resp = (_Resp("not found", 404, "text/plain"), 404)
            finally:
                for fn in app.teardown:
                    fn(None)
            r, code = resp
            if isinstance(r, tuple):
                r, code = r
            body = r.body.encode("utf-8") if isinstance(r.body, str) else r.body
            self.send_response(code)
            self.send_header("Content-Type", r.ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _handle
        do_POST = _handle
        do_DELETE = _handle

    HTTPServer(("127.0.0.1", port), H).serve_forever()
