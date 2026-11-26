"""额度服务的 HTTP 接口（仅监听回环地址，供各闸口值班机接入）。"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .model import NotFound, ReconcileError, TrailError, Conflict
from .service import QuotaService

ROUTES = [
    ("POST", "/service/allot"),
    ("POST", "/service/lend"),
    ("POST", "/service/permits"),
    ("GET", "/service/permits/{permit}"),
    ("POST", "/service/moves"),
    ("POST", "/service/moves/{group}/decision"),
    ("POST", "/service/gates/{gate}/disconnect"),
    ("POST", "/service/gates/{gate}/sync"),
    ("POST", "/service/closures"),
    ("POST", "/service/closures/seen"),
    ("POST", "/service/closures/closed"),
    ("POST", "/service/returns/request"),
    ("POST", "/service/returns"),
    ("POST", "/service/use-records"),
    ("GET", "/service/gates/{gate}/bundle"),
    ("GET", "/service/gates/{gate}"),
    ("GET", "/service/overview"),
]


def _pattern(path):
    import re
    parts = path.split("/")
    return "/".join(
        r"[^/]+" if p.startswith("{") else re.escape(p) for p in parts
    )


class ServiceHandler(BaseHTTPRequestHandler):
    def _send(self, status, value):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= length <= 1 << 20:
            raise TrailError("请求体超出限制")
        if not length:
            return {}
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise TrailError("请求必须是 JSON 对象")
        return body

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self):
        path = urlsplit(self.path).path
        try:
            body = self._read_body() if self.command == "POST" else {}
            result = self.server.backend.handle(self.command, path, body)
            self._send(200, result)
        except (NotFound, Conflict, ReconcileError) as exc:
            self._send(exc.status, {"message": str(exc),
                                    "error": type(exc).__name__})
        except (ValueError, json.JSONDecodeError, TrailError) as exc:
            self._send(400, {"message": str(exc), "error": "BadRequest"})

    def log_message(self, *args):
        pass


class ServiceHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, service: QuotaService):
        super().__init__(address, ServiceHandler)
        self.service = service
        self.backend = HTTPBackend(service)


class HTTPBackend:
    """把 HTTP 调用映射到 :class:`QuotaService` 的方法，便于单元测试。"""

    def __init__(self, service: QuotaService):
        self.service = service

    def handle(self, method, path, body):
        s = self.service
        if method == "POST" and path == "/service/allot":
            return s.allot(body["gate"], body["window"], body["grant_id"],
                           body["count"], body["at"])
        if method == "POST" and path == "/service/lend":
            return s.lend(body["grant_id"], body["to_gate"], body["count"],
                          body["at"], body.get("new_grant_id"))
        if method == "POST" and path == "/service/permits":
            return s.issue_permit(body["permit"], body["group"], body["gate"],
                                  body["grant_id"], body["count"], body["at"])
        if method == "GET" and path.startswith("/service/permits/"):
            return s.get_permit(path.rsplit("/", 1)[-1])
        if method == "POST" and path == "/service/moves":
            return s.move(body["group"], body["gate"], body["window"],
                          body["at"], body.get("new_permit"))
        if method == "POST" and path.startswith("/service/moves/") \
                and path.endswith("/decision"):
            group = path.split("/")[-2]
            return s.decide_move(group, body["decision"], body["at"],
                                 body.get("new_permit"))
        if method == "POST" and path.endswith("/disconnect"):
            gate = path.split("/")[-2]
            return s.report_disconnect(gate, body["clock_error"], body["at"])
        if method == "POST" and path.endswith("/sync"):
            gate = path.split("/")[-2]
            return s.report_sync(gate, body["at"])
        if method == "POST" and path == "/service/closures":
            return s.closure_publish(body["direction"], body["at"])
        if method == "POST" and path == "/service/closures/seen":
            return s.closure_seen(body["gate"], body["at"])
        if method == "POST" and path == "/service/closures/closed":
            return s.closure_closed(body["gate"], body["direction"], body["at"])
        if method == "POST" and path == "/service/returns/request":
            return s.request_return(body["gate"], body["grant_id"],
                                    body["count"], body["at"])
        if method == "POST" and path == "/service/returns":
            return s.record_return(body["gate"], body["grant_id"],
                                   body["returned"], body["last_use_no"],
                                   body["reported_consumed"], body["at"])
        if method == "POST" and path == "/service/use-records":
            return s.accept_use_record(body["gate"], body["grant_id"],
                                       body["use_no"], body["permit"],
                                       body["count"], body["local_time"],
                                       body["at"])
        if method == "GET" and path.endswith("/bundle"):
            return s.gate_bundle(path.split("/")[-2])
        if method == "GET" and path.startswith("/service/gates/"):
            return s.gate_view(path.rsplit("/", 1)[-1])
        if method == "GET" and path == "/service/overview":
            return s.overview()
        raise NotFound(f"没有接口 {method} {path}")


def run(service, host="127.0.0.1", port=8091):
    with ServiceHTTPServer((host, port), service) as httpd:
        print(f"额度服务 http://{host}:{httpd.server_port}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
