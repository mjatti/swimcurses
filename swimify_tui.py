# swimify_tui.py
import asyncio, json, argparse
from datetime import datetime, UTC
from pathlib import Path
import requests, websockets
from requests.exceptions import ReadTimeout, ConnectionError
from textual.app import App, ComposeResult
from textual.widgets import Static, OptionList
from textual.widgets.option_list import Option
from textual.reactive import reactive
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

HTTP_URL = "https://data-eu.swimify.com/v1/graphql"
WS_URL   = "wss://data-eu.swimify.com/v1/graphql"
HEADERS  = {
    "Content-Type": "application/json",
    "Origin": "https://live.swimify.com",
    "x-hasura-public-secret-key": "GkK9Mjfnxq44Zjs4QamHeduqy6ABM2PLvEmkLJuGNeMShdRMhZSPW6adj78qF6BB",
}
NOW = datetime.now(UTC).isoformat(timespec="milliseconds")

# ---------- GraphQL ----------
Q_GET_TIME = """
query GetCurrentTime_WL {
  get_current_time { time }
}
"""

Q_COMPS = """
query filteredCompetitionsThisWeekQuery_WL($_start: date = "", $_end: date = "", $nation: String)
@cached(ttl: 60) {
  competitions(
    order_by: [{manual_live_sort_order: desc_nulls_last}, {name: asc}],
    where: {endDate: {_gte: $_end}, startDate: {_lte: $_start}, nation_code: {_eq: $nation}}
  ) { id name city pool_name startDate endDate }
}
"""

Q_EVENTS_BY_COMP = """
query eventsByCompetition($comp: uuid!) {
  time_program_entry(
    where: {competition_id: {_eq: $comp}},
    order_by: {sort_order: asc}
  ) { id oid name start_time }
}
"""

Q_HEATS_FOR_EVENT = """
query heatsQuery_WL($oid: Int!, $competitionId: uuid = "") {
  time_program_entry(where:{competition_id:{_eq:$competitionId}, oid:{_eq:$oid}}) {
    id
    heats(order_by:{number:asc}) {
      id name number status estimated_start_time start_time
      lanes(order_by:{number:asc}) {
        id number heat_rank result_text
        competitor { full_name club { short_name } }
        sub_results(order_by:{order:asc}) { done_at order result_value_text }
      }
    }
  }
}
"""

Q_HEAT_DETAIL = """
query heatByIdQuery_WL($id: Int!) {
  heat_by_pk(id: $id) {
    id name number status
    lanes(order_by:{number:asc}) {
      number
      result_text
      competitor { full_name club { short_name } }
      sub_results(order_by:{order:asc}) { done_at result_value_text }
    }
  }
}
"""

SUB_CURRENT = """
subscription heatWatcher($comp: uuid!) {
  current_heat(where:{competition_id:{_eq:$comp}}) {
    heat_type
    heat {
      id name status
      lanes {
        number
        result_text
        competitor { full_name club { short_name } }
        sub_results(order_by:{order:asc}) { done_at result_value_text }
      }
    }
  }
}
"""

# ---------- CLI ----------
def parse_args():
    ap = argparse.ArgumentParser(description="Swimify TUI")
    ap.add_argument("--api-log", help="Kirjoita GraphQL API -logit JSONL-muodossa tähän tiedostoon")
    ap.add_argument("--verbose", action="store_true", help="Näytä viimeisin GraphQL-viesti tiivistettynä ruudulla")
    ap.add_argument("--nation", default="FIN", help="Maa-suodatin, oletus FIN")
    return ap.parse_args()

# ---------- HTTP ----------
def http_post(payload, api_logger=None, timeout=10, retries=2):
    if api_logger:
        api_logger("http_request", {"url": HTTP_URL, "payload": payload})
    last_err = None
    for _ in range(max(1, retries + 1)):
        try:
            r = requests.post(HTTP_URL, headers=HEADERS, json=payload, timeout=timeout)
            try:
                body = r.json()
            except Exception:
                body = {"raw": r.text}
            if api_logger:
                api_logger("http_response", {"status": r.status_code, "body": body})
            r.raise_for_status()
            return body
        except (ReadTimeout, ConnectionError) as e:
            last_err = e
            continue
    raise last_err

