from flask import Flask, jsonify, send_from_directory
import requests

app = Flask(__name__, static_folder='static')

# Endpoint to fetch data from the external service
DATA_ENDPOINT = "http://127.0.0.1/emeter/0"

@app.route('/api/power', methods=['GET'])
def get_power_data():
    try:
        # Fetch data from external service
        # Basic Auth admin:admin
        response = requests.get(DATA_ENDPOINT, auth=('admin', 'admin'))
        data = response.json()
       # Log data to console
        watt_level = data.get("power", 0)
        return jsonify({"watt_level": watt_level})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/', methods=['GET'])
def home():
    try:
        return app.send_static_file('index.html')
    except FileNotFoundError:
        return "index.html not found", 404

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
