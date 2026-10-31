"""A tiny OpenAI-compatible endpoint for tests: GET /models and streamed POST /chat/completions.

Replies "echo: <last user text> (n=<messages>)". A user text starting with "run: " gets a bash tool call for the
rest first; after the tool result comes back, the reply quotes the tool output. Requires `Authorization: Bearer
test-key`. Run alone: python3 tests/fake_openai.py [port]
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ["fake-small", "fake-large"]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth(self):
        if self.headers.get("Authorization") != "Bearer test-key":
            self._json(401, {"error": {"message": "bad key"}})
            return False
        return True

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models") and self._auth():
            self._json(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions") or not self._auth():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        msgs = body["messages"]
        if body.get("model") not in MODELS:
            return self._json(404, {"error": {"message": f"no model {body.get('model')}"}})
        last = msgs[-1]
        user = next(m for m in reversed(msgs) if m["role"] == "user")["content"]
        if last["role"] == "user" and user.startswith("run: "):
            delta = {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                     "function": {"name": "bash", "arguments": json.dumps({"command": user[5:]})}}]}
        elif last["role"] == "tool":
            delta = {"content": "tool said: " + last["content"].split("\n")[0]}
        else:
            delta = {"content": f"echo: {user} (n={len(msgs)})"}
        if not body.get("stream"):
            return self._json(200, {"choices": [{"message": {"role": "assistant", **delta}}]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]}]
        if "content" in delta:  # stream the text in two pieces
            t = delta["content"]
            chunks += [{"choices": [{"index": 0, "delta": {"content": t[:5]}}]},
                       {"choices": [{"index": 0, "delta": {"content": t[5:]}}]}]
        else:
            chunks.append({"choices": [{"index": 0, "delta": delta}]})
        chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 10 * len(msgs), "completion_tokens": 5}})
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def serve(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


if __name__ == "__main__":
    srv, url = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8790)
    print(url, flush=True)
    threading.Event().wait()
