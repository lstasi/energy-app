import json
import logging
import os
import requests
from datetime import datetime
from flask import Flask, jsonify, request
from config import EM_URL, EM_USER, EM_PASS

app = Flask(__name__, static_folder='static')
logger = logging.getLogger(__name__)

REE_API_URL = (
    "https://apidatos.ree.es/es/datos/mercados/precios-mercados-tiempo-real"
    "?time_trunc=hour"
    "&start_date={date}T00:00"
    "&end_date={date}T23:59"
    "&geo_trunc=electric_system"
    "&geo_limit=peninsular"
    "&geo_ids=8741"
)
REE_API_TIMEOUT = 10   # seconds
MWH_TO_KWH = 1000      # REE API returns prices in €/MWh; divide by this to get €/kWh
PRICE_GREEN  = 0.10
PRICE_YELLOW = 0.18
COSTS_FILE = os.path.expanduser('~/.energy-app-costs.json')

# ── Cost state (in-memory, persisted to COSTS_FILE) ──────────────────────────
_costs_last_loaded = None

def _load_costs():
    global _costs_last_loaded
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    month = now.strftime('%Y-%m')
    try:
        with open(COSTS_FILE) as f:
            saved = json.load(f)
        daily   = saved['daily_cost']   if saved.get('day')   == today else 0.0
    except Exception:
        daily = 0.0
    _costs_last_loaded = {'daily': daily,
                          'day': today, 'month': month, 'ts': now}
    return _costs_last_loaded

def _get_costs():
    global _costs_last_loaded
    if _costs_last_loaded is None:
        _load_costs()
    return _costs_last_loaded

def _add_cost(watts, price_kwh):
    """Accumulate €-cost for one server poll tick (~2 s)."""
    c = _get_costs()
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    month = now.strftime('%Y-%m')
    # Reset on day/month boundary
    if c['day'] != today:
        c['daily'] = 0.0
        c['day'] = today
    if c['month'] != month:
        c['monthly'] = 0.0
        c['month'] = month
    increment = (watts / 1000 / 3600) * price_kwh * 2  # 2-second poll interval
    c['daily'] += increment
    # Save every ~60 s
    if (now - c['ts']).total_seconds() >= 60:
        try:
            with open(COSTS_FILE, 'w') as f:
                json.dump({'day': c['day'], 'month': c['month'],
                           'daily_cost': c['daily'],
                           'saved_at': now.isoformat(timespec='seconds')}, f)
        except Exception:
            pass
        c['ts'] = now

# ── Cached current price (refreshed by /api/prices calls) ────────────────────
_current_price_kwh = None


@app.route('/api/power', methods=['GET'])
def get_power_data():
    try:
        response = requests.get(EM_URL, auth=(EM_USER, EM_PASS))
        data = response.json()
        watt_level = data.get("power", 0)
        if _current_price_kwh is not None and watt_level:
            _add_cost(watt_level, _current_price_kwh)
        return jsonify({"watt_level": watt_level})
    except Exception:
        logger.exception("Error fetching power data")
        return jsonify({"error": "Failed to fetch power data"}), 500


@app.route('/api/costs', methods=['GET'])
def get_costs():
    c = _get_costs()
    return jsonify({'daily_cost': round(c['daily'], 4)})


def _fetch_hourly(date_str):
    """Fetch and parse PVPC hourly prices for a given date string (YYYY-MM-DD)."""
    url = REE_API_URL.format(date=date_str)
    resp = requests.get(url, headers={'Accept': 'application/json'}, timeout=REE_API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    pvpc_values = None
    for item in data.get('included', []):
        if str(item.get('id', '')) == '1001':
            pvpc_values = item['attributes']['values']
            break
    if pvpc_values is None:
        for item in data.get('data', []):
            if str(item.get('id', '')) == '1001':
                pvpc_values = item['attributes']['values']
                break
    if not pvpc_values:
        return []

    prices = []
    for v in pvpc_values:
        price_kwh = round(float(v['value']) / MWH_TO_KWH, 4)
        hour = int(v['datetime'][11:13])
        prices.append({'hour': hour, 'price_kwh': price_kwh})
    prices.sort(key=lambda x: x['hour'])
    return prices


@app.route('/api/prices', methods=['GET'])
def get_prices():
    try:
        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        date_param = request.args.get('date', today)

        hourly_prices = _fetch_hourly(date_param)

        if not hourly_prices:
            return jsonify({"error": "PVPC data not found"}), 500

        # After 20:30 and only when browsing today, also append next-day prices
        is_today = (date_param == today)
        if is_today and (now.hour > 20 or (now.hour == 20 and now.minute >= 30)):
            from datetime import timedelta
            tomorrow = (now + timedelta(days=1)).strftime('%Y-%m-%d')
            try:
                next_prices = _fetch_hourly(tomorrow)
                # Tag tomorrow's entries so the client can distinguish them
                for p in next_prices:
                    p['tomorrow'] = True
                hourly_prices.extend(next_prices)
            except Exception:
                logger.warning("Next-day prices not yet available")

        current_hour = now.hour

        # Keep only the past 2 hours and all upcoming hours (today's entries only)
        hourly_prices = [
            p for p in hourly_prices
            if p.get('tomorrow') or p['hour'] >= current_hour - 2
        ]

        current_price = next(
            (p['price_kwh'] for p in hourly_prices
             if not p.get('tomorrow') and p['hour'] == current_hour),
            None
        )

        # Cache current price for cost accumulation
        global _current_price_kwh
        _current_price_kwh = current_price

        # Next 3 upcoming hours
        next3 = [p for p in hourly_prices
                 if not p.get('tomorrow') and p['hour'] > current_hour][:3]

        # Next GREEN hour (price ≤ PRICE_GREEN)
        next_green_hour = next_green_price = None
        next_green_tomorrow = False
        for p in hourly_prices:
            is_future = p.get('tomorrow') or p['hour'] > current_hour
            if is_future and p['price_kwh'] <= PRICE_GREEN:
                next_green_hour      = p['hour']
                next_green_price     = p['price_kwh']
                next_green_tomorrow  = bool(p.get('tomorrow'))
                break
        # If after 20:30 and still no green found, flag as tomorrow TBD
        if next_green_hour is None and not any(p.get('tomorrow') for p in hourly_prices):
            next_green_tomorrow = True  # tomorrow prices not fetched yet

        return jsonify({
            'date': date_param,
            'current_hour': current_hour,
            'current_price_kwh': current_price,
            'next3': next3,
            'next_green_hour': next_green_hour,
            'next_green_price': next_green_price,
            'next_green_tomorrow': next_green_tomorrow,
            'hourly_prices': hourly_prices,
        })
    except Exception:
        logger.exception("Error fetching prices")
        return jsonify({"error": "Failed to fetch price data"}), 500


@app.route('/', methods=['GET'])
def home():
    try:
        return app.send_static_file('index.html')
    except FileNotFoundError:
        return "index.html not found", 404


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=80)
