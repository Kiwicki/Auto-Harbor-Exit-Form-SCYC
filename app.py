import io
import math
import time
import sys
from datetime import datetime, timedelta
import requests
import streamlit as st

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.platypus import Paragraph
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# ---------------------------------------------------------------------------
# Location / station constants for Santa Cruz Harbor
# ---------------------------------------------------------------------------
NOAA_STATION_ID = "9413745"          # Santa Cruz Municipal Wharf, CA
HARBOR_MOUTH_LAT, HARBOR_MOUTH_LON = 36.9624, -122.0011

CRITERIA = {
    "beginner": {
        "label": "Small Boat L2S or FJ BEGINNER",
        "sounding_min": 12,
        "wind_max": 12,
        "fj_wind_max": None,
    },
    "intermediate": {
        "label": "Small Boat INTERMEDIATE",
        "sounding_min": 12,
        "wind_max": 15,
        "fj_wind_max": None,
    },
    "advanced": {
        "label": "FJ or SB Dev",
        "sounding_min": 10,
        "wind_max": 18,
        "fj_wind_max": 20,
    },
}

CLASS_TO_LEVEL_DEFAULT = {
    "SB-L2S": "beginner",
    "SB-INT": "intermediate",
    "SB-DEV": "advanced",
    "FJ": "advanced",
}

# ---------------------------------------------------------------------------
# Data Fetching & Processing Logic
# ---------------------------------------------------------------------------
def parse_flexible_time(time_str):
    time_str = time_str.strip()
    for fmt in ("%I:%M %p", "%H:%M", "%H%M", "%I:%M%p"):
        try:
            return datetime.strptime(time_str, fmt)
        except ValueError:
            pass
    return None

def fetch_tide_data(date_str):
    try:
        target_dt = datetime.strptime(date_str, "%m/%d/%Y")
        start_date = (target_dt - timedelta(days=1)).strftime("%Y%m%d")
        end_date = (target_dt + timedelta(days=1)).strftime("%Y%m%d")
        
        url = (
            "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
            f"?product=predictions&application=harbor_checklist&begin_date={start_date}"
            f"&end_date={end_date}&datum=MLLW&station={NOAA_STATION_ID}"
            "&time_zone=lst_ldt&units=english&interval=hilo&format=json"
        )
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                r = requests.get(url, timeout=10)
                r.raise_for_status()
                json_data = r.json()
                
                if "error" in json_data:
                    return []
                    
                predictions = json_data.get("predictions", [])
                events = []
                for p in predictions:
                    t_dt = datetime.strptime(p["t"], "%Y-%m-%d %H:%M")
                    v_ft = float(p["v"])
                    t_type = p.get("type", "").upper()
                    events.append({"time": t_dt, "value": v_ft, "type": t_type})
                return sorted(events, key=lambda x: x["time"])
                
            except (requests.exceptions.RequestException, ValueError):
                if attempt < max_retries - 1:
                    time.sleep(1.5)
        return []
    except Exception:
        return []

def parse_tide_summary(events, date_str, leave_time_str, return_time_str):
    result = {"high_time": None, "high_ft": None, "low_time": None, "low_ft": None, "absolute_low_day_ft": None}
    if not events:
        return result
        
    d_parsed = datetime.strptime(date_str, "%m/%d/%Y")
    day_events = [e for e in events if e["time"].date() == d_parsed.date()]
    day_lows = [e["value"] for e in day_events if e["type"].startswith("L")]
    if day_lows:
        result["absolute_low_day_ft"] = min(day_lows)
        
    t_leave = parse_flexible_time(leave_time_str)
    t_ret = parse_flexible_time(return_time_str)
    
    dt_leave = datetime(d_parsed.year, d_parsed.month, d_parsed.day, t_leave.hour, t_leave.minute) if t_leave else None
    dt_ret = datetime(d_parsed.year, d_parsed.month, d_parsed.day, t_ret.hour, t_ret.minute) if t_ret else None
    
    highs = [e for e in events if e["type"].startswith("H")]
    lows = [e for e in events if e["type"].startswith("L")]
    
    if highs:
        if dt_leave and dt_ret:
            best_high = min(highs, key=lambda x: min(abs((x["time"] - dt_leave).total_seconds()), abs((x["time"] - dt_ret).total_seconds())))
        else:
            best_high = max(highs, key=lambda x: x["value"]) if day_events else highs[0]
        result["high_time"] = best_high["time"].strftime("%I:%M %p")
        result["high_ft"] = best_high["value"]
        
    if lows:
        if dt_leave and dt_ret:
            best_low = min(lows, key=lambda x: min(abs((x["time"] - dt_leave).total_seconds()), abs((x["time"] - dt_ret).total_seconds())))
        else:
            best_low = min(lows, key=lambda x: x["value"]) if day_events else lows[0]
        result["low_time"] = best_low["time"].strftime("%I:%M %p")
        result["low_ft"] = best_low["value"]
        
    return result

