"""Контролируемый клиент Android-контракта. Использует только синтетические строки."""

import argparse
from hashlib import sha256
from secrets import token_urlsafe
from time import sleep

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("origin")
    parser.add_argument("telegram_id", type=int)
    args = parser.parse_args()
    if not args.origin.startswith(("https://", "http://localhost:")):
        parser.error("Укажи HTTPS или localhost через SSH-туннель.")
    verifier = token_urlsafe(32)
    with httpx.Client(base_url=args.origin.rstrip("/") + "/api/v1/", timeout=12) as client:
        link = client.post("auth/link/start", json={
            "telegram_id": args.telegram_id, "device_name": "Клиент синтетической проверки",
            "app_version": "test-client", "proof_sha256": sha256(verifier.encode("ascii")).hexdigest(),
        })
        link.raise_for_status()
        body = link.json()
        print(body["telegram_url"])
        input("Подтверди подключение в Telegram и нажми Enter. ")
        response = client.post("auth/link/exchange", json={"challenge": body["challenge"], "verifier": verifier})
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        try:
            for endpoint in ("auth/me", "catalog", "features", "sync"):
                reply = client.get(endpoint)
                reply.raise_for_status()
                print(endpoint, reply.status_code)
            key = token_urlsafe(18)
            response = client.post("telegram/exports", json={
                "request_id": key, "mode": "pairs",
                "report": {"matched": 2, "expected": 2, "missing_count": 0, "extra_count": 0},
                "first": ["DEMO-001", "DEMO-002", "DEMO-001", "DEMO-002"],
            })
            response.raise_for_status()
            terminal = {"delivered", "partially_delivered", "failed", "delivery_unknown", "cancelled", "expired"}
            for _ in range(64):
                reply = client.get("telegram/exports/" + key)
                reply.raise_for_status()
                state = reply.json()
                if state["status"] in terminal:
                    print(state)
                    return state["status"] != "delivered"
                sleep(2)
            return 1
        finally:
            client.post("auth/logout").raise_for_status()


if __name__ == "__main__":
    raise SystemExit(main())