def fetch_server_now(api_logger=None):
    payload = {"operationName": "GetCurrentTime_WL", "query": Q_GET_TIME, "variables": {}}
    try:
        resp = http_post(payload, api_logger, timeout=5, retries=1)
        data = (resp.get("data", {}) or {}).get("get_current_time") or []
        if isinstance(data, list) and data:
            s = data[0].get("time", "")
        elif isinstance(data, dict):
            s = data.get("time", "")
        else:
            s = ""
        if not s:
            return datetime.now(UTC)
        if "Z" in s:
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.now(UTC)

def fetch_current_comps(nation, api_logger=None):
    payload = {
        "operationName": "filteredCompetitionsThisWeekQuery_WL",
        "query": Q_COMPS,
        "variables": {"_start": NOW, "_end": NOW, "nation": nation},
    }
    return http_post(payload, api_logger).get("data", {}).get("competitions", [])

def fetch_events_by_comp(comp_id, api_logger=None):
    payload = {"operationName": "eventsByCompetition", "query": Q_EVENTS_BY_COMP, "variables": {"comp": comp_id}}
    return http_post(payload, api_logger).get("data", {}).get("time_program_entry", [])

def fetch_heats_for_event(comp_id, event_oid, api_logger=None):
    payload = {
        "operationName": "heatsQuery_WL",
        "query": Q_HEATS_FOR_EVENT,
        "variables": {"competitionId": comp_id, "oid": int(event_oid)},
    }
    tpe = http_post(payload, api_logger).get("data", {}).get("time_program_entry") or []
    return (tpe[0]["heats"] if tpe else [])

def fetch_heat_detail(heat_id, api_logger=None):
    payload = {"operationName": "heatByIdQuery_WL", "query": Q_HEAT_DETAIL, "variables": {"id": int(heat_id)}}
    return http_post(payload, api_logger).get("data", {}).get("heat_by_pk")

# ---------- Utils ----------
def lane_signature(lane: dict) -> tuple:
    splits = lane.get("sub_results") or []
    return (
        lane.get("result_text") or "",
        len(splits),
        tuple(sr.get("result_value_text") for sr in splits),
    )

# ---------- Widgets ----------
class Scoreboard(Static):
    data = reactive({})
    changed_lanes = reactive(set)

    def render(self):
        if not self.data:
            return Panel("Ei dataa", title="Scoreboard")
        t = Table(title=f"Event: {self.data['name']}  |  Heat #{self.data.get('number')}")
        t.add_column("Rata"); t.add_column("Uimari"); t.add_column("Seura")
        t.add_column("Aika"); t.add_column("Väliajat"); t.add_column("Sija")
        for lane in sorted(self.data.get("lanes", []), key=lambda l: l["number"]):
            c = lane.get("competitor")
            name = c["full_name"] if c else "-"
            club = c["club"]["short_name"] if c and c.get("club") else ""
            result = lane.get("result_text") or ""
            splits = " ".join(
                f"{sr['done_at']}m:{sr['result_value_text'] or '-'}"
                for sr in (lane.get("sub_results") or [])
            )
            style = "bright_green" if lane["number"] in self.changed_lanes else None
            rank = lane.get("heat_rank")
            medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else ""
            rank_cell = f"{rank or ''} {medal}".strip()
            t.add_row(
                str(lane["number"]),
                Text(name, style=style),
                Text(club, style=style),
                Text(result, style=style),
                Text(splits, style=style),
                Text(rank_cell, style=style),
            )
        return Panel(t, title="Nykyinen erä")

