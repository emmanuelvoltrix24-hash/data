#!/usr/bin/env python3
"""
VFL Auto-Bettor
Two modes:
  MODE = 'api' — direct API call (fast, with human delay + UA spoofing)
  MODE = 'ui'  — full UI click-through via Playwright (slowest, most human-like)
"""
import asyncio, json, random, time
from datetime import datetime
from playwright.async_api import async_playwright
from pnl import log_bet

BET_URL    = 'https://api.betkraft.co.uk/v1/bet/league/550e8400-e29b-41d4-a716-446655440000'
IFRAME_URL = "https://ug.bandabets.com/iframe?IsDemo=0&providerID=55&gameName=Euro+Virtuals&gameID=550e8400-e29b-41d4-a716-446655440000"

STAKE_BASE = 50
DRY_RUN    = False
MODE       = 'ui'   # 'api' or 'ui'
HEADLESS   = False  # set True for production

HUMAN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

MOBILE_VIEWPORTS = [
    {'width': 390, 'height': 844},   # iPhone 14
    {'width': 412, 'height': 915},   # Pixel 7
    {'width': 414, 'height': 896},   # iPhone 11
    {'width': 393, 'height': 873},   # Pixel 6
]

def random_viewport():
    return random.choice(MOBILE_VIEWPORTS)

# market_id → UI tab label
MARKET_TAB_LABELS = {
    '1X2': '1X2', 'GG': 'GG', 'TG15': 'OV/UN 1.5', 'TG25': 'OV/UN 2.5',
    'DC': 'DC', 'TG35': 'OV/UN 3.5', 'H1X2': 'HT', 'DCH': 'DC (HT)',
    'HS': 'Half-Time Score', '1X2G': '1X2 & BTTS', '1X2OU15': '1X2 & OV/UN 1.5',
    '1X2OU25': '1X2 & OV/UN 2.5', '1X2OU35': '1X2 & OV/UN 3.5',
    '1X2OU45': '1X2 & OV/UN 4.5', '1X2OU55': '1X2 & OV/UN 5.5',
    'CS': 'Correct Score', 'DR': 'HT/FT', 'FTS': 'First Team to Score',
    'HGG': 'Goal:Goal Half Time', 'MG': 'Multi-Goals',
    'T1G': 'Team 1 Goal/No Goal', 'T1OU15': 'Team 1 OV/UN 1.5',
    'T2G': 'Team 2 Goal/No Goal', 'T2OU15': 'Team 2 OV/UN 1.5',
    'TFG': 'Time of First Goal', 'TG': 'Total Goals', 'TGOE': 'Total Goals Odd/Even',
}
async def random_click(page, element):
    """Move mouse naturally toward element then click a random point within it."""
    box = await element.bounding_box()
    if not box:
        await element.click()
        return

    # Target: random point within button
    tx = box['x'] + random.uniform(box['width'] * 0.2, box['width'] * 0.8)
    ty = box['y'] + random.uniform(box['height'] * 0.2, box['height'] * 0.8)

    # 40%: just click directly (no movement simulation)
    if random.random() < 0.4:
        await page.mouse.click(tx, ty)
        return

    # 60%: bezier curve path toward target
    vp = page.viewport_size or {'width': 390, 'height': 844}
    # Start near the target, constrained to avoid floating widgets in corners
    sx = tx + random.uniform(-60, 60)
    sy = ty + random.uniform(-80, -20)
    # Keep away from corners where chat widgets live
    sx = max(20, min(vp['width'] - 20, sx))
    sy = max(100, min(vp['height'] - 100, sy))

    # Quadratic bezier control point — offset perpendicular to the line
    mx, my = (sx + tx) / 2, (sy + ty) / 2
    perp_x = -(ty - sy)
    perp_y = tx - sx
    length = (perp_x**2 + perp_y**2) ** 0.5 or 1
    offset = random.uniform(-0.3, 0.3)  # curve intensity
    cx = mx + perp_x / length * offset * ((tx - sx)**2 + (ty - sy)**2) ** 0.5
    cy = my + perp_y / length * offset * ((tx - sx)**2 + (ty - sy)**2) ** 0.5

    steps = random.randint(6, 12)
    for i in range(1, steps + 1):
        t = i / steps
        # Quadratic bezier: B(t) = (1-t)²P0 + 2(1-t)tP1 + t²P2
        bx = (1-t)**2 * sx + 2*(1-t)*t * cx + t**2 * tx
        by = (1-t)**2 * sy + 2*(1-t)*t * cy + t**2 * ty
        await page.mouse.move(bx + random.uniform(-2, 2), by + random.uniform(-2, 2))
        await asyncio.sleep(random.uniform(0.01, 0.06))

    await page.mouse.click(tx, ty)


