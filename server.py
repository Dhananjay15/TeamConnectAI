# server.py
import os
import socketio
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# ================================
# 🔹 FastAPI + Socket.IO Setup
# ================================
app = FastAPI()
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*"
)

# Wrap FastAPI with Socket.IO ASGI app
socket_app = socketio.ASGIApp(
    sio,
    other_asgi_app=app,
    socketio_path="socket.io"
)

# ================================
# 🔹 Paths
# ================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GAMES_DIR = os.path.join(BASE_DIR, "games")

# ================================
# 🔹 Serve Frontend
# ================================
@app.get("/")
async def root():
    """Serve main index.html"""
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

# Mount each game's static assets dynamically
for game in os.listdir(GAMES_DIR):
    game_path = os.path.join(GAMES_DIR, game, "static")
    if os.path.isdir(game_path):
        app.mount(f"/games/{game}", StaticFiles(directory=game_path), name=game)
        print(f"📂 Mounted static for game: {game} -> /games/{game}")

# ================================
# 🔹 Import & Register Game Logic
# ================================
# Example: Team Shout game
from games.teamshout.team_shout import handle_team_shout

handle_team_shout(sio)  # Register the Team Shout Socket.IO events

# Future games can be added like:
# from games.othergame.othergame import handle_other_game
# handle_other_game(sio)

# ================================
# 🔹 Run Server
# ================================
if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 3000))
    print(f"🚀 Starting server on http://0.0.0.0:{PORT}")
    uvicorn.run(socket_app, host="0.0.0.0", port=PORT)
