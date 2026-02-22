import time
import logging
from typing import Any, Dict, List, Optional, Tuple

from plugins.base_plugin.base_plugin import BasePlugin
from utils.http_client import get_http_session

logger = logging.getLogger(__name__)

ESPN_RANKINGS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings"


class CfbRankings(BasePlugin):
    """College Football Rankings (AP / Coaches / CFP)

    - Font size selector: normal / large / larger / largest
    - Two-column layout whenever Top N > 15
    - Records auto-hidden when Top N > 20
    - Team line shows logo + School (Nickname), with nickname styled smaller in the template
    - Uses HTML/CSS render pipeline with style settings enabled

    Note on CFP:
      The ESPN rankings feed does not always include CFP rankings (outside CFP release windows).
      If CFP is not present, the plugin will raise a clear RuntimeError message.
    """

    _cache: Dict[str, Any] = {"ts": {}, "data": {}}

    def generate_settings_template(self):
        params = super().generate_settings_template()
        params["style_settings"] = True
        return params

    def generate_image(self, settings: Dict[str, Any], device_config):
        poll_choice = (settings.get("poll") or "auto").strip().lower()
        top_n = max(1, min(25, int(settings.get("top_n") or 20)))

        font_size = (settings.get("font_size") or "normal").strip().lower()
        if font_size not in ("normal", "large", "larger", "largest"):
            font_size = "normal"

        show_record_user = self._to_bool(settings.get("show_record", True))
        show_meta = self._to_bool(settings.get("show_meta", True))
        show_record = bool(show_record_user and top_n <= 20)

        cache_minutes = max(0, min(1440, int(settings.get("cache_minutes") or 30)))
        ttl = cache_minutes * 60

        dimensions = self._get_dimensions(settings, device_config)

        # Two-column layout whenever Top N > 15
        two_column = top_n > 15

        data, poll = self._get_poll_data(poll_choice, ttl)

        poll_name = (poll.get("name") or poll.get("shortName") or "College Football Rankings").strip()
        title = poll_name

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

        template_params = {
            "title": title,
            "meta": meta,
            "poll_date": poll_date,
            "note": "",
            "rows": rows,
            "show_record": show_record,
            "two_column": two_column,
            "top_n": top_n,
            "font_size": font_size,
            "plugin_settings": settings,
        }

        return self.render_image(dimensions, "cfbrankings.html", "cfbrankings.css", template_params)

    def _get_poll_data(self, poll_choice: str, ttl: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        data = self._fetch_json_cached(ESPN_RANKINGS_URL, ttl)
        poll = self._select_poll(data, poll_choice)
        if poll is None:
            if poll_choice == "cfp":
                raise RuntimeError(
                    "CFP ranking is not available from ESPN right now. "
                    "This typically means the selection committee rankings are not currently published for the active season/week."
                )
            raise RuntimeError("No suitable poll found in ESPN response.")
        return data, poll

    def _fetch_json_cached(self, url: str, ttl: int) -> Dict[str, Any]:
        now = time.time()
        ts = self._cache["ts"].get(url, 0.0)
        if ttl > 0 and url in self._cache["data"] and (now - ts) < ttl:
            return self._cache["data"][url]

        session = get_http_session()
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        if ttl > 0:
            self._cache["ts"][url] = now
            self._cache["data"][url] = data

        return data

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
            t = str(p.get("type") or "").lower()
            if t in ("ap", "coaches", "cfp"):
                return t

            name = str(p.get("name") or "").lower()
            short = str(p.get("shortName") or p.get("abbreviation") or "").lower()
            head = str(p.get("headline") or "").lower()
            blob = " ".join([name, short, head])

            if "playoff" in blob or "cfp" in blob or "selection committee" in blob:
                return "cfp"
            if "ap" in blob and "poll" in blob:
                return "ap"
            if "coaches" in blob or "afca" in blob:
                return "coaches"
            return ""

        if choice in ("ap", "coaches", "cfp"):
            matches = [p for p in polls if kind(p) == choice]
            if not matches:
                return None
            matches.sort(key=parse_date, reverse=True)
            return matches[0]

        order = {"cfp": 3, "ap": 2, "coaches": 1}
        candidates = [(parse_date(p), order.get(kind(p), 0), p) for p in polls if kind(p) in order]
        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return candidates[0][2]

        polls.sort(key=parse_date, reverse=True)
        return polls[0]

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

            label = (poll_name or "").strip()
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
            label = (poll_name or "").strip()
            if label:
                return f"Updated {date_str} • {label}"
            return f"Updated {date_str}"

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

            school = team.get("shortDisplayName") or team.get("location") or team.get("displayName") or team.get("abbreviation") or team.get("name") or "Unknown"
            nickname = team.get("name") or team.get("nickname") or ""

            # Avoid duplication
            nick_out = ""
            if nickname and nickname.lower() not in school.lower():
                nick_out = nickname

            logo = ""
            logos = team.get("logos")
            if isinstance(logos, list) and logos:
                href = None
                for item in logos:
                    if not isinstance(item, dict):
                        continue
                    rel = item.get("rel")
                    if isinstance(rel, list) and "default" in rel and item.get("href"):
                        href = item.get("href")
                        break
                if not href:
                    for item in logos:
                        if isinstance(item, dict) and item.get("href"):
                            href = item.get("href")
                            break
                logo = href or ""

            rec = entry.get("recordSummary") or entry.get("record") or ""

            rows.append({
                "rank": rk if rk is not None else "--",
                "school": school,
                "nickname": nick_out,
                "logo": logo,
                "record": rec if show_record else "",
            })

        return rows

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
