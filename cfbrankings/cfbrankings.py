import time
import logging
from typing import Any, Dict, List, Optional, Tuple

from plugins.base_plugin.base_plugin import BasePlugin
from utils.http_client import get_http_session

logger = logging.getLogger(__name__)

ESPN_RANKINGS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings"


class CfbRankings(BasePlugin):
    """College Football Rankings plugin (AP / Coaches / CFP)

    - Poll: auto / AP / Coaches / CFP (auto picks most recent known poll)
    - Top N: 1-25; records auto-hidden when Top N > 20
    - 2-column layout whenever Top N > 15 (independent of record visibility)
    - Renders via HTML/CSS with style settings enabled
    - Team label shows "School (Nickname)"
    """

    _cache: Dict[str, Any] = {"ts": 0.0, "key": None, "data": None}

    def generate_settings_template(self):
        t = super().generate_settings_template()
        t["style_settings"] = True
        return t

    def generate_image(self, settings: Dict[str, Any], device_config):
        poll_choice = (settings.get("poll") or "auto").strip().lower()
        top_n = max(1, min(25, int(settings.get("top_n") or 20)))

        show_record_user = self._to_bool(settings.get("show_record", True))
        show_meta = self._to_bool(settings.get("show_meta", True))
        show_record = bool(show_record_user and top_n <= 20)

        cache_minutes = max(0, min(1440, int(settings.get("cache_minutes") or 30)))
        ttl = cache_minutes * 60

        dimensions = self._get_dimensions(settings, device_config)

        # NEW: 2-column strictly when > 15 teams selected
        two_column = top_n > 15

        try:
            data = self._get_rankings_cached(ttl)
        except Exception as e:
            raise RuntimeError(f"Failed to fetch rankings: {e}")

        poll = self._select_poll(data, poll_choice)
        if not poll:
            # Make the error user-visible as per InkyPi guidance
            raise RuntimeError("No suitable poll found in ESPN response.")

        poll_name = poll.get("name") or poll.get("shortName") or "College Football Rankings"
        title = poll_name

        # Extract ranks robustly (different polls have different shapes)
        ranks = poll.get("ranks")
        if isinstance(ranks, dict):
            ranks = ranks.get("items") or ranks.get("entries") or ranks.get("ranks")
        if not isinstance(ranks, list):
            ranks = poll.get("entries") or []
        if not isinstance(ranks, list):
            ranks = []

        rows = self._build_rows(ranks, top_n, show_record)

        meta = ""
        if show_meta:
            season = (data.get("season") or {}).get("year")
            week = (data.get("week") or {}).get("number")
            if season and week:
                meta = f"Season {season} • Week {week}"
            elif season:
                meta = f"Season {season}"

        poll_date = self._format_poll_date(poll, poll_name, device_config)

        params = {
            "title": title,
            "meta": meta,
            "poll_date": poll_date,
            "rows": rows,
            "show_record": show_record,
            "two_column": two_column,
            "top_n": top_n,
            "plugin_settings": settings,
        }
        return self.render_image(dimensions, "cfbrankings.html", "cfbrankings.css", params)

    # ----------------------------
    # Fetch/cache
    # ----------------------------
    def _get_rankings_cached(self, ttl: int) -> Dict[str, Any]:
        now = time.time()
        key = "rankings"
        if ttl > 0 and self._cache["data"] is not None:
            if self._cache["key"] == key and (now - self._cache["ts"]) < ttl:
                return self._cache["data"]
        session = get_http_session()
        resp = session.get(ESPN_RANKINGS_URL, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if ttl > 0:
            self._cache.update({"ts": now, "key": key, "data": data})
        return data

    # ----------------------------
    # Poll selection (robust CFP matching)
    # ----------------------------
    def _select_poll(self, data: Dict[str, Any], choice: str) -> Optional[Dict[str, Any]]:
        from datetime import datetime, timezone
        polls = data.get("rankings")
        if isinstance(polls, dict):
            polls = polls.get("items") or polls.get("rankings")
        if not isinstance(polls, list) or not polls:
            return None

        def parse_date(p: Dict[str, Any]) -> float:
            for k in ("date", "lastUpdated", "lastUpdate", "updated", "updateDate"):
                v = p.get(k)
                if not v:
                    continue
                try:
                    ds = str(v).replace("Z", "+00:00")
                    dt = datetime.fromisoformat(ds)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.timestamp()
                except Exception:
                    pass
            return 0.0

        def kind(p: Dict[str, Any]) -> str:
            # Some objects include a stable type field like "ap", "coaches", "cfp"
            t = (p.get("type") or "").lower()
            if t:
                return t
            # Otherwise examine names
            name = (p.get("name") or "").lower()
            short = (p.get("shortName") or p.get("abbreviation") or "").lower()
            if "playoff" in name or "playoff" in short or "cfp" in name or "cfp" in short:
                return "cfp"
            if "ap" in name or short == "ap":
                return "ap"
            if "coaches" in name or "afca" in name or short == "coaches":
                return "coaches"
            return ""

        # If user asked for a specific poll
        if choice in ("ap", "coaches", "cfp"):
            matches = [p for p in polls if kind(p) == choice]
            if matches:
                # pick most recent by date
                matches.sort(key=parse_date, reverse=True)
                return matches[0]
            return None

        # AUTO: prefer most recent among known kinds
        candidates = [(parse_date(p), kind(p), p) for p in polls if kind(p) in ("cfp", "ap", "coaches")]
        if candidates:
            # sort by date, then prefer CFP over AP over Coaches if same date
            order = {"cfp": 3, "ap": 2, "coaches": 1}
            candidates.sort(key=lambda x: (x[0], order.get(x[1], 0)), reverse=True)
            return candidates[0][2]
        # As last resort return the most recent by date
        polls.sort(key=parse_date, reverse=True)
        return polls[0] if polls else None

    # ----------------------------
    # Date formatting
    # ----------------------------
    def _get_tzinfo(self, device_config):
        try:
            tz_name = device_config.get_config("timezone")
        except Exception:
            tz_name = None
        if not tz_name:
            return None
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz_name)
        except Exception:
            return None

    def _format_poll_date(self, poll: Dict[str, Any], poll_name: str, device_config) -> str:
        from datetime import datetime, timezone
        date_str = None
        for k in ("date", "lastUpdated", "lastUpdate", "updated", "updateDate"):
            v = poll.get(k)
            if v:
                date_str = str(v)
                break
        if not date_str:
            return ""
        tzinfo = self._get_tzinfo(device_config)
        try:
            ds = date_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ds)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_local = dt.astimezone(tzinfo) if tzinfo else dt.astimezone()
            label = (poll_name or "").strip() or (poll.get("shortName") or "").strip() or (poll.get("name") or "").strip()
            hour = dt_local.strftime("%I").lstrip("0") or "12"
            minute = dt_local.strftime("%M")
            ampm = dt_local.strftime("%p")
            tz_abbr = (dt_local.strftime("%Z") or "").strip()
            date_part = dt_local.strftime("%b %d, %Y")
            time_part = f"{hour}:{minute} {ampm}"
            if tz_abbr:
                time_part = f"{time_part} {tz_abbr}"
            if label:
                return f"Updated {date_part} {time_part} • {label}"
            return f"Updated {date_part} {time_part}"
        except Exception:
            label = (poll_name or "").strip() or (poll.get("shortName") or "").strip() or (poll.get("name") or "").strip()
            if label:
                return f"Updated {date_str} • {label}"
            return f"Updated {date_str}"

    # ----------------------------
    # Rows
    # ----------------------------
    def _build_rows(self, ranks: List[Dict[str, Any]], top_n: int, show_record: bool):
        rows = []
        if isinstance(ranks, dict):
            ranks = ranks.get("items") or ranks.get("entries") or ranks.get("ranks") or []
        if not isinstance(ranks, list):
            ranks = []
        for entry in ranks[:top_n]:
            if not isinstance(entry, dict):
                continue
            rk = entry.get("current") or entry.get("rank") or entry.get("position") or entry.get("ranking")
            team = entry.get("team") or entry.get("school") or {}
            if not isinstance(team, dict):
                team = {}
            school = team.get("shortDisplayName") or team.get("location") or team.get("abbreviation") or team.get("displayName") or team.get("name") or "Unknown"
            nickname = team.get("name") or team.get("nickname") or ""
            team_display = school if not nickname or nickname.lower() in school.lower() else f"{school} ({nickname})"
            rec = entry.get("recordSummary") or entry.get("record") or ""
            rows.append({
                "rank": rk if rk is not None else "--",
                "team": team_display,
                "record": rec if show_record else "",
            })
        return rows

    # ----------------------------
    # Dimensions / orientation
    # ----------------------------
    def _get_dimensions(self, settings: Dict[str, Any], device_config) -> Tuple[int, int]:
        screen_size = (settings.get("screen_size") or "auto").strip().lower()
        if screen_size == "800x480":
            dims = (800, 480)
        elif screen_size == "1600x1200":
            dims = (1600, 1200)
        else:
            dims = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dims = dims[::-1]
        return dims

    def _to_bool(self, v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on", "checked")
        return bool(v)
