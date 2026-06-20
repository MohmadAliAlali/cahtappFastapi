"""
Comprehensive real-time test for the chat backend.
Tests: registration, login, conversation creation, display names, WebSocket messaging.
"""

import asyncio
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
import traceback

import httpx
import socketio

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PYTHON = os.path.join(BACKEND_DIR, "venv/bin/python")
BASE_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000"
OUTPUT_LOG = os.path.join(BACKEND_DIR, "tests/test_output.log")
RESULTS_LOG = os.path.join(BACKEND_DIR, "tests/test_results.log")

results = []
output_lines = []
server_process = None


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    output_lines.append(line)
    print(line)


def write_logs():
    with open(OUTPUT_LOG, "w") as f:
        f.write("\n".join(output_lines) + "\n")
    pass_count = sum(1 for r in results if r[1] == "PASS")
    fail_count = sum(1 for r in results if r[1] == "FAIL")
    summary = [
        "=" * 60,
        "TEST RESULTS SUMMARY",
        "=" * 60,
    ]
    for name, status, detail in results:
        summary.append(f"  [{status:4s}] {name}")
        if detail:
            summary.append(f"           -> {detail}")
    summary.append("=" * 60)
    summary.append(f"  TOTAL: {len(results)}  |  PASS: {pass_count}  |  FAIL: {fail_count}")
    summary.append("=" * 60)
    with open(RESULTS_LOG, "w") as f:
        f.write("\n".join(summary) + "\n")


