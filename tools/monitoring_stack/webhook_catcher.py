from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
            print("=== Alertmanager webhook received ===")
            for a in data.get('alerts', []):
                print(f"  status={a.get('status')} alertname={a.get('labels', {}).get('alertname')} "
                      f"summary={a.get('annotations', {}).get('summary')}")
        except Exception as e:
            print("parse error:", e, body[:200])
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


if __name__ == '__main__':
    print("webhook catcher listening on :9199/webhook")
    ThreadingHTTPServer(('0.0.0.0', 9199), Handler).serve_forever()
