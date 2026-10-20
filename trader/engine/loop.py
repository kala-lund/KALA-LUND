from __future__ import annotations

"""Pipeline + aggregation + automation loop (single module, no duplication)."""

import threading
import time
import uuid
from dataclasses import dataclass, field

from ..core.kit import log, safe_msg
from ..data.market import MarketStore, TF_MS
from ..data.quality import check
from ..features.feat import features
from ..risk.guard import Account, Risk
from ..strategies.strats import ALL


@dataclass
class Agg:
    action: str
    score: float
    conf: float
    disagree: float
    who: list
    reason: str


def combine(sigs, weights: dict, max_age_s: int = 3600, min_conf: float = 0.3,
            max_dis: float = 0.7, now_ms: int = 0) -> Agg:
    now = now_ms or int(time.time() * 1000)
    fresh = [s for s in sigs if (now - s.ts) <= max_age_s * 1000]
    if not fresh:
        return Agg("ABSTAIN", 0.0, 0.0, 0.0, [], "No fresh signals.")
    vals = [(max(-1.0, min(1.0, s.signal)),
             float(weights.get(s.name, 1.0)) * max(0.0, min(1.0, s.conf)), s.name) for s in fresh]
    tw = sum(abs(w) for _, w, _ in vals)
    score = sum(v * w for v, w, _ in vals) / (tw or 1e-12)
    mean = sum(v for v, _, _ in vals) / len(vals)
    dis = (sum((v - mean) ** 2 for v, _, _ in vals) / len(vals)) ** 0.5
    conf = sum(s.conf for s in fresh) / len(fresh)
    who = [s.name for s in fresh]
    if dis > max_dis:
        return Agg("ABSTAIN", round(score, 4), round(conf, 4), round(dis, 4), who, "Disagreement too high.")
    if conf < min_conf:
        return Agg("ABSTAIN", round(score, 4), round(conf, 4), round(dis, 4), who, "Confidence too low.")
    act = "BUY" if score > 0.25 else ("SELL" if score < -0.25 else "HOLD")
    return Agg(act, round(score, 4), round(conf, 4), round(dis, 4), who, f"Score {score:.3f}.")


def run_cycle(symbols: list[str], timeframe: str, weights: dict, store: MarketStore,
              stale_s: int, max_conc: float, equity: float, ai=None) -> dict:
    """One full pass over the universe. Concurrent fetch; per-symbol isolation."""
    out: dict = {"symbols": {}, "signals": {}, "halted": False}
    raw = store.many(list(symbols), timeframe)
    for sym in symbols:
        data = raw[sym]
        if isinstance(data, Exception):
            out["symbols"][sym] = {"error": safe_msg(data)}
            continue
        rep = check(data, TF_MS.get(timeframe, 900_000), stale_s)
        if rep["stale"] or not rep["ok"]:
            out["symbols"][sym] = {"error": "; ".join(rep["issues"]), "quality": rep, "stale": rep["stale"]}
            continue
        try:
            feats = features(data)
            sigs = [s for s in (t.run(sym, feats) for t in ALL) if s is not None]
            agg = combine(sigs, weights)
            out["signals"][sym] = {"signals": [vars(s) for s in sigs], "aggregate": vars(agg)}
            out["symbols"][sym] = out["signals"][sym]
        except Exception as exc:  # noqa: BLE001
            out["symbols"][sym] = {"error": safe_msg(exc)}
    if ai is not None and getattr(ai, "available", False) and out["signals"]:
        res = ai.analyze({"signals": out["signals"]})
        out["ai"] = vars(res) if res else {"status": "unavailable; abstaining"}
    scores = [out["signals"].get(s, {}).get("aggregate", {}).get("score", 0.0) for s in symbols]
    top = max(scores, key=abs) if scores else 0.0
    alloc_w, alloc_n = {}, {}
    if top and equity > 0:
        direction = 1.0 if top > 0 else -1.0
        strength = min(1.0, abs(top))
        for s in symbols:
            w = max(-max_conc, min(max_conc, (strength / len(symbols)) * direction))
            alloc_w[s], alloc_n[s] = round(w, 4), round(w * equity, 2)
    out["allocation"] = {"weights": alloc_w, "notionals": alloc_n}
    if not out["signals"]:
        out["halted"], out["halt_reason"] = True, "No symbol produced fresh, valid signals."
    log("pipeline", symbols=list(symbols), halted=out["halted"],
        skipped=[s for s in symbols if s not in out["signals"]])
    return out


