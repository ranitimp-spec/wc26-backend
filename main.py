import os
import urllib.parse
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
    sofascore_id = Column(String, nullable=True) 

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


# --- DYNAMIC AI MATCH STATS ENGINE (With Strict 2026 Grounding) ---
@app.get("/api/match-stats/{team1}/{team2}")
def get_live_sofascore_stats(team1: str, team2: str, db: Session = Depends(get_db)):
    match = db.query(MatchDB).filter(
        ((MatchDB.team1 == team1) & (MatchDB.team2 == team2)) |
        ((MatchDB.team1 == team2) & (MatchDB.team2 == team1))
    ).first()

    if not match:
        return {"error": True, "message": "Match not found in local database."}

    is_inverted = (match.team1 != team1)
    home_team = match.team1
    away_team = match.team2
    home_score = match.score1 if match.score1 is not None else 0
    away_score = match.score2 if match.score2 is not None else 0

    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    if not GROQ_API_KEY:
        return {"error": True, "message": "Groq API token configuration missing on server."}

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Enforced 2026 timeline parameters with explicit whitelists & blacklists
    prompt = (
        f"It is the current year 2026. Generate a highly realistic tactical match statistics profile and goal events "
        f"for a 2026 World Cup fixture where {home_team} played at home against {away_team} away. "
        f"The verified final score line was: {home_team} {home_score} - {away_score} {away_team}. "
        f"\n\nCRITICAL TIMELINE CONSTRAINTS:\n"
        f"1. The goals array object MUST contain EXACTLY {home_score} goal(s) scored by current, active 2026 roster players from {home_team} "
        f"and EXACTLY {away_score} goal(s) scored by current, active 2026 roster players from {away_team}.\n"
        f"2. ABSOLUTELY FORBIDDEN: Do not use any players who retired from international football prior to 2026. For example, "
        f"Angel Di Maria, Eden Hazard, Toni Kroos, Gareth Bale, and Thiago Silva are STRICTLY FORBIDDEN.\n"
        f"3. ROSTER ANCHORS: Only pick from highly plausible active elite players. For example:\n"
        f"   - Argentina: Lionel Messi, Julián Álvarez, Lautaro Martínez, Alexis Mac Allister, Enzo Fernández, Alejandro Garnacho, Rodrigo De Paul.\n"
        f"   - England: Harry Kane, Jude Bellingham, Bukayo Saka, Phil Foden, Cole Palmer, Ollie Watkins, Declan Rice.\n"
        f"   - France: Kylian Mbappé, Marcus Thuram, Ousmane Dembélé, Bradley Barcola, Antoine Griezmann.\n"
        f"   - Spain: Lamine Yamal, Nico Williams, Dani Olmo, Álvaro Morata, Pedri, Gavi.\n"
        f"Ensure all simulated metrics (possession splits, xG values, shot counts, and POTM) match the intensity of this specific scoreline."
    )

    payload = {
        "model": "llama-3.3-70b-versatile",
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an elite, highly precise football analytics data engine. Your sole purpose is to synthesize "
                    "incredibly authentic statistical summaries for completed matches. You must output exclusively a valid "
                    "JSON object matching this strict layout with no markdown formatting tags, explanation text, or extra characters:\n"
                    "{\n"
                    "  \"possession\": {\"home\": 52, \"away\": 48},\n"
                    "  \"xg\": {\"home\": \"1.42\", \"away\": \"1.10\"},\n"
                    "  \"shots\": {\"home\": 12, \"away\": 9},\n"
                    "  \"shots_on_target\": {\"home\": 5, \"away\": 3},\n"
                    "  \"chances_created\": {\"home\": 2, \"away\": 2},\n"
                    "  \"potm\": \"Player Name\",\n"
                    "  \"goals\": [{\"player\": \"Player Name\", \"time\": 42}]\n"
                    "}"
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
    }

    try:
        response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
        if response.status_code == 200:
            ai_data = response.json()
            stats_json = json.loads(ai_data["choices"][0]["message"]["content"])
            
            if is_inverted:
                swapped_stats = {
                    "possession": {"home": stats_json["possession"]["away"], "away": stats_json["possession"]["home"]},
                    "xg": {"home": stats_json["xg"]["away"], "away": stats_json["xg"]["home"]},
                    "shots": {"home": stats_json["shots"]["away"], "away": stats_json["shots"]["home"]},
                    "shots_on_target": {"home": stats_json["shots_on_target"]["away"], "away": stats_json["shots_on_target"]["home"]},
                    "chances_created": {"home": stats_json["chances_created"]["away"], "away": stats_json["chances_created"]["home"]},
                    "potm": stats_json.get("potm", "Unavailable"),
                    "goals": stats_json.get("goals", [])
                }
                return {"error": False, "stats": swapped_stats}
                
            return {"error": False, "stats": stats_json}
        else:
            return {"error": True, "message": f"AI Engine configuration error: Status {response.status_code}"}
    except Exception as e:
        return {"error": True, "message": f"Failed to compute match analytics frame: {str(e)}"}


# --- GROQ AI INTEGRATION (Tactical Coach) ---
class ChatRequest(BaseModel):
    message: str

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

    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "llama-3.3-70b-versatile", 
        "messages": [
            {
                "role": "system", 
                "content": f"""You are GROQ-Tactical, a highly advanced, robotic football analyst AI. You speak with a clinical, tactical, and slightly robotic tone. 
                
CRITICAL DIRECTIVE: When a user asks for a PREDICTION about a match or tournament, you MUST generate a heavily detailed, multi-tiered analysis in the following format:
**TACTICAL MATCHUP:** Break down the formations and styles of play.
**KEY BATTLES:** Identify 2-3 specific player matchups that will decide the game.
**WIN PROBABILITY:** Give exact percentages (e.g., Team A: 45%, Draw: 25%, Team B: 30%).
**PREDICTED SCORELINE:** Give your exact final score prediction with a brief robotic justification.

If they are not asking for a prediction, provide deep, analytical football insight in a concise manner.

You have access to the live tournament database. Use this data to accurately answer questions about current teams, who is playing, scores, or tournament progress:
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