import logging
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


@app.route('/api/power', methods=['GET'])
def get_power_data():
    try:
        response = requests.get(EM_URL, auth=(EM_USER, EM_PASS))
        data = response.json()
        watt_level = data.get("power", 0)
        return jsonify({"watt_level": watt_level})
    except Exception:
        logger.exception("Error fetching power data")
        return jsonify({"error": "Failed to fetch power data"}), 500


@app.route('/api/prices', methods=['GET'])
def get_prices():
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        date_param = request.args.get('date', today)

        url = REE_API_URL.format(date=date_param)
        resp = requests.get(url, headers={'Accept': 'application/json'}, timeout=REE_API_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        # Find PVPC values – the API returns them in the 'included' array (id "1001")
        pvpc_values = None
        for item in data.get('included', []):
            if str(item.get('id', '')) == '1001':
                pvpc_values = item['attributes']['values']
                break

        # Fallback: some response shapes put everything in 'data'
        if pvpc_values is None:
            for item in data.get('data', []):
                if str(item.get('id', '')) == '1001':
                    pvpc_values = item['attributes']['values']
                    break

        if pvpc_values is None:
            return jsonify({"error": "PVPC data not found"}), 500

        # Build hourly list – REE values are in €/MWh, convert to €/kWh
        hourly_prices = []
        for v in pvpc_values:
            price_kwh = round(float(v['value']) / MWH_TO_KWH, 4)
            dt_str = v['datetime']
            hour = int(dt_str[11:13])
            hourly_prices.append({'hour': hour, 'price_kwh': price_kwh})

        hourly_prices.sort(key=lambda x: x['hour'])

        current_hour = datetime.now().hour
        current_price = next(
            (p['price_kwh'] for p in hourly_prices if p['hour'] == current_hour),
            None
        )

        # Find the next hour that is strictly cheaper than the current one
        next_cheaper_hour = None
        next_cheaper_price = None
        if current_price is not None:
            for p in hourly_prices:
                if p['hour'] > current_hour and p['price_kwh'] < current_price:
                    next_cheaper_hour = p['hour']
                    next_cheaper_price = p['price_kwh']
                    break

        return jsonify({
            'date': date_param,
            'current_hour': current_hour,
            'current_price_kwh': current_price,
            'next_cheaper_hour': next_cheaper_hour,
            'next_cheaper_price': next_cheaper_price,
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
    app.run(debug=True, host='0.0.0.0', port=5000)
