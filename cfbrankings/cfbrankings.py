import time
import logging
from typing import Any, Dict, List, Optional, Tuple

from plugins.base_plugin.base_plugin import BasePlugin
from utils.http_client import get_http_session

logger = logging.getLogger(__name__)

ESPN_RANKINGS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings"


class CfbRankings(BasePlugin):
    """College Football Rankings plugin.

    - Selects AP/Coaches/CFP from ESPN rankings feed and always chooses the most recent poll instance.
    - Optional toggles: record, movement, nickname, meta.
    - Compact mode for tighter spacing.
    - Updated timestamp is shown in upper-right and contains only date + time.
    """

    _cache: Dict[str, Any] = {"ts": 0.0, "data": None}

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

        show_record = self._to_bool(settings.get("show_record", True))
        show_movement = self._to_bool(settings.get("show_movement", True))
        show_nickname = self._to_bool(settings.get("show_nickname", True))
        show_meta = self._to_bool(settings.get("show_meta", True))
        compact_mode = self._to_bool(settings.get("compact_mode", False))

        
        color_logos = self._to_bool(settings.get("color_logos", True))
cache_minutes = max(0, min(1440, int(settings.get("cache_minutes") or 30)))
        ttl = cache_minutes * 60

        dimensions = self._get_dimensions(settings, device_config)
        two_column = top_n > 15

        data = self._get_rankings_cached(ttl)
        poll = self._pick_poll(data, poll_choice)
        if poll is None:
            if poll_choice == "cfp":
                raise RuntimeError(
                    "CFP poll not found in ESPN rankings response. Outside the committee ranking window, ESPN may omit it."
                )
            raise RuntimeError("Selected poll not found in ESPN response.")

        poll_name = (poll.get("name") or poll.get("shortName") or "College Football Rankings").strip()
        title = poll_name

        ranks = self._extract_ranks(poll)
        rows = self._build_rows(ranks, top_n, show_record)

        meta = ""
        if show_meta:
            season = (data.get("season") or {}).get("year")
            week = (data.get("week") or {}).get("number")
            if season and week:
                meta = f"Season {season} • Week {week}"
            elif season:
                meta = f"Season {season}"

        poll_date = self._format_poll_date(poll, device_config)

        template_params = {
            "title": title,
            "meta": meta,
            "poll_date": poll_date,
            "rows": rows,
            "show_record": bool(show_record),
            "show_movement": bool(show_movement),
            "show_nickname": bool(show_nickname),
            "compact_mode": bool(compact_mode),
            "two_column": two_column,
            "top_n": top_n,
            "font_size": font_size,
            "plugin_settings": settings,
        }

        return self.render_image(dimensions, "cfbrankings.html", "cfbrankings.css", template_params)

    # ----------------------------
    # Fetch/cache
    # ----------------------------

    def _get_rankings_cached(self, ttl: int) -> Dict[str, Any]:
        now = time.time()
        if ttl > 0 and self._cache["data"] is not None and (now - self._cache["ts"]) < ttl:
            return self._cache["data"]

        session = get_http_session()
        resp = session.get(ESPN_RANKINGS_URL, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        if ttl > 0:
            self._cache["ts"] = now
            self._cache["data"] = data

        return data

    # ----------------------------
    # Poll selection (most recent)
    # ----------------------------

    def _pick_poll(self, data: Dict[str, Any], choice: str) -> Optional[Dict[str, Any]]:
        polls = data.get("rankings")
        if isinstance(polls, dict):
            polls = polls.get("items") or polls.get("rankings")
        if not isinstance(polls, list):
            return None

        def norm(s: Any) -> str:
            return str(s or "").strip().lower()

        def parse_date(p: Dict[str, Any]) -> float:
            import datetime
            for k in ("date", "lastUpdated", "lastUpdate", "updated", "updateDate"):
                v = p.get(k)
                if not v:
                    continue
                try:
                    ds = str(v).replace("Z", "+00:00")
                    dt = datetime.datetime.fromisoformat(ds)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    return dt.timestamp()
                except Exception:
                    pass
            occ = p.get("occurrence")
            if isinstance(occ, dict):
                for k in ("startDate", "endDate"):
                    v = occ.get(k)
                    if not v:
                        continue
                    try:
                        ds = str(v).replace("Z", "+00:00")
                        dt = datetime.datetime.fromisoformat(ds)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=datetime.timezone.utc)
                        return dt.timestamp()
                    except Exception:
                        pass
            return 0.0

        def is_ap(p: Dict[str, Any]) -> bool:
            t = norm(p.get("type"))
            if t == "ap":
                return True
            n = norm(p.get("name"))
            s = norm(p.get("shortName"))
            return ("ap" in s) or ("ap top" in n)

        def is_coaches(p: Dict[str, Any]) -> bool:
            t = norm(p.get("type"))
            if t == "coaches":
                return True
            n = norm(p.get("name"))
            s = norm(p.get("shortName"))
            return ("coaches" in n) or ("afca" in n) or ("coaches" in s)

        def is_cfp(p: Dict[str, Any]) -> bool:
            n = norm(p.get("name"))
            if n == "playoff selection committee rankings":
                return True
            if "playoff selection committee" in n:
                return True
            s = norm(p.get("shortName"))
            t = norm(p.get("type"))
            h = norm(p.get("headline"))
            blob = " ".join([n, s, t, h])
            return ("playoff" in blob and "committee" in blob) or ("cfp" in blob) or ("selection committee" in blob)

        ap_list = [p for p in polls if isinstance(p, dict) and is_ap(p)]
        coaches_list = [p for p in polls if isinstance(p, dict) and is_coaches(p)]
        cfp_list = [p for p in polls if isinstance(p, dict) and is_cfp(p)]

        ap_list.sort(key=parse_date, reverse=True)
        coaches_list.sort(key=parse_date, reverse=True)
        cfp_list.sort(key=parse_date, reverse=True)

        if choice == "cfp":
            return cfp_list[0] if cfp_list else None
        if choice == "ap":
            return ap_list[0] if ap_list else None
        if choice == "coaches":
            return coaches_list[0] if coaches_list else None

        candidates = []
        if cfp_list:
            candidates.append(cfp_list[0])
        if ap_list:
            candidates.append(ap_list[0])
        if coaches_list:
            candidates.append(coaches_list[0])
        if not candidates:
            return polls[0] if polls and isinstance(polls[0], dict) else None
        candidates.sort(key=parse_date, reverse=True)
        return candidates[0]

    def _extract_ranks(self, poll: Dict[str, Any]) -> List[Dict[str, Any]]:
        ranks = poll.get("ranks")
        if isinstance(ranks, dict):
            ranks = ranks.get("items") or ranks.get("entries") or ranks.get("ranks")
        if not isinstance(ranks, list):
            ranks = poll.get("entries") or []
        if not isinstance(ranks, list):
            ranks = []
        return [r for r in ranks if isinstance(r, dict)]

    # ----------------------------
    # Date formatting (date+time only)
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

    def _format_poll_date(self, poll: Dict[str, Any], device_config) -> str:
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

            date_part = dt_local.strftime("%b %d, %Y")
            hour = dt_local.strftime("%I").lstrip("0") or "12"
            minute = dt_local.strftime("%M")
            ampm = dt_local.strftime("%p")
            tz_abbr = (dt_local.strftime("%Z") or "").strip()
            time_part = f"{hour}:{minute} {ampm}" + (f" {tz_abbr}" if tz_abbr else "")
            return f"{date_part} {time_part}"
        except Exception:
            return date_str

    # ----------------------------
    # Rows
    # ----------------------------

    def _build_rows(self, ranks: List[Dict[str, Any]], top_n: int, show_record: bool):
        rows = []

        def _to_int(x):
            try:
                if x is None:
                    return None
                if isinstance(x, str) and not x.strip().isdigit():
                    return None
                return int(x)
            except Exception:
                return None

        for entry in ranks[:top_n]:
            rk = entry.get("current") or entry.get("rank") or entry.get("position") or entry.get("ranking")
            prev = entry.get("previous")

            cur_i = _to_int(rk)
            prev_i = _to_int(prev)

            move_dir = ""
            move_delta = 0
            if cur_i is not None and prev_i is not None and cur_i != prev_i:
                if cur_i < prev_i:
                    move_dir = "up"
                    move_delta = prev_i - cur_i
                elif cur_i > prev_i:
                    move_dir = "down"
                    move_delta = cur_i - prev_i

            team = entry.get("team") or entry.get("school") or {}
            if not isinstance(team, dict):
                team = {}

            school = team.get("shortDisplayName") or team.get("location") or team.get("displayName") or team.get("abbreviation") or team.get("name") or "Unknown"
            nickname = team.get("name") or team.get("nickname") or ""

            nick_out = ""
            if nickname and nickname.lower() not in str(school).lower():
                nick_out = nickname

            logo = ""
            logos = team.get("logos")
            if isinstance(logos, list) and logos:
                href = None
                svg = None
                best = (0, None)  # (area, href)
                for item in logos:
                    if not isinstance(item, dict):
                        continue
                    u = item.get("href")
                    if not u:
                        continue
                    if str(u).lower().endswith(".svg"):
                        svg = u
                    w = item.get("width") or 0
                    hgt = item.get("height") or 0
                    try:
                        area = int(w) * int(hgt)
                    except Exception:
                        area = 0
                    if area > best[0]:
                        best = (area, u)
                if svg:
                    href = svg
                else:
                    for item in logos:
                        if not isinstance(item, dict):
                            continue
                        rel = item.get("rel")
                        if isinstance(rel, list) and "default" in rel and item.get("href"):
                            href = item.get("href")
                            break
                    if not href and best[1]:
                        href = best[1]
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
                "move_dir": move_dir,
                "move_delta": move_delta,
            })

        return rows

    # ----------------------------
    # Utilities
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
