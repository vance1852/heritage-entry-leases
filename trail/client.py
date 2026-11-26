"""值班机侧的额度服务 HTTP 客户端。

所有方法都对应 :mod:`trail.httpapi` 的一个接口；网络层失败（超时、断连）
抛出 :class:`ServiceUnreachable`，调用方据此转入离线流程而不是误判为拒绝。
"""

import http.client
import json


class ServiceUnreachable(RuntimeError):
    """服务不可达：闸口应继续按本机离线档案核验。"""


class ServiceClient:
    def __init__(self, host="127.0.0.1", port=8091, timeout=3):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _call(self, method, path, body=None):
        conn = http.client.HTTPConnection(self.host, self.port,
                                          timeout=self.timeout)
        try:
            payload = json.dumps(body, ensure_ascii=False) if body else None
            conn.request(method, path, payload,
                         {"Content-Type": "application/json"} if payload else {})
            response = conn.getresponse()
            raw = response.read()
            data = json.loads(raw) if raw else {}
            if response.status >= 400:
                raise ServiceError(response.status, data)
            return data
        except (OSError, http.client.HTTPException) as exc:
            raise ServiceUnreachable(str(exc)) from exc
        finally:
            conn.close()

    def allot(self, **kw):
        return self._call("POST", "/service/allot", kw)

    def lend(self, **kw):
        return self._call("POST", "/service/lend", kw)

    def issue_permit(self, **kw):
        return self._call("POST", "/service/permits", kw)

    def get_permit(self, permit):
        return self._call("GET", f"/service/permits/{permit}")

    def move(self, **kw):
        return self._call("POST", "/service/moves", kw)

    def decide_move(self, group, **kw):
        return self._call("POST", f"/service/moves/{group}/decision", kw)

    def report_disconnect(self, gate, **kw):
        return self._call("POST", f"/service/gates/{gate}/disconnect", kw)

    def report_sync(self, gate, **kw):
        return self._call("POST", f"/service/gates/{gate}/sync", kw)

    def closure_publish(self, **kw):
        return self._call("POST", "/service/closures", kw)

    def closure_seen(self, **kw):
        return self._call("POST", "/service/closures/seen", kw)

    def closure_closed(self, **kw):
        return self._call("POST", "/service/closures/closed", kw)

    def request_return(self, **kw):
        return self._call("POST", "/service/returns/request", kw)

    def record_return(self, **kw):
        return self._call("POST", "/service/returns", kw)

    def accept_use_record(self, **kw):
        return self._call("POST", "/service/use-records", kw)

    def gate_bundle(self, gate):
        return self._call("GET", f"/service/gates/{gate}/bundle")

    def gate_view(self, gate):
        return self._call("GET", f"/service/gates/{gate}")

    def overview(self):
        return self._call("GET", "/service/overview")


class ServiceError(RuntimeError):
    def __init__(self, status, data):
        super().__init__(data.get("message", f"HTTP {status}"))
        self.status = status
        self.data = data