def estimate_tide_at(events, date_str, time_str):
    if not events:
        return None
    t_parsed = parse_flexible_time(time_str)
    if not t_parsed:
        return None
    d_parsed = datetime.strptime(date_str, "%m/%d/%Y")
    target_dt = datetime(d_parsed.year, d_parsed.month, d_parsed.day, t_parsed.hour, t_parsed.minute)
    
    before = [e for e in events if e["time"] <= target_dt]
    after = [e for e in events if e["time"] >= target_dt]
    if not before or not after:
        return None
        
    e1 = max(before, key=lambda x: x["time"])
    e2 = min(after, key=lambda x: x["time"])
    if e1["time"] == e2["time"]:
        return round(e1["value"], 2)
        
    t1, v1 = e1["time"], e1["value"]
    t2, v2 = e2["time"], e2["value"]
    
    total_duration = (t2 - t1).total_seconds()
    elapsed = (target_dt - t1).total_seconds()
    
    fraction = elapsed / total_duration
    cos_val = math.cos(fraction * math.pi)
    return round(((v1 + v2) / 2.0) + ((v1 - v2) / 2.0) * cos_val, 2)

def fetch_marine_swell_api(date_str, leave_time_str, return_time_str):
    out = {"leave": None, "return": None}
    try:
        target_date = datetime.strptime(date_str, "%m/%d/%Y").strftime("%Y-%m-%d")
        url = (
            f"https://marine-api.open-meteo.com/v1/marine"
            f"?latitude={HARBOR_MOUTH_LAT}&longitude={HARBOR_MOUTH_LON}"
            f"&hourly=wave_height,wave_direction,wave_period"
            f"&start_date={target_date}&end_date={target_date}"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        hourly_data = r.json().get("hourly", {})
        
        times = [datetime.strptime(t, "%Y-%m-%dT%H:%M") for t in hourly_data.get("time", [])]
        heights = hourly_data.get("wave_height", [])
        directions = hourly_data.get("wave_direction", [])
        periods = hourly_data.get("wave_period", [])
        
        if not times:
            return out

        for key, time_str in (("leave", leave_time_str), ("return", return_time_str)):
            t_parsed = parse_flexible_time(time_str)
            if not t_parsed:
                continue
            dt_parsed = datetime.strptime(date_str, "%m/%d/%Y")
            target_dt = datetime(dt_parsed.year, dt_parsed.month, dt_parsed.day, t_parsed.hour, t_parsed.minute)
            idx = min(range(len(times)), key=lambda i: abs((times[i] - target_dt).total_seconds()))
            
            raw_height = heights[idx] if idx < len(heights) else None
            wave_height_ft = round(raw_height * 3.28084, 1) if raw_height is not None else None
            raw_deg = directions[idx] if idx < len(directions) else None
            wave_dir = deg_to_compass(raw_deg) if raw_deg is not None else None
            wave_period = round(periods[idx], 1) if idx < len(periods) else None
            
            out[key] = {"wave_dir": wave_dir, "wave_height_ft": wave_height_ft, "wave_period": wave_period}
    except Exception:
        pass
    return out

def deg_to_compass(deg):
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((deg / 22.5) + 0.5) % 16]

