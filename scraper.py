import os
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# --- Railway-friendly configuration ---
show_browser = os.getenv("SHOW_BROWSER", "false").lower() == "true"
csv_filename = os.getenv("CSV_PATH", "/data/results.csv")
ROTATE_INTERVAL_MINUTES = 120

# Make sure the CSV directory exists (volume mount)
csv_dir = os.path.dirname(csv_filename)
if csv_dir and not os.path.exists(csv_dir):
    os.makedirs(csv_dir, exist_ok=True)


class CsvHandler(BaseHTTPRequestHandler):
    """Serves the results CSV so it can be viewed/downloaded from the service's public URL."""

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/results.csv"):
            try:
                with open(csv_filename, "rb") as f:
                    body = f.read()
            except FileNotFoundError:
                body = b"timestamp,multiplier,color\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            if path == "/results.csv" and "download" in self.path:
                self.send_header("Content-Disposition", 'attachment; filename="results.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def start_csv_server():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), CsvHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"CSV available over HTTP on port {port} at /results.csv", flush=True)


def convert_rgb_to_name(rgb_string):
    if not rgb_string or rgb_string == "-":
        return "Black"
    clean_rgb = rgb_string.replace(" ", "").replace('"', '').replace("'", "").lower()
    if "119,107,0" in clean_rgb:
        return "Bronze-Green (5x+)"
    elif "119,79,0" in clean_rgb:
        return "Bronze-Orange (10x+)"
    elif "119,52,0" in clean_rgb:
        return "Deep-Orange (20x+)"
    elif "134,25,0" in clean_rgb:
        return "Red (50x+)"
    elif "98,3,9" in clean_rgb:
        return "Dark-Purple-Jackpot (150x+)"
    return "Black"


def initialize_new_browser_flow(playwright_instance, show_window):
    """Safely initializes an isolated mobile layout browser instance with stealth."""
    browser_obj = playwright_instance.chromium.launch(
        headless=not show_window,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    mobile_device = playwright_instance.devices['iPhone 13']
    context_obj = browser_obj.new_context(**mobile_device)
    page_obj = context_obj.new_page()

    page_obj.goto("https://www.simacombet.com/pocket-rocket", timeout=45000)

    try:
        page_obj.wait_for_selector('.n-i.n-i-close-a', timeout=10000)
        page_obj.wait_for_timeout(1500)
        page_obj.click('.n-i.n-i-close-a', delay=150)
    except Exception:
        pass

    outer_frame = page_obj.frame_locator("#NVNVPocketRocket")
    tap_element = outer_frame.get_by_text("Tap anywhere to start!")

    tap_element.wait_for(state="visible", timeout=25000)
    page_obj.wait_for_timeout(1500)
    tap_element.click(delay=150)
    page_obj.wait_for_timeout(5000)

    target_frame = next(
        (f for f in page_obj.frames if "games-client-pocket-rocket.nsoft.app" in f.url),
        None
    )

    if target_frame is None:
        try:
            browser_obj.close()
        except Exception:
            pass
        raise RuntimeError("Failed to locate target engine iframe stream.")

    if not show_window:
        def block_assets(route):
            if route.request.resource_type in ["image", "stylesheet", "media", "font"]:
                route.abort()
            else:
                route.continue_()
        page_obj.route("**/*", block_assets)

    return browser_obj, context_obj, page_obj, target_frame


if not os.path.exists(csv_filename):
    with open(csv_filename, "w", encoding="utf-8") as f:
        f.write("timestamp,multiplier,color\n")

start_csv_server()

with Stealth().use_sync(sync_playwright()) as p:
    print("Initial startup: Launching Primary Active Browser (Browser A)...")
    current_browser, current_context, current_page, active_history_frame = initialize_new_browser_flow(p, show_browser)
    print("Browser A successfully engaged and online.")

    last_processed_multipliers = []
    next_rotation_target = time.time() + (ROTATE_INTERVAL_MINUTES * 60)

    pending_browser = None
    pending_context = None
    pending_page = None
    pending_history_frame = None
    rotation_in_progress = False

    print("Starting infinite intersection-matching loop. Press Ctrl+C to stop.")

    with open(csv_filename, "a", encoding="utf-8") as csv_file:
        while True:
            current_time = time.time()

            # --- PHASE 1: PERIODIC TIME TRIGGER ---
            if current_time >= next_rotation_target and not rotation_in_progress:
                print(f"\n🔄 Rotation Interval Hit. Spinning up Background Browser (Browser B)...")
                try:
                    pending_browser, pending_context, pending_page, pending_history_frame = initialize_new_browser_flow(p, show_browser)
                    print("Browser B successfully initialized. Scanning for overlap anywhere in history...")
                    rotation_in_progress = True
                except Exception as b_err:
                    print(f"⚠️ Error opening Browser B: {b_err}. Purging artifacts and resetting timer.")
                    try:
                        if pending_page: pending_page.close()
                        if pending_context: pending_context.close()
                        if pending_browser: pending_browser.close()
                    except Exception:
                        pass
                    next_rotation_target = time.time() + (ROTATE_INTERVAL_MINUTES * 60)
                    rotation_in_progress = False
                    pending_browser = pending_context = pending_page = pending_history_frame = None

            # --- PHASE 2: PRIMARY ACTIVE SCRAPING LOOP ---
            try:
                history_list = active_history_frame.evaluate("""
                    () => {
                        const items = document.querySelectorAll('.rounds-history-inner-wrapper .history-item');
                        return items.length > 0 ? Array.from(items).map(h => ({
                            mult: (h.innerText || '').trim(),
                            bg: window.getComputedStyle(h).backgroundColor
                        })) : null;
                    }
                """)

                if history_list:
                    latest_live_item = history_list[0]["mult"].replace('x', '').strip()
                    latest_live_color = history_list[0]["bg"]

                    if not last_processed_multipliers:
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        color_name = convert_rgb_to_name(latest_live_color)
                        log_line = f"{timestamp},{latest_live_item},{color_name}\n"
                        print(log_line.strip())
                        csv_file.write(log_line)
                        csv_file.flush()
                        last_processed_multipliers.append({"mult": latest_live_item, "bg": latest_live_color})

                    last_known_top = last_processed_multipliers[-1]

                    match_index = -1
                    for idx, item in enumerate(history_list):
                        clean_m = item["mult"].replace('x', '').strip()
                        if clean_m == last_known_top["mult"] and item["bg"] == last_known_top["bg"]:
                            match_index = idx
                            break

                    if match_index > 0:
                        for i in range(match_index - 1, -1, -1):
                            new_mult = history_list[i]["mult"].replace('x', '').strip()
                            new_color = history_list[i]["bg"]

                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            color_name = convert_rgb_to_name(new_color)

                            log_line = f"{timestamp},{new_mult},{color_name}\n"
                            print(log_line.strip())
                            csv_file.write(log_line)
                            csv_file.flush()

                            last_processed_multipliers.append({"mult": new_mult, "bg": new_color})

                        if len(last_processed_multipliers) > 100:
                            last_processed_multipliers = last_processed_multipliers[-100:]

                # --- PHASE 3: DYNAMIC TIMELINE INTERSECTION SYNCHRONIZATION ---
                if rotation_in_progress and pending_history_frame:
                    res_b = pending_history_frame.evaluate("""
                        () => {
                            const items = document.querySelectorAll('.rounds-history-inner-wrapper .history-item');
                            return items.length > 0 ? Array.from(items).map(h => ({
                                mult: (h.innerText || '').trim(),
                                bg: window.getComputedStyle(h).backgroundColor
                            })) : null;
                        }
                    """)

                    if res_b and len(last_processed_multipliers) >= 3:
                        # Grab the last 3 items Browser A recorded to use as our "anchor signature"
                        a_sig = last_processed_multipliers[-3:]
                        a_sig_str = "|".join([f"{item['mult']}_{item['bg'].replace(' ', '')}" for item in a_sig])

                        sync_found = False
                        matched_offset = -1

                        # Scan Browser B's ribbon list to find WHERE this anchor signature intersects
                        for offset in range(len(res_b) - 2):
                            b_sig_str = "|".join([
                                f"{item['mult'].replace('x', '').strip()}_{item['bg'].replace(' ', '')}"
                                for item in res_b[offset:offset + 3]
                            ])

                            if b_sig_str == a_sig_str:
                                sync_found = True
                                matched_offset = offset
                                break

                        if sync_found:
                            print(f"🎯 Overlap Found at Browser B offset position [{matched_offset}]!")

                            # If matched_offset > 0, Browser B is further ahead.
                            # We catch up and write any missed intermediate values before dropping Browser A.
                            if matched_offset > 0:
                                print(f"📈 Recovering {matched_offset} missed rounds due to Browser A lag...")
                                for i in range(matched_offset - 1, -1, -1):
                                    missed_mult = res_b[i]["mult"].replace('x', '').strip()
                                    missed_color = res_b[i]["bg"]
                                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    color_name = convert_rgb_to_name(missed_color)
                                    log_line = f"{timestamp},{missed_mult},{color_name}\n"
                                    print(log_line.strip())
                                    csv_file.write(log_line)
                                    csv_file.flush()
                                    last_processed_multipliers.append({"mult": missed_mult, "bg": missed_color})

                            # Execute the browser swap instantly
                            old_page = current_page
                            old_context = current_context
                            old_browser = current_browser

                            current_browser = pending_browser
                            current_context = pending_context
                            current_page = pending_page
                            active_history_frame = pending_history_frame

                            print("Terminating old Browser A contexts instantly.")
                            try:
                                old_page.close()
                                old_context.close()
                                old_browser.close()
                            except Exception:
                                pass

                            rotation_in_progress = False
                            pending_browser = pending_context = pending_page = pending_history_frame = None
                            next_rotation_target = time.time() + (ROTATE_INTERVAL_MINUTES * 60)
                            print("🔄 Handoff Successful! Memory refreshed without data gaps.")
            except KeyboardInterrupt:
                print("\nScraping stopped by user.")
                break
            except Exception:
                pass
            current_page.wait_for_timeout(1)

    try:
        current_context.close()
        current_browser.close()
    except Exception:
        pass
