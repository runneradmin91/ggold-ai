import os, time, hashlib, hmac
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
import xml.etree.ElementTree as ET
import requests
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
load_dotenv()

APP_NAME = "Gold AI MT5 Bridge"
ACCESS_TOKEN = os.getenv("GOLD_AI_ACCESS_TOKEN", "").strip()
MT5_SYMBOL = os.getenv("MT5_SYMBOL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5").strip()
ALLOWED_ORIGINS = [x.strip() for x in os.getenv("ALLOWED_ORIGINS", "").split(",") if x.strip()]
NEWS_URLS = [
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("BLS", "https://www.bls.gov/feed/bls_latest.rss"),
]
app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=False, allow_methods=["GET"], allow_headers=["Authorization", "Content-Type"])
_mt5 = None
_news_cache = {"at": 0, "items": []}
_ai_cache = {"key": "", "at": 0, "text": ""}

def require_auth(authorization: Optional[str]):
    if not ACCESS_TOKEN or len(ACCESS_TOKEN) < 32:
        raise HTTPException(503, "الخادم غير مضبوط: عيّن GOLD_AI_ACCESS_TOKEN بطول 32 حرفًا على الأقل.")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not supplied or not hmac.compare_digest(supplied, ACCESS_TOKEN):
        raise HTTPException(401, "رمز الوصول غير صحيح.")

def mt5mod():
    global _mt5
    if _mt5 is not None: return _mt5
    try:
        import MetaTrader5 as mt5
    except Exception as exc:
        raise HTTPException(503, "حزمة MetaTrader5 غير مثبتة. شغّل الخادم على Windows مع MT5 terminal.") from exc
    if not mt5.initialize():
        err = mt5.last_error()
        raise HTTPException(503, f"تعذر الاتصال ببرنامج MT5. افتح الطرفية وسجّل الدخول، ثم تحقق من الإعدادات. رمز الخطأ: {err}")
    _mt5 = mt5
    return mt5

def find_symbol(mt5):
    candidates = [MT5_SYMBOL] if MT5_SYMBOL else []
    candidates += ["XAUUSD", "XAUUSDm", "XAUUSD.", "GOLD", "GOLD.", "XAUUSD#"]
    seen=set()
    for s in candidates:
        if not s or s in seen: continue
        seen.add(s)
        info=mt5.symbol_info(s)
        if info:
            if not info.visible: mt5.symbol_select(s, True)
            return s, info
    # fallback: broker suffixes/prefixes, only if unambiguous
    matches=[]
    try:
        for info in (mt5.symbols_get() or []):
            name=info.name.upper()
            if "XAUUSD" in name or name == "GOLD" or name.startswith("GOLD."):
                matches.append(info)
    except Exception: pass
    if len(matches)==1:
        info=matches[0]; mt5.symbol_select(info.name, True); return info.name, info
    if len(matches)>1:
        exact=[x for x in matches if x.name.upper() in ("XAUUSD","GOLD")]
        if len(exact)==1:
            info=exact[0]; mt5.symbol_select(info.name, True); return info.name, info
        raise HTTPException(503, "وجدت أكثر من رمز محتمل للذهب. ضع اسم الرمز الدقيق في MT5_SYMBOL داخل .env.")
    raise HTTPException(503, "لم أجد رمز الذهب في Market Watch. افتح MT5 وحدد اسم الرمز في MT5_SYMBOL داخل .env.")

def bars_for(mt5, symbol, timeframe, count=120):
    rates=mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates)<30:
        raise HTTPException(503, f"شموع MT5 غير كافية للرمز {symbol}. افتح الرسم البياني وانتظر تحميل البيانات.")
    out=[]
    for r in rates:
        out.append({"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"]), "volume": int(r["tick_volume"])})
    return out

def ema(vals, period):
    alpha=2/(period+1); result=[]; cur=vals[0]
    for v in vals:
        cur=alpha*v+(1-alpha)*cur; result.append(cur)
    return result

