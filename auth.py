#!/usr/bin/env python3
"""
Auto-login for bandabets.
Uses login_tag timestamp from session cookie to detect expiry.
Auto-relogs via Playwright when needed.
"""
import os, json, time, requests, asyncio
from urllib.parse import unquote
from playwright.async_api import async_playwright

COOKIES_FILE = os.environ.get('COOKIES_FILE', os.path.join(os.path.dirname(__file__), 'bandabets_cookies.json'))
APIKEY_FILE  = os.environ.get('APIKEY_FILE',  os.path.join(os.path.dirname(__file__), 'bandabets_apikey.txt'))
WALLET_URL   = 'https://wallet.banda.software/balance?lang=en&country_code=ug'
VFL_URL      = 'https://ug.bandabets.com/iframe?IsDemo=0&providerID=55&gameName=Euro+Virtuals&gameID=550e8400-e29b-41d4-a716-446655440000'
SESSION_TTL  = 20 * 3600  # re-login after 20 hours

def _creds():
    # Railway: set BANDABETS_USER and BANDABETS_PASS as env vars
    # Local: reads from .env file
    env_file = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_file):
        for line in open(env_file):
            k, _, v = line.strip().partition('=')
            os.environ.setdefault(k, v)
    return os.environ.get('BANDABETS_USER'), os.environ.get('BANDABETS_PASS')

def load_cookies():
    with open(COOKIES_FILE) as f:
        return {c['name']: c['value'] for c in json.load(f)}

def _session_data():
    try:
        with open(COOKIES_FILE) as f:
            cookies = json.load(f)
        raw = next((c['value'] for c in cookies if c['name'] == 'session'), None)
        return json.loads(unquote(raw)) if raw else {}
    except:
        return {}

def get_balance():
    """Return wallet balance using api-key header."""
    try:
        api_key = open(APIKEY_FILE).read().strip() if os.path.exists(APIKEY_FILE) else None
        if not api_key:
            return None
        headers = {
            'api-key': api_key,
            'content-type': 'application/json',
            'Referer': 'https://ug.bandabets.com/',
            'Origin': 'https://ug.bandabets.com',
        }
        r = requests.get(WALLET_URL, headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None

def is_valid():
    """Session valid if login_tag < SESSION_TTL and balance is reachable."""
    sd = _session_data()
    if not sd.get('auth'):
        return False
    login_tag = int(sd.get('login_tag', 0))
    if time.time() - login_tag > SESSION_TTL:
        return False
    return get_balance() is not None

async def _login(username, password):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto('https://ug.bandabets.com/login', timeout=30000)
        await page.wait_for_selector('input[name="phone"]', timeout=10000)
        await page.fill('input[name="phone"]', username)
        await page.fill('input[type="password"]', password)
        await page.click('button[type="submit"]')
        await page.wait_for_timeout(5000)

        # Capture api-key from wallet/banda.software requests
        api_key = {}
        async def on_req(req):
            if 'banda.software' in req.url and req.headers.get('api-key'):
                api_key['value'] = req.headers['api-key']
        context.on('request', on_req)

        # Load VFL to trigger all auth requests
        await page.goto(VFL_URL, timeout=30000)
        await page.wait_for_timeout(8000)

        cookies = await context.cookies()
        with open(COOKIES_FILE, 'w') as f:
            json.dump(cookies, f, indent=2)

        if api_key.get('value'):
            with open(APIKEY_FILE, 'w') as f:
                f.write(api_key['value'])
            print(f"✓ API key captured")
        else:
            print("⚠ API key not captured — balance check may fail")

        await browser.close()
        print(f"✓ Auto-login done — {len(cookies)} cookies saved")
        return True

def ensure_session():
    """Re-login if session expired. Returns True if session is valid."""
    if is_valid():
        return True
    print("Session expired — logging in...")
    user, pwd = _creds()
    if not user or not pwd:
        print("✗ No credentials in .env")
        return False
    return asyncio.run(_login(user, pwd))

if __name__ == '__main__':
    ok = ensure_session()
    print("Session valid:", ok)
    print("Balance:", get_balance())
    sd = _session_data()
    print(f"User: {sd.get('first_name')} | msisdn: {sd.get('msisdn')}")
