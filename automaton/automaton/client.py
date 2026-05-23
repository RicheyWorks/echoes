"""Python client for the automaton HTTP API.

Designed for agents: import, instantiate, call methods, no HTTP details.

    from automaton.client import AutomatonClient

    c = AutomatonClient("http://127.0.0.1:8080", token="...")
    c.register_workflow({"name": "wf", "steps": [...]})
    run = c.trigger("wf", payload={"caller": "my-agent"})
    c.signal(run["run_id"], "agent_response", payload={"answer": 42})

For incoming webhook callers, `send_signed_webhook` signs and posts the body
in one call:

    c.send_signed_webhook("gh-push", secret_hex, body={"event": "push"})

Synchronous (httpx). No async client - the workflow engine is fundamentally
poll-based and an async wrapper adds complexity without value here. If an
agent needs async, it should call these from a thread.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Optional

import httpx


class AutomatonError(Exception):
    def __init__(self, status_code: int, body: Any):
        super().__init__(f"automaton API error ({status_code}): {body!r}")
        self.status_code = status_code
        self.body = body


class AutomatonClient:
    def __init__(self, base_url: str, token: Optional[str] = None,
                 timeout: float = 30.0, trust_env: bool = True):
        """trust_env: pass False to bypass system HTTP_PROXY/SOCKS_PROXY env
        vars - useful for localhost tests where the env may declare proxies
        that don't actually serve loopback."""
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = httpx.Client(timeout=timeout, trust_env=trust_env)

    # --- read ---

    def health(self) -> dict:
        return self._get("/healthz")

    def runs(self) -> list[dict]:
        return self._get("/api/runs")

    def run(self, run_id: int) -> dict:
        return self._get(f"/api/run/{run_id}")

    def step_types(self) -> dict:
        return self._get("/api/step_types")

    def crons(self) -> list[dict]:
        return self._get("/api/crons")

    def signals(self) -> list[dict]:
        return self._get("/api/signals")

    def webhooks(self) -> list[dict]:
        return self._get("/api/webhooks")

    # --- write ---

    def register_workflow(self, spec: dict) -> dict:
        """Register or update a workflow. Returns {workflow_def_id, name}."""
        return self._post("/api/workflows", body=spec)

    def trigger(self, workflow_name: str, payload: Optional[Any] = None) -> dict:
        """Trigger a run. Returns {run_id}."""
        body = {}
        if payload is not None:
            body["payload"] = payload
        return self._post(f"/api/trigger/{workflow_name}", body=body)

    def register_cron(self, workflow_name: str, cron_expr: str) -> dict:
        return self._post("/api/crons",
                          body={"workflow_name": workflow_name,
                                "cron_expr": cron_expr})

    def cancel(self, run_id: int, reason: Optional[str] = None) -> dict:
        """Cancel an in-flight run."""
        body = {}
        if reason is not None:
            body["reason"] = reason
        return self._post(f"/api/run/{run_id}/cancel", body=body)

    def signal(self, run_id: int, name: str,
               payload: Optional[Any] = None) -> dict:
        """Send a signal to a parked run. Returns {signal_id}."""
        body = {}
        if payload is not None:
            body["payload"] = payload
        return self._post(f"/api/signals/{run_id}/{name}", body=body)

    # --- webhook side: signing for callers acting as upstream ---

    def send_signed_webhook(
        self,
        endpoint_name: str,
        secret_hex: str,
        body: Any,
        algo: str = "sha256",
        header: str = "X-Automaton-Signature",
    ) -> dict:
        """For testing webhook endpoints or for agents that act as upstream
        callers. Computes the HMAC over the JSON-serialized body, attaches
        it as the signature header, and POSTs to /webhook/<name>."""
        body_bytes = json.dumps(body).encode("utf-8") if not isinstance(body, (bytes, str)) else (
            body if isinstance(body, bytes) else body.encode("utf-8")
        )
        key = bytes.fromhex(secret_hex)
        digestmod = {"sha256": hashlib.sha256, "sha1": hashlib.sha1,
                     "sha512": hashlib.sha512}[algo]
        sig = f"{algo}=" + hmac.new(key, body_bytes, digestmod).hexdigest()
        url = f"{self.base_url}/webhook/{endpoint_name}"
        r = self._client.post(url, content=body_bytes, headers={
            "Content-Type": "application/json",
            header: sig,
        })
        return self._unwrap(r)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- internals ---

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _get(self, path: str) -> Any:
        r = self._client.get(self.base_url + path, headers=self._headers())
        return self._unwrap(r)

    def _post(self, path: str, body: Any) -> Any:
        r = self._client.post(self.base_url + path, json=body,
                              headers=self._headers())
        return self._unwrap(r)

    @staticmethod
    def _unwrap(r: httpx.Response) -> Any:
        try:
            data = r.json()
        except Exception:
            data = r.text
        if r.status_code >= 400:
            raise AutomatonError(r.status_code, data)
        return data