def calc_indicators(bars):
    c=[b["close"] for b in bars]; highs=[b["high"] for b in bars]; lows=[b["low"] for b in bars]
    e9=ema(c,9); e21=ema(c,21); e12=ema(c,12); e26=ema(c,26); macdline=[a-b for a,b in zip(e12,e26)]; signal=ema(macdline,9)
    diffs=[c[i]-c[i-1] for i in range(1,len(c))]; gains=[max(x,0) for x in diffs[-14:]]; losses=[max(-x,0) for x in diffs[-14:]]
    avg_gain=sum(gains)/max(1,len(gains)); avg_loss=sum(losses)/max(1,len(losses)); rsi=100 if avg_loss==0 else 100-(100/(1+avg_gain/avg_loss))
    trs=[]
    for i in range(1,len(bars)):
        trs.append(max(highs[i]-lows[i],abs(highs[i]-c[i-1]),abs(lows[i]-c[i-1])))
    atr=sum(trs[-14:])/max(1,len(trs[-14:]))
    trend="صاعد" if e9[-1]>e21[-1] else "هابط" if e9[-1]<e21[-1] else "محايد"
    return {"ema9":e9[-1],"ema21":e21[-1],"trend":trend,"rsi":rsi,"macd":macdline[-1],"macd_signal":signal[-1],"atr":atr,"close":c[-1]}

def build_analysis(mt5, symbol, bars15=None):
    if bars15 is None: bars15=bars_for(mt5,symbol,mt5.TIMEFRAME_M15,180)
    bars1h=bars_for(mt5,symbol,mt5.TIMEFRAME_H1,100)
    i=calc_indicators(bars15); h=calc_indicators(bars1h)
    votes=[]; reasons=[]
    if i["ema9"]>i["ema21"]: votes.append(1); reasons.append("EMA 9 أعلى من EMA 21 على M15")
    else: votes.append(-1); reasons.append("EMA 9 أدنى من EMA 21 على M15")
    if i["macd"]>i["macd_signal"]: votes.append(1); reasons.append("MACD أعلى من خط الإشارة")
    else: votes.append(-1); reasons.append("MACD أدنى من خط الإشارة")
    if i["rsi"]>55: votes.append(1); reasons.append("RSI يدعم الزخم الصاعد")
    elif i["rsi"]<45: votes.append(-1); reasons.append("RSI يدعم الزخم الهابط")
    else: votes.append(0); reasons.append("RSI في منطقة وسطية")
    if h["trend"]=="صاعد": votes.append(1); reasons.append("اتجاه H1 صاعد")
    elif h["trend"]=="هابط": votes.append(-1); reasons.append("اتجاه H1 هابط")
    else: votes.append(0)
    score=sum(votes); action="BUY" if score>=3 else "SELL" if score<=-3 else "WAIT"
    confidence=int(round(abs(score)/4*100))
    close=i["close"]; atr=i["atr"]
    # Stop/target are illustrative ATR distances, not broker-specific order instructions.
    stop=close-1.5*atr if action=="BUY" else close+1.5*atr if action=="SELL" else None
    target=close+2*atr if action=="BUY" else close-2*atr if action=="SELL" else None
    return {"signal":{"action":action,"score":confidence,"summary":{"BUY":"توافق فني صاعد؛ ليس ضمانًا للصعود","SELL":"توافق فني هابط؛ ليس ضمانًا للهبوط","WAIT":"المؤشرات غير متوافقة بما يكفي؛ الانتظار أكثر تحفظًا"}[action],"reasons":reasons,"invalidation":i["ema21"] if action=="BUY" else i["ema21"] if action=="SELL" else None,"stop_loss":stop,"take_profit":target},"indicators":{"trend":i["trend"],"rsi":i["rsi"],"macd":i["macd"],"atr":i["atr"],"ema9":i["ema9"],"ema21":i["ema21"],"h1_trend":h["trend"]},"ai_enabled":False,"ai_summary":"التحليل الأساسي حُسب من شموع MT5 الفعلية. أضف OPENAI_API_KEY لتفعيل شرح AI؛ غياب المفتاح لا يغيّر الإشارة الفنية."}

def fetch_news():
    now=time.time()
    if now-_news_cache["at"]<180 and _news_cache["items"]: return _news_cache["items"]
    items=[]
    for source,url in NEWS_URLS:
        try:
            res=requests.get(url,timeout=7,headers={"User-Agent":"GoldAI/1.0 (economic-news-reader)"}); res.raise_for_status()
            root=ET.fromstring(res.content)
            for node in root.findall(".//item")[:15]:
                title=(node.findtext("title") or "").strip(); link=(node.findtext("link") or "").strip(); desc=(node.findtext("description") or "").strip()
                pub=(node.findtext("pubDate") or "").strip(); dt=None
                try: dt=parsedate_to_datetime(pub).astimezone(timezone.utc).isoformat() if pub else None
                except Exception: dt=None
                if title and link: items.append({"title":title,"url":link,"summary":desc[:500],"source":source,"published_at":dt})
        except Exception: continue
    items.sort(key=lambda x:x.get("published_at") or "",reverse=True)
    _news_cache.update({"at":now,"items":items[:25]})
    return _news_cache["items"]

