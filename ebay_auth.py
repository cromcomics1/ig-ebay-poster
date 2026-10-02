"""
One-time sign-in that lets the relist tool set Promoted Listings ad rates.

You paste the web address eBay sends you to after "Test Sign-In" (it contains a
one-time code). This script swaps the code for a long-lived advertising token
and stores it as the EBAY_REFRESH_TOKEN secret. The token is never printed.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO = os.environ["GITHUB_REPOSITORY"]


def post_form(url, form, headers):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"eBay refused the code (HTTP {e.code}): {e.read().decode(errors='replace')[:300]}\n"
                 "Codes expire after 5 minutes and work once. Do Test Sign-In again and paste the new address.")


def gh(method, path, body=None):
    headers = {"Authorization": f"Bearer {os.environ['GH_PAT']}", "Accept": "application/vnd.github+json",
               "User-Agent": "ig-ebay-poster"}
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub refused to save the secret (HTTP {e.code}). Check that the GH_PAT secret exists and "
                 "has Secrets: Read and write permission for this repository.")


def main():
    if not os.environ.get("GH_PAT"):
        sys.exit("The GH_PAT secret is missing. Add it first (GitHub fine-grained token with Secrets: Read and write).")
    pasted = os.environ.get("PASTED", "").strip()
    runame = os.environ.get("RUNAME", "").strip()
    if not pasted or not runame:
        sys.exit("Both boxes are required: the address you were sent to, and your RuName.")
    # accept either the whole address or just the code
    if "code=" in pasted:
        query = urllib.parse.urlparse(pasted).query or pasted.split("?", 1)[-1]
        code = urllib.parse.parse_qs(query).get("code", [""])[0]
    else:
        code = urllib.parse.unquote(pasted)
    if not code:
        sys.exit("Could not find the code in what you pasted. Paste the full address from the browser bar.")

    creds = base64.b64encode(f"{os.environ['EBAY_APP_ID']}:{os.environ['EBAY_CERT_ID']}".encode()).decode()
    tok = post_form("https://api.ebay.com/identity/v1/oauth2/token",
                    {"grant_type": "authorization_code", "code": code, "redirect_uri": runame},
                    {"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"})
    refresh = tok.get("refresh_token")
    if not refresh:
        sys.exit("eBay did not return a long-lived token. Try Test Sign-In again.")

    from nacl import encoding, public
    key = gh("GET", "/actions/secrets/public-key")
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    encrypted = base64.b64encode(box.encrypt(refresh.encode())).decode()
    gh("PUT", "/actions/secrets/EBAY_REFRESH_TOKEN", {"encrypted_value": encrypted, "key_id": key["key_id"]})
    days = int(tok.get("refresh_token_expires_in", 0)) // 86400
    print(f"Advertising access saved as the EBAY_REFRESH_TOKEN secret. Valid for about {days} days.")


if __name__ == "__main__":
    main()
