# games/teamshout/team_shout.py
import random
import time
import asyncio
import socketio
from typing import List, Dict, Any
from cohere import ClientV2
import json
import os

# ====== CONFIG ======
cohere_api_key = os.getenv("COHERE_API_KEY")
if not cohere_api_key:
    raise ValueError("❌ COHERE_API_KEY not set in environment!")
cohere_client = ClientV2(api_key=cohere_api_key)

DEFAULT_THEME = "general"
DEFAULT_DIFFICULTY = "easy"
DEFAULT_NUM_PROMPTS = 10

MAX_ROUNDS = 15   # upper bound; host can request up to this
ROUND_TIME = 10   # seconds per round
AUTO_NEXT_DELAY = 2  # seconds between round-ended and next round

# ====== UTIL ======
def shuffle(array: List) -> List:
    a = array[:]
    for i in range(len(a) - 1, 0, -1):
        j = random.randint(0, i)
        a[i], a[j] = a[j], a[i]
    return a

# Robust one-shot prompt generation instructing model NOT to include MCQ options
async def generate_room_prompts(theme: str = DEFAULT_THEME, difficulty: str = DEFAULT_DIFFICULTY, num_prompts: int = DEFAULT_NUM_PROMPTS) -> List[Dict[str, Any]]:
    def extract_json_from_text(text: str):
        text = (text or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            pass
        for start_ch in ('{', '['):
            start_idx = text.find(start_ch)
            if start_idx == -1:
                continue
            stack = []
            for i in range(start_idx, len(text)):
                ch = text[i]
                if ch in '{[':
                    stack.append(ch)
                elif ch in '}]':
                    if not stack:
                        break
                    stack.pop()
                    if not stack:
                        candidate = text[start_idx:i+1]
                        try:
                            return json.loads(candidate)
                        except Exception:
                            break
        return None

    user_message = (
        f"Generate {num_prompts} short game prompts for theme '{theme}' and difficulty '{difficulty}'.\n"
        f"IMPORTANT: Return ONLY valid JSON in this exact shape:\n\n"
        f'{{ "prompts": [{{ "text": "prompt text here", "answers": ["answer1","answer2"] }}, ...] }}\n\n'
        f"- PROMPT TEXT MUST NOT include multiple-choice options or 'which of these' wording.\n"
        f"- Each prompt should be either open-ended (many acceptable answers in the answers array) or factual (canonical answer in the array) But should have only one word answers.\n"
        f"- No backticks or extra commentary.\n"
    )

    print(f"⏳ Generating {num_prompts} prompts (one-shot) — theme={theme} difficulty={difficulty}")
    try:
        response = cohere_client.chat(
            model="command-a-03-2025",
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as e:
        print("❌ Cohere API call failed:", e)
        response = None

    chat_text = ""
    if response:
        try:
            for item in response.message.content:
                if getattr(item, "type", None) == "text":
                    chat_text += item.text
                elif isinstance(item, str):
                    chat_text += item
                else:
                    chat_text += str(getattr(item, "text", item))
        except Exception:
            chat_text = str(response)

    chat_text = (chat_text or "").strip()
    if chat_text:
        print("🔍 Model output preview:", chat_text[:800] + ("..." if len(chat_text) > 800 else ""))

    parsed = extract_json_from_text(chat_text)
    prompts: List[Dict[str, Any]] = []

    if parsed and (isinstance(parsed, dict) and isinstance(parsed.get("prompts"), list)):
        candidate_list = parsed["prompts"]
        for p in candidate_list:
            if not isinstance(p, dict):
                continue
            text = (p.get("text") or "").strip()
            answers = p.get("answers") or []
            if isinstance(answers, str):
                answers = [a.strip() for a in answers.split(",") if a.strip()]
            if not text or not isinstance(answers, list):
                continue
            norm_answers = [str(a).strip().lower() for a in answers if str(a).strip()]
            if not norm_answers:
                continue
            prompts.append({"text": text, "answers": norm_answers})
        print(f"ℹ️ Parsed {len(prompts)} prompts from model output.")
    else:
        print("⚠️ Model output could not be parsed or missing prompts key — will use fallback prompts.")

    # fallback/pad if insufficient
    if len(prompts) < num_prompts:
        print(f"⚠️ Only {len(prompts)} prompts parsed; padding to {num_prompts}.")
        fallback_pool = [
            {"text": "Name a color in the rainbow", "answers": ["red", "orange", "yellow", "green", "blue", "indigo", "violet"]},
            {"text": "What is the largest gland in the human body?", "answers": ["liver"]},
            {"text": "Name a fruit", "answers": ["apple", "banana", "orange", "grape", "mango"]},
            {"text": "Name any planet in our solar system", "answers": ["mercury", "venus", "earth", "mars", "jupiter", "saturn", "uranus", "neptune"]},
            {"text": "Name an animal that can fly", "answers": ["eagle", "sparrow", "bat", "butterfly"]},
        ]
        existing = {p["text"].lower() for p in prompts}
        i = 0
        while len(prompts) < num_prompts:
            cand = fallback_pool[i % len(fallback_pool)]
            if cand["text"].lower() not in existing:
                prompts.append(cand.copy())
                existing.add(cand["text"].lower())
            i += 1

    final = []
    for p in prompts[:num_prompts]:
        answers = [a.lower() for a in p["answers"] if a and str(a).strip()]
        if not answers:
            answers = ["other"]
        final.append({"text": p["text"].strip(), "answers": answers})

    print(f"✅ Returning {len(final)} prompts.")
    return final

# ====== Game handlers ======
def handle_team_shout(sio: socketio.AsyncServer):
    rooms: Dict[str, Dict[str, Any]] = {}

    def apply_scoring_from_order(r: Dict[str, Any]):
        order = [a for a in r.get("answer_order", []) if a.get("isCorrect")]
        order.sort(key=lambda x: x["ts"])
        points = [5, 3, 2]
        awarded = []
        for i, item in enumerate(order[:3]):
            pid = item["playerId"]
            player = next((p for p in r["players"] if p["playerId"] == pid), None)
            if player:
                pts = points[i]
                player["score"] = player.get("score", 0) + pts
                awarded.append({"playerId": pid, "name": player["name"], "points": pts, "position": i+1})
        return awarded

    async def end_round(room: str, reason: str):
        r = rooms.get(room)
        if not r or r.get("roundEnded"):
            return
        r["roundEnded"] = True
        print(f"⏹️ End round {r['currentRound']} in {room} ({reason})")

        awarded = apply_scoring_from_order(r)
        scoreboard = [{"name": p["name"], "score": p["score"]} for p in r["players"]]
        await sio.emit("round-ended", {"scoreboard": scoreboard, "awarded": awarded}, room=room, namespace="/teamshout")

        # game over?
        if r["currentRound"] >= r.get("numRounds", DEFAULT_NUM_PROMPTS):
            r["gameEnded"] = True
            print(f"🏁 Game over in {room}")
            await sio.emit("game-over", r["players"], room=room, namespace="/teamshout")
            return

        async def delayed_next():
            await asyncio.sleep(AUTO_NEXT_DELAY)
            rr = rooms.get(room)
            if not rr or rr.get("gameEnded"):
                return
            await start_round(room)

        sio.start_background_task(delayed_next)

    async def start_round(room: str):
        r = rooms[room]
        if r["currentRound"] >= r.get("numRounds", DEFAULT_NUM_PROMPTS):
            r["gameEnded"] = True
            await sio.emit("game-over", r["players"], room=room, namespace="/teamshout")
            return

        r["answers"] = {}
        r["answer_order"] = []
        r["roundEnded"] = False
        r["roundId"] += 1
        local_round_id = r["roundId"]

        prompt = r["prompts"][r["currentRound"]]
        r["currentPrompt"] = prompt
        r["currentRound"] += 1
        r["roundStartTime"] = time.time()
        print(f"▶️ Start round {r['currentRound']} in {room}: {prompt['text']}")
        await sio.emit("new-round", {"prompt": prompt["text"], "round": r["currentRound"], "players": r["players"], "time": ROUND_TIME}, room=room, namespace="/teamshout")

        async def round_timer():
            await asyncio.sleep(ROUND_TIME)
            rr = rooms.get(room)
            if not rr:
                return
            if rr.get("roundId") == local_round_id and not rr.get("roundEnded"):
                await end_round(room, reason="timer")

        r["roundTask"] = sio.start_background_task(round_timer)

    # === socket handlers ===
    @sio.on("connect", namespace="/teamshout")
    async def connect(sid, environ):
        print(f"🧩 New connection: {sid}")

    @sio.on("join-room", namespace="/teamshout")
    async def join_room(sid, data):
        room = data.get("room")
        name = data.get("name")
        player_id = data.get("playerId")
        is_host_flag = data.get("isHost", False)
        if not room or not room.startswith("shout-"):
            return {"success": False, "error": "Invalid room code"}

        if room not in rooms:
            rooms[room] = {
                "players": [],
                "playerSockets": {},
                "socketToPlayerId": {},
                "host": None,
                "currentPrompt": None,
                "prompts": [],
                "prompts_ready": False,
                "answers": {},
                "answer_order": [],
                "gameEnded": False,
                "gameStarted": False,
                "currentRound": 0,
                "roundId": 0,
                "roundTask": None,
                "roundEnded": False,
                "roundStartTime": None,
                "numRounds": DEFAULT_NUM_PROMPTS,
                "lastActive": time.time(),
            }
            print(f"📦 Created room {room}")

        r = rooms[room]
        r["lastActive"] = time.time()

        existing_idx = next((i for i,p in enumerate(r["players"]) if p["playerId"] == player_id), -1)
        if existing_idx != -1:
            prev_score = r["players"][existing_idx]["score"]
            r["players"].pop(existing_idx)
            r["playerSockets"].pop(player_id, None)
            for sock, pid in list(r["socketToPlayerId"].items()):
                if pid == player_id:
                    r["socketToPlayerId"].pop(sock, None)
        else:
            prev_score = 0

        r["players"].append({"name": name, "playerId": player_id, "score": prev_score})
        r["playerSockets"][player_id] = sid
        r["socketToPlayerId"][sid] = player_id

        if not r["host"] or r["host"] not in r["playerSockets"]:
            r["host"] = player_id if is_host_flag else r["host"] or player_id

        await sio.enter_room(sid, room, namespace="/teamshout")
        await sio.emit("player-list", {"players": r["players"], "hostPlayerId": r["host"]}, room=room, namespace="/teamshout")

        if r.get("prompts_ready"):
            await sio.emit("prompts-status", {"status": "ready"}, room=sid, namespace="/teamshout")

        if r.get("currentPrompt") and not r.get("roundEnded"):
            remaining = ROUND_TIME
            if r.get("roundStartTime"):
                elapsed = time.time() - r["roundStartTime"]
                remaining = max(0, int(ROUND_TIME - elapsed))
            await sio.emit("new-round", {"prompt": r["currentPrompt"]["text"], "round": r["currentRound"], "players": r["players"], "time": remaining}, room=sid, namespace="/teamshout")

        return {"success": True, "room": room, "playerId": player_id}

    @sio.on("generate-prompts", namespace="/teamshout")
    async def generate_prompts_handler(sid, data):
        room = data.get("room")
        theme = data.get("theme") or DEFAULT_THEME
        difficulty = data.get("difficulty") or DEFAULT_DIFFICULTY
        num_prompts = int(data.get("numPrompts") or DEFAULT_NUM_PROMPTS)
        r = rooms.get(room)
        if not r:
            return {"success": False, "error": "Room missing"}
        player_id = r["socketToPlayerId"].get(sid)
        if player_id != r["host"]:
            return {"success": False, "error": "Only host can generate prompts"}

        if num_prompts < 1: num_prompts = DEFAULT_NUM_PROMPTS
        if num_prompts > MAX_ROUNDS: num_prompts = MAX_ROUNDS

        r["prompts_ready"] = False
        r["numRounds"] = num_prompts
        await sio.emit("prompts-status", {"status": "generating"}, room=room, namespace="/teamshout")

        async def task():
            try:
                prompts = await generate_room_prompts(theme, difficulty, num_prompts)
                r["prompts"] = prompts
                r["prompts_ready"] = True
                print(f"✅ Prompts ready for {room} (theme={theme}, diff={difficulty}, n={num_prompts})")
                await sio.emit("prompts-status", {"status": "ready"}, room=room, namespace="/teamshout")
            except Exception as e:
                print("❌ Error generating prompts:", e)
                fallback = [{"text": "Name a fruit", "answers": ["apple","banana","orange"]}] * num_prompts
                r["prompts"] = fallback
                r["prompts_ready"] = True
                await sio.emit("prompts-status", {"status": "ready"}, room=room, namespace="/teamshout")

        sio.start_background_task(task)
        return {"success": True}

    @sio.on("start-game", namespace="/teamshout")
    async def start_game(sid, data):
        room = None
        if isinstance(data, str):
            room = data
        elif isinstance(data, dict):
            room = data.get("room") or data.get("roomCode")
        if not room:
            return {"success": False, "error": "Missing room"}
        r = rooms.get(room)
        if not r:
            return {"success": False, "error": "Room missing"}
        player_id = r["socketToPlayerId"].get(sid)
        if player_id != r["host"]:
            return {"success": False, "error": "Only host can start"}

        # If prompts not ready, generate defaults synchronously (so host ack waits)
        if not r.get("prompts_ready", False):
            print(f"⚠️ Prompts not ready in {room} when host started; generating defaults.")
            r["prompts_ready"] = False
            r["numRounds"] = DEFAULT_NUM_PROMPTS
            await sio.emit("prompts-status", {"status": "generating"}, room=room, namespace="/teamshout")
            try:
                r["prompts"] = await generate_room_prompts(DEFAULT_THEME, DEFAULT_DIFFICULTY, DEFAULT_NUM_PROMPTS)
                r["prompts_ready"] = True
                await sio.emit("prompts-status", {"status": "ready"}, room=room, namespace="/teamshout")
            except Exception as e:
                print("❌ Fallback prompts due to error:", e)
                r["prompts"] = [{"text": "Name a fruit", "answers": ["apple","banana","orange"]}] * DEFAULT_NUM_PROMPTS
                r["prompts_ready"] = True
                await sio.emit("prompts-status", {"status": "ready"}, room=room, namespace="/teamshout")

        if r.get("gameStarted"):
            return {"success": False, "error": "Game already started"}

        r["gameStarted"] = True
        r["answers"] = {}
        r["answer_order"] = []
        r["gameEnded"] = False
        r["currentRound"] = 0
        r["roundId"] = 0
        r["roundEnded"] = False
        r["roundStartTime"] = None

        print(f"✅ Host {player_id} starting game in {room}")

        # Ensure broadcast goes out first; start rounds in background
        await sio.emit("game-start", {}, room=room, namespace="/teamshout")
        sio.start_background_task(start_round, room)
        return {"success": True}

    @sio.on("submit-answer", namespace="/teamshout")
    async def submit_answer(sid, data):
        room = data.get("room")
        raw = (data.get("answer") or "").strip()
        answer = raw.lower()
        player_id = data.get("playerId")
        r = rooms.get(room)
        if not r or not r.get("currentPrompt") or r.get("roundEnded"):
            return
        if player_id in r["answers"]:
            return
        player = next((p for p in r["players"] if p["playerId"] == player_id), None)
        if not player:
            return

        valid_answers = [a.lower() for a in r["currentPrompt"]["answers"]]
        is_correct = answer in valid_answers
        ts = time.time()
        r["answers"][player_id] = {"answer": raw, "isCorrect": is_correct, "ts": ts}
        r["answer_order"].append({"playerId": player_id, "ts": ts, "isCorrect": is_correct, "answer": raw})

        await sio.emit("answer-received", {"name": player["name"], "answer": raw, "isCorrect": is_correct}, room=room, namespace="/teamshout")

        if len(r["answers"]) == len(r["players"]) and not r.get("roundEnded"):
            await end_round(room, reason="all-answered")

    @sio.on("next-round", namespace="/teamshout")
    async def next_round(sid, data):
        room = data if isinstance(data, str) else (data.get("room") if isinstance(data, dict) else None)
        r = rooms.get(room)
        if not r:
            return
        player_id = r["socketToPlayerId"].get(sid)
        if player_id != r["host"]:
            return
        await start_round(room)

    @sio.on("disconnect", namespace="/teamshout")
    async def disconnect(sid):
        for room, r in list(rooms.items()):
            pid = r["socketToPlayerId"].get(sid)
            if not pid:
                continue
            r["playerSockets"].pop(pid, None)
            r["socketToPlayerId"].pop(sid, None)
            if r["host"] == pid:
                r["host"] = next((p["playerId"] for p in r["players"] if p["playerId"] in r["playerSockets"]), None)
            await sio.emit("player-list", {"players": r["players"], "hostPlayerId": r["host"]}, room=room, namespace="/teamshout")
            async def cleanup():
                await asyncio.sleep(10)
                if not r["playerSockets"]:
                    rooms.pop(room, None)
                    print(f"🧹 Room {room} cleaned up")
            sio.start_background_task(cleanup)
