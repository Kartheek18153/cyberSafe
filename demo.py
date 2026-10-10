"""Hackathon demo: simulated SIM-swap protection flow (mock mode, no network).

Runs the full FastAPI stack in-process and narrates three scenarios:
  1. Legitimate user, no recent swap  -> normal step-up auth.
  2. SIM-swapped number               -> friction (deny SMS OTP, escalate).
  3. Retrieve-date evidence           -> timestamp or null handling.

Mock convention (dev double only, see README): numbers ending in 0000
simulate a recent swap; all others simulate no swap. Nothing here proves
real-world detection — it demonstrates integration, risk signals, and UX flow.

Usage:  python demo.py
"""

from fastapi.testclient import TestClient

from app.main import app

CLEAN_NUMBER = "+346661113334"
SWAPPED_NUMBER = "+3412340000"


def show(title: str, body: dict) -> None:
    print(f"\n=== {title} ===")
    print(f"  status            : {body['status']}")
    print(f"  risk_signal       : {body['risk_signal']}")
    print(f"  sim_swap_detected : {body['sim_swap_detected']}")
    print(f"  masked_phone      : {body['masked_phone']}")
    print(f"  detail            : {body['detail']}")
    if body.get("last_swap_at"):
        print(f"  last_swap_at      : {body['last_swap_at']}")


def main() -> None:
    with TestClient(app) as client:
        health = client.get("/health").json()
        print(
            f"Service: cybersafe-sim-swap | mode="
            f"{'MOCK (simulated)' if not health['live_configured'] else 'LIVE'} | "
            f"provider={health['provider']}"
        )

        # Scenario 1: legitimate user about to reset a password.
        res = client.post(
            "/api/security/sim-swap/check",
            json={"phone_number": CLEAN_NUMBER, "lookback_hours": 72},
        ).json()
        show("Scenario 1 — password reset pre-check (clean number)", res)
        print("  -> Decision: proceed with SMS OTP as usual.")

        # Scenario 2: same flow, but the number was SIM-swapped yesterday.
        res = client.post(
            "/api/security/sim-swap/check",
            json={"phone_number": SWAPPED_NUMBER, "lookback_hours": 72},
        ).json()
        show("Scenario 2 — password reset pre-check (swapped number)", res)
        print("  -> Decision: BLOCK SMS OTP, require in-branch / ID verification.")

        # Scenario 3: evidence behind the verdict.
        res = client.post(
            "/api/security/sim-swap/retrieve-date",
            json={"phone_number": SWAPPED_NUMBER},
        ).json()
        show("Scenario 3 — retrieve-date evidence (swapped number)", res)

        res = client.post(
            "/api/security/sim-swap/retrieve-date",
            json={"phone_number": CLEAN_NUMBER},
        ).json()
        show("Scenario 3b — retrieve-date evidence (clean number)", res)

    print(
        "\nDemo complete. All responses above are SIMULATED by the mock provider; "
        "swap the mock for live GSMA credentials to use real operator data."
    )


if __name__ == "__main__":
    main()
