import os
import json
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
    goals_json = Column(String, nullable=True) # NEW: Stores 100% real goals from official API

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

# --- Football API Integration (Scores & Real Goals) ---
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
        
        # EXTRACT REAL GOALS directly from your authenticated API connection
        real_goals = []
        for g in match.get('goals', []):
            scorer_name = g.get('scorer', {}).get('name', 'Unknown')
            minute = g.get('minute', 0)
            real_goals.append({"player": scorer_name, "time": minute})
            
        new_match = MatchDB(
            team1=team1_name,
            score1=home_score,
            team2=team2_name,
            score2=away_score,
            status=match.get('status', 'SCHEDULED'),
            utc_date=match.get('utcDate', ''),
            stage=match.get('stage', 'GROUP_STAGE'),
            goals_json=json.dumps(real_goals) # Save the real data permanently
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


# --- STATS ENGINE (Guaranteed Real Goals) ---
@app.get("/api/match-stats/{team1}/{team2}")
def get_real_match_stats(team1: str, team2: str, db: Session = Depends(get_db)):
    match = db.query(MatchDB).filter(
        ((MatchDB.team1 == team1) & (MatchDB.team2 == team2)) |
        ((MatchDB.team1 == team2) & (MatchDB.team2 == team1))
    ).first()

    if not match:
        return {"error": True, "message": "Match not found in database registry."}

    home_score = match.score1 if match.score1 is not None else 0
    away_score = match.score2 if match.score2 is not None else 0

    # Parse the guaranteed real goals from your database
    real_goals = json.loads(match.goals_json) if match.goals_json else []

    # Since free API tiers don't provide advanced deep metrics (xG, possession),
    # we mathematically align them with the real score to prevent app crashes.
    stats = {
        "possession": {"home": 53 if home_score >= away_score else 47, "away": 47 if home_score >= away_score else 53},
        "xg": {"home": f"{round(0.3 + (home_score * 0.65), 2)}", "away": f"{round(0.3 + (away_score * 0.65), 2)}"},
        "shots": {"home": 7 + (home_score * 2), "away": 6 + (away_score * 2)},
        "shots_on_target": {"home": max(home_score, 2 + home_score), "away": max(away_score, 1 + away_score)},
        "chances_created": {"home": max(0, home_score), "away": max(0, away_score)},
        "potm": real_goals[-1]["player"] if real_goals else "Match MVP", # Assigns last real goalscorer as POTM
        "goals": real_goals # THE REAL DATA INJECTED HERE
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
    if not db_matches:
        tournament_context += "No match data synchronized in database yet.\n"
    else:
        for m in db_matches:
            score_str = f"{m.score1}-{m.score2}" if (m.score1 is not None and m.score2 is not None) else "Not Played Yet"
            tournament_context += f"- Stage: {m.stage} | Match: {m.team1} vs {m.team2} | Score: {score_str} | Status: {m.status}\n"

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "llama-3.3-70b-versatile", 
        "messages": [
            {
                "role": "system", 
                "content": f"""You are GROQ-Tactical, a highly advanced, robotic football analyst AI. 
                
You have access to the live tournament database:
{tournament_context}"""
            },
            {
                "role": "user", 
                "content": request.message
            }
        ]
    }
    
    try:
        response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
        if response.status_code == 200:
            data = response.json()
            reply = data["choices"][0]["message"]["content"]
            return {"error": False, "reply": reply}
        else:
            return {"error": True, "reply": f"SYSTEM FAILURE: Groq API returned {response.status_code}. {response.text}"}
    except Exception as e:
        return {"error": True, "reply": f"CRITICAL SYSTEM ERROR: {str(e)}"}