def fetch_wind_window(date_str, leave_time_str, return_time_str):
    try:
        d_parsed = datetime.strptime(date_str, "%m/%d/%Y")
        target_date = d_parsed.strftime("%Y-%m-%d")
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={HARBOR_MOUTH_LAT}&longitude={HARBOR_MOUTH_LON}"
            f"&hourly=wind_speed_10m&wind_speed_unit=kn"
            f"&start_date={target_date}&end_date={target_date}"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        hourly = r.json().get("hourly", {})
        times, winds = hourly.get("time", []), hourly.get("wind_speed_10m", [])
        
        t_leave, t_ret = parse_flexible_time(leave_time_str), parse_flexible_time(return_time_str)
        if not times or not winds or not t_leave or not t_ret:
            return None
            
        dt_start = datetime(d_parsed.year, d_parsed.month, d_parsed.day, t_leave.hour, t_leave.minute)
        dt_end = datetime(d_parsed.year, d_parsed.month, d_parsed.day, t_ret.hour, t_ret.minute)
        
        window_winds = []
        for t_str, speed in zip(times, winds):
            dt_item = datetime.strptime(t_str, "%Y-%m-%dT%H:%M")
            if dt_start <= dt_item <= dt_end or (dt_item.hour == dt_end.hour and dt_end.minute > 0):
                window_winds.append(speed)
        return round(max(window_winds)) if window_winds else round(winds[0])
    except Exception:
        return None

def fetch_aqi():
    try:
        url = f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={HARBOR_MOUTH_LAT}&longitude={HARBOR_MOUTH_LON}&current=us_aqi"
        r = requests.get(url, timeout=10)
        return round(r.json()["current"]["us_aqi"])
    except Exception:
        return None

def fetch_advisory():
    try:
        url = f"https://api.weather.gov/alerts/active?point={HARBOR_MOUTH_LAT},{HARBOR_MOUTH_LON}"
        r = requests.get(url, headers={"User-Agent": "(scyc-web-tool)"}, timeout=10)
        features = r.json().get("features", [])
        matched = []
        full_descriptions = []
        keywords = ("small craft", "gale", "storm warning", "dense fog", "fog advisory", "high surf", "swell")
        for f in features:
            props = f.get("properties", {})
            event = props.get("event", "")
            if any(kw in event.lower() for kw in keywords) and event not in matched:
                matched.append(event)
                desc = props.get("description", "No detailed description available.")
                full_descriptions.append(f"=== {event} ===\n{desc}")
        
        if matched:
            return "Y", "; ".join(matched), "\n\n".join(full_descriptions)
        return "N", None, None
    except Exception:
        return None, None, None

def evaluate(level, class_name, wind_harbor, sounding, advisory, wave_state, returning_ok, aqi):
    c = CRITERIA[level]
    wmax = c["fj_wind_max"] if (level == "advanced" and class_name == "FJ") else c["wind_max"]
    
    # Strictly evaluate the mathematical reality of all checklist rows
    return {
        "advisory": ("No Small Craft, Swell, or Fog Advisory", None if advisory is None else advisory.upper() == "N"),
        "sounding": (f"Harbor mouth sounding \u2265 {c['sounding_min']}ft", None if sounding is None else sounding >= c["sounding_min"]),
        "returning": ("NOT returning in \u2264 8ft depth" if level == "advanced" else "NOT returning on negative tide", returning_ok),
        "wave_state": ("Harbor mouth wave cresting \u2264 half the channel", None if wave_state is None else wave_state in ("none", "half")),
        "wind": (f"Predicted Wind \u2264{wmax} kn", None if wind_harbor is None else wind_harbor <= wmax),
        "aqi": ("AQI \u2264 100ppm", None if aqi is None else aqi <= 100)
    }

