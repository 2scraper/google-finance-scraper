"""Drive the three engines' `_fetch_one_page` with STUBBED drivers.

Run by smoke_test.py in a subprocess (the stubs replace the driver modules
in `sys.modules`, which must not leak into the suite's own real-engine
checks). Needs no engine library installed, so it runs in CI's offline job.
Prints one JSON object of results.

  * pyppeteer: a navigation failure with NO proxy pool must not crash
    (it once raised AttributeError on `pool.advance`);
  * pyppeteer and Selenium classify with the run's --mode, so a markets,
    movers or earnings page is `content`, not `empty`;
  * Playwright: a solver that FAILS is still called at most SOLVES_PER_PAGE
    times per page, with and without a pool.
"""
import sys, types, json, os, argparse, tempfile, io, contextlib, logging
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import atexit, shutil
_work = tempfile.mkdtemp(prefix="gf-stub-")
atexit.register(shutil.rmtree, _work, True)
os.chdir(_work)
logging.disable(logging.CRITICAL)
# --- stub driver modules ---
def mod(name, **kw):
    m = types.ModuleType(name); m.__dict__.update(kw); sys.modules[name] = m; return m
class PWError(Exception): pass
class PWTimeout(PWError): pass
mod("playwright"); mod("playwright.sync_api", sync_playwright=lambda: None, Error=PWError, TimeoutError=PWTimeout)
mod("pyppeteer", launch=None, connect=None)
class WDE(Exception): pass
mod("selenium"); mod("selenium.webdriver", Chrome=None, ChromeOptions=None); sys.modules["selenium"].webdriver = sys.modules["selenium.webdriver"]
mod("selenium.common"); mod("selenium.common.exceptions", TimeoutException=WDE, WebDriverException=WDE)
mod("selenium.webdriver.chrome"); mod("selenium.webdriver.chrome.options", Options=object)
mod("selenium.webdriver.common"); mod("selenium.webdriver.common.by", By=types.SimpleNamespace(CSS_SELECTOR="css"))
import page_flow, time
time.sleep = lambda s: None
fx = json.load(open(os.path.join(REPO, "fixtures_generated.json")))["fixtures"]
def args(mode="quote", **kw):
    a = argparse.Namespace(mode=mode, pages=1, retries=1, retry_delay=0.0, out="probe", market=None,
        category=None, solve_captcha="when-blocked", twocaptcha_key="x"*32, captcha_api="v2",
        min_score=0.7, proxy_block_retries=3, dump_html=None, headless=True, cdp_endpoint=None, locale=None)
    a.__dict__.update(kw); return a
results = {}
_out = sys.stdout
sys.stdout = io.StringIO()
# ---- 1. pyppeteer no-pool load failure ----
import puppeteer_scraper as pp
class Bridge:
    def __init__(s, html=None, fail=False): s.html, s.fail = html, fail
    def run(s, x):
        if x == "GOTO":
            if s.fail: raise Exception("Navigation Timeout Exceeded: 60000 ms exceeded")
            return types.SimpleNamespace(status=200)
        if x == "CONTENT": return s.html
        if x == "QSA": return []
        return x
class PPage:
    url = "https://www.google.com/finance/"
    def goto(s, *a, **k): return "GOTO"
    def content(s): return "CONTENT"
    def querySelectorAll(s, sel): return "QSA"
    def evaluate(s, *a): return None
class PSess:
    def __init__(s, **kw): s.bridge = Bridge(**kw); s.page = PPage()
    def relaunch(s): pass
try:
    o = pp._fetch_one_page(PSess(fail=True), args(), None, 1, "https://www.google.com/finance/quote/GOOGL:NASDAQ")
    results["pyppeteer_no_pool_load_failure"] = "ok load_failed=%s" % o.load_failed
except Exception as e:
    results["pyppeteer_no_pool_load_failure"] = "CRASH %s: %s" % (type(e).__name__, e)
# ---- 2. mirror classify mode ----
pp.handle_captcha_if_present = lambda *a, **k: False
for name, mode in (("markets_us", "markets"), ("movers_us", "movers"), ("earnings_us", "earnings")):
    f = fx[name]; PPage.url = f.get("url", PPage.url)
    o = pp._fetch_one_page(PSess(html=f["html"]), args(mode=mode), None, 1, f.get("url"))
    results["pyppeteer state %s" % mode] = o.state
import selenium_scraper as se
se.handle_captcha_if_present = lambda *a, **k: False
class Drv:
    def __init__(s, html, url): s.page_source, s.current_url = html, url
    def get(s, url): pass
    def find_elements(s, *a): return []
class SSess:
    def __init__(s, html, url): s.driver = Drv(html, url)
    def relaunch(s): pass
for name, mode in (("markets_us", "markets"), ("movers_us", "movers"), ("earnings_us", "earnings")):
    f = fx[name]
    o = se._fetch_one_page(SSess(f["html"], f.get("url")), args(mode=mode), None, 1, f.get("url"))
    results["selenium state %s" % mode] = o.state
# ---- 3. solve budget, Playwright, failing solver, forced detection ----
import playwright_scraper as pw
calls = {"n": 0}
def bad_solver(*a, **k):
    calls["n"] += 1; raise RuntimeError("ERROR_CAPTCHA_UNSOLVABLE")
pw.solve_recaptcha = bad_solver
pw.reconcile_detections = lambda a, b: types.SimpleNamespace(kind="recaptcha_v2", source="stub", sitekey="k", action=None)
pw.detect_recaptcha_v3 = lambda *a, **k: None
pw.detect_recaptcha_in_page = lambda *a, **k: None
pw._content_when_settled = lambda page: "<html>challenge</html>"
class WPage:
    url = "https://www.google.com/finance/quote/GOOGL:NASDAQ"
    def goto(s, *a, **k): return types.SimpleNamespace(status=200)
    def query_selector_all(s, sel): return []
    def wait_for_timeout(s, ms): pass
    def evaluate(s, *a): return None
    def reload(s, **k): pass
class WSess:
    def __init__(s): s.page = WPage()
    def relaunch(s): pass
class Pool:
    current = "http://h:1"
    def __len__(s): return 3
    def advance(s, why): pass
for st in ("challenge", "blocked"):
    for label, pool in (("no pool", None), ("3-exit pool", Pool())):
        calls["n"] = 0
        pw._classify = lambda page, html, status=None, mode="quote", st=st: st
        try:
            pw._fetch_one_page(WSess(), args(), pool, 1, WPage.url)
        except Exception as e:
            results["budget %s %s" % (st, label)] = "CRASH %r" % e; continue
        results["budget %s %s" % (st, label)] = "solver calls=%d (limit %d)" % (calls["n"], page_flow.SOLVES_PER_PAGE)
# ---- 4. a GOOGLE_FINANCE_URL in the environment must not refuse --symbols ----
os.environ["GOOGLE_FINANCE_URL"] = "https://www.google.com/finance/quote/GOOGL:NASDAQ"
for name, eng in (("playwright", pw), ("pyppeteer", pp), ("selenium", se)):
    for label, argv in (("symbols", ["--symbols", "MSFT:NASDAQ"]),
                        ("symbols+typed url", ["--symbols", "MSFT:NASDAQ", "--url",
                         "https://www.google.com/finance/quote/GOOGL:NASDAQ"])):
        sys.argv = [name + "_scraper.py"] + argv
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                a = eng.parse_args()
            results["env url %s %s" % (name, label)] = "ok url=%s" % a.url
        except SystemExit as e:
            results["env url %s %s" % (name, label)] = "exit %s" % e.code
_out.write(json.dumps(results) + "\n")