# ---------- App ----------
class SwimifyTUI(App):
    CSS = "Screen { layout: vertical; } #comps, #heats, #verbose { height: 3; content-align: center middle; }"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("h", "toggle_events", "Events"),
        ("H", "toggle_events", "Events"),
        ("c", "go_current", "Current"),
        ("C", "go_current", "Current"),
    ]

    def __init__(self, api_log_path: str | None, verbose: bool, nation: str):
        super().__init__()
        self.api_log_path = Path(api_log_path) if api_log_path else None
        self.verbose_enabled = verbose
        self.nation = nation
        self.comps = []
        self.comp_idx = 0
        self.ws_worker = None

        self.events_menu: OptionList | None = None
        self.heats_menu: OptionList | None = None
        self._opt_to_event_oid = {}
        self._opt_to_heat_id = {}

        self.selected_heat_id: int | None = None
        self._last_lane_signatures: dict[int, tuple] = {}
        self._latest_ws_heat: dict | None = None

    # ----- logging -----
    def api_logger(self, event: str, obj: dict):
        if not self.api_log_path:
            return
        rec = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "event": event, "data": obj}
        self.api_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.api_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ----- websocket -----
    async def watch_current_competition(self):
        cid = self.comps[self.comp_idx]["id"]
        while True:
            try:
                async with websockets.connect(
                    WS_URL,
                    additional_headers=list(HEADERS.items()),
                    subprotocols=["graphql-transport-ws"],
                ) as ws:
                    await ws.send(json.dumps({"type": "connection_init", "payload": {}}))
                    await ws.send(
                        json.dumps(
                            {"id": "1", "type": "subscribe", "payload": {"query": SUB_CURRENT, "variables": {"comp": cid}}}
                        )
                    )
                    while True:
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        self.api_logger("ws_recv", msg)
                        if self.verbose_enabled:
                            self.query_one("#verbose", Static).update(self._summarize_msg(msg))
                        if msg.get("type") != "next":
                            continue
                        items = msg["payload"]["data"].get("current_heat") or []
                        heats = [it["heat"] for it in items if it.get("heat")]
                        if not heats:
                            continue
                        self._latest_ws_heat = heats[0]
                        if self.selected_heat_id is None:
                            self._apply_heat(self._latest_ws_heat)
            except Exception as e:
                self.api_logger("ws_error", {"error": str(e)})
                if self.verbose_enabled:
                    self.query_one("#verbose", Static).update(Text(f"WS virhe: {e}", style="red"))
                await asyncio.sleep(1)

    # ----- compose -----
    def compose(self) -> ComposeResult:
        yield Static(id="comps")
        yield Static("Prev | Current | Next | [H] Events→Heats  |  [C] Current", id="heats")
        if self.verbose_enabled:
            yield Static("Verbose: odotetaan dataa…", id="verbose")
        yield Scoreboard()

    def on_mount(self):
        self.comps = fetch_current_comps(self.nation, api_logger=self.api_logger)
        self._refresh_comps()
        if self.comps:
            self.ws_worker = self.run_worker(self.watch_current_competition(), exclusive=True)

    # ----- helpers -----
    def _refresh_comps(self):
        if not self.comps:
            self.query_one("#comps", Static).update("Ei kilpailuja")
            return
        parts = [(f"[bold]{c['name']}[/bold]" if i == self.comp_idx else c["name"]) for i, c in enumerate(self.comps)]
        self.query_one("#comps", Static).update("  |  ".join(parts))

    def _summarize_msg(self, msg: dict) -> Text:
        try:
            if "errors" in msg:
                return Text("GraphQL virhe: " + json.dumps(msg["errors"]), style="bold red")
            items = msg.get("payload", {}).get("data", {}).get("current_heat") or []
            heats = [it["heat"] for it in items if it.get("heat")]
            if not heats:
                return Text("Ei heat-päivityksiä", style="yellow")
            h = heats[0]
            lanes = h.get("lanes") or []
            filled = sum(1 for ln in lanes if ln.get("competitor"))
            finish = sum(1 for ln in lanes if (ln.get("result_text") or "").strip())
            splits = sum(len(ln.get("sub_results") or []) for ln in lanes)
            return Text(
                f"Heat {h.get('id')} '{h.get('name')}' status={h.get('status')} lanes={len(lanes)} "
                f"swimmers={filled} finishes={finish} splits={splits}",
                style="yellow",
            )
        except Exception as e:
            return Text(f"Yhteenveto epäonnistui: {e}", style="red")

    def _apply_heat(self, heat: dict):
        new_sigs = {ln["number"]: lane_signature(ln) for ln in heat.get("lanes") or []}
        changed = {num for num, sig in new_sigs.items() if self._last_lane_signatures.get(num) != sig}
        self._last_lane_signatures = new_sigs
        sb = self.query_one(Scoreboard)
        sb.changed_lanes = changed
        sb.data = heat

    # ----- actions -----
    def action_quit(self):
        self.exit()

    def action_toggle_events(self):
        if self.heats_menu:
            self.heats_menu.remove()
            self.heats_menu = None
        if self.events_menu:
            self.events_menu.remove()
            self.events_menu = None
            return

        if not self.comps:
            return
        comp_id = self.comps[self.comp_idx]["id"]
        events = fetch_events_by_comp(comp_id, api_logger=self.api_logger)
        server_now = fetch_server_now(api_logger=self.api_logger)
        if not events:
            self.query_one("#heats", Static).update("Events→Heats — ei eventtejä")
            return

        ol = OptionList()
        ol.styles.height = 15
        ol.border_title = "Events (↑/↓, Enter, H sulkee)"
        ol.id = "events_menu"
        self._opt_to_event_oid = {}

        for ev in events:
            ts = ev.get("start_time")
            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
            hhmm = ts_dt.astimezone(UTC).strftime("%H:%M") if ts_dt else ""
            delta = ""
            if ts_dt:
                diff = ts_dt - server_now
                total_min = int(diff.total_seconds() // 60)
                sign = "-" if total_min < 0 else "+"
                h, m = divmod(abs(total_min), 60)
                delta = f"{sign}{h:02d}:{m:02d}"
            label = f"{hhmm:>5}  ({delta})  |  {ev['name']}  (oid {ev['oid']})"
            opt = Option(label)
            self._opt_to_event_oid[id(opt)] = ev["oid"]
            ol.add_option(opt)

        self.mount(ol, after=self.query_one("#heats", Static))
        self.events_menu = ol
        ol.focus()

    def action_go_current(self):
        self.selected_heat_id = None
        if self._latest_ws_heat:
            self._apply_heat(self._latest_ws_heat)

    # ----- selections -----
    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        if self.events_menu and event.option in self.events_menu.options:
            comp_id = self.comps[self.comp_idx]["id"]
            event_oid = self._opt_to_event_oid.get(id(event.option))
            heats = fetch_heats_for_event(comp_id, event_oid, api_logger=self.api_logger)
            self.events_menu.remove()
            self.events_menu = None

            if not heats:
                self.query_one("#heats", Static).update("Heats — ei heateja")
                return

            ol = OptionList()
            ol.styles.height = 15
            ol.border_title = f"Heats for oid {event_oid} (↑/↓, Enter)"
            ol.id = "heats_menu"
            self._opt_to_heat_id = {}
            for h in heats:
                label = f"{h['number']:02d}  |  {h['name']}  (id {h['id']}, status {h.get('status')})"
                opt = Option(label)
                self._opt_to_heat_id[id(opt)] = h["id"]
                ol.add_option(opt)
            self.mount(ol, after=self.query_one("#heats", Static))
            self.heats_menu = ol
            ol.focus()
            return

        if self.heats_menu and event.option in self.heats_menu.options:
            heat_id = self._opt_to_heat_id.get(id(event.option))
            if heat_id is not None:
                detail = fetch_heat_detail(heat_id, api_logger=self.api_logger)
                if detail:
                    self.selected_heat_id = heat_id
                    self._apply_heat(detail)
            self.heats_menu.remove()
            self.heats_menu = None

    # ----- arrows for competitions -----
    def on_key(self, event):
        if self.events_menu or self.heats_menu:
            return
        if event.key == "right" and self.comps:
            self.comp_idx = (self.comp_idx + 1) % len(self.comps)
            self.selected_heat_id = None
            self._latest_ws_heat = None
            self._last_lane_signatures.clear()
            self._refresh_comps()
            if self.ws_worker:
                self.ws_worker.cancel()
            self.ws_worker = self.run_worker(self.watch_current_competition(), exclusive=True)
        elif event.key == "left" and self.comps:
            self.comp_idx = (self.comp_idx - 1) % len(self.comps)
            self.selected_heat_id = None
            self._latest_ws_heat = None
            self._last_lane_signatures.clear()
            self._refresh_comps()
            if self.ws_worker:
                self.ws_worker.cancel()
            self.ws_worker = self.run_worker(self.watch_current_competition(), exclusive=True)

# ---------- main ----------
if __name__ == "__main__":
    args = parse_args()
    SwimifyTUI(api_log_path=args.api_log, verbose=args.verbose, nation=args.nation).run()