@dataclass
class AutoState:
    running: bool = False
    dry_run: bool = True
    minutes: int = 15
    last: str = ""
    result: str = ""
    next: str = ""
    runs: int = 0
    live_orders: int = 0
    dry_orders: int = 0


class Loop:
    """Automation loop with op throttle (max submits/min) + position awareness."""

    def __init__(self, settings, cycle_fn, risk: Risk, broker, account_fn,
                 db=None, max_submits_per_min: int = 10) -> None:
        self.s, self.cycle_fn, self.risk = settings, cycle_fn, risk
        self.broker, self.account_fn, self.db = broker, account_fn, db
        self.max_spm = max_submits_per_min
        self._submit_ts: list[float] = []
        self.st = AutoState(dry_run=settings.automate_dry_run, minutes=settings.automate_minutes)
        self._th: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending_take_profit: set[str] = set()

    def start(self) -> AutoState:
        with self._lock:
            if not self.st.running:
                reconciliation = self.reconcile()
                if not reconciliation.get("reconciled", False):
                    self.st.running = False
                    self._stop.set()
                    log("auto_start_blocked", level="error", reconciliation=reconciliation)
                    return self.st
                self.st.running = True
                self._stop.clear()
                self._th = threading.Thread(target=self._loop, daemon=True)
                self._th.start()
                log("auto_start", dry=self.st.dry_run, minutes=self.st.minutes)
            return self.st

    def stop(self) -> AutoState:
        with self._lock:
            self.st.running = False
            self._stop.set()
            log("auto_stop")
            return self.st

    def reconcile(self) -> dict:
        """Reconcile broker open orders with local order records.

        Never submits, replaces, or cancels orders. Reports incomplete
        reconciliation explicitly rather than falsely claiming success.
        """
        if self.broker is None:
            return {"reconciled": False, "status": "no_broker",
                    "matched": 0, "db_open": 0, "broker_open": 0,
                    "uncertain": 0, "errors": ["no broker configured"]}
        get_open_orders = getattr(self.broker, "get_open_orders", None)
        if get_open_orders is None:
            result = {"reconciled": False, "status": "incomplete",
                      "matched": 0, "db_open": 0, "broker_open": 0,
                      "uncertain": 0, "errors": ["broker open-order reconciliation unavailable"]}
            trip = getattr(self.risk, "trip", None)
            if callable(trip):
                trip("Order reconciliation incomplete; new submissions halted.")
            return result
        else:
            broker_orders, berr = get_open_orders()
            if broker_orders is None:
                broker_orders = []
        if self.db is not None:
            db_orders, derr = self.db.get_open_orders()
            if db_orders is None:
                db_orders = []
        else:
            db_orders, derr = [], None
        errors = []
        if berr:
            errors.append(berr)
        if derr:
            errors.append(derr)
        broker_by_cid = {}
        for o in broker_orders:
            cid = o.get("client_order_id")
            if cid:
                broker_by_cid[cid] = o
        matched = 0
        uncertain = 0
        db_ids = {o.get("client_order_id") for o in db_orders if o.get("client_order_id")}
        for o in db_orders:
            cid = o.get("client_order_id")
            if cid and cid in broker_by_cid:
                matched += 1
                if broker_by_cid[cid].get("status") in ("unknown", ""):
                    uncertain += 1
        if self.db is not None:
            uncertain += sum(1 for cid in broker_by_cid if cid not in db_ids)
        elif broker_by_cid or broker_orders:
            uncertain += len(broker_orders)
        status = "incomplete" if errors or uncertain else "complete"
        if status != "complete":
            trip = getattr(self.risk, "trip", None)
            if callable(trip):
                trip("Order reconciliation incomplete; new submissions halted.")
        return {
            "reconciled": status == "complete",
            "status": status,
            "matched": matched,
            "db_open": len(db_orders),
            "broker_open": len(broker_orders),
            "uncertain": uncertain,
            "errors": errors,
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.once()
            except Exception as exc:  # noqa: BLE001
                log("auto_failed", level="error", error=safe_msg(exc))
            self._stop.wait(self.st.minutes * 60)

    def _throttled(self) -> bool:
        now = time.time()
        self._submit_ts = [t for t in self._submit_ts if now - t < 60]
        if len(self._submit_ts) >= self.max_spm:
            return True
        self._submit_ts.append(now)
        return False

    def _submission_enabled(self) -> bool:
        """Return whether this loop may call a broker submission method."""
        return not self.st.dry_run and bool(getattr(self.s, "enable_paper_orders", False))

    def once(self) -> dict:
        if self.risk.killed:
            return self._done("halted: kill switch", {"action": "halted", "reason": "Kill switch engaged."})
        try:
            pipe = self.cycle_fn()
        except Exception as exc:  # noqa: BLE001
            return self._done("halted: pipeline failed", {"action": "halted", "reason": safe_msg(exc)})
        if pipe.get("halted"):
            return self._done("halted: " + pipe.get("halt_reason", "no signals"),
                              {"action": "halted", "reason": pipe.get("halt_reason", "no signals"),
                               "pipe": pipe})
        acct = self.account_fn()
        if acct is None:
            return self._done("halted: unknown account", {"action": "halted", "reason": "Account unknown."})
        results = []
        cap = float(getattr(self.s, "max_position_notional", 2000.0) or 2000.0)
        for sym in self.s.crypto_symbols:
            agg = (pipe.get("signals", {}).get(sym, {}).get("aggregate") or {})
            act, conf = agg.get("action"), float(agg.get("confidence", 0.0) or 0.0)
            if act not in ("BUY", "SELL") or conf < self.s.automate_min_conf:
                results.append({"symbol": sym, "skipped": f"{act} conf={conf:.2f}"})
                continue
            notionals = (pipe.get("allocation", {}).get("notionals") or {})
            notional = min(abs(float(notionals.get(sym, 0.0) or 0.0)), self.s.automate_max_notional)
            if notional <= 0:
                results.append({"symbol": sym, "skipped": "zero size"})
                continue
            held = float(acct.positions.get(sym, 0.0) or 0.0)
            if (act == "BUY" and held > 0.25 * cap) or (act == "SELL" and held < -0.25 * cap):
                results.append({"symbol": sym, "skipped": f"already positioned ({held:.2f})"})
                continue
            cid = f"auto-{uuid.uuid4().hex[:10]}"
            dec = self.risk.check(sym, act, notional, acct, cid=cid)
            if not dec.ok:
                log("auto_rejected", symbol=sym, reasons=dec.reasons)
                results.append({"symbol": sym, "rejected": dec.reasons})
                continue
            if not self._submission_enabled():
                log("auto_dry", symbol=sym, side=act, notional=notional)
                self.st.dry_orders += 1
                results.append({"symbol": sym, "dry": {"side": act, "notional": notional}})
                continue
            if self._throttled():
                results.append({"symbol": sym, "skipped": "submit throttle"})
                continue
            try:
                res = self.broker.send(sym, act, notional=notional, cid=cid)
            except Exception as exc:  # noqa: BLE001 - submission outcome is uncertain
                self.risk.trip("Order submission failed; reconciliation required.")
                log("auto_order_failed", level="error", symbol=sym, error=safe_msg(exc))
                if self.db is not None:
                    try:
                        self.db.save_order(sym, act, float(notional), "unknown", cid)
                    except Exception:  # noqa: BLE001 - persistence failure cannot change fail-closed state
                        pass
                self.st.live_orders += 1
                results.append({"symbol": sym, "status": "unknown", "client_order_id": cid,
                                "reconcile_required": True})
                continue
            log("auto_order", symbol=sym, side=act, status=res.status)
            if self.db is not None:
                try:
                    self.db.save_order(sym, act, float(notional), res.status, res.cid or "")
                except Exception:  # noqa: BLE001 - never crash the loop over persistence
                    pass
            if res.status not in ("filled", "partially_filled", "submitted", "accepted",
                                  "new", "pending_new", "held"):
                # submit failure or uncertain fill: reconcile, never assume fill
                self.st.live_orders += 1
                if res.status == "unknown":
                    self.risk.trip("Order outcome unknown; reconciliation required.")
                results.append({"symbol": sym, "order": vars(res),
                                "status": res.status, "reconcile": True})
                continue
            self.st.live_orders += 1
            acct.pending += 1
            results.append({"symbol": sym, "order": vars(res)})

        # Take-profit: close profitable positions
        tp = getattr(self.s, "take_profit_pct", 0.0)
        if tp and tp > 0:
            try:
                positions = self.broker.positions_detail()
                active_symbols = {str(p.get("symbol", "")) for p in positions}
                self._pending_take_profit.intersection_update(active_symbols)
                for p in positions:
                    up_pct = float(p.get("unrealized_pl_pct", 0.0) or 0.0)
                    if up_pct >= tp:
                        sym = p["symbol"]
                        if sym in self._pending_take_profit:
                            results.append({"symbol": sym, "skipped": "take-profit pending reconciliation"})
                            continue
                        try:
                            notional = abs(float(p.get("market_value", 0.0) or 0.0))
                        except (TypeError, ValueError):
                            results.append({"symbol": sym, "rejected": ["Invalid position market value."],
                                            "reason": "take_profit"})
                            continue
                        if notional > 0:
                            cid = f"tp-{uuid.uuid4().hex[:10]}"
                            position_side = str(p.get("side", "") or "").lower().strip()
                            if position_side not in ("long", "short"):
                                results.append({"symbol": sym, "rejected": ["Position side is unverified."],
                                                "reason": "take_profit"})
                                continue
                            exit_side = "BUY" if position_side == "short" else "SELL"
                            raw_qty = p.get("qty")
                            quantity = None
                            if raw_qty is not None:
                                try:
                                    quantity = float(raw_qty)
                                except (TypeError, ValueError):
                                    quantity = float("nan")
                            decision = self.risk.check_exit(
                                sym, exit_side, notional, acct, cid=cid,
                                position_side=position_side, quantity=quantity,
                                position_quantity=abs(quantity) if quantity is not None else None,
                            )
                            if not decision.ok:
                                results.append({"symbol": sym, "rejected": decision.reasons,
                                                "reason": "take_profit"})
                                continue
                            if not self._submission_enabled():
                                log("auto_dry", symbol=sym, side=exit_side,
                                    notional=notional, reason="take_profit")
                                self.st.dry_orders += 1
                                results.append({"symbol": sym, "dry": {"side": exit_side,
                                                                          "notional": notional,
                                                                          "reason": "take_profit"}})
                                continue
                            self._pending_take_profit.add(sym)
                            send_kwargs = {"cid": cid}
                            qty = float(p.get("qty", 0.0) or 0.0)
                            if qty > 0:
                                send_kwargs["qty"] = qty
                            else:
                                send_kwargs["notional"] = notional
                            try:
                                res = self.broker.send(sym, exit_side, **send_kwargs)
                            except Exception as exc:  # noqa: BLE001 - exit outcome is uncertain
                                self.risk.trip("Take-profit submission failed; reconciliation required.")
                                log("take_profit_failed", level="error", symbol=sym, error=safe_msg(exc))
                                if self.db is not None:
                                    try:
                                        self.db.save_order(sym, exit_side, float(notional), "unknown", cid)
                                    except Exception:  # noqa: BLE001
                                        pass
                                self.st.live_orders += 1
                                results.append({"symbol": sym, "status": "unknown", "client_order_id": cid,
                                                "reconcile_required": True, "reason": "take_profit"})
                                continue
                            log("tp_sell", symbol=sym, side=exit_side, notional=notional,
                                reason="take_profit", up_pct=up_pct, status=res.status)
                            if self.db is not None:
                                try:
                                    self.db.save_order(sym, exit_side, float(notional),
                                                       res.status, res.cid or "")
                                except Exception:
                                    pass
                            if res.status not in ("filled", "partially_filled",
                                                  "submitted", "accepted",
                                                  "new", "pending_new", "held"):
                                self.st.live_orders += 1
                                if res.status == "unknown":
                                    self.risk.trip("Take-profit outcome unknown; reconciliation required.")
                                elif res.status in ("rejected", "canceled", "expired", "done_for_day"):
                                    self._pending_take_profit.discard(sym)
                                results.append({"symbol": sym, "order": vars(res),
                                              "status": res.status, "reconcile": True})
                                continue
                            self.st.live_orders += 1
                            results.append({"symbol": sym, "order": vars(res)})
            except Exception as exc:  # noqa: BLE001 - fail closed without killing the loop thread
                log("take_profit_failed", level="error", error=safe_msg(exc))

        return self._done(f"cycled {len(results)}", {"action": "cycled", "results": results})

    def _done(self, summary: str, out: dict) -> dict:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        self.st.last, self.st.result, self.st.runs = now.isoformat(), summary, self.st.runs + 1
        if self.st.running:
            self.st.next = (now + datetime.timedelta(minutes=self.st.minutes)).isoformat()
        out["status"] = vars(self.st)
        return out
