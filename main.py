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
    sofascore_id = Column(String, nullable=True) 

Base.metadata.create_all(bind=engine)

# --- FastAPI Setup ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://wc26-woad-six.vercel.app", "http://localhost:3000"],
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

# --- Football API Integration (Scores & Auto-Sync) ---
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


# --- AI MYTHICAL ARENA ENGINE ---
class ComparisonRequest(BaseModel):
    item1: str
    item2: str
    mode: str  # "teams" or "players"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

@app.post("/api/ai-compare")
def simulate_mythical_battle(request: ComparisonRequest):
    if not GROQ_API_KEY:
        return {"error": True, "message": "Groq API key missing on the server environment."}

    # Set up mode-specific labels
    if request.mode == "players":
        stats_labels = ["Pace", "Shooting", "Passing", "Dribbling", "Defence", "Physical"]
    else:
        stats_labels = ["Tactical Ability", "Firepower", "Defensive Solidity", "Midfield Control", "Team Chemistry", "Squad Depth"]

    # Structural prompt system that forces output matching exactly our keys
    system_prompt = (
        "You are an elite football analytics engine specialized in historical mythical matchups.\n"
        "You must simulate the requested confrontation and respond ONLY with a valid, raw JSON object matching the requested schema exactly.\n"
        "Do not include any markdown format tags, backticks, or text outside the JSON block.\n\n"
        "JSON Schema Requirement:\n"
        "{\n"
        "  \"verdict\": \"A detailed 3-sentence expert breakdown detailing how this match/clash runs tactically in their absolute primes.\",\n"
        "  \"title1\": \"Formatted Name of Side A\",\n"
        "  \"title2\": \"Formatted Name of Side B\",\n"
        "  \"score1\": \"Numeric metric value (e.g., goals scored if teams, or overall composite rating out of 100 if players)\",\n"
        "  \"score2\": \"Numeric metric value (e.g., goals scored if teams, or overall composite rating out of 100 if players)\",\n"
        "  \"potm\": \"Name of the standout individual performer or winner\",\n"
        "  \"stats\": [\n"
        f"    {{\"label\": \"{stats_labels[0]}\", \"home\": 85, \"away\": 90}},\n"
        f"    {{\"label\": \"{stats_labels[1]}\", \"home\": 78, \"away\": 82}},\n"
        f"    {{\"label\": \"{stats_labels[2]}\", \"home\": 80, \"away\": 75}},\n"
        f"    {{\"label\": \"{stats_labels[3]}\", \"home\": 72, \"away\": 88}},\n"
        f"    {{\"label\": \"{stats_labels[4]}\", \"home\": 90, \"away\": 95}},\n"
        f"    {{\"label\": \"{stats_labels[5]}\", \"home\": 85, \"away\": 88}}\n"
        "  ]\n"
        "}\n\n"
        f"CRUCIAL: You MUST strictly use the exact 6 labels in the 'stats' array: "
        f"'{stats_labels[0]}', '{stats_labels[1]}', '{stats_labels[2]}', '{stats_labels[3]}', '{stats_labels[4]}', and '{stats_labels[5]}'. "
        "For each metric, assign realistic integer attributes (0-100) representing their respective tactical strengths in that area."
    )

    user_prompt = f"Simulate this mythical debate: {request.item1} versus {request.item2}. The operational simulation mode is '{request.mode}'."

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
    }

    try:
        response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=12)
        if response.status_code != 200:
            return {"error": True, "message": f"Groq network error: {response.text}"}
            
        ai_content = response.json()["choices"][0]["message"]["content"]
        parsed_data = json.loads(ai_content)
        return {"error": False, "arena": parsed_data}
    except Exception as e:
        return {"error": True, "message": f"Simulation failure: {str(e)}"}


# --- GROQ AI INTEGRATION (Tactical Coach) ---
class ChatRequest(BaseModel):
    message: str

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