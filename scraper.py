import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

# --- Railway-friendly configuration ---
show_browser = os.getenv("SHOW_BROWSER", "false").lower() == "true"
csv_filename = os.getenv("CSV_PATH", "/data/results.csv")
LESOTHO_TZ = timezone(timedelta(hours=2))

csv_dir = os.path.dirname(csv_filename)
if csv_dir and not os.path.exists(csv_dir):
    os.makedirs(csv_dir, exist_ok=True)


class CsvHandler(BaseHTTPRequestHandler):
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
    """Launch a mobile Chromium and navigate to the game iframe.

    NOTE: No playwright-stealth — it hooks into every page call and was
    intermittently freezing frame.evaluate() on this container, which is what
    caused the watchdog to kill the container every ~15 minutes.
    """
    browser_obj = playwright_instance.chromium.launch(
        headless=not show_window,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-features=IsolateOrigins,site-per-process",
            "--renderer-process-limit=1",
            "--js-flags=--max-old-space-size=256",
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
        try:
            browser_obj.close()
        except Exception:
            pass
        raise


# --- One-time clean start ---
RESET_ID = "2026-11-08-no-stealth"
reset_marker = os.path.join(csv_dir or ".", ".reset_id")
previous_reset = open(reset_marker).read().strip() if os.path.exists(reset_marker) else None
if previous_reset != RESET_ID:
    with open(reset_marker, "w") as f:
        f.write(RESET_ID)

if not os.path.exists(csv_filename):
    with open(csv_filename, "w", encoding="utf-8") as f:
        f.write("timestamp,multiplier,color\n")


# --- Watchdog: request a soft browser recycle instead of killing the container ---
HEARTBEAT_TIMEOUT_SECONDS = 60
NO_NEW_ROW_TIMEOUT_SECONDS = 600

heartbeat = {"ok": time.time()}
recycle_requested = {"flag": False}


def watchdog():
    last_size = os.path.getsize(csv_filename) if os.path.exists(csv_filename) else 0
    last_growth = time.time()
    while True:
        time.sleep(10)
        now = time.time()
        size = os.path.getsize(csv_filename) if os.path.exists(csv_filename) else 0
        if size != last_size:
            last_size, last_growth = size, now
        if now - heartbeat["ok"] > HEARTBEAT_TIMEOUT_SECONDS:
            print(f"WATCHDOG: no heartbeat for {int(now - heartbeat['ok'])}s. Requesting browser recycle.", flush=True)
            recycle_requested["flag"] = True
            heartbeat["ok"] = now  # reset so we don't spam
        if now - last_growth > NO_NEW_ROW_TIMEOUT_SECONDS:
            print(f"WATCHDOG: no new rows for {int(now - last_growth)}s. Requesting browser recycle.", flush=True)
            recycle_requested["flag"] = True
            last_growth = now


start_csv_server()

HISTORY_JS = """
() => {
    const wrapper = document.querySelector('.rounds-history-inner-wrapper');
    if (!wrapper) return null;
    const items = wrapper.querySelectorAll('.history-item');
    if (items.length === 0) return null;
    const limit = Math.min(items.length, 25);
    const out = [];
    for (let i = 0; i < limit; i++) {
        const h = items[i];
        let bg = h.style.backgroundColor;
        if (!bg) {
            try { bg = window.getComputedStyle(h).backgroundColor; } catch (e) { bg = '-'; }
        }
        out.push({ mult: (h.innerText || '').trim(), bg: bg });
    }
    return out;
}
"""

# Runs frame.evaluate() in a worker thread so a hung iframe cannot block the main loop.
_eval_pool = ThreadPoolExecutor(max_workers=1)


def safe_evaluate(frame, script, timeout=15):
    future = _eval_pool.submit(frame.evaluate, script)
    try:
        return future.result(timeout=timeout)
    except FutureTimeout:
        raise RuntimeError("evaluate() timeout — browser hung")


BROWSER_ERROR_RESTART_SECONDS = 20
NO_MULTIPLIER_RESTART_SECONDS = 180
RELAUNCH_RETRY_SECONDS = 10
MAX_TRACKED_ROWS = 100


def close_browser_quietly(browser_obj):
    try:
        if browser_obj:
            browser_obj.close()
    except Exception:
        pass


def now_str():
    return datetime.now(LESOTHO_TZ).strftime("%Y-%m-%d %H:%M:%S")


def load_recent_rows_from_csv():
    rows = []
    try:
        with open(csv_filename, "r", encoding="utf-8") as f:
            for line in f.read().splitlines()[1:]:
                parts = line.split(",", 2)
                if len(parts) == 3:
                    rows.append((parts[1], parts[2]))
    except FileNotFoundError:
        pass
    return rows[-MAX_TRACKED_ROWS:]


def find_new_rows(recorded, history_oldest_first):
    best_end, best_len = None, 0
    for end in range(len(history_oldest_first) - 1, -1, -1):
        length = 0
        while (
            length < len(recorded)
            and end - length >= 0
            and history_oldest_first[end - length] == recorded[-1 - length]
        ):
            length += 1
        if length == 0:
            continue
        if length == end + 1 or length == len(recorded) or length >= 3:
            if length > best_len:
                best_end, best_len = end, length
    if best_end is None:
        return None, False
    return history_oldest_first[best_end + 1:], True


with sync_playwright() as p:
    browser = page = frame = None
    page_state = {"crashed": False}
    recorded_rows = load_recent_rows_from_csv()
    first_error_time = None
    last_error_log = 0
    last_new_row_time = time.time()
    browser_launches = 0
    needs_initial_sync = True

    print(f"Starting scraper with {len(recorded_rows)} rows already on file. Press Ctrl+C to stop.", flush=True)

    with open(csv_filename, "a", encoding="utf-8") as csv_file:

        def write_row(mult, color_name):
            global last_new_row_time
            log_line = f"{now_str()},{mult},{color_name}\n"
            print(log_line.strip(), flush=True)
            csv_file.write(log_line)
            csv_file.flush()
            recorded_rows.append((mult, color_name))
            last_new_row_time = time.time()

        threading.Thread(target=watchdog, daemon=True).start()

        while True:
            try:
                # --- Watchdog requested a soft recycle ---
                if recycle_requested["flag"]:
                    recycle_requested["flag"] = False
                    print("Recycling browser per watchdog request.", flush=True)
                    close_browser_quietly(browser)
                    browser = page = frame = None
                    continue

                # --- (Re)start the single browser when there is none ---
                if browser is None:
                    heartbeat["ok"] = time.time()
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
                    needs_initial_sync = True
                    last_new_row_time = time.time()
                    heartbeat["ok"] = time.time()
                    print("Browser online.", flush=True)

                if page_state["crashed"] or page.is_closed() or not browser.is_connected():
                    raise RuntimeError("browser or page crashed")

                if time.time() - last_new_row_time > NO_MULTIPLIER_RESTART_SECONDS:
                    print(f"No new multiplier for {NO_MULTIPLIER_RESTART_SECONDS}s. Recycling browser.", flush=True)
                    close_browser_quietly(browser)
                    browser = page = frame = None
                    continue

                history_list = safe_evaluate(frame, HISTORY_JS, timeout=15)
                first_error_time = None

                if history_list:
                    heartbeat["ok"] = time.time()
                    visible = [
                        (item["mult"].replace("x", "").strip(), convert_rgb_to_name(item["bg"]))
                        for item in reversed(history_list)
                    ]

                    if not recorded_rows:
                        print(f"Empty file: writing all {len(visible)} visible rounds.", flush=True)
                        for mult, color_name in visible:
                            write_row(mult, color_name)
                        needs_initial_sync = False
                    else:
                        new_rows, overlap_found = find_new_rows(recorded_rows, visible)
                        if overlap_found:
                            if needs_initial_sync and new_rows:
                                print(f"Recovered {len(new_rows)} round(s) missed while the browser was down.", flush=True)
                            for mult, color_name in new_rows:
                                write_row(mult, color_name)
                            needs_initial_sync = False
                        elif needs_initial_sync:
                            print(f"No overlap after restart: writing all {len(visible)} visible rounds.", flush=True)
                            for mult, color_name in visible:
                                write_row(mult, color_name)
                            needs_initial_sync = False

                    if len(recorded_rows) > MAX_TRACKED_ROWS:
                        del recorded_rows[:-MAX_TRACKED_ROWS]

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
                    or "crashed" in str(loop_err).lower()
                    or "closed" in str(loop_err).lower()
                    or "timeout" in str(loop_err).lower()
                )
                if browser_dead or now_err - first_error_time > BROWSER_ERROR_RESTART_SECONDS:
                    print("Browser crashed or unresponsive. Recycling (data kept).", flush=True)
                    close_browser_quietly(browser)
                    browser = page = frame = None

            time.sleep(0.5)

    close_browser_quietly(browser)
