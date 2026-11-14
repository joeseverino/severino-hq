"""audit:supports: what the browser gates' own engines answer to CSS.supports.

browser-compat-data (audit:bcd) says what each browser release supports; the
gates' WebKit is a trunk build ahead of any Safari release, so only asking it
says what the gates draw. Each condition is asked as CSS.supports reads one:
`property: value`, which it wraps in parentheses, or a whole condition
(`selector(:heading)`, `(a: b) and (c: d)`).
"""

import os
import sys

from playwright.sync_api import sync_playwright

ENGINES = {"webkit": ("webkit",), "chromium": ("chromium",), "gates": ("webkit", "chromium")}


def condition(text: str) -> str:
    """``text`` as CSS.supports's one argument."""

    text = text.strip()
    bare = ":" in text and not text.startswith(("(", "selector(", "not ", "font-tech(", "font-format("))
    return f"({text})" if bare else text


def main(engine: str, conditions: list[str]) -> None:
    asked = [condition(each) for each in conditions]
    options: dict[str, object] = {"headless": True}
    if channel := os.environ.get("HQ_BROWSER_CHANNEL"):
        options["channel"] = channel
    with sync_playwright() as driver:
        for name in ENGINES[engine]:
            browser = getattr(driver, name).launch(**options)
            try:
                page = browser.new_page()
                answers = page.evaluate("(asked) => asked.map((each) => CSS.supports(each))", asked)
            finally:
                browser.close()
            for text, answer in zip(conditions, answers, strict=True):
                print(f"{name} {browser.version}: {text}: {'yes' if answer else 'no'}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
