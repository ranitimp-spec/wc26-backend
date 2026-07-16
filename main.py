import os
import urllib.parse
import json
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from pydantic import BaseModel
import requests
from playwright.sync_api import sync_playwright

# --- Database Setup ---
DATABASE_URL = "sqlite:///./football.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class MatchDB(Base):
    __tablename__ = "matches"
    id = Column(Integer, primary_key=True, index=True)
    team1 = Column(String, index=True)
    score1 = Column(Integer)
    team2 = Column(String)
    score2 = Column(Integer)
    status = Column(String)
    utc_date = Column(String)
    stage = Column(String)
    sofascore_id = Column(String, nullable=True) 

Base.metadata.create_all(bind=engine)

# --- FastAPI Setup ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://wc26-woad-six.vercel.app"],  # Your explicit frontend URL
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

# --- Football API Integration (Scores) ---
FOOTBALL_API_KEY = "5cd9e16068fe417b9815290010d55d87" 

@app.post("/api/sync")
def sync_live_matches(db: Session = Depends(get_db)):
    headers = { 'X-Auth-Token': FOOTBALL_API_KEY }
    response = requests.get('http://api.football-data.org/v4/competitions/WC/matches', headers=headers)
    
    if response.status_code != 200:
        return {"error": "Failed to fetch from API", "details": response.json()}
        
    data = response.json()
    matches = data.get('matches', [])
    
    db.query(MatchDB).delete()
    
    matches_added = 0
    for match in matches: 
        score = match.get('score', {}).get('fullTime', {})
        home_score = score.get('home') if score.get('home') is not None else 0
        away_score = score.get('away') if score.get('away') is not None else 0
        
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
    return {"message": f"Successfully synced {matches_added} matches!"}

@app.get("/api/matches")
def get_matches(db: Session = Depends(get_db)):
    return db.query(MatchDB).all()


# --- AUTOMATED PLAYWRIGHT SCRAPERS (Deep Match Stats) ---
def scrape_match_data_playwright(team1: str, team2: str, existing_id: str = None):
    with sync_playwright() as p:
        # CRITICAL FIX: headless=False forces a real browser window to bypass Cloudflare
        # STEALTH HEADLESS: Runs invisibly but hides the bot flags from Cloudflare
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled", 
                "--headless=new"
            ]
        )
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()
        
        try:
            current_id = existing_id
            
            if not current_id:
                # Step 1: Search for team1 to find its Sofascore entity ID
                encoded_query = urllib.parse.quote(team1)
                search_url = f"https://api.sofascore.com/api/v1/search/all?q={encoded_query}&page=0"
                
                page.goto(search_url)
                content = page.locator("body").inner_text()
                search_data = json.loads(content)
                
                team_id = None
                for result in search_data.get('results', []):
                    if result.get('type') == 'team':
                        team_id = str(result.get('entity', {}).get('id'))
                        break
                
                # Step 2: Query team1's recent and upcoming fixtures to find the opponent match (team2)
                if team_id:
                    events_urls = [
                        f"https://api.sofascore.com/api/v1/team/{team_id}/events/last/0",
                        f"https://api.sofascore.com/api/v1/team/{team_id}/events/next/0"
                    ]
                    
                    for url in events_urls:
                        try:
                            page.goto(url)
                            events_content = page.locator("body").inner_text()
                            events_data = json.loads(events_content)
                            
                            for event in events_data.get('events', []):
                                home_name = event.get('homeTeam', {}).get('name', '').lower()
                                away_name = event.get('awayTeam', {}).get('name', '').lower()
                                
                                # Use fuzzy logic to see if team2 is in either side of the matchup
                                if team2.lower() in home_name or team2.lower() in away_name:
                                    current_id = str(event.get('id'))
                                    break
                            if current_id:
                                break
                        except Exception:
                            continue  # Fallback to next endpoint if one fails
            
            if not current_id:
                return None, {"error": True, "message": f"Headless browser could not find a match ID for {team1} vs {team2}."}
                
            page.goto(f"https://api.sofascore.com/api/v1/event/{current_id}/statistics")
            stats_content = page.locator("body").inner_text()
            stats_data = json.loads(stats_content)
            
            page.goto(f"https://api.sofascore.com/api/v1/event/{current_id}/incidents")
            incidents_content = page.locator("body").inner_text()
            incidents_data = json.loads(incidents_content)
            
            return current_id, {"stats": stats_data, "incidents": incidents_data}
            
        except Exception as e:
            return None, {"error": True, "message": f"Browser engine failed: {str(e)}"}
        finally:
            browser.close()