# ---------------------------------------------------------------------------
# PDF Generation Canvas Engine
# ---------------------------------------------------------------------------
def draw_checkbox(c, x, y, checked):
    c.saveState()
    c.rect(x, y, 9, 9, stroke=1, fill=0)
    if checked is True:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x + 1.3, y + 1, "X")
    c.restoreState()

def build_pdf(target, data_dict, checks_dict, approval_status):
    c = canvas.Canvas(target, pagesize=letter)
    W, H = letter
    x0 = 0.6 * inch
    y = H - 0.6 * inch

    # --- PAGE 1: PRIMARY CHECKLIST ---
    c.setFont("Helvetica-Bold", 16)
    c.drawString(x0, y, "Harbor Exit Checklist & Approval")
    c.setFont("Helvetica", 9)
    c.drawRightString(W - x0, y, "Santa Cruz Yacht Club")
    y -= 12
    c.drawRightString(W - x0, y, "244 Fourth Ave, Santa Cruz, CA 95062")
    y -= 12
    c.drawRightString(W - x0, y, "(831) 425-0690  |  scyc.org/weather")
    y -= 26

    c.setFont("Helvetica-Bold", 11)
    c.drawString(x0, y, f"DATE: {data_dict['date']}")
    c.drawString(x0 + 3 * inch, y, f"Class: {data_dict['boat_class']}   ({CRITERIA[data_dict['level']]['label']})")
    y -= 24

    def section(title):
        nonlocal y
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x0, y, title)
        y -= 16

    def line(label, value, warn=False):
        nonlocal y
        c.setFont("Helvetica", 10)
        c.setFillColor(colors.black)
        c.drawString(x0 + 0.15 * inch, y, f"{label}:")
        if value is None:
            c.line(x0 + 2.3 * inch, y - 2, x0 + 3.55 * inch, y - 2)
        else:
            c.setFont("Helvetica-Bold", 10)
            c.setFillColor(colors.red if warn else colors.black)
            c.drawString(x0 + 2.3 * inch, y, str(value))
        y -= 15

    section("Wind Speed  (SailFlow / PredictWind / SC Harbor / 1 Mile Buoy)")
    line("Max Predicted Wind in Window", f"{data_dict['wind_harbor']} kn" if data_dict['wind_harbor'] is not None else None, data_dict['wind_harbor'] is None)
    line("@ Mile Buoy (Max in Window)", f"{data_dict['wind_buoy']} kn" if data_dict['wind_buoy'] is not None else None, data_dict['wind_buoy'] is None)
    y -= 4

    section("Tide  (NOAA Tides & Currents)")
    line("High Tide / Feet", f"{data_dict['high_time']} / {data_dict['high_ft']} ft" if data_dict['high_time'] else None, data_dict['high_time'] is None)
    line("Low Tide / Feet", f"{data_dict['low_time']} / {data_dict['low_ft']} ft" if data_dict['low_time'] else None, data_dict['low_time'] is None)
    line("Leaving time", data_dict['leave'])
    line("Return time", data_dict['ret'])
    if data_dict['tide_at_return'] is not None:
        neg = data_dict['tide_at_return'] < 0
        line("Predicted tide @ return", f"{data_dict['tide_at_return']} ft" + ("  <- NEGATIVE TIDE" if neg else ""), neg)
    y -= 4

    section("Sea / Swell State @ Harbor Mouth  (wave state: manual check; swell dir/ht/period: API, verify)")
    wave_label = {"none": "No Waves", "half": "Waves breaking, half way", "full": "Wave breaking ALL the way across"}.get(data_dict['wave_state'], "Not Checked")
    line("Wave state", wave_label, data_dict['wave_state'] is None)
    line("Swell direction", data_dict['swell_dir'], data_dict['swell_dir'] is None)
    line("Swell height", f"{data_dict['swell_height']} ft" if data_dict['swell_height'] is not None else None, data_dict['swell_height'] is None)
    line("Swell period", f"{data_dict['swell_period']} sec" if data_dict['swell_period'] is not None else None, data_dict['swell_period'] is None)
    y -= 4

    section("Harbor Advisory Status  (sounding = manual check required)")
    line("Harbor mouth sounding", f"{data_dict['sounding']} ft" if data_dict['sounding'] is not None else None, data_dict['sounding'] is None)
    line("NOAA Advisory (Y/N)", data_dict['advisory'], data_dict['advisory'] is None)
    if data_dict['advisory_type']:
        line("Advisory type", data_dict['advisory_type'])
    y -= 4

    section("Air Quality Index (AQI)")
    line("AQI", data_dict['aqi'], data_dict['aqi'] is None)
    y -= 10

    c.setFont("Helvetica-Bold", 12)
    exit_box = "X" if data_dict['exiting'] is True else " "
    not_exit_box = "X" if data_dict['exiting'] is False else " "
    c.drawString(x0, y, f"[{exit_box}] Exiting Harbor      [{not_exit_box}] Not Exiting Harbor")
    y -= 22

    c.setFont("Helvetica-Bold", 11)
    c.drawString(x0, y, f"Criteria: {CRITERIA[data_dict['level']]['label']}")
    y -= 16
    c.setFont("Helvetica", 9)
    c.drawString(x0, y, "[X] = meets criteria   [ ] = does NOT meet criteria")
    y -= 16

    # Draw checkboxes dynamically using true validation statuses
    for key, (text, met) in checks_dict.items():
        draw_checkbox(c, x0 + 0.15 * inch, y - 7, met)
        c.setFont("Helvetica", 10)
        c.drawString(x0 + 0.4 * inch, y - 6, text)
        y -= 17

    y -= 6
    c.setFont("Helvetica-Bold", 10)
    
    # Audit trail validation mapping labels
    if approval_status == "passed_exit":
        c.setFillColor(colors.green)
        c.drawString(x0, y, "All criteria met - single Coach signature required to exit harbor.")
    elif approval_status == "passed_inside":
        c.setFillColor(colors.green)
        c.drawString(x0, y, "Practice restricted to INSIDE. Harbor mouth criteria not met, Coach signature authorized.")
    elif approval_status == "failed_inside":
        c.setFillColor(colors.red)
        c.drawString(x0, y, "Inside thresholds exceeded (Wind/AQI/Alert) - BOTH Coach & Director approval required.")
    else:
        c.setFillColor(colors.red)
        c.drawString(x0, y, "One or more criteria NOT met - BOTH Coach & Director approval required to exit harbor.")
        
    c.setFillColor(colors.black)
    y -= 28

    c.setLineWidth(0.75)
    c.line(x0, y, x0 + 2.6 * inch, y)
    c.line(x0 + 3.0 * inch, y, x0 + 3.8 * inch, y)
    y -= 12
    c.setFont("Helvetica", 9)
    c.drawString(x0, y, "Coach signature")
    c.drawString(x0 + 3.0 * inch, y, "Date")
    y -= 26

    c.line(x0, y, x0 + 2.6 * inch, y)
    y -= 12
    c.drawString(x0, y, "Director signature (if required above)")
    y -= 30

    c.setFont("Helvetica", 8)
    c.drawString(x0, y, "Program Director: Andreas Kesting 831-295-3893    Small Boat Director: Rino Rodriguez 619-902-1885")
    y -= 11
    c.drawString(x0, y, "Advanced Director: Nick Halmos 561-371-4453    Scholastic FJ Director: Ross Arthur 831-346-2707")
    y -= 11
    c.drawString(x0, y, "Harbormaster (if sounding <12ft or advisory active): (831) 475-6161")
    y -= 16
    c.setFont("Helvetica-Oblique", 7)
    c.drawString(x0, y, f"Auto-generated {datetime.now().strftime('%m/%d/%Y %I:%M %p')} | Harbor_Exit_Procedure_v_5")

    # --- PAGE 2: DETAILED NWS ADVISORY TEXT ---
    if data_dict.get("advisory_full_text"):
        c.showPage()
        y_p2 = H - 0.6 * inch
        
        c.setFont("Helvetica-Bold", 14)
        c.drawString(x0, y_p2, "Official National Weather Service Advisory Text")
        y_p2 -= 8
        c.setLineWidth(1)
        c.line(x0, y_p2, W - x0, y_p2)
        y_p2 -= 20
        
        styles = getSampleStyleSheet()
        alert_style = ParagraphStyle(
            'NWSAlertStyle',
            parent=styles['Normal'],
            fontName='Courier',
            fontSize=9.5,
            leading=13,
            textColor=colors.HexColor("#222222")
        )
        
        formatted_text = data_dict["advisory_full_text"].replace("\n", "<br/>")
        p = Paragraph(formatted_text, alert_style)
        p.wrapOn(c, W - 2*x0, H - 2*inch)
        p.drawOn(c, x0, y_p2 - p.height)

    c.save()