async def human_scroll(page, target_element=None):
    """Light, inconsistent scroll — 3 behaviours chosen randomly."""
    roll = random.random()
    if roll < 0.10:
        return  # 10%: no scroll, target already visible
    elif roll < 0.40:
        # 30%: scroll directly to target
        if target_element:
            await target_element.scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(0.2, 0.5))
    else:
        # 60%: casual browse scroll downward
        steps = random.randint(1, 3)
        for _ in range(steps):
            await page.mouse.wheel(0, random.uniform(80, 250))
            await asyncio.sleep(random.uniform(0.1, 0.4))


def human_stake():
    return random.choice([50, 52, 55, 50, 50, 53])

def human_delay():
    delay = random.uniform(8, 35)
    print(f"  [bet] Human delay {delay:.0f}s...")
    time.sleep(delay)

def human_mouse_delay():
    time.sleep(random.uniform(0.5, 2.0))


# ── API mode ──────────────────────────────────────────────────────────────────

async def get_api_context():
    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=HEADLESS)
    context = await browser.new_context(user_agent=HUMAN_UA, viewport=random_viewport())
    with open('bandabets_cookies.json') as f:
        await context.add_cookies(json.load(f))
    print("  [bet] Loading session...")
    page = await context.new_page()
    await page.goto(IFRAME_URL, wait_until='domcontentloaded', timeout=30000)
    await asyncio.sleep(10)
    await page.close()
    print("  [bet] Session ready")
    return p, browser, context

async def place_bet_api(context, event_id, market_id, outcome_id, stake, competition_id=1):
    payload = {
        'amount': stake,
        'data': [{'competition_id': competition_id, 'country_id': 0,
                  'event_id': event_id, 'market_id': market_id, 'outcome_id': outcome_id}]
    }
    print(f"  [bet] Payload: event={event_id} {market_id}:{outcome_id} stake={stake}")
    r = await context.request.post(BET_URL, data=json.dumps(payload),
        headers={'content-type': 'application/json', 'referer': 'https://legacy-ui.betkraft.co.uk/'})
    body = await r.text()
    print(f"  [bet] Response ({r.status}): {body[:200]}")
    data = json.loads(body)
    return data.get('status_code') == 200, data


# ── UI mode ───────────────────────────────────────────────────────────────────

