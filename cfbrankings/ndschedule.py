import time
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

from plugins.base_plugin.base_plugin import BasePlugin
from utils.http_client import get_http_session

logger = logging.getLogger(__name__)

ND_TEAM_ID = 87
TEAM_URL = f"https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams/{ND_TEAM_ID}"
TEAM_DETAIL_URL_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams/"
RANKINGS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings"
LEAGUE_CORE_URL = "https://sports.core.api.espn.com/v2/sports/football/leagues/college-football?lang=en&region=us"
ND_LOGO_URL = "https://a.espncdn.com/i/teamlogos/ncaa/500/87.png"


def _ensure_icon_file():
    try:
        here = Path(__file__).resolve().parent
        icon_path = here / "icon.png"
        if icon_path.exists() and icon_path.stat().st_size > 2000:
            return
        session = get_http_session()
        resp = session.get(ND_LOGO_URL, timeout=15)
        if resp.status_code == 200 and resp.content and len(resp.content) > 2000:
            icon_path.write_bytes(resp.content)
    except Exception:
        return


_ensure_icon_file()


class NdSchedule(BasePlugin):
    """Notre Dame Football schedule (Large-only simplified).
    - Large layout only (largest font); no mode or font-size presets in UI
    - Configurable toggles remain: show_time, show_rank, hide_rank, hide_nickname, hide_logo
    """

    _cache: Dict[str, Any] = {"ts": {}, "data": {}}

    def generate_settings_template(self):
        params = super().generate_settings_template()
        # No style controls on the UI (we removed presets)
        params["style_settings"] = False
        return params

    def generate_image(self, settings: Dict[str, Any], device_config):
        # User-controlled toggles (with sane defaults)
        show_time = self._to_bool(settings.get("show_time", True))
        show_rank_setting = self._to_bool(settings.get("show_rank", True))
        hide_rank = self._to_bool(settings.get("hide_rank", False))
        hide_nickname = self._to_bool(settings.get("hide_nickname", False))
        hide_logo = self._to_bool(settings.get("hide_logo", False))

        cache_minutes = max(0, min(1440, int(settings.get("cache_minutes") or 30)))
        ttl = cache_minutes * 60

        # Resolve canvas from device config (no target-display override)
        dims = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dims = dims[::-1]

        # Season year
        current_year = self._detect_current_season_year(ttl)
        selected = settings.get("season_year")
        try:
            season_year = int(str(selected)) if str(selected).strip() else current_year
        except Exception:
            season_year = current_year

        # Fetch data
        sched = self._fetch_schedule_for_year(ND_TEAM_ID, season_year, ttl)
        nd_logo = "" if hide_logo else self._fetch_team_logo(ttl)

        effective_show_rank = bool(show_rank_setting and season_year == current_year and not hide_rank)
        rank_map: Dict[str, int] = {}
        rank_label = ""
        rank_updated = ""
        if effective_show_rank:
            rank_map, rank_label, rank_updated = self._get_rank_map(ttl)

        rows = self._build_rows(
            sched, rank_map, effective_show_rank, season_year, ttl,
            show_time=show_time, hide_logo=hide_logo
        )

        if effective_show_rank and rank_label:
            update_line = (
                f"Updated {rank_updated} • Rank source: {rank_label}" if rank_updated else f"Rank source: {rank_label}"
            )
        else:
            sched_updated = self._format_updated(sched)
            update_line = f"Updated {sched_updated}" if sched_updated else f"Season {season_year}"

        template_params = {
            "title": f"Notre Dame Football Schedule for {season_year}",
            "nd_logo": nd_logo,
            "update_line": update_line,
            "rows": rows,
            "show_time": bool(show_time),
            "hide_rank": bool(hide_rank),
            "hide_nickname": bool(hide_nickname),
            "hide_logo": bool(hide_logo),
            "plugin_settings": settings,
        }
        return self.render_image(dims, "ndschedule.html", "ndschedule.css", template_params)

    # ----------------------------
    # HTTP + caching
    # ----------------------------
    def _fetch_json_cached(self, url: str, ttl: int) -> Dict[str, Any]:
        now = time.time()
        ts = self._cache["ts"].get(url, 0.0)
        if ttl > 0 and url in self._cache["data"] and (now - ts) < ttl:
            return self._cache["data"][url]
        session = get_http_session()
        resp = session.get(url, timeout=25)
        resp.raise_for_status()
        data = resp.json()
        if ttl > 0:
            self._cache["ts"][url] = now
            self._cache["data"][url] = data
        return data

    def _detect_current_season_year(self, ttl: int) -> int:
        from datetime import datetime
        year_guess = datetime.now().year
        try:
            core = self._fetch_json_cached(LEAGUE_CORE_URL, ttl)
            season = core.get("season")
            if isinstance(season, dict) and season.get("year"):
                return int(season.get("year"))
        except Exception:
            pass
        return year_guess

    def _fetch_schedule_for_year(self, team_id: int, year: int, ttl: int) -> Dict[str, Any]:
        base = f"{TEAM_DETAIL_URL_BASE}{team_id}/schedule"
        candidates = [
            f"{base}?season={year}",
            f"{base}?year={year}",
            f"{base}?season={year}&seasontype=2",
            f"{base}?season={year}&seasontype=3",
            f"{base}?year={year}&seasontype=2",
            f"{base}?year={year}&seasontype=3",
            base,
        ]
        last = None
        for url in candidates:
            try:
                data = self._fetch_json_cached(url, ttl)
                last = data
                if isinstance(data.get("events"), list) and data.get("events"):
                    return data
            except Exception:
                continue
        return last or {"events": []}

    def _fetch_team_logo(self, ttl: int) -> str:
        try:
            data = self._fetch_json_cached(TEAM_URL, ttl)
            team = data.get("team") if isinstance(data.get("team"), dict) else data
            logos = (team.get("logos") or []) if isinstance(team, dict) else []
            for item in logos:
                if isinstance(item, dict) and item.get("href"):
                    return item["href"]
        except Exception:
            pass
        return ND_LOGO_URL

    def _get_team_meta(self, team_id: int, ttl: int) -> Dict[str, Any]:
        try:
            url = f"{TEAM_DETAIL_URL_BASE}{team_id}"
            data = self._fetch_json_cached(url, ttl)
            team = data.get("team") if isinstance(data.get("team"), dict) else data
            return team if isinstance(team, dict) else {}
        except Exception:
            return {}

    # ----------------------------
    # Utilities
    # ----------------------------
    def _safe_int(self, v: Any) -> Optional[int]:
        try:
            if v is None:
                return None
            if isinstance(v, (int, float)):
                return int(v)
            if isinstance(v, dict):
                for k in ("value", "displayValue", "score"):
                    if v.get(k) is not None:
                        return self._safe_int(v.get(k))
                return None
            if isinstance(v, list) and v:
                return self._safe_int(v[0])
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    return None
                if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
                    return int(s)
                return int(float(s))
        except Exception:
            return None
        return None

    def _parse_iso(self, iso_str: str):
        from datetime import datetime
        if not iso_str:
            return None
        try:
            return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        except Exception:
            return None

    def _is_finalish(self, comp: Dict[str, Any]) -> bool:
        if not isinstance(comp, dict):
            return False
        st = (comp.get("status") or {}).get("type") or {}
        if isinstance(st, dict):
            if st.get("completed") is True:
                return True
            if str(st.get("state") or "").lower() == "post":
                return True
            for k in ("name", "detail", "shortDetail", "description"):
                val = str(st.get(k) or "")
                if val.upper().startswith("FINAL") or val.upper().startswith("STATUS_FINAL"):
                    return True
        return str(comp.get("status") or "").lower() in ("final", "post")

    def _eastern_tz(self):
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo("America/New_York")
        except Exception:
            return None

    def _choose_school(self, team: Dict[str, Any], meta: Dict[str, Any]) -> str:
        for c in (
            team.get("shortDisplayName"), team.get("location"), team.get("displayName"), team.get("abbreviation"),
            meta.get("shortDisplayName"), meta.get("location"), meta.get("displayName"), meta.get("abbreviation"),
            team.get("name"), meta.get("name"),
        ):
            if isinstance(c, str) and c.strip():
                return c.strip()
        return "Unknown"

    def _nickname_v22(self, team: Dict[str, Any], school: str) -> str:
        nickname = (team.get("name") or team.get("nickname") or "").strip()
        if nickname and nickname.lower() not in str(school).lower():
            return nickname
        return ""

    def _opponent_pregame_record(self, opp_team_id: int, season_year: int, game_dt_utc, ttl: int) -> str:
        # Optional feature; safe to return empty if any issue
        if not opp_team_id or not game_dt_utc:
            return ""
        opp_sched = self._fetch_schedule_for_year(int(opp_team_id), season_year, ttl)
        events = opp_sched.get("events") or []
        if not isinstance(events, list):
            return ""
        wins = losses = ties = 0
        for ev in events:
            if not isinstance(ev, dict):
                continue
            ev_dt = self._parse_iso(str(ev.get("date") or ""))
            if not ev_dt or ev_dt >= game_dt_utc:
                continue
            comps = ev.get("competitions")
            comp = comps[0] if isinstance(comps, list) and comps else ev
            if not isinstance(comp, dict):
                continue
            competitors = comp.get("competitors") or []
            if not isinstance(competitors, list) or len(competitors) < 2:
                continue
            my_side = other_side = None
            for c in competitors:
                if not isinstance(c, dict):
                    continue
                team = c.get("team") or {}
                if str(team.get("id")) == str(opp_team_id):
                    my_side = c
                else:
                    other_side = c
            if not my_side or not other_side:
                continue
            my_score = self._safe_int(my_side.get("score"))
            other_score = self._safe_int(other_side.get("score"))
            winner_flag = my_side.get("winner")
            if my_score is None or other_score is None:
                if isinstance(winner_flag, bool):
                    wins += 1 if winner_flag else 0
                    losses += 0 if winner_flag else 1
                continue
            if isinstance(winner_flag, bool):
                wins += 1 if winner_flag else 0
                losses += 0 if winner_flag else 1
            else:
                if my_score > other_score:
                    wins += 1
                elif my_score < other_score:
                    losses += 1
                else:
                    ties += 1
        return f"{wins}-{losses}-{ties}" if ties else f"{wins}-{losses}"

    def _get_rank_map(self, ttl: int) -> Tuple[Dict[str, int], str, str]:
        data = self._fetch_json_cached(RANKINGS_URL, ttl)
        polls = data.get("rankings")
        if isinstance(polls, dict):
            polls = polls.get("items") or polls.get("rankings")
        if not isinstance(polls, list):
            return {}, "", ""

        def norm(x: Any) -> str:
            return str(x or "").strip().lower()

        def poll_iso(p: Dict[str, Any]) -> str:
            for k in ("date", "lastUpdated", "lastUpdate", "updated", "updateDate"):
                if p.get(k):
                    return str(p.get(k))
            return ""

        def poll_epoch(p: Dict[str, Any]) -> float:
            import datetime
            iso = poll_iso(p)
            if not iso:
                return 0.0
            try:
                dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                return dt.timestamp()
            except Exception:
                return 0.0

        def is_cfp(p: Dict[str, Any]) -> bool:
            n = norm(p.get("name"))
            return "playoff selection committee" in n or "cfp" in norm(p.get("shortName"))

        def is_ap(p: Dict[str, Any]) -> bool:
            t = norm(p.get("type"))
            if t == "ap":
                return True
            n = norm(p.get("name"))
            s = norm(p.get("shortName"))
            return ("ap" in s) or ("ap top" in n)

        cfp = [p for p in polls if isinstance(p, dict) and is_cfp(p)]
        ap = [p for p in polls if isinstance(p, dict) and is_ap(p)]
        cfp.sort(key=poll_epoch, reverse=True)
        ap.sort(key=poll_epoch, reverse=True)
        poll = cfp[0] if cfp else (ap[0] if ap else None)
        if not poll:
            return {}, "", ""

        label = (poll.get("shortName") or poll.get("name") or "").strip()
        updated_fmt = self._format_iso_datetime(poll_iso(poll))

        ranks = poll.get("ranks")
        if isinstance(ranks, dict):
            ranks = ranks.get("items") or ranks.get("entries") or ranks.get("ranks")
        if not isinstance(ranks, list):
            ranks = poll.get("entries") or []
        if not isinstance(ranks, list):
            ranks = []

        rank_map: Dict[str, int] = {}
        for r in ranks:
            if not isinstance(r, dict):
                continue
            rk = r.get("current") or r.get("rank") or r.get("position")
            team = r.get("team") or {}
            tid = team.get("id")
            try:
                if tid is not None and rk is not None:
                    rank_map[str(tid)] = int(rk)
            except Exception:
                pass
        rank_map = {k: v for k, v in rank_map.items() if 1 <= v <= 25}
        return rank_map, label, updated_fmt

    def _build_rows(self, sched: Dict[str, Any], rank_map: Dict[str, int], show_rank: bool,
                    season_year: int, ttl: int, show_time: bool = True, hide_logo: bool = False) -> List[Dict[str, Any]]:
        events = sched.get("events") or []
        if not isinstance(events, list):
            events = []
        rows: List[Dict[str, Any]] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            iso_date = str(ev.get("date") or "")
            game_dt = self._parse_iso(iso_date)
            comps = ev.get("competitions")
            comp = comps[0] if isinstance(comps, list) and comps else ev

            date_disp = self._format_game_datetime(iso_date, show_time=show_time)

            competitors = (comp.get("competitors") or []) if isinstance(comp, dict) else []
            if not isinstance(competitors, list):
                competitors = []
            nd_side = opp_side = None
            for c in competitors:
                if not isinstance(c, dict):
                    continue
                team = c.get("team") or {}
                if str(team.get("id")) == str(ND_TEAM_ID):
                    nd_side = c
                else:
                    opp_side = c
            if not nd_side or not opp_side:
                continue

            opp_team = opp_side.get("team") or {}
            opp_id = str(opp_team.get("id") or "")
            opp_meta = self._get_team_meta(int(opp_id), ttl) if opp_id.isdigit() else {}
            school = self._choose_school(opp_team, opp_meta)
            nickname = self._nickname_v22(opp_meta if opp_meta else opp_team, school)

            logo = ""
            if not hide_logo:
                logos = opp_team.get("logos")
                if isinstance(logos, list):
                    for item in logos:
                        if isinstance(item, dict) and item.get("href"):
                            logo = item["href"]
                            break
                if not logo:
                    logo = str(opp_team.get("logo") or "")

            rk = rank_map.get(opp_id) if show_rank else None
            opp_record = self._opponent_pregame_record(int(opp_id), season_year, game_dt, ttl) if (opp_id.isdigit() and game_dt) else ""

            ha = str(nd_side.get("homeAway") or "").lower()
            neutral = bool(comp.get("neutralSite")) if isinstance(comp, dict) else False
            site = "Neutral" if neutral else ("Home" if ha == "home" else ("Away" if ha == "away" else ""))

            nd_score = self._safe_int(nd_side.get("score"))
            opp_score = self._safe_int(opp_side.get("score"))
            has_winner_flag = isinstance(nd_side.get("winner"), bool) or isinstance(opp_side.get("winner"), bool)
            result = ""
            result_class = ""
            if nd_score is not None and opp_score is not None and (self._is_finalish(comp) or has_winner_flag):
                if nd_score > opp_score:
                    result = f"W {nd_score}-{opp_score}"
                    result_class = "win"
                elif nd_score < opp_score:
                    result = f"L {nd_score}-{opp_score}"
                    result_class = "lose"
                else:
                    result = f"T {nd_score}-{opp_score}"
                    result_class = "tie"

            rows.append({
                "date": date_disp,
                "site": site,
                "opp_rank": rk,
                "logo": logo,
                "opp_school": school,
                "opp_nickname": nickname,
                "opp_record": opp_record,
                "result": result,
                "result_class": result_class,
            })
        return rows

    def _format_game_datetime(self, iso_str: str, show_time: bool = True) -> str:
        from datetime import datetime, timezone
        if not iso_str:
            return "TBD"
        tzinfo = self._eastern_tz()
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_local = dt.astimezone(tzinfo) if tzinfo else dt.astimezone()
            date_part = dt_local.strftime("%b %d")
            if not show_time:
                return date_part
            hour = dt_local.strftime("%I").lstrip("0") or "12"
            minute = dt_local.strftime("%M")
            ampm = dt_local.strftime("%p")
            return f"{date_part} / {hour}:{minute} {ampm}"
        except Exception:
            return iso_str[:10]

    def _format_iso_datetime(self, iso_str: str) -> str:
        from datetime import datetime, timezone
        if not iso_str:
            return ""
        tzinfo = self._eastern_tz()
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_local = dt.astimezone(tzinfo) if tzinfo else dt.astimezone()
            date_part = dt_local.strftime("%b %d, %Y")
            hour = dt_local.strftime("%I").lstrip("0") or "12"
            minute = dt_local.strftime("%M")
            ampm = dt_local.strftime("%p")
            return f"{date_part} {hour}:{minute} {ampm}"
        except Exception:
            return iso_str

    def _format_updated(self, data: Dict[str, Any]) -> str:
        date_str = None
        for k in ("timestamp", "lastUpdated", "date", "updateDate"):
            v = data.get(k)
            if v:
                date_str = str(v)
                break
        if not date_str:
            return ""
        from datetime import datetime, timezone
        tzinfo = self._eastern_tz()
        try:
            if date_str.isdigit() and len(date_str) >= 12:
                dt = datetime.fromtimestamp(int(date_str) / 1000, tz=timezone.utc)
            elif date_str.isdigit():
                dt = datetime.fromtimestamp(int(date_str), tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_local = dt.astimezone(tzinfo) if tzinfo else dt.astimezone()
            date_part = dt_local.strftime("%b %d, %Y")
            hour = dt_local.strftime("%I").lstrip("0") or "12"
            minute = dt_local.strftime("%M")
            ampm = dt_local.strftime("%p")
            return f"{date_part} {hour}:{minute} {ampm}"
        except Exception:
            return ""

    def _to_bool(self, v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        if isinstance(v, (list, tuple)) and v:
            v = v[-1]
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("1", "true", "yes", "on", "checked"):
                return True
            if s in ("0", "false", "no", "off", ""):
                return False
            return True
        return bool(v)
