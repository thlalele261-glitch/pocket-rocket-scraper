import os
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# --- Railway-friendly configuration ---
show_browser = os.getenv("SHOW_BROWSER", "false").lower() == "true"
csv_filename = os.getenv("CSV_PATH", "/data/results.csv")
# Lesotho time (SAST, UTC+2, no daylight saving)
LESOTHO_TZ = timezone(timedelta(hours=2))

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
            "--disable-extensions",
            "--disable-background-networking",
            "--mute-audio",
        ],
    )
    try:
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
    except Exception:
        # Never leave a half-started browser behind: memory is tight.
        try:
            browser_obj.close()
        except Exception:
            pass
        raise


# One-time clean start: change RESET_ID to wipe the CSV once on the next deploy.
RESET_ID = "2026-11-07-single-browser"
reset_marker = os.path.join(csv_dir or ".", ".reset_id")
previous_reset = open(reset_marker).read().strip() if os.path.exists(reset_marker) else None
if previous_reset != RESET_ID:
    if os.path.exists(csv_filename):
        os.remove(csv_filename)
        print("Previous results.csv deleted for a clean start.", flush=True)
    with open(reset_marker, "w") as f:
        f.write(RESET_ID)

if not os.path.exists(csv_filename):
    with open(csv_filename, "w", encoding="utf-8") as f:
        f.write("timestamp,multiplier,color\n")

# --- Watchdog: restart the container if scraping silently stalls ---
# The main loop swallows errors, and a crashed/frozen browser can make it hang forever
# without exiting, so Railway never restarts it. This thread force-exits so Railway restarts it.
HEARTBEAT_TIMEOUT_SECONDS = 120    # no successful read of the history list
NO_NEW_ROW_TIMEOUT_SECONDS = 600   # no new row written to the CSV
heartbeat = {"ok": time.time()}


def watchdog():
    last_size = os.path.getsize(csv_filename) if os.path.exists(csv_filename) else 0
    last_growth = time.time()
    while True:
        time.sleep(15)
        now = time.time()
        size = os.path.getsize(csv_filename) if os.path.exists(csv_filename) else 0
        if size != last_size:
            last_size, last_growth = size, now
        if now - heartbeat["ok"] > HEARTBEAT_TIMEOUT_SECONDS:
            print(f"WATCHDOG: browser unresponsive for {int(now - heartbeat['ok'])}s. Restarting.", flush=True)
            os._exit(1)
        if now - last_growth > NO_NEW_ROW_TIMEOUT_SECONDS:
            print(f"WATCHDOG: no new rows for {int(now - last_growth)}s. Restarting.", flush=True)
            os._exit(1)


start_csv_server()

HISTORY_JS = """
() => {
    const items = document.querySelectorAll('.rounds-history-inner-wrapper .history-item');
    return items.length > 0 ? Array.from(items).map(h => ({
        mult: (h.innerText || '').trim(),
        bg: window.getComputedStyle(h).backgroundColor
    })) : null;
}
"""

# Restart the (single) browser after this many seconds of continuous read errors.
BROWSER_ERROR_RESTART_SECONDS = 20
RELAUNCH_RETRY_SECONDS = 10


def close_browser_quietly(browser_obj):
    try:
        if browser_obj:
            browser_obj.close()
    except Exception:
        pass


def now_str():
    return datetime.now(LESOTHO_TZ).strftime("%Y-%m-%d %H:%M:%S")


with Stealth().use_sync(sync_playwright()) as p:
    # Only ONE browser is ever open (memory is limited). If it crashes or stops
    # responding it is closed and a fresh one is started; the CSV is never touched.
    browser = page = frame = None
    page_state = {"crashed": False}
    last_processed_multipliers = []
    first_error_time = None
    last_error_log = 0
    browser_launches = 0

    print("Starting scraper. Press Ctrl+C to stop.", flush=True)

    with open(csv_filename, "a", encoding="utf-8") as csv_file:

        def write_row(mult, bg):
            color_name = convert_rgb_to_name(bg)
            log_line = f"{now_str()},{mult},{color_name}\n"
            print(log_line.strip(), flush=True)
            csv_file.write(log_line)
            csv_file.flush()
            last_processed_multipliers.append({"mult": mult, "bg": bg})

        threading.Thread(target=watchdog, daemon=True).start()

        while True:
            try:
                # --- (Re)start the single browser when there is none ---
                if browser is None:
                    heartbeat["ok"] = time.time()  # launching can take a while
                    browser_launches += 1
                    print(f"Launching browser (start #{browser_launches})...", flush=True)
                    try:
                        browser, _context, page, frame = initialize_new_browser_flow(p, show_browser)
                    except Exception as launch_err:
                        browser = page = frame = None
                        print(f"Browser launch failed: {str(launch_err)[:200]}. Retrying in {RELAUNCH_RETRY_SECONDS}s.", flush=True)
                        time.sleep(RELAUNCH_RETRY_SECONDS)
                        continue
                    page_state["crashed"] = False
                    page.on("crash", lambda *_: page_state.update(crashed=True))
                    first_error_time = None
                    heartbeat["ok"] = time.time()
                    print("Browser online.", flush=True)

                if page_state["crashed"] or page.is_closed() or not browser.is_connected():
                    raise RuntimeError("browser or page crashed")

                history_list = frame.evaluate(HISTORY_JS)
                first_error_time = None

                if history_list:
                    heartbeat["ok"] = time.time()
                    latest_mult = history_list[0]["mult"].replace("x", "").strip()
                    latest_bg = history_list[0]["bg"]

                    match_index = -1
                    if last_processed_multipliers:
                        last_known_top = last_processed_multipliers[-1]
                        for idx, item in enumerate(history_list):
                            clean_m = item["mult"].replace("x", "").strip()
                            if clean_m == last_known_top["mult"] and item["bg"] == last_known_top["bg"]:
                                match_index = idx
                                break

                    if not last_processed_multipliers:
                        # First read ever: record the latest round as the anchor.
                        write_row(latest_mult, latest_bg)
                    elif match_index == -1:
                        # Gap longer than the visible history (e.g. after a browser restart): re-anchor.
                        print("Resync: previous round no longer in history, re-anchoring.", flush=True)
                        write_row(latest_mult, latest_bg)
                    elif match_index > 0:
                        # Write every round newer than the last one we recorded, oldest first.
                        for i in range(match_index - 1, -1, -1):
                            write_row(history_list[i]["mult"].replace("x", "").strip(), history_list[i]["bg"])

                    if len(last_processed_multipliers) > 100:
                        last_processed_multipliers = last_processed_multipliers[-100:]

            except KeyboardInterrupt:
                print("\nScraping stopped by user.")
                break
            except Exception as loop_err:
                now_err = time.time()
                if now_err - last_error_log > 30:
                    print(f"Scrape loop error: {str(loop_err)[:200]}", flush=True)
                    last_error_log = now_err
                if first_error_time is None:
                    first_error_time = now_err
                browser_dead = (
                    page_state["crashed"]
                    or "crashed" in str(loop_err)
                    or "closed" in str(loop_err).lower()
                )
                if browser_dead or now_err - first_error_time > BROWSER_ERROR_RESTART_SECONDS:
                    print("Browser crashed or unresponsive. Closing it and starting a fresh one (data kept).", flush=True)
                    close_browser_quietly(browser)
                    browser = page = frame = None

            time.sleep(0.3)

    close_browser_quietly(browser)
