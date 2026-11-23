"""Single-user recipe cloud + TikTok transcription. Requires a persistent DATA_DIR volume."""
import base64, io, json, os, re, secrets, sqlite3, tempfile, threading
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp

app=FastAPI(title='Mon Carnet Gourmand Cloud')
origins=[x.strip().rstrip('/') for x in os.getenv('ALLOWED_ORIGINS','').split(',') if x.strip()]
app.add_middleware(CORSMiddleware,allow_origins=origins,allow_methods=['GET','POST','PUT'],allow_headers=['Authorization','Content-Type'])
DATA_DIR=Path(os.getenv('DATA_DIR','/data'));DATA_DIR.mkdir(parents=True,exist_ok=True)
DB=DATA_DIR/'recipes.sqlite3';lock=threading.Lock();whisper_lock=threading.Lock();whisper_model=None
with sqlite3.connect(DB) as db:
    db.execute('CREATE TABLE IF NOT EXISTS store (id INTEGER PRIMARY KEY CHECK (id=1), recipes TEXT NOT NULL)')
    db.execute("INSERT OR IGNORE INTO store (id,recipes) VALUES (1,'[]')")

def auth(authorization):
    secret=os.getenv('CARNET_SECRET','')
    if len(secret)<20: raise HTTPException(503,'Configure CARNET_SECRET (au moins 20 caractères).')
    if not authorization or not authorization.startswith('Bearer ') or not secrets.compare_digest(authorization[7:],secret):
        raise HTTPException(401,'Clé secrète incorrecte.')

class RecipesPayload(BaseModel):
    recipes:list[dict]
class ExtractPayload(BaseModel):
    url:str

@app.get('/health')
def health():return {'ok':True}

@app.get('/recipes')
def get_recipes(authorization:str|None=Header(default=None)):
    auth(authorization)
    with lock,sqlite3.connect(DB) as db: data=db.execute('SELECT recipes FROM store WHERE id=1').fetchone()[0]
    return {'recipes':json.loads(data)}

@app.put('/recipes')
def put_recipes(payload:RecipesPayload,authorization:str|None=Header(default=None)):
    auth(authorization)
    if len(payload.recipes)>3000:raise HTTPException(413,'Trop de recettes.')
    for r in payload.recipes:
        if not isinstance(r.get('id'),str) or not isinstance(r.get('title'),str):raise HTTPException(422,'Recette invalide.')
    raw=json.dumps(payload.recipes,ensure_ascii=False)
    if len(raw.encode())>35_000_000:raise HTTPException(413,'Sauvegarde trop volumineuse (photos).')
    with lock,sqlite3.connect(DB) as db:db.execute('UPDATE store SET recipes=? WHERE id=1',(raw,))
    return {'ok':True,'count':len(payload.recipes)}

def recipe_from_text(description,transcript,title):
    text='\n'.join(x for x in [description,transcript] if x)
    sentences=[x.strip(' -•\t') for x in re.split(r'(?<=[.!?])\s+|\n+',text) if x.strip()]
    qty=re.compile(r'(?i)\b\d+[\d,. /]*\s*(?:g|kg|ml|cl|litres?|c\.?\s*à\s*(?:soupe|café)|cuillères?|oeufs?|œufs?|pincées?)\b')
    actions=re.compile(r'(?i)\b(préchauffe\w*|enfourne\w*|mélange\w*|ajoute\w*|incorpore\w*|verse\w*|fouette\w*|pétris\w*|laisse\w*|réserve\w*|coupe\w*|étale\w*|fais cuire|faites cuire)\b')
    ingredients=[];steps=[]
    for line in sentences:
        if actions.search(line):steps.append(line)
        elif qty.search(line) and len(line)<160:ingredients.append(line)
    cook=re.search(r'(?i)(\d+\s*(?:à|a|-)\s*\d+|\d+)\s*(?:minutes?|min)\b',text)
    temp=re.search(r'(?i)(\d{2,3})\s*°?\s*[cC]\b',text)
    clean_title=re.sub(r'#\w+','',title or '').strip(' .:-')[:100] or 'Recette TikTok à compléter'
    return {'title':clean_title,'category':'Autres','servings':'','prep':'','cook':(cook.group(0) if cook else ''),'rest':'','temp':(temp.group(1)+' °C' if temp else ''),'ingredients':ingredients,'steps':steps,'notes':'Extraction automatique : vérifie les quantités.\n\nDescription :\n'+description[:6000]+'\n\nTranscription :\n'+transcript[:12000]}

@app.post('/extract')
def extract(payload:ExtractPayload,authorization:str|None=Header(default=None)):
    auth(authorization)
    parsed=urlparse(payload.url);host=(parsed.hostname or '').lower()
    if parsed.scheme!='https' or not (host=='tiktok.com' or host.endswith('.tiktok.com')):raise HTTPException(400,'Lien TikTok HTTPS invalide.')
    with tempfile.TemporaryDirectory() as folder:
        opts={'format':'bestaudio/best','outtmpl':str(Path(folder)/'video.%(ext)s'),'noplaylist':True,'quiet':True,'no_warnings':True,'socket_timeout':25,'retries':1,'match_filter':yt_dlp.utils.match_filter_func('duration <= 600')}
        try:
            with yt_dlp.YoutubeDL(opts) as dl:info=dl.extract_info(payload.url,download=True)
        except Exception as e:raise HTTPException(422,'Vidéo TikTok inaccessible ou téléchargement refusé.') from e
        description=(info.get('description') or '')[:8000]
        title=(info.get('title') or description.split('\n')[0] or '')[:120]
        files=[f for f in Path(folder).glob('video.*') if f.is_file()]
        transcript=''
        if files:
            try:
                global whisper_model
                with whisper_lock:
                    if whisper_model is None:
                        from faster_whisper import WhisperModel
                        whisper_model=WhisperModel(os.getenv('WHISPER_MODEL','small'),device='cpu',compute_type='int8')
                    segments,_=whisper_model.transcribe(str(files[0]),language='fr',vad_filter=True,beam_size=5,condition_on_previous_text=False)
                    transcript=' '.join(x.text.strip() for x in segments)
            except Exception as e:raise HTTPException(503,'Transcription audio indisponible. Vérifie les ressources du serveur.') from e
        recipe=recipe_from_text(description,transcript,title)
        thumbnail=info.get('thumbnail') or ''
        if thumbnail.startswith('https://'):
            try:
                req=Request(thumbnail,headers={'User-Agent':'Mozilla/5.0'})
                with urlopen(req,timeout=10) as response:
                    if response.headers.get('Content-Type','').startswith('image/'):
                        data=response.read(1_500_001)
                        if len(data)<=1_500_000:
                            from PIL import Image
                            im=Image.open(io.BytesIO(data)).convert('RGB');im.thumbnail((850,850));out=io.BytesIO();im.save(out,format='JPEG',quality=72)
                            recipe['photo']='data:image/jpeg;base64,'+base64.b64encode(out.getvalue()).decode()
            except Exception:pass
        return {'recipe':recipe,'warning':'Les vidéos sans explication des gestes peuvent donner des étapes incomplètes.'}