async def place_bet_ui(event_id, market_id, outcome_id, stake, match_index=9):
    """Click through the betkraft UI to place a bet."""
    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=HEADLESS)
    vp = random_viewport()
    context = await browser.new_context(user_agent=HUMAN_UA, viewport=vp)
    print(f"  [ui] Viewport: {vp['width']}x{vp['height']}")
    with open('bandabets_cookies.json') as f:
        await context.add_cookies(json.load(f))

    page = await context.new_page()
    print("  [ui] Loading VFL page...")
    await page.goto(IFRAME_URL, wait_until='domcontentloaded', timeout=30000)
    await asyncio.sleep(15)

    # Dismiss any floating overlays (Zoho chat widget etc.)
    try:
        close_btns = await page.query_selector_all('[class*="close"], [aria-label*="close"], [title*="close"]')
        for btn in close_btns:
            if await btn.is_visible():
                await btn.click()
                await asyncio.sleep(0.5)
    except: pass

    bk_frame = next((f for f in page.frames if 'betkraft' in f.url or 'legacy-ui' in f.url), None)
    if not bk_frame:
        print("  [ui] ✗ Frame not found")
        await page.screenshot(path='data/ui_bet_debug.png')
        await browser.close(); await p.stop()
        return False, {}

    # Click the next future match tab (not LIVE, not active — the first upcoming one)
    try:
        time_tabs = await bk_frame.query_selector_all('li.next-game-time')
        print(f"  [ui] Found {len(time_tabs)} time tabs")
        if time_tabs:
            # Click first non-active future tab
            for tab in time_tabs:
                cls = await tab.get_attribute('class') or ''
                if 'active' not in cls:
                    txt = await tab.inner_text()
                    print(f"  [ui] Clicking future round tab: {txt}")
                    await tab.click()
                    await asyncio.sleep(3)
                    break
    except Exception as e:
        print(f"  [ui] Tab click error: {e}")

    # Wait for betting window — poll until .match-selection rows appear
    print("  [ui] Waiting for betting window...")
    for _ in range(30):  # up to 60s
        rows = await bk_frame.query_selector_all('.match-selection')
        if len(rows) >= 10:
            break
        await asyncio.sleep(2)
    else:
        print("  [ui] ✗ Betting window did not open in time")
        await page.screenshot(path='data/ui_bet_debug.png')
        await browser.close(); await p.stop()
        return False, {}

    print(f"  [ui] Betting window open — {len(rows)} matches")

    # Re-query rows fresh to avoid stale DOM references
    rows = await bk_frame.query_selector_all('.match-selection')

    # Browse scroll — simulate reading through matches
    await human_scroll(page)
    await asyncio.sleep(random.uniform(0.5, 1.5))

    try:
        target_row = rows[match_index]  # configurable match slot
        home = await (await target_row.query_selector('.home-team .teamname')).inner_text()
        away = await (await target_row.query_selector('.away-team .teamname')).inner_text()
        print(f"  [ui] M{match_index+1}: {home} vs {away}")

        # Switch to correct market tab if not 1X2
        if market_id != '1X2':
            tab_label = MARKET_TAB_LABELS.get(market_id, market_id)
            mkt_tabs = await bk_frame.query_selector_all('.marketstab button')
            for tab in mkt_tabs:
                if (await tab.inner_text()).strip() == tab_label:
                    await random_click(page, tab)
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    print(f"  [ui] Switched to market: {tab_label}")
                    # Re-query rows after market switch
                    rows = await bk_frame.query_selector_all('.match-selection')
                    target_row = rows[match_index]
                    break

        # Scroll to target match
        await human_scroll(page, target_row)
        await asyncio.sleep(random.uniform(0.3, 0.8))

        btns = await target_row.query_selector_all('.btn-option')
        # Find button by outcome_id label match
        target_btn = None
        for btn in btns:
            lbl = await (await btn.query_selector('.market')).inner_text()
            if lbl.strip() == outcome_id:
                target_btn = btn
                break
        if not target_btn:
            # Fallback: use index for 1X2
            btn_idx = {'1': 0, 'X': 1, '2': 2}.get(outcome_id, 0)
            target_btn = btns[btn_idx] if btn_idx < len(btns) else btns[0]
        label = await (await target_btn.query_selector('.market')).inner_text()
        odds_val = await (await target_btn.query_selector('.market-selection')).inner_text()
        print(f"  [ui] Clicking {label} @ {odds_val}")
        await random_click(page, target_btn)
        human_mouse_delay()
        await page.screenshot(path='data/ui_after_click.png')
    except Exception as e:
        print(f"  [ui] ✗ Match click error: {e}")
        await page.screenshot(path='data/ui_bet_debug.png')
        await browser.close(); await p.stop()
        return False, {}

    # Place Bet is an <a> tag inside .placebet-wrapper — opens betslip
    try:
        place_link = await bk_frame.query_selector('.mybetslip a, a:has-text("Place Bet")')
        if place_link:
            await asyncio.sleep(random.uniform(0.5, 1.2))
            await random_click(page, place_link)
            print("  [ui] Opened betslip")
            await asyncio.sleep(2)
        else:
            print("  [ui] ✗ Place Bet link not found")
            await page.screenshot(path='data/ui_bet_debug.png')
            await browser.close(); await p.stop()
            return False, {}
    except Exception as e:
        print(f"  [ui] Betslip open error: {e}")
        await browser.close(); await p.stop()
        return False, {}

    # Clear default stake and enter our stake
    try:
        stake_input = await page.query_selector('input[type="number"], input[type="text"]')
        if not stake_input:
            stake_input = await bk_frame.query_selector('input')
        if stake_input:
            await stake_input.click(click_count=3)  # select all
            await stake_input.fill(str(stake))
            print(f"  [ui] Entered stake {stake}")
            human_mouse_delay()
    except Exception as e:
        print(f"  [ui] Stake error (continuing): {e}")

    # Click the final Place Bet button in the betslip
    try:
        final_btn = await page.query_selector('button:has-text("Place Bet"), a:has-text("Place Bet")')
        if not final_btn:
            final_btn = await bk_frame.query_selector('button:has-text("Place Bet"), a:has-text("Place Bet")')
        if final_btn:
            await random_click(page, final_btn)
            print("  [ui] Clicked final Place Bet")
            await asyncio.sleep(3)
            await page.screenshot(path='data/ui_after_bet.png')
            print("  [ui] Screenshot: data/ui_after_bet.png")
            await browser.close(); await p.stop()
            return True, {'status_description': 'UI bet placed'}
        print("  [ui] ✗ Final Place Bet not found")
        await page.screenshot(path='data/ui_bet_debug.png')
    except Exception as e:
        print(f"  [ui] Final bet error: {e}")

    await browser.close(); await p.stop()
    return False, {}

    await browser.close()
    await p.stop()
    return True, {'status_description': 'UI bet placed'}


