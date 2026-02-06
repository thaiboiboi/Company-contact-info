import re
import time
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

START_URL = "https://kbopub.economie.fgov.be/kbopub/zoeknummerform.html"

def normalize_kbo(n: str) -> str:
    digits = re.sub(r"\D+", "", str(n))
    if len(digits) == 9:
        digits = "0" + digits
    if len(digits) != 10:
        raise ValueError(f"Invalid Belgian enterprise number: {n}")
    return digits

def extract_from_detail_page(page) -> dict:
    """
    KBO pages are in Dutch/French; labels vary.
    We’ll parse by looking for common label keywords and reading the value next to them.
    Works best when the contact info is shown in a key/value table.
    """
    text = page.inner_text("body")

    def find_one(patterns):
        for pat in patterns:
            m = re.search(pat, text, flags=re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return ""

    # Loose patterns: these are intentionally tolerant of whitespace and separators.
    phone = find_one([
        r"(?:Tel\.?|Téléphone)\s*[:\-]\s*([+()\d][^\n\r]*)",
        r"(?:Telefoon)\s*[:\-]\s*([+()\d][^\n\r]*)",
    ])
    email = find_one([
        r"(?:E-?mail)\s*[:\-]\s*([^\s\n\r]+@[^\s\n\r]+)",
    ])
    website = find_one([
        r"(?:Website|Site web)\s*[:\-]\s*(https?://[^\s\n\r]+|www\.[^\s\n\r]+)",
    ])

    # Company name is usually prominent
    name = ""
    for sel in ["h1", "h2", "title"]:
        try:
            t = page.inner_text(sel).strip()
            if t and len(t) < 200:
                name = t
                break
        except Exception:
            pass

    return {
        "name": name,
        "phone": phone,
        "email": email,
        "website": website,
        "url": page.url,
    }

def maybe_wait_for_human_check(page):
    """
    If KBO shows a human-check/captcha page, pause so user can solve it.
    """
    body = ""
    try:
        body = page.inner_text("body").lower()
    except Exception:
        return

    if any(k in body for k in ["captcha", "human", "robot", "verify", "verifieer", "contrôle", "controle"]):
        print("\n⚠️ Human check detected. Please solve it in the browser window.")
        input("Press ENTER here after you finish the human check...")

def scrape_one(page, enterprise_number: str) -> dict:
    page.goto(START_URL, wait_until="domcontentloaded")
    maybe_wait_for_human_check(page)

    # Fill the enterprise number field.
    # The input id/name can change; we try a few common selectors.
    possible_inputs = [
        'input[name="nummer"]',
        'input#nummer',
        'input[type="text"]'
    ]

    input_found = False
    for sel in possible_inputs:
        try:
            page.wait_for_selector(sel, timeout=2000)
            page.fill(sel, enterprise_number)
            input_found = True
            break
        except PWTimeoutError:
            continue

    if not input_found:
        raise RuntimeError("Could not find the enterprise number input field on the KBO form.")

    # Submit: try common submit buttons
    possible_submit = [
        'input[type="submit"]',
        'button[type="submit"]',
        'button:has-text("Zoeken")',
        'button:has-text("Rechercher")',
        'input[value*="Zoek" i]',
        'input[value*="Recher" i]',
    ]

    submitted = False
    for sel in possible_submit:
        try:
            page.click(sel, timeout=2000)
            submitted = True
            break
        except Exception:
            continue

    if not submitted:
        # fallback: press Enter
        page.keyboard.press("Enter")

    page.wait_for_load_state("domcontentloaded")
    maybe_wait_for_human_check(page)

    # Sometimes you land on results list, sometimes directly on detail.
    # If there's a results table, click the first result.
    try:
        # Look for a link that contains the enterprise number
        link = page.locator(f'a:has-text("{enterprise_number}")').first
        if link.count() > 0:
            link.click()
            page.wait_for_load_state("domcontentloaded")
            maybe_wait_for_human_check(page)
    except Exception:
        pass

    # Now extract from detail page (best effort)
    data = extract_from_detail_page(page)
    return data

def main():
    # Input: a CSV with a column called enterprise_number OR a txt (one number per line)
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="companies.csv or companies.txt")
    ap.add_argument("--output", default="kbo_contacts.csv")
    ap.add_argument("--headless", action="store_true", help="Run headless (NOT recommended; more likely to trigger anti-bot).")
    ap.add_argument("--slowmo", type=int, default=80, help="Slow down actions (ms). Helps reduce bot triggers.")
    args = ap.parse_args()

    if args.input.lower().endswith(".csv"):
        df_in = pd.read_csv(args.input)
        if "enterprise_number" in df_in.columns:
            nums = df_in["enterprise_number"].tolist()
        else:
            # fallback: first column
            nums = df_in.iloc[:, 0].tolist()
    else:
        with open(args.input, "r", encoding="utf-8") as f:
            nums = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]

    nums = [normalize_kbo(n) for n in nums]

    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless, slow_mo=args.slowmo)
        context = browser.new_context()
        page = context.new_page()

        for i, n in enumerate(nums, start=1):
            print(f"[{i}/{len(nums)}] {n}")
            try:
                data = scrape_one(page, n)
                rows.append({"enterprise_number": n, **data})
            except Exception as e:
                rows.append({"enterprise_number": n, "name": "", "phone": "", "email": "", "website": "", "url": "", "error": str(e)})

            # polite pacing
            time.sleep(0.6)

        browser.close()

    df_out = pd.DataFrame(rows)
    df_out.to_csv(args.output, index=False)
    print(f"\nSaved: {args.output}")

if __name__ == "__main__":
    main()
