#!/usr/bin/env python3
"""ops/supabase_client.py — `SupabaseClient` + `load_env`, `ops/control.py`'s
only dependency outside the stdlib.

Superseded by `services/core`'s hosted cycle endpoint, fed by that same live
session's broker reads; its own Gemini-orchestrated predecessor had already
been retired). `ops/control.py` was the only other importer of this class, so
it moves with nothing else pulled along.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore


# --- Configuration & Environment Setup ------------------------------------

def load_env(env_path: Optional[str] = None) -> Dict[str, str]:
    """Load environment variables from file candidates if not already present."""
    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    if os.getenv("EDGE_ENV_FILE"):
        candidates.append(Path(os.getenv("EDGE_ENV_FILE")))
    if os.getenv("HOME"):
        candidates.append(Path(os.getenv("HOME")) / ".env")
    if os.getenv("USERPROFILE"):
        candidates.append(Path(os.getenv("USERPROFILE")) / ".env")

    root_dir = Path(__file__).resolve().parent.parent
    candidates.append(root_dir / ".env")
    candidates.append(root_dir / "ops" / ".env")

    for path in candidates:
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
            break

    return dict(os.environ)


# --- Supabase RPC Client ----------------------------------------------------

class SupabaseClient:
    """Client for executing deterministic-trader Supabase RPC functions."""

    def __init__(self, url: Optional[str] = None, key: Optional[str] = None):
        self.url = (url if url is not None else os.getenv("EDGE_SUPABASE_URL", "")).rstrip("/")
        self.key = key if key is not None else os.getenv("EDGE_SUPABASE_KEY", "")
        self.is_configured = bool(self.url and self.key)


    def _rpc(self, fn_name: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        if not self.is_configured:
            raise RuntimeError("Supabase client is not configured (missing URL or KEY).")
        if httpx is None:
            raise RuntimeError("httpx package is required for Supabase RPC calls.")

        endpoint = f"{self.url}/rest/v1/rpc/{fn_name}"
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(endpoint, headers=headers, json=payload or {})
            resp.raise_for_status()
            return resp.json()

    def _select(self, table: str, params: Dict[str, str]) -> Any:
        """Both tables have RLS enabled with no anon/authenticated policy so this only works with the service-role key this
        client is always constructed with, which bypasses RLS the same way
        every RPC call above already does.
        """
        if not self.is_configured:
            raise RuntimeError("Supabase client is not configured (missing URL or KEY).")
        if httpx is None:
            raise RuntimeError("httpx package is required for Supabase REST calls.")

        endpoint = f"{self.url}/rest/v1/{table}"
        headers = {"apikey": self.key, "Authorization": f"Bearer {self.key}"}
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(endpoint, headers=headers, params=params)
            resp.raise_for_status()
            return resp.json()

    def get_control(self) -> Optional[Dict[str, Any]]:
        """Call 1: select edge_control_json() as control"""
        try:
            res = self._rpc("edge_control_json")
            if isinstance(res, dict):
                return res
            return None
        except Exception as e:
            sys.stderr.write(f"[Supabase] edge_control_json failed: {e}\n")
            return None

    def claim_cycle(self, cycle_id: str, cycle_type: str) -> Dict[str, Any]:
        """Call 2: select edge_cycle_claim(id, type)"""
        return self._rpc("edge_cycle_claim", {"p_cycle_id": cycle_id, "p_cycle_type": cycle_type})

    def renew_cycle(self, cycle_id: str) -> Any:
        """Call 3: select edge_cycle_renew(id)"""
        return self._rpc("edge_cycle_renew", {"p_cycle_id": cycle_id})

    def release_cycle(self, cycle_id: str) -> Any:
        """Call 4: select edge_cycle_release(id)"""
        return self._rpc("edge_cycle_release", {"p_cycle_id": cycle_id})

    def get_prior(self, trading_day: str) -> Dict[str, Any]:
        """Call 5: select edge_prior(trading_day)"""
        return self._rpc("edge_prior", {"p_trading_day": trading_day})

    def get_play_state(self) -> Dict[str, Any]:
        """Call 6: select edge_play_state_json()"""
        return self._rpc("edge_play_state_json")

    def get_loss_closes(self) -> List[Dict[str, Any]]:
        """Call 7: select edge_loss_closes_recent()"""
        return self._rpc("edge_loss_closes_recent")

    def write_runlog(
        self,
        cycle_id: str,
        cycle_type: str,
        trading_day: str,
        command: str,
        payload: Optional[Dict[str, Any]] = None,
        placements: Optional[List[Dict[str, Any]]] = None,
        rejections: Optional[List[Dict[str, Any]]] = None,
        state_updates: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Call 8: select edge_runlog_write(...)"""
        params = {
            "p_cycle_id": cycle_id,
            "p_cycle_type": cycle_type,
            "p_trading_day": trading_day,
            "p_command": command,
            "p_payload": payload or {},
            "p_placements": placements or [],
            "p_rejections": rejections or [],
            "p_state_updates": state_updates or {},
        }
        return self._rpc("edge_runlog_write", params)

    def apply_control(self, updates: Dict[str, Any]) -> Any:
        """Call 9a: select edge_control_apply(...)"""
        return self._rpc("edge_control_apply", {"p_updates": updates})

    def get_dip_config(self) -> Optional[Dict[str, Any]]:
        """ops/control: select edge_dip_recovery_config_json()"""
        try:
            res = self._rpc("edge_dip_recovery_config_json")
            return res if isinstance(res, dict) else None
        except Exception as e:
            sys.stderr.write(f"[Supabase] edge_dip_recovery_config_json failed: {e}\n")
            return None

    def apply_dip_config(self, updates: Dict[str, Any]) -> Any:
        """ops/control: select edge_dip_recovery_config_apply(...)"""
        return self._rpc("edge_dip_recovery_config_apply", {"p_updates": updates})

    def apply_play_state(self, updates: Dict[str, Any]) -> Any:
        """Call 9b: select edge_play_state_apply(...)"""
        return self._rpc("edge_play_state_apply", {"p_updates": updates})

    def append_loss_closes(self, loss_closes: List[Dict[str, Any]], cycle_id: str) -> Any:
        """Call 9c: select edge_loss_closes_append(...)"""
        return self._rpc("edge_loss_closes_append", {"p_loss_closes": loss_closes, "p_cycle_id": cycle_id})

    def upsert_benchmark(self, benchmark: Dict[str, Any]) -> Any:
        """Call 9d: select edge_benchmark_upsert(...)"""
        return self._rpc("edge_benchmark_upsert", {"p_row": benchmark})

    def enqueue_alerts(self, alerts: List[Dict[str, Any]]) -> Any:
        """Call 9e: select edge_alerts_enqueue(...)"""
        return self._rpc("edge_alerts_enqueue", {"p_alerts": alerts})

    def get_benchmark_latest(self) -> Optional[Dict[str, Any]]:
        """Call 19: select edge_benchmark_latest."""
        try:
            res = self._rpc("edge_benchmark_latest")
            return res if isinstance(res, dict) else None
        except Exception as e:
            sys.stderr.write(f"[Supabase] edge_benchmark_latest failed: {e}\n")
            return None

    def apply_session_status(self, cycle_id: str, status: str) -> Any:
        """Call 20: select edge_session_status_apply(id, status).
        The cycle's last write — `status` is `report.json`'s own `status`
        field, not a second derivation of it."""
        return self._rpc(
            "edge_session_status_apply", {"p_cycle_id": cycle_id, "p_status": status}
        )

    def get_current_session(self) -> Optional[Dict[str, Any]]:
        """`ops/control.py`'s `step` verb: the most recently *started* cycle's
        `edge_session_stats` row with its step-log columns. Ordered
        by `started_at`, unlike `get_last_session`, because the point is to see
        a cycle that is still running."""
        try:
            rows = self._select(
                "edge_session_stats",
                {
                    "select": "cycle_id,cycle_type,status,started_at,finished_at,"
                              "current_step,current_step_at,last_beat_at,rows_written",
                    "order": "started_at.desc.nullslast",
                    "limit": "1",
                },
            )
            return rows[0] if rows else None
        except Exception as e:
            sys.stderr.write(f"get_current_session failed: {e}\n")
            return None

    def get_last_session(self) -> Optional[Dict[str, Any]]:
        """`ops/control.py`'s `last` verb: the most recently finished cycle's
        `edge_session_stats` row. `finished_at` first, so an in-flight
        cycle's still-null `finished_at` row never outranks the previous
        completed one; `started_at` breaks the tie for two cycles that
        finished in the same second."""
        try:
            rows = self._select(
                "edge_session_stats",
                {
                    "select": "cycle_id,cycle_type,status,started_at,finished_at",
                    "order": "finished_at.desc.nullslast,started_at.desc",
                    "limit": "1",
                },
            )
            return rows[0] if rows else None
        except Exception as e:
            sys.stderr.write(f"[Supabase] edge_session_stats select failed: {e}\n")
            return None

    def get_last_alert_for_cycle(self, cycle_id: str) -> Optional[Dict[str, Any]]:
        """The status alert `report._status_alert` enqueued for that cycle — its
        body already ends with `_footer_with_link`'s `cycle · time · routine ·
        runlog · doc-link` line."""
        try:
            rows = self._select(
                "edge_alerts",
                {
                    "select": "title,body,created_at",
                    "cycle_id": f"eq.{cycle_id}",
                    "order": "created_at.desc",
                    "limit": "1",
                },
            )
            return rows[0] if rows else None
        except Exception as e:
            sys.stderr.write(f"[Supabase] edge_alerts select failed: {e}\n")
            return None