# ── Main entry ────────────────────────────────────────────────────────────────

async def bet_on_predictions(predictions):
    if not predictions:
        return

    ts = datetime.now().strftime('%H:%M:%S')
    print(f"\n[{ts}] {'DRY RUN' if DRY_RUN else f'🎯 BETTING [{MODE.upper()} mode]'} — {len(predictions)} bet(s)")
    print(f"  {'─'*60}")

    human_delay()

    if MODE == 'api' and not DRY_RUN:
        p, browser, context = await get_api_context()

    for pred in predictions:
        stake = human_stake()
        desc, sig = pred['match_desc'], pred['signal']
        eid, mkt, oid = pred['event_id'], pred['market_id'], pred['outcome_id']

        if DRY_RUN:
            print(f"  [DRY] {desc} | {sig} | {mkt}:{oid} stake={stake}")
            continue

        if MODE == 'api':
            ok, resp = await place_bet_api(context, eid, mkt, oid, stake)
        else:
            ok, resp = await place_bet_ui(eid, mkt, oid, stake)

        status = resp.get('status_description', str(resp))
        print(f"  {'✓' if ok else '✗'} {desc} | {sig} | stake={stake} | {status}")
        if ok:
            odds = pred.get('odds', '?')
            log_bet(eid, pred.get('round_id'), desc, mkt, oid, stake, odds, sig)

    if MODE == 'api' and not DRY_RUN:
        await browser.close()
        await p.stop()


if __name__ == '__main__':
    test_preds = [{
        'event_id': 12773232, 'market_id': '1X2', 'outcome_id': '1',
        'match_desc': 'Test Home vs Test Away', 'signal': '★M10:H(HIGH)',
    }]
    asyncio.run(bet_on_predictions(test_preds))