def maybe_ai(analysis, news, force=False):
    if not OPENAI_API_KEY: return analysis
    cache_key=hashlib.sha256((str(analysis.get("signal"))+str([n["title"] for n in news[:8]])).encode()).hexdigest()
    if not force and _ai_cache["key"]==cache_key and time.time()-_ai_cache["at"]<300:
        analysis["ai_enabled"]=True; analysis["ai_summary"]=_ai_cache["text"]; return analysis
    try:
        from openai import OpenAI
        client=OpenAI(api_key=OPENAI_API_KEY)
        payload={"technical_analysis":analysis,"news":[{"title":n["title"],"source":n["source"],"published_at":n["published_at"],"summary":n["summary"]} for n in news[:10]]}
        response=client.responses.create(model=OPENAI_MODEL, input=[{"role":"system","content":"أنت مساعد تحليل مالي باللغة العربية. حلل فقط البيانات المرفقة ولا تخترع أسعارًا أو أخبارًا أو أوقاتًا. وضح ما يدعم الذهب وما قد يضغط عليه، وميّز الحقيقة عن الاستنتاج، واذكر إذا كانت الأخبار غير كافية أو قديمة. لا تقدم ضمانات ولا تدّعِ احتمال نجاح محسوبًا. لا تغيّر إشارة المؤشرات؛ اشرحها بحذر في 4-7 جمل قصيرة."},{"role":"user","content":str(payload)}], max_output_tokens=450)
        text=response.output_text.strip()
        if text:
            _ai_cache.update({"key":cache_key,"at":time.time(),"text":text}); analysis["ai_enabled"]=True; analysis["ai_summary"]=text
    except Exception as exc:
        analysis["ai_enabled"]=False; analysis["ai_summary"]="تعذر الحصول على تحليل AI من الخدمة. الإشارة المعروضة ما زالت مبنية على المؤشرات فقط. تحقق من مفتاح OpenAI وحدود الاستخدام." 
    return analysis

@app.get("/api/health")
def health(authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)
    return {"ok":True,"service":APP_NAME,"mt5_available":bool(_mt5 is not None),"ai_configured":bool(OPENAI_API_KEY),"time":datetime.now(timezone.utc).isoformat()}

@app.get("/api/market")
def market(authorization: Optional[str] = Header(default=None)):
    require_auth(authorization); mt5=mt5mod(); symbol,info=find_symbol(mt5)
    tick=mt5.symbol_info_tick(symbol)
    if tick is None: raise HTTPException(503,"لا توجد بيانات تيك من MT5 لهذا الرمز.")
    bars=bars_for(mt5,symbol,mt5.TIMEFRAME_M15,100)
    tick_time=datetime.fromtimestamp(tick.time,timezone.utc).isoformat() if getattr(tick,"time",0) else None
    return {"symbol":symbol,"source":"MetaTrader 5 / broker feed","digits":int(info.digits),"tick":{"bid":float(tick.bid),"ask":float(tick.ask),"last":float(tick.last) if getattr(tick,"last",0) else None},"tick_time":tick_time,"bar_time":datetime.fromtimestamp(bars[-1]["time"],timezone.utc).isoformat(),"bars":bars,"server_time":datetime.now(timezone.utc).isoformat()}

@app.get("/api/analysis")
def analysis(authorization: Optional[str] = Header(default=None), refresh_ai: bool = Query(default=False)):
    require_auth(authorization); mt5=mt5mod(); symbol,_=find_symbol(mt5); result=build_analysis(mt5,symbol); news=fetch_news(); return maybe_ai(result,news,refresh_ai)

@app.get("/api/news")
def news(authorization: Optional[str] = Header(default=None)):
    require_auth(authorization); items=fetch_news(); return {"items":items,"sources":[{"name":n,"url":u} for n,u in NEWS_URLS],"fetched_at":datetime.now(timezone.utc).isoformat(),"note":"RSS feeds may be delayed or temporarily unavailable."}