@app.get("/api/match-stats/{team1}/{team2}")
def get_live_sofascore_stats(team1: str, team2: str, db: Session = Depends(get_db)):
    match = db.query(MatchDB).filter(
        ((MatchDB.team1 == team1) & (MatchDB.team2 == team2)) |
        ((MatchDB.team1 == team2) & (MatchDB.team2 == team1))
    ).first()

    if not match:
        return {"error": True, "message": "Match not found in local database."}

    new_id, scraped_data = scrape_match_data_playwright(team1, team2, match.sofascore_id)
    
    if scraped_data.get('error'):
        return scraped_data

    if new_id and new_id != match.sofascore_id:
        match.sofascore_id = new_id
        db.commit()

    stats_groups = scraped_data['stats'].get('statistics', [{}])[0].get('groups', [])
    incidents_data = scraped_data['incidents'].get('incidents', [])
    
    goals = []
    for incident in incidents_data:
        if incident.get('incidentType') == 'goal':
            goals.append({
                "player": incident.get('player', {}).get('name', 'Unknown'),
                "time": incident.get('time', 0)
            })
    
    parsed_stats = {
        "possession": {"home": 50, "away": 50},
        "xg": {"home": "0.00", "away": "0.00"},
        "shots": {"home": 0, "away": 0},
        "shots_on_target": {"home": 0, "away": 0},
        "chances_created": {"home": 0, "away": 0},
        "potm": "Unavailable", 
        "goals": goals
    }

    for group in stats_groups:
        for item in group.get('statisticsItems', []):
            name = item.get('name')
            home_val = item.get('home')
            away_val = item.get('away')
            
            if name == "Ball possession":
                parsed_stats["possession"]["home"] = int(home_val.replace('%', '')) if type(home_val) == str else home_val
                parsed_stats["possession"]["away"] = int(away_val.replace('%', '')) if type(away_val) == str else away_val
            elif name == "Expected goals":
                parsed_stats["xg"]["home"] = home_val
                parsed_stats["xg"]["away"] = away_val
            elif name == "Total shots":
                parsed_stats["shots"]["home"] = home_val
                parsed_stats["shots"]["away"] = away_val
            elif name == "Shots on target":
                parsed_stats["shots_on_target"]["home"] = home_val
                parsed_stats["shots_on_target"]["away"] = away_val
            elif name == "Big chances created":
                parsed_stats["chances_created"]["home"] = home_val
                parsed_stats["chances_created"]["away"] = away_val

    return {"error": False, "stats": parsed_stats}


# --- GROQ AI INTEGRATION (Tactical Coach) ---
class ChatRequest(BaseModel):
    message: str

# TODO: PASTE YOUR REAL GROQ API KEY HERE!
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

@app.post("/api/chat")
def chat_with_ai(request: ChatRequest):
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "llama-3.3-70b-versatile", 
        "messages": [
            {
                "role": "system", 
                "content": """You are GROQ-Tactical, a highly advanced, robotic football analyst AI. You speak with a clinical, tactical, and slightly robotic tone. 
                
CRITICAL DIRECTIVE: When a user asks for a PREDICTION about a match or tournament, you MUST generate a heavily detailed, multi-tiered analysis in the following format:
**TACTICAL MATCHUP:** Break down the formations and styles of play.
**KEY BATTLES:** Identify 2-3 specific player matchups that will decide the game.
**WIN PROBABILITY:** Give exact percentages (e.g., Team A: 45%, Draw: 25%, Team B: 30%).
**PREDICTED SCORELINE:** Give your exact final score prediction with a brief robotic justification.

If they are not asking for a prediction, provide deep, analytical football insight in a concise manner."""
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