def test_result(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    results.append((name, status, detail))
    log(f"  >>> {name}: {status}" + (f" - {detail}" if detail else ""))


def start_server():
    global server_process
    log("Starting uvicorn server...")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["TESTING"] = "1"
    server_process = subprocess.Popen(
        [VENV_PYTHON, "-m", "uvicorn", "app.main:asgi_app",
         "--host", "0.0.0.0", "--port", "8000",
         "--log-level", "error"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=BACKEND_DIR,
        env=env,
    )
    for _ in range(30):
        time.sleep(0.5)
        try:
            import urllib.request
            resp = urllib.request.urlopen(f"{BASE_URL}/health", timeout=2)
            if resp.status == 200:
                log("Server is healthy and ready.")
                return
        except Exception:
            continue
    raise RuntimeError("Server failed to start within 15 seconds")


def stop_server():
    global server_process
    if server_process:
        log("Stopping uvicorn server...")
        server_process.terminate()
        try:
            server_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_process.kill()
            server_process.wait(timeout=5)
        server_process = None


def drain_server_output() -> str:
    """Read all available data from server stdout (both stdout and stderr)."""
    if not server_process or not server_process.stdout:
        return ""
    try:
        import fcntl
        fd = server_process.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        raw = os.read(fd, 65536)
        return raw.decode("utf-8", errors="replace")
    except (ValueError, OSError, AttributeError, BlockingIOError):
        pass
    return ""


def extract_verification_code(email: str) -> str | None:
    output = drain_server_output()
    if not output:
        return None
    pattern = re.compile(rf"\[EMAIL\] Verification code for {re.escape(email)}:\s*(\d{{6}})")
    match = pattern.search(output)
    if match:
        return match.group(1)
    return None


async def register_user_with_code(client: httpx.AsyncClient, email: str, password: str, display_name: str) -> dict | None:
    resp = await client.post("/api/v1/auth/register", json={
        "email": email, "password": password, "display_name": display_name,
    })
    if resp.status_code != 200:
        log(f"  Register failed: {resp.status_code} {resp.text}")
        return None
    log(f"  Register OK")

    code = None
    for _ in range(20):
        code = extract_verification_code(email)
        if code:
            log(f"  Code: {code}")
            break
        await asyncio.sleep(0.5)

    if not code:
        log(f"  ERROR: No verification code captured for {email}")
        return None

    resp = await client.post("/api/v1/auth/verify-email", json={"code": code})
    if resp.status_code != 200:
        log(f"  Verify email failed: {resp.status_code} {resp.text}")
        return None
    log(f"  Email verified")

    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if resp.status_code != 200:
        log(f"  Login failed: {resp.status_code} {resp.text}")
        return None
    data = resp.json()
    log(f"  Login OK: user={data['user']['display_name']} id={data['user']['id']}")
    return data


async def main():
    log("=" * 60)
    log("REAL-TIME BACKEND TEST SUITE")
    log("=" * 60)

    # Start server if not already running
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=5) as c:
            r = await c.get("/health")
            if r.status_code == 200:
                log("Server already running.")
    except Exception:
        log("No server detected, starting one...")
        try:
            start_server()
        except RuntimeError as e:
            log(f"FATAL: {e}")
            write_logs()
            return

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        # ---- 1. Health check ----
        log("\n--- Health Check ---")
        try:
            resp = await client.get("/health")
            test_result("Health check", resp.status_code == 200)
            log(f"  Health: {resp.json()}")
        except Exception as e:
            test_result("Health check", False, str(e))

        # ---- 2. Register 3 users ----
        log("\n--- Registration ---")
        users = {}
        base_ts = int(time.time())
        for i in range(1, 4):
            email = f"testuser{i}_{base_ts}@example.com"
            result = await register_user_with_code(client, email, "TestPass123!", f"Test User {i}")
            if result:
                users[f"user{i}"] = {
                    "email": email,
                    "password": "TestPass123!",
                    "display_name": f"Test User {i}",
                    "access_token": result["access_token"],
                    "refresh_token": result["refresh_token"],
                    "id": result["user"]["id"],
                }
                test_result(f"Register user{i} ({email})", True)
            else:
                test_result(f"Register user{i} ({email})", False)

        if len(users) < 3:
            log("FATAL: Could not register all 3 users.")
            write_logs()
            return

        # ---- 3. Login each user ----
        log("\n--- Login Verification ---")
        for name, info in users.items():
            resp = await client.post("/api/v1/auth/login", json={
                "email": info["email"], "password": info["password"],
            })
            ok = resp.status_code == 200
            test_result(f"Login {name}", ok)
            if ok:
                data = resp.json()
                users[name]["access_token"] = data["access_token"]
                users[name]["id"] = data["user"]["id"]
                users[name]["display_name"] = data["user"]["display_name"]

        # ---- 4. Create conversations ----
        log("\n--- Conversation Creation ---")
        h1 = {"Authorization": f"Bearer {users['user1']['access_token']}"}
        h2 = {"Authorization": f"Bearer {users['user2']['access_token']}"}

        resp = await client.post("/api/v1/conversations",
            json={"user_id": users["user2"]["id"]}, headers=h1)
        ok = resp.status_code == 200
        test_result("user1 creates conversation with user2", ok)
        conv12_id = resp.json().get("id") if ok else None
        if ok:
            log(f"  Conv: {conv12_id}, name='{resp.json().get('name')}'")

        resp = await client.post("/api/v1/conversations",
            json={"user_id": users["user3"]["id"]}, headers=h2)
        ok = resp.status_code == 200
        test_result("user2 creates conversation with user3", ok)
        if ok:
            log(f"  Conv: {resp.json().get('id')}, name='{resp.json().get('name')}'")

        # ---- 5. user1 lists conversations ----
        log("\n--- List Conversations (user1) ---")
        resp = await client.get("/api/v1/conversations", headers=h1)
        ok = resp.status_code == 200
        test_result("user1 lists conversations", ok)
        if ok:
            convs = resp.json()
            log(f"  user1 has {len(convs)} conversation(s)")
            for c in convs:
                log(f"    name='{c.get('name')}' participants={c.get('participant_ids')}")
            name_ok = any(c.get("name") == users["user2"]["display_name"] for c in convs)
            test_result("user1 sees user2's display name", name_ok)

        # ---- 6. user2 lists conversations ----
        log("\n--- List Conversations (user2) ---")
        resp = await client.get("/api/v1/conversations", headers=h2)
        ok = resp.status_code == 200
        test_result("user2 lists conversations", ok)
        if ok:
            convs = resp.json()
            log(f"  user2 has {len(convs)} conversation(s)")
            for c in convs:
                log(f"    name='{c.get('name')}' participants={c.get('participant_ids')}")
            name_u1 = any(c.get("name") == users["user1"]["display_name"] for c in convs)
            name_u3 = any(c.get("name") == users["user3"]["display_name"] for c in convs)
            test_result("user2 sees user1's display name", name_u1)
            test_result("user2 sees user3's display name", name_u3)

        # ---- 7. Socket.IO testing ----
        log("\n--- Socket.IO Testing ---")
        if not conv12_id:
            test_result("Socket.IO test", False, "no conversation")
        else:
            token_u1 = users["user1"]["access_token"]

            sio_client = socketio.AsyncClient()
            received_messages = []

            @sio_client.event
            async def connect():
                log("  Socket.IO connected")

            @sio_client.event
            async def connected(data):
                log(f"  Connected event: user_id={data.get('user_id')}")
                test_result("SI connected event", data.get("user_id") is not None)

            @sio_client.event
            async def joined(data):
                log(f"  Joined: {data}")
                test_result("SI join conversation", data.get("conversation_id") == conv12_id)

            @sio_client.event
            async def new_message(data):
                log(f"  Got new_message: type={data.get('type')}")
                received_messages.append(data)

            @sio_client.event
            async def left(data):
                log(f"  Left: {data}")
                test_result("SI leave conversation", data == {})

            @sio_client.event
            async def connect_error(data):
                log(f"  Connection error: {data}")

            try:
                await sio_client.connect(
                    'http://localhost:8000',
                    auth={'token': token_u1},
                    transports=['websocket'],
                    wait_timeout=10
                )
                log("  Socket.IO connection established")

                # Join conversation
                await sio_client.emit('join', {'conversation_id': conv12_id})
                await asyncio.sleep(1)

                # Send a message
                test_body = f"Hello from user1 at {time.time()}"
                log(f"  Sending: '{test_body}'")
                await sio_client.emit('send_message', {'conversation_id': conv12_id, 'body': test_body})

                # Wait for broadcast with extended timeout
                log("  Waiting for broadcast...")
                try:
                    await asyncio.wait_for(asyncio.sleep(0), timeout=10)
                    # Give time for message to arrive
                    await asyncio.sleep(3)

                    if received_messages:
                        msg = received_messages[0]
                        is_new_msg = msg.get("type") == "new_message"
                        body_match = msg.get("message", {}).get("body") == test_body
                        sender_match = msg.get("message", {}).get("sender_name") == users["user1"]["display_name"]

                        test_result("SI message send & receive",
                                    is_new_msg and body_match and sender_match,
                                    f"type={msg.get('type')}, body_match={body_match}, sender_match={sender_match}")
                        if not is_new_msg:
                            log(f"  Full msg: {json.dumps(msg, indent=2)[:500]}")
                    else:
                        log("  WARNING: No broadcast received")
                        test_result("SI message send & receive", False, "no message received")

                        # Verify message was saved via REST API
                        log("  Checking message via REST API...")
                        resp = await client.get(
                            f"/api/v1/conversations/{conv12_id}/messages",
                            headers=h1,
                        )
                        if resp.status_code == 200:
                            msgs = resp.json().get("messages", [])
                            log(f"  Found {len(msgs)} messages in conversation")
                            for m in msgs:
                                log(f"    body='{m.get('body')}' sender='{m.get('sender_name')}'")
                                if m.get("body") == test_body:
                                    log("  Message WAS saved to DB (broadcast issue only)")
                        else:
                            log(f"  REST failed: {resp.status_code} {resp.text}")

                except asyncio.TimeoutError:
                    log("  WARNING: Broadcast timed out")
                    test_result("SI message send & receive", False, "timeout")

                # Leave
                await sio_client.emit('leave', {'conversation_id': conv12_id})
                await asyncio.sleep(0.5)
                await sio_client.disconnect()

            except asyncio.TimeoutError:
                log("  Socket.IO timeout during setup")
                test_result("Socket.IO connection", False, "timeout")
            except Exception as e:
                log(f"  Socket.IO error: {e}")
                traceback.print_exc()
                test_result("Socket.IO connection", False, str(e))
            finally:
                if sio_client.connected:
                    await sio_client.disconnect()

    write_logs()
    log("\n" + "=" * 60)
    log("TEST SUITE COMPLETE")
    log("=" * 60)

    pass_count = sum(1 for r in results if r[1] == "PASS")
    fail_count = sum(1 for r in results if r[1] == "FAIL")
    print(f"\n{'=' * 60}")
    print(f"RESULTS: {pass_count} PASS, {fail_count} FAIL out of {len(results)} total")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        stop_server()