# ---------------------------------------------------------------------------
# Streamlit Frontend View Template
# ---------------------------------------------------------------------------
st.set_page_config(page_title="SCYC Harbor Exit Tool", page_icon="⛵", layout="centered")

st.title("⛵ SCYC Harbor Exit Checklist")

st.markdown("""
**Official Harbor Links:**
* 📏 [Santa Cruz Harbor Entrance Soundings](https://www.santacruzharbor.org/entrance-sounding)
* 📹 [Santa Cruz Harbor Webcam](https://www.santacruzharbor.org/santa-cruz-harbor-webcam)
---
""")

exiting_plan = st.radio(
    "What is today's practice plan?", 
    ["Exiting Harbor", "Not Exiting Harbor (Staying Inside)"], 
    index=0, 
    horizontal=True
)
exiting_bool = True if exiting_plan == "Exiting Harbor" else False

col1, col2 = st.columns(2)
with col1:
    boat_class = st.selectbox("Boat Class", list(CLASS_TO_LEVEL_DEFAULT.keys()))
    sailing_date = st.date_input("Sailing Date", datetime.now())
    leave_time = st.text_input("Leaving Time", "10:00 AM")
with col2:
    sounding_input = st.number_input(
        "Harbor Mouth Sounding (ft)", 
        min_value=0.0, max_value=30.0, value=12.0, step=0.5,
        help="Required for compliance logging even if staying inside harbor lines."
    )
    return_time = st.text_input("Return Time", "2:00 PM")

