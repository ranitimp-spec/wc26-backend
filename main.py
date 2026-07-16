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


# --- REAL MATCH STATS ENGINE (Via FotMob API Gateway) ---
@app.get("/api/match-stats/{team1}/{team2}")
def get_real_match_stats(team1: str, team2: str, db: Session = Depends(get_db)):
    match = db.query(MatchDB).filter(
        ((MatchDB.team1 == team1) & (MatchDB.team2 == team2)) |
        ((MatchDB.team1 == team2) & (MatchDB.team2 == team1))
    ).first()

    if not match:
        return {"error": True, "message": "Match not found in local database."}

    # Format date from match timeline to match YYYYMMDD string parameters
    date_str = match.utc_date[:10].replace("-", "")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        # Step 1: Scan target matchday layout for the proper ID frame
        day_url = f"https://www.fotmob.com/api/matches?date={date_str}"
        day_resp = requests.get(day_url, headers=headers, timeout=8)
        
        fotmob_id = None
        is_inverted = False
        
        if day_resp.status_code == 200:
            day_data = day_resp.json()
            # Iterate through available league configurations
            for league in day_data.get("leagues", []):
                for m in league.get("matches", []):
                    h_name = m.get("home", {}).get("name", "").lower()
                    a_name = m.get("away", {}).get("name", "").lower()
                    
                    if team1.lower() in h_name and team2.lower() in a_name:
                        fotmob_id = m.get("id")
                        break
                    elif team2.lower() in h_name and team1.lower() in a_name:
                        fotmob_id = m.get("id")
                        is_inverted = True
                        break
                        
        if not fotmob_id:
            return {"error": True, "message": f"Real-time analytics path unavailable for {team1} vs {team2}."}

        # Step 2: Grab full details directly from the computed entry layout
        details_url = f"https://www.fotmob.com/api/matchDetails?matchId={fotmob_id}"
        details_resp = requests.get(details_url, headers=headers, timeout=8)
        
        if details_resp.status_code != 200:
            return {"error": True, "message": "Failed to pull match analytics data profile."}
            
        data = details_resp.json()
        
        # Base setup defaults
        parsed_stats = {
            "possession": {"home": 50, "away": 50},
            "xg": {"home": "0.00", "away": "0.00"},
            "shots": {"home": 0, "away": 0},
            "shots_on_target": {"home": 0, "away": 0},
            "chances_created": {"home": 0, "away": 0},
            "potm": "Unavailable",
            "goals": []
        }

        # Pull match events directly
        content = data.get("content", {})
        
        # Parse official player performance evaluation structures
        top_players = content.get("matchFacts", {}).get("topPlayers", {})
        home_potm = top_players.get("homePlayer", {}).get("name", "")
        away_potm = top_players.get("awayPlayer", {}).get("name", "")
        parsed_stats["potm"] = home_potm if home_potm else (away_potm if away_potm else "Unavailable")

        # Parse goals data with absolute accuracy
        for event in data.get("header", {}).get("teams", []):
            for g in event.get("goalEvents", []):
                parsed_stats["goals"].append({
                    "player": g.get("name", "Unknown Player"),
                    "time": g.get("time", 0)
                })

        # Process team statistic metrics grid arrays
        stats_groups = content.get("stats", {}).get("stats", [])
        if stats_groups:
            # Look inside the first layout group tab matrix
            stats_list = stats_groups[0].get("stats", [])
            for stat in stats_list:
                title = stat.get("title")
                stats_val = stat.get("stats", [0, 0])
                
                if title == "Ball possession":
                    parsed_stats["possession"]["home"] = int(stats_val[0])
                    parsed_stats["possession"]["away"] = int(stats_val[1])
                elif title == "Expected goals (xG)":
                    parsed_stats["xg"]["home"] = str(stats_val[0])
                    parsed_stats["xg"]["away"] = str(stats_val[1])
                elif title == "Total shots":
                    parsed_stats["shots"]["home"] = int(stats_val[0])
                    parsed_stats["shots"]["away"] = int(stats_val[1])
                elif title == "Shots on target":
                    parsed_stats["shots_on_target"]["home"] = int(stats_val[0])
                    parsed_stats["shots_on_target"]["away"] = int(stats_val[1])
                elif title == "Big chances":
                    parsed_stats["chances_created"]["home"] = int(stats_val[0])
                    parsed_stats["chances_created"]["away"] = int(stats_val[1])

        # Safely mirror dimensions dynamically if inverted matching occurred
        if is_inverted:
            swapped_stats = {
                "possession": {"home": parsed_stats["possession"]["away"], "away": parsed_stats["possession"]["home"]},
                "xg": {"home": parsed_stats["xg"]["away"], "away": parsed_stats["xg"]["home"]},
                "shots": {"home": parsed_stats["shots"]["away"], "away": parsed_stats["shots"]["home"]},
                "shots_on_target": {"home": parsed_stats["shots_on_target"]["away"], "away": parsed_stats["shots_on_target"]["home"]},
                "chances_created": {"home": parsed_stats["chances_created"]["away"], "away": parsed_stats["chances_created"]["home"]},
                "potm": parsed_stats["potm"],
                "goals": parsed_stats["goals"]
            }
            return {"error": False, "stats": swapped_stats}

        return {"error": False, "stats": parsed_stats}

    except Exception as e:
        return {"error": True, "message": f"Global Analytics Engine Error: {str(e)}"}


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