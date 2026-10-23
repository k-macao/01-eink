#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""🧪 Jev 协议兼容回声服务（离线自检 / 联调用，不需要模型权重）。

为什么需要它：真实的开源 Jev 模型（laya / kev / NanoJev）要从 Hugging Face 拉
1~2 GB 权重才能跑。本服务用标准库实现同一套 ``POST /v1/systemone`` 协议与返回结构，
让接入层（``output/jev_bridge.py``）可以在**断网、无 Key、无 GPU** 的环境里被完整验证：
结构校验、概率对账、收敛护栏、缺席降级、留痕结算都能跑通，只有「概率本身的预测力」
需要真实权重（那是模型的事，不是接入的事）。

    python3 tools/jev_mock_server.py --port 8009 &          # 正常：确定性伪概率
    python3 tools/jev_mock_server.py --scenario extreme     # 极端概率 0.995（测收敛护栏）
    python3 tools/jev_mock_server.py --scenario malformed   # 结构不合法（测校验拒绝）
    python3 tools/jev_mock_server.py --scenario boom        # HTTP 500（测降级）
    python3 tools/jev_mock_server.py --scenario slow        # 延迟 2 秒（测超时）
    python3 tools/jev_mock_server.py --api-key secret       # 强制 Bearer 鉴权（测 401）

概率由 ``sha256(state + qid)`` 导出：同一输入永远得到同一份分布（可复现，便于回归测试），
但它**不含任何市场预测力**——它是协议夹具，不是模型。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCENARIOS = ("normal", "extreme", "malformed", "boom", "slow", "noauth")
READY_PATH = "/ready"
SYSTEM_ONE_PATH = "/v1/systemone"


def _seeded(state_key, qid, n):
    """由 state+qid 导出 n 个确定性伪随机数（与模型无关，只为可复现）。"""
    digest = hashlib.sha256(f"{state_key}|{qid}".encode("utf-8")).digest()
    return [digest[i] / 255.0 for i in range(n)]


def _normalize(vals):
    total = sum(vals) or 1.0
    return [v / total for v in vals]


def _answer(qid, qdef, state_key, scenario):
    qtype = qdef.get("type")
    crit = qdef.get("criteria")
    if qtype == "choice":
        keys = list(crit.keys()) if isinstance(crit, dict) else list(crit or [])
        k = max(2, len(keys))
        raw = _seeded(state_key, qid, k)
        if scenario == "extreme":
            probs = [0.005] * k
            probs[max(range(k), key=lambda i: raw[i])] = 0.995
        else:
            probs = _normalize([v ** 2 + 0.05 for v in raw])
        dist = {key: round(p, 4) for key, p in zip(keys, probs)}
        top = max(dist, key=dist.get)
        return {"type": "choice", "choice": top, "probabilities": dist,
                "confidence": round(max(dist.values()), 4)}
    if qtype == "score":
        levels = list(crit or [])
        k = max(2, len(levels))
        raw = _seeded(state_key, qid, k)
        probs = _normalize([v ** 2 + 0.05 for v in raw])
        return {"type": "score",
                "score": round(sum(i * p for i, p in enumerate(probs)), 4),
                "legend": {str(i): c for i, c in enumerate(levels)},
                "probabilities": {str(i): round(p, 4) for i, p in enumerate(probs)},
                "confidence": round(max(probs), 4)}
    raw = _seeded(state_key, qid, 2)
    p = 0.995 if scenario == "extreme" else round(raw[0], 4)
    return {"type": "noul", "noul": p, "confidence": round(max(p, 1 - p), 4)}


def _malformed(qid, qdef, state_key):
    """结构不合法：概率和 != 1、choice 与最大概率不一致、noul 缺失。"""
    qtype = qdef.get("type")
    if qtype == "choice":
        keys = list(qdef["criteria"].keys())
        return {"type": "choice", "choice": keys[-1],
                "probabilities": {k: 0.4 for k in keys}, "confidence": 0.9}
    if qtype == "score":
        return {"type": "score", "score": 99.0, "probabilities": {"0": 0.5, "9": 0.5}}
    return {"type": "noul", "confidence": 0.5}          # 缺 noul 字段


def build_response(payload, scenario=""):
    """按协议组装一次响应；返回 (http_status, body)。"""
    if scenario == "boom":
        return 500, {"error": {"message": "内部错误（自检场景）", "type": "server_error"}}
    state = payload.get("state")
    questions = payload.get("questions")
    if state is None or not isinstance(questions, dict) or not questions:
        return 400, {"error": {"message": "需要 state 和 questions",
                               "type": "invalid_request_error"}}
    state_key = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False,
                                                               sort_keys=True)
    answers = {}
    for qid, qdef in questions.items():
        if scenario == "malformed":
            answers[qid] = _malformed(qid, qdef, state_key)
        else:
            answers[qid] = _answer(qid, qdef, state_key, scenario)
    tokens = len(json.dumps(state, ensure_ascii=False)) // 4
    return 200, {"model": "mock-jev-echo", "answers": answers,
                 "usage": {"input_tokens": tokens, "output_tokens": 0}}


def make_handler(scenario="", api_key="", latency=0.0, log=False):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            if log:
                print("[mock] " + fmt % args, flush=True)

        def do_GET(self):
            if self.path.rstrip("/") in (READY_PATH, "/health"):
                return self._send(200, {"ok": True, "model": "mock-jev-echo",
                                        "backend": "echo", "precision": "n/a",
                                        "scenario": scenario or "normal"})
            self._send(404, {"error": {"message": "not found"}})

        def do_POST(self):
            if self.path.rstrip("/") != SYSTEM_ONE_PATH:
                return self._send(404, {"error": {"message": "not found"}})
            if api_key:
                if self.headers.get("Authorization", "") != f"Bearer {api_key}":
                    return self._send(401, {"error": {"message": "bad api key",
                                                      "type": "authentication_error"}})
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception as exc:
                return self._send(400, {"error": {"message": f"请求不是合法 JSON：{exc}",
                                                  "type": "invalid_request_error"}})
            if latency:
                time.sleep(latency)
            code, body = build_response(payload, scenario)
            self._send(code, body)

    return Handler


def serve(port=8009, host="127.0.0.1", scenario="", api_key="", latency=0.0, log=False):
    srv = ThreadingHTTPServer((host, port), make_handler(scenario, api_key, latency, log))
    print(f"🧪 Jev 协议回声服务 http://{host}:{port}{SYSTEM_ONE_PATH}"
          f"（scenario={scenario or 'normal'}，无模型权重）", flush=True)
    srv.serve_forever()
    return srv


def main(argv=None):
    parser = argparse.ArgumentParser(description="Jev 协议兼容回声服务（离线自检用）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8009)
    parser.add_argument("--scenario", default="normal", choices=SCENARIOS)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--latency", type=float, default=0.0, help="每题延迟秒数（测超时用）")
    parser.add_argument("--log", action="store_true")
    args = parser.parse_args(argv)
    try:
        serve(args.port, args.host, args.scenario, args.api_key, args.latency, args.log)
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
