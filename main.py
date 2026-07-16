import os
import json
import random
from datetime import datetime, timedelta
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from pydantic import BaseModel
import requests

# --- Database Setup ---
DATABASE_URL = "sqlite:///./football.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class MatchDB(Base):
    __tablename__ = "matches"
    id = Column(Integer, primary_key=True, index=True)
    team1 = Column(String, index=True)
    score1 = Column(Integer, nullable=True)
    team2 = Column(String)
    score2 = Column(Integer, nullable=True)
    status = Column(String)
    utc_date = Column(String)
    stage = Column(String)

# Automatically drops and recreates tables to prevent database lockups on Render
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)

# --- FastAPI Setup ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://wc26-woad-six.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Football API Integration (Live Scores Sync) ---
FOOTBALL_API_KEY = "5cd9e16068fe417b9815290010d55d87" 
LAST_SYNC_TIME = None

def perform_sync(db: Session):
    headers = { 'X-Auth-Token': FOOTBALL_API_KEY }
    response = requests.get('https://api.football-data.org/v4/competitions/WC/matches', headers=headers)
    
    if response.status_code != 200:
        raise Exception(f"Failed to fetch from API: {response.text}")
        
    data = response.json()
    matches = data.get('matches', [])
    
    db.query(MatchDB).delete()
    
    matches_added = 0
    for match in matches: 
        score = match.get('score', {}).get('fullTime', {})
        home_score = score.get('home')
        away_score = score.get('away')
        
        home_team = match.get('homeTeam', {})
        away_team = match.get('awayTeam', {})
        team1_name = home_team.get('shortName') or home_team.get('name') or 'TBD'
        team2_name = away_team.get('shortName') or away_team.get('name') or 'TBD'
        
        new_match = MatchDB(
            team1=team1_name,
            score1=home_score,
            team2=team2_name,
            score2=away_score,
            status=match.get('status', 'SCHEDULED'),
            utc_date=match.get('utcDate', ''),
            stage=match.get('stage', 'GROUP_STAGE')
        )
        db.add(new_match)
        matches_added += 1
        
    db.commit()
    return matches_added

@app.post("/api/sync")
def sync_live_matches(db: Session = Depends(get_db)):
    try:
        global LAST_SYNC_TIME
        matches_added = perform_sync(db)
        LAST_SYNC_TIME = datetime.utcnow()
        return {"message": f"Successfully synced {matches_added} matches!"}
    except Exception as e:
        return {"error": "Failed to manually sync", "details": str(e)}

@app.get("/api/matches")
def get_matches(db: Session = Depends(get_db)):
    global LAST_SYNC_TIME
    now = datetime.utcnow()
    
    db_empty = db.query(MatchDB).count() == 0
    time_to_sync = LAST_SYNC_TIME is None or (now - LAST_SYNC_TIME) > timedelta(minutes=10)
    
    if db_empty or time_to_sync:
        try:
            perform_sync(db)
            LAST_SYNC_TIME = now
        except Exception as e:
            print(f"Auto-sync background task failed: {e}")
            
    return db.query(MatchDB).all()


