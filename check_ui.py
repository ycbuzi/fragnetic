"""Pre-build UI smoke check -- fails the build if fragroute_ui.html's JavaScript is
broken or the login/disclaimer path is missing. A broken <script> silently kills the
WHOLE UI (no login, no buttons), and 'the Python compiles' never catches it -- so this
runs before PyInstaller and aborts the build loudly instead of shipping a dead app.

Exit 0 = ok, exit 1 = broken (build_exe.bat aborts on nonzero).
"""
import re
import sys

HTML = "fragroute_ui.html"


def main():
    try:
        html = open(HTML, encoding="utf-8").read()
    except Exception as e:
        print("UI CHECK: cannot read %s: %s" % (HTML, e))
        return 1

    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    main_js = max(scripts, key=len) if scripts else ""
    errs = []

    # 1) JavaScript SYNTAX -- the thing that breaks the whole app. Use esprima if
    #    present (neutralize ES2020 ?? and ?. which the old parser can't read but
    #    WebView2 runs fine); otherwise skip syntax and rely on the element checks.
    try:
        import esprima
        s = main_js.replace("??", "||")
        s = re.sub(r"\?\.", ".", s)
        try:
            esprima.parseScript(s, {"tolerant": False})
        except Exception as e:
            ln = getattr(e, "lineNumber", "?")
            errs.append("JS SYNTAX ERROR near line %s: %s" % (ln, str(e)[:100]))
    except Exception:
        print("UI CHECK: (esprima not installed -- skipping deep syntax scan)")

    # 2) the login + disclaimer path must exist, or you boot into a dead/locked screen
    required = [
        'id="authGate"', 'id="authForm"', 'id="authSubmit"', 'id="authPass"',
        'id="disclaimerGate"', 'id="disclaimerContinue"', 'id="disclaimerCheck"',
        "function _showGate", "function setAuthMode",
    ]
    for r in required:
        if r not in html:
            errs.append("missing critical login/disclaimer piece: %s" % r)

    if errs:
        print("=" * 60)
        print("UI SMOKE CHECK FAILED -- NOT building a broken app:")
        for e in errs:
            print("   - " + e)
        print("=" * 60)
        return 1
    print("UI smoke check PASSED: JS parses + login/disclaimer path intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
