import curses
import json
import os
import time
import requests
from config import EM_URL, EM_USER, EM_PASS
import collections
from datetime import datetime, timedelta

REE_API_URL = (
    "https://apidatos.ree.es/es/datos/mercados/precios-mercados-tiempo-real"
    "?time_trunc=hour"
    "&start_date={date}T00:00"
    "&end_date={date}T23:59"
    "&geo_trunc=electric_system"
    "&geo_limit=peninsular"
    "&geo_ids=8741"
)
REE_API_TIMEOUT = 10
MWH_TO_KWH = 1000

# Traffic-light thresholds (€/kWh) — matches web UI
PRICE_GREEN  = 0.10
PRICE_YELLOW = 0.18

COSTS_FILE = os.path.expanduser("~/.energy-app-costs.json")


class PowerMeter:
    def __init__(self):
        self.stdscr = curses.initscr()
        curses.start_color()
        curses.init_pair(1, curses.COLOR_GREEN,  curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.init_pair(3, curses.COLOR_RED,    curses.COLOR_BLACK)
        curses.init_pair(4, curses.COLOR_CYAN,   curses.COLOR_BLACK)
        curses.curs_set(0)
        self.max_power = 8625
        # Request small terminal on Pi (ignored by most desktop terminals)
        print('\x1b[8;15;40t')

        self.power_readings = collections.deque(maxlen=5)

        # Price cache
        self.price_data = {}
        self.price_fetched_at = None
        self.price_fetched_hour = -1
        self.tmr_prices = []        # cached tomorrow prices
        self.tmr_fetched_date = None  # the 'tomorrow' date we fetched

        # Cost accumulation — loaded from / saved to file
        self._load_costs()
        self._last_save = datetime.now()

    # ── Persistence ────────────────────────────────────────────────────────

    def _load_costs(self):
        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        month = now.strftime('%Y-%m')
        defaults = {'daily_cost': 0.0, 'day': today, 'month': month}
        try:
            with open(COSTS_FILE) as f:
                saved = json.load(f)
            self.daily_cost = saved['daily_cost'] if saved.get('day') == today else 0.0
        except Exception:
            self.daily_cost = defaults['daily_cost']

    def _save_costs(self):
        now = datetime.now()
        data = {
            'day':          now.strftime('%Y-%m-%d'),
            'month':        now.strftime('%Y-%m'),
            'daily_cost':   self.daily_cost,
            'saved_at':     now.isoformat(timespec='seconds'),
        }
        try:
            with open(COSTS_FILE, 'w') as f:
                json.dump(data, f)
        except Exception:
            pass

    # ── Shelly EM ──────────────────────────────────────────────────────────

    def get_shelly(self):
        """Return (avg_power_w, total_wh). Returns (0, None) on error."""
        try:
            resp = requests.get(EM_URL, auth=(EM_USER, EM_PASS), timeout=5)
            data = resp.json()
            power = int(data.get('power', 0))
            total = data.get('total')  # cumulative Wh

            now = datetime.now()
            self.power_readings.append((power, now))
            cutoff = now - timedelta(seconds=10)
            self.power_readings = collections.deque(
                [(p, t) for p, t in self.power_readings if t >= cutoff], maxlen=5
            )
            avg = int(sum(p for p, _ in self.power_readings) / len(self.power_readings)) \
                  if self.power_readings else power
            return avg, total
        except Exception:
            return 0, None

    # ── REE prices ─────────────────────────────────────────────────────────

    def _fetch_hourly(self, date_str):
        url = REE_API_URL.format(date=date_str)
        resp = requests.get(url, headers={'Accept': 'application/json'}, timeout=REE_API_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        pvpc = None
        for item in data.get('included', []):
            if str(item.get('id', '')) == '1001':
                pvpc = item['attributes']['values']
                break
        if pvpc is None:
            for item in data.get('data', []):
                if str(item.get('id', '')) == '1001':
                    pvpc = item['attributes']['values']
                    break
        if not pvpc:
            return []
        return sorted(
            [{'hour': int(v['datetime'][11:13]),
              'price_kwh': round(float(v['value']) / MWH_TO_KWH, 4)}
             for v in pvpc],
            key=lambda x: x['hour']
        )

    def get_prices(self):
        """Refresh prices once per hour (or on hour change). Returns price_data dict."""
        now = datetime.now()
        today    = now.strftime('%Y-%m-%d')
        tomorrow = (now + timedelta(days=1)).strftime('%Y-%m-%d')
        age = (now - self.price_fetched_at).total_seconds() if self.price_fetched_at else 9999

        # Re-fetch tomorrow's prices if past 20:30 and we don't have them yet
        after_2030 = now.hour > 20 or (now.hour == 20 and now.minute >= 30)
        need_tmr = after_2030 and self.tmr_fetched_date != tomorrow
        if need_tmr:
            try:
                self.tmr_prices = self._fetch_hourly(tomorrow)
                self.tmr_fetched_date = tomorrow
            except Exception:
                pass  # Not yet available — will retry next loop

        # Skip full re-fetch if same hour and cache is fresh
        if self.price_fetched_hour == now.hour and age < 3600 and not need_tmr:
            return self.price_data

        try:
            today_prices = self._fetch_hourly(today)
            tmr_prices   = self.tmr_prices

            current_price = next(
                (p['price_kwh'] for p in today_prices if p['hour'] == now.hour), None
            )

            # Next 3 upcoming hours (today only)
            next3 = [p for p in today_prices if p['hour'] > now.hour][:3]

            # Next green: search today remainder, then tomorrow
            next_green_hour = next_green_price = None
            next_green_tomorrow = False
            for p in today_prices:
                if p['hour'] > now.hour and p['price_kwh'] <= PRICE_GREEN:
                    next_green_hour  = p['hour']
                    next_green_price = p['price_kwh']
                    break
            if next_green_hour is None:
                if tmr_prices:
                    for p in tmr_prices:
                        if p['price_kwh'] <= PRICE_GREEN:
                            next_green_hour     = p['hour']
                            next_green_price    = p['price_kwh']
                            next_green_tomorrow = True
                            break
                    if next_green_hour is None:
                        next_green_tomorrow = True   # tomorrow exists but no green either
                else:
                    next_green_tomorrow = after_2030  # after 20:30 = TBD, before = not applicable

            spend_rate = (current_price * (
                sum(p for p, _ in self.power_readings) / len(self.power_readings) / 1000
            )) if current_price and self.power_readings else None

            # Build display hourly list: current + next 8 (+ tomorrow if needed)
            today_window = [p for p in today_prices if p['hour'] >= now.hour][:9]
            display_hours = today_window
            remaining = 9 - len(today_window)
            for p in tmr_prices[:max(0, remaining)]:
                display_hours.append({**p, 'tomorrow': True})

            self.price_data = {
                'current_price_kwh':   current_price,
                'spend_rate':          spend_rate,
                'next3':               next3,
                'next_green_hour':     next_green_hour,
                'next_green_price':    next_green_price,
                'next_green_tomorrow': next_green_tomorrow,
                'hourly_prices':       display_hours,
            }
            self.price_fetched_at   = now
            self.price_fetched_hour = now.hour
        except Exception:
            pass
        return self.price_data

    # ── Drawing helpers ────────────────────────────────────────────────────

    def _addstr(self, y, x, text, attr=0):
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        # Writing to the very last cell (h-1, w-1) raises an error in curses;
        # truncate one char short only on the last row.
        max_len = (w - x - 1) if y == h - 1 else (w - x)
        try:
            self.stdscr.addstr(y, x, text[:max_len], attr)
        except curses.error:
            pass

    def _price_color(self, price):
        if price is None:
            return curses.color_pair(2)
        if price <= PRICE_GREEN:
            return curses.color_pair(1)
        if price <= PRICE_YELLOW:
            return curses.color_pair(2)
        return curses.color_pair(3)

    # ── Main draw ──────────────────────────────────────────────────────────

    def draw(self, power, prices):
        h, w = self.stdscr.getmaxyx()
        now_hour      = datetime.now().hour
        current_price = prices.get('current_price_kwh')
        pcolor        = self._price_color(current_price)
        hourly        = prices.get('hourly_prices', [])

        power_ratio = min(1.0, power / self.max_power)
        if power_ratio < 0.6:
            bcolor = curses.color_pair(1)
        elif power_ratio < 0.8:
            bcolor = curses.color_pair(2)
        else:
            bcolor = curses.color_pair(3)

        # Fixed price scale — never adapts to current values
        PRICE_MIN = 0.0
        PRICE_MAX = 0.25  # €/kWh — covers full realistic Spanish range

        # Layout: chart = top half (row 0 = hour labels, rows 1..chart_h = graph)
        chart_total = max(3, h // 2)   # top half of terminal
        graph_top   = 1                 # row 0 reserved for hour labels
        graph_rows  = chart_total - 1   # number of graph rows
        base        = chart_total       # first info row

        n     = len(hourly)
        col_w = max(1, (w - 2) // n) if n else 1

        # ── Hour labels on top row ──
        for col, p in enumerate(hourly):
            x = 1 + col * col_w
            if x >= w - 1:
                break
            is_current = not p.get('tomorrow') and p['hour'] == now_hour
            label = f"{p['hour']:02d}" if col_w >= 2 else str(p['hour'])[-1]
            self._addstr(0, x, label[:col_w],
                         curses.A_BOLD | curses.A_REVERSE if is_current else curses.A_BOLD)

        # ── Price chart — pre-compute each column's fill start row ──
        span = PRICE_MAX - PRICE_MIN
        col_meta = []
        for col, p in enumerate(hourly):
            x = 1 + col * col_w
            if x >= w - 1:
                break
            is_current = not p.get('tomorrow') and p['hour'] == now_hour
            color = self._price_color(p['price_kwh'])
            attr  = color | curses.A_BOLD if is_current else color
            row_frac = min(1.0, max(0.0, (p['price_kwh'] - PRICE_MIN) / span))
            line_row = round((1.0 - row_frac) * (graph_rows - 1))
            line_row = max(0, min(line_row, graph_rows - 1))
            lc = '-' * max(1, min(col_w, w - x - 1))
            col_meta.append((x, attr, line_row, lc))

        # Draw row by row so every intermediate row is written explicitly
        for graph_row in range(graph_rows):
            for (x, attr, line_row, lc) in col_meta:
                if graph_row >= line_row:
                    self._addstr(graph_top + graph_row, x, lc, attr)

        # ── Info rows at bottom ──

        # Row 0 (base) — Consumption: value [|||||   ] pct%
        bar_w  = max(4, w - 18)
        filled = int(power_ratio * bar_w)
        bar    = '|' * filled + ' ' * (bar_w - filled)
        val_str = f"{power/1000:.2f}kW" if power >= 1000 else f"{int(power)}W"
        pct_str = f"{int(power_ratio * 100)}%"
        pow_line = f"{val_str} [{bar}] {pct_str}"
        self._addstr(base, 0, pow_line[:w - 1], bcolor | curses.A_BOLD)

        # Row 1 — Price + spend rate
        if current_price is not None:
            spend  = power / 1000 * current_price
            pr_str = f"Price:{current_price:.4f}€/kWh  Spend:{spend:.4f}€/h"
        else:
            pr_str = "Price: --"
        self._addstr(base + 1, 0, pr_str[:w - 1], pcolor | curses.A_BOLD)

        # Row 2 — Next 3 hours
        next3 = prices.get('next3', [])
        if next3:
            parts = [f"{p['hour']}h {p['price_kwh']:.3f}€" for p in next3]
            n3_str = "Next: " + " · ".join(parts)
        else:
            n3_str = "Next: --"
        self._addstr(base + 2, 0, n3_str[:w - 1], curses.A_BOLD)

        # Row 3 — Next green
        ng_hour  = prices.get('next_green_hour')
        ng_price = prices.get('next_green_price')
        ng_tmr   = prices.get('next_green_tomorrow', False)
        if ng_hour is not None:
            prefix = "tmr " if ng_tmr else ""
            ng_str = f"Green: {prefix}{ng_hour:02d}:00 {ng_price:.4f}€/kWh"
        elif ng_tmr:
            ng_str = "Green: tomorrow (TBD)"
        else:
            ng_str = "Green: none today"
        self._addstr(base + 3, 0, ng_str[:w - 1], curses.color_pair(1) | curses.A_BOLD)

    # ── Loop ───────────────────────────────────────────────────────────────

    def run(self):
        try:
            while True:
                power, _ = self.get_shelly()
                prices   = self.get_prices()

                # Accumulate cost: power (W) × 1s = Wh/3600 = kWh; × price = €
                current_price = prices.get('current_price_kwh')
                if current_price is not None and power > 0:
                    increment = (power / 1000 / 3600) * current_price  # € per second
                    self.daily_cost += increment

                # Persist every 30 s
                if (datetime.now() - self._last_save).total_seconds() >= 30:
                    self._save_costs()
                    self._last_save = datetime.now()

                self.stdscr.clear()
                self.draw(power, prices)
                self.stdscr.refresh()
                time.sleep(1)
        except KeyboardInterrupt:
            self._save_costs()
            curses.endwin()


if __name__ == "__main__":
    meter = PowerMeter()
    meter.run()