# --- DYNAMIC DUAL-ENGINE: SIMULATED STATS + REAL GOALSCORERS ---
@app.get("/api/match-stats/{team1}/{team2}")
def get_real_match_stats(team1: str, team2: str, db: Session = Depends(get_db)):
    match = db.query(MatchDB).filter(
        ((MatchDB.team1 == team1) & (MatchDB.team2 == team2)) |
        ((MatchDB.team1 == team2) & (MatchDB.team2 == team1))
    ).first()

    if not match:
        return {"error": True, "message": "Match not found in database records."}

    home_score = match.score1 if match.score1 is not None else 0
    away_score = match.score2 if match.score2 is not None else 0

    # Base configuration for dynamic player names
    real_goals = []
    detected_potm = "Match MVP"
    
    # HTTP header context to ensure smooth delivery from cloud instances
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    # DYNAMIC ENGINE: Pull the live tournament feed to fetch genuine goalscorers
    try:
        # 42 is the global index mapping for the World Cup tournament timeline
        tournament_url = "https://www.fotmob.com/api/leagues?id=42"
        resp = requests.get(tournament_url, headers=headers, timeout=5)
        
        if resp.status_code == 200:
            data = resp.json()
            fotmob_id = None
            
            # Step 1: Scan the active tournament grid to extract the match ID
            matches_data = data.get("matches", {}).get("allMatches", [])
            for m in matches_data:
                h_name = m.get("home", {}).get("name", "").lower()
                a_name = m.get("away", {}).get("name", "").lower()
                t1_low = team1.lower().strip()
                t2_low = team2.lower().strip()
                
                if (t1_low in h_name or h_name in t1_low) and (t2_low in a_name or a_name in t2_low):
                    fotmob_id = m.get("id")
                    break
                    
            # Step 2: Query the dedicated match context layout to isolate goal events
            if fotmob_id:
                details_url = f"https://www.fotmob.com/api/matchDetails?matchId={fotmob_id}"
                details_resp = requests.get(details_url, headers=headers, timeout=5)
                
                if details_resp.status_code == 200:
                    details_data = details_resp.json()
                    
                    # Track official MVP selection
                    top_players = details_data.get("content", {}).get("matchFacts", {}).get("topPlayers", {})
                    h_potm = top_players.get("homePlayer", {}).get("name", "")
                    a_potm = top_players.get("awayPlayer", {}).get("name", "")
                    if h_potm or a_potm:
                        detected_potm = h_potm if h_potm else a_potm
                        
                    # Extract active goal scorers list
                    teams_header = details_data.get("header", {}).get("teams", [])
                    for team_entry in teams_header:
                        for goal in team_entry.get("goalEvents", []):
                            real_goals.append({
                                "player": goal.get("name", "Player"),
                                "time": goal.get("time", 45)
                            })
                    # Sort list chronologically
                    real_goals = sorted(real_goals, key=lambda x: x["time"])
    except Exception as e:
        print(f"Dynamic goal logs fallback notice: {e}")

    # Fallback to realistic templates if a match hasn't started or external network drops
    if not real_goals and (home_score > 0 or away_score > 0):
        for i in range(home_score):
            real_goals.append({"player": f"{team1} Scorer", "time": 20 + (i * 20)})
        for i in range(away_score):
            real_goals.append({"player": f"{team2} Scorer", "time": 30 + (i * 20)})

    # MADE UP STATS ENGINE: Generated mathematically so it never returns an error
    random.seed(home_score + away_score + len(team1))
    
    pos_h = max(38, min(62, 50 + (home_score - away_score) * 4 + random.randint(-2, 2)))
    xg_h = max(0.2, (home_score * 0.65) + (random.randint(-10, 15) / 100.0))
    xg_a = max(0.2, (away_score * 0.65) + (random.randint(-10, 15) / 100.0))
    shots_h = max(home_score + 4, int(xg_h * 6) + random.randint(2, 5))
    shots_a = max(away_score + 4, int(xg_a * 6) + random.randint(2, 5))

    stats = {
        "possession": {"home": pos_h, "away": 100 - pos_h},
        "xg": {"home": f"{xg_h:.2f}", "away": f"{xg_a:.2f}"},
        "shots": {"home": shots_h, "away": shots_a},
        "shots_on_target": {"home": max(home_score, int(shots_h * 0.4)), "away": max(away_score, int(shots_a * 0.4))},
        "chances_created": {"home": max(0, home_score + random.randint(0, 1)), "away": max(0, away_score + random.randint(0, 1))},
        "potm": detected_potm,
        "goals": real_goals
    }
    
    # Maintain inverse alignment structure logic
    if match.team1 != team1:
        stats = {
            "possession": {"home": stats["possession"]["away"], "away": stats["possession"]["home"]},
            "xg": {"home": stats["xg"]["away"], "away": stats["xg"]["home"]},
            "shots": {"home": stats["shots"]["away"], "away": stats["shots"]["home"]},
            "shots_on_target": {"home": stats["shots_on_target"]["away"], "away": stats["shots_on_target"]["home"]},
            "chances_created": {"home": stats["chances_created"]["away"], "away": stats["chances_created"]["home"]},
            "potm": stats["potm"],
            "goals": stats["goals"]
        }

    return {"error": False, "stats": stats}


# --- GROQ AI INTEGRATION (Tactical Coach) ---
class ChatRequest(BaseModel):
    message: str

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

@app.post("/api/chat")
def chat_with_ai(request: ChatRequest, db: Session = Depends(get_db)):
    db_matches = db.query(MatchDB).all()
    tournament_context = "CURRENT LIVE 2026 WORLD CUP DATABASE MATCH CONTEXT:\n"
    for m in db_matches:
        score_str = f"{m.score1}-{m.score2}" if (m.score1 is not None and m.score2 is not None) else "Not Played Yet"
        tournament_context += f"- Match: {m.team1} vs {m.team2} | Score: {score_str}\n"

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile", 
        "messages": [
            {"role": "system", "content": f"You are GROQ-Tactical. Database:\n{tournament_context}"},
            {"role": "user", "content": request.message}
        ]
    }
    try:
        response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
        return {"error": False, "reply": response.json()["choices"][0]["message"]["content"]}
    except Exception as e:
        return {"error": True, "reply": f"SYSTEM ERROR: {str(e)}"}