wave_state = st.selectbox(
    "Wave State at Harbor Mouth", 
    ["none", "half", "full"], 
    index=0
)

with st.expander("⚠️ Manual API Overrides (Use if Government Servers Timeout)"):
    st.markdown("*Leave these empty to allow the script to automatically fetch live environmental data.*")
    m_high_tide = st.text_input("Manual High Tide Time & Height (e.g., 11:30 AM / 4.5 ft)")
    m_low_tide = st.text_input("Manual Low Tide Time & Height (e.g., 5:15 PM / -0.2 ft)")
    m_ret_tide = st.text_input("Manual Tide Height at Return Time (ft)", value="")

if st.button("Generate Checklist PDF", type="primary"):
    date_str = sailing_date.strftime("%m/%d/%Y")
    level = CLASS_TO_LEVEL_DEFAULT[boat_class]

    with st.spinner("Fetching live marine data endpoints..."):
        tide_events = fetch_tide_data(date_str)
        tide = parse_tide_summary(tide_events, date_str, leave_time, return_time)
        tide_at_return = estimate_tide_at(tide_events, date_str, return_time)
        
        if m_high_tide:
            tide["high_time"], tide["high_ft"] = m_high_tide.split("/") if "/" in m_high_tide else (m_high_tide, "Unknown")
        if m_low_tide:
            tide["low_time"], tide["low_ft"] = m_low_tide.split("/") if "/" in m_low_tide else (m_low_tide, "Unknown")
        if m_ret_tide:
            try: tide_at_return = float(m_ret_tide)
            except ValueError: pass

        if not tide_events and not m_high_tide:
            st.warning("⚠️ NOAA Tide API timed out. Consider filling out the Manual Overrides menu above.")

        wind_harbor = fetch_wind_window(date_str, leave_time, return_time)
        wind_buoy = wind_harbor
        aqi = fetch_aqi()
        adv_yn, adv_type, advisory_full_text = fetch_advisory()

        wf = fetch_marine_swell_api(date_str, leave_time, return_time)
        wf_return = wf.get("return") or {}
        swell_dir = wf_return.get("wave_dir", "W")
        swell_height = wf_return.get("wave_height_ft", 3.0)
        swell_period = wf_return.get("wave_period", 10.0)

        returning_ok = None
        if tide_at_return is not None:
            returning_ok = (sounding_input + tide_at_return) > 8.0 if level == "advanced" else tide_at_return >= 0
        elif level != "advanced" and tide.get("absolute_low_day_ft") is not None:
            returning_ok = tide["absolute_low_day_ft"] >= 0

        # Execute absolute calculations
        checks_dict = evaluate(level, boat_class, wind_harbor, sounding_input, adv_yn, wave_state, returning_ok, aqi)
        
        # Smart override routing rule analysis
        director_required = False
        inside_failed = False
        
        for key, (text, met) in checks_dict.items():
            if met is False:
                if exiting_bool:
                    # Exiting requires a clean sweep across all categories
                    director_required = True
                else:
                    # Inside practice only escalates signature if core fields fail
                    if key in ["wind", "aqi", "advisory"]:
                        director_required = True
                        inside_failed = True

        # Assign conditional statuses for reporting maps
        if exiting_bool:
            approval_status = "failed_exit" if director_required else "passed_exit"
        else:
            approval_status = "failed_inside" if inside_failed else "passed_inside"

        payload = {
            "date": date_str, "boat_class": boat_class, "level": level, "leave": leave_time, "ret": return_time,
            "exiting": exiting_bool, "sounding": sounding_input, "wave_state": wave_state,
            "wind_harbor": wind_harbor, "wind_buoy": wind_buoy, "aqi": aqi,
            "high_time": tide.get("high_time"), "high_ft": tide.get("high_ft"),
            "low_time": tide.get("low_time"), "low_ft": tide.get("low_ft"),
            "tide_at_return": tide_at_return, "advisory": adv_yn, "advisory_type": adv_type,
            "advisory_full_text": advisory_full_text,
            "swell_dir": swell_dir, "swell_height": swell_height, "swell_period": swell_period
        }

        st.markdown("---")
        
        if adv_yn == "Y":
            st.error(f"⚠️ **ACTIVE MARINE ADVISORY DETECTED:** {adv_type}")
            with st.expander("👀 View Full National Weather Service Bulletin"):
                st.code(advisory_full_text, language="text")
        else:
            st.info("ℹ️ No active NWS small craft or marine weather alerts detected for this area.")

        # Display smart UI notices reflecting true operational boundaries
        if approval_status == "passed_exit":
            st.success("✅ **All Safety Criteria Passed.** Single Coach signature required to exit harbor.")
        elif approval_status == "passed_inside":
            st.success("ℹ️ **Practice Restricted to INSIDE Harbor.** Harbor mouth criteria not met, but inside safety lines are clear. **Single Coach signature required.**")
        elif approval_status == "failed_inside":
            st.error("🚨 **CRITICAL INSIDE SAFETY VIOLATION (Wind/AQI/Advisory).** Requires BOTH Coach & Program Director approval to conduct inside practice.")
        else:
            st.error("🚨 **Safety Threshold Exceeded.** Requires BOTH Coach & Program Director approval to exit harbor.")

        pdf_buffer = io.BytesIO()
        build_pdf(pdf_buffer, payload, checks_dict, approval_status)
        pdf_buffer.seek(0)

        st.download_button(
            label="⬇️ Download Completed Checklist PDF",
            data=pdf_buffer,
            file_name=f"SCYC_Harbor_Checklist_{date_str.replace('/', '-')}.pdf",
            mime="application/pdf",
            use_container_width=True
        )
