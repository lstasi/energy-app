import curses
import time
import requests
from config import EM_URL, EM_USER, EM_PASS

class PowerMeter:
    def __init__(self):
        self.stdscr = curses.initscr()
        curses.start_color()
        curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.init_pair(3, curses.COLOR_RED, curses.COLOR_BLACK)
        curses.curs_set(0)
        self.max_power = 5000
        # Set terminal size for 480x320 display (approximately 40x15 characters)
        print('\x1b[8;15;40t')

    def get_power(self):
        try:
            response = requests.get(EM_URL, auth=(EM_USER, EM_PASS))
            data = response.json()
            return int(data.get("power", 0))
        except:
            return 0

    def draw_bar(self, power):
        height, width = self.stdscr.getmaxyx()
        bar_width = width - 6  # Increase usable width
        power_ratio = power / self.max_power
        filled_width = int(bar_width * power_ratio)
        
        # Draw title and border
        self.stdscr.box()
        title = "Power Monitor"
        self.stdscr.addstr(0, (width - len(title)) // 2, title)
        
        # Draw multiple power bars for better visualization
        for i in range(6):
            if power_ratio < 0.6:
                color = curses.color_pair(1)
            elif power_ratio < 0.8:
                color = curses.color_pair(2)
            else:
                color = curses.color_pair(3)
            self.stdscr.addstr(i+2, 3, f"{'█' * filled_width}{' ' * (bar_width - filled_width)}", color)
        
        # Format and center power value
        if power >= 1000:
            power_str = f"{power/1000:.2f} kW / {self.max_power/1000:.2f} kW"
        else:
            power_str = f"{int(power)} W / {self.max_power} W"
        
        # Add timestamp and centered power value
        timestamp = time.strftime("%H:%M:%S")
        self.stdscr.addstr(10, (width - len(power_str)) // 2, power_str, color)
        self.stdscr.addstr(height-2, 2, f"Updated: {timestamp}")

    def run(self):
        try:
            while True:
                power = self.get_power()
                self.stdscr.clear()
                self.draw_bar(power)
                self.stdscr.refresh()
                time.sleep(1)
        except KeyboardInterrupt:
            curses.endwin()

if __name__ == "__main__":
    meter = PowerMeter()
    meter.run()