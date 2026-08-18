import httpx

from config import settings


SECRETS = [
    settings.quickcommerce_api_key,
    settings.twilio_account_sid,
    settings.twilio_api_key,
    settings.twilio_api_secret,
]


def redact(text: str) -> str:
    out = str(text)
    for s in SECRETS:
        if s:
            out = out.replace(s, "***")
    return out


def check_quickcommerce() -> None:
    key = settings.quickcommerce_api_key
    if not key:
        print("QUICKCOMMERCE: no key set")
        return
    try:
        r = httpx.get(
            "https://api.quickcommerceapi.com/v1/credits",
            headers={"X-API-Key": key},
            timeout=20,
        )
        if r.status_code == 200:
            summ = r.json().get("summary", {})
            print(f"QUICKCOMMERCE key: VALID (HTTP 200), credits_available={summ.get('total_available')}")
        elif r.status_code == 401:
            print("QUICKCOMMERCE key: INVALID (401) -- wrong/expired X-API-Key")
        else:
            print(f"QUICKCOMMERCE key: HTTP {r.status_code} -- {redact(r.text[:150])}")
    except Exception as exc:  # noqa: BLE001
        print("QUICKCOMMERCE error:", redact(exc))


def check_twilio() -> None:
    sid = settings.twilio_account_sid
    key = settings.twilio_api_key
    secret = settings.twilio_api_secret
    if not (sid and key and secret):
        print("TWILIO: creds missing")
        return
    if sid[:2] != "AC":
        print(f"TWILIO note: Account SID starts with '{sid[:2]}', but Twilio Account SIDs normally start with 'AC'.")
    try:
        r = httpx.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
            auth=(key, secret),
            timeout=20,
        )
        if r.status_code == 200:
            print("TWILIO creds: VALID (HTTP 200)")
        elif r.status_code == 401:
            print("TWILIO creds: INVALID (401) -- wrong API key/secret")
        elif r.status_code == 404:
            print("TWILIO creds: account SID not found (404) -- check TWILIO_ACCOUNT_SID")
        else:
            print(f"TWILIO creds: HTTP {r.status_code} -- {redact(r.text[:150])}")
    except Exception as exc:  # noqa: BLE001
        print("TWILIO error:", redact(exc))


if __name__ == "__main__":
    check_quickcommerce()
    check_twilio()
