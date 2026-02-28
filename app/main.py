import logging
import traceback

from fastapi import FastAPI, Header, HTTPException

from app.config import settings
from app.schemas import ScrapeResponse
from app.scrapers.wellnessliving import scrape_wellnessliving
from app.scrapers.spark import scrape_spark
from app.scrapers.clubready import scrape_clubready

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Lead Scraper", version="0.1.0", docs_url=None, redoc_url=None)


def _check_secret(secret: str | None):
    if secret != settings.api_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/scrape/wellnessliving", response_model=ScrapeResponse)
async def scrape_wl(x_external_api_secret: str | None = Header(None)):
    _check_secret(x_external_api_secret)
    try:
        return await scrape_wellnessliving()
    except Exception as e:
        log.error("WellnessLiving scrape failed: %s\n%s", e, traceback.format_exc())
        return ScrapeResponse(leadCount=0, leads=[], errors=[str(e)], metadata={})


@app.post("/scrape/spark", response_model=ScrapeResponse)
async def scrape_sp(x_external_api_secret: str | None = Header(None)):
    _check_secret(x_external_api_secret)
    try:
        return await scrape_spark()
    except Exception as e:
        log.error("Spark scrape failed: %s\n%s", e, traceback.format_exc())
        return ScrapeResponse(leadCount=0, leads=[], errors=[str(e)], metadata={})


@app.post("/scrape/clubready", response_model=ScrapeResponse)
async def scrape_cr(x_external_api_secret: str | None = Header(None)):
    _check_secret(x_external_api_secret)
    try:
        return await scrape_clubready()
    except Exception as e:
        log.error("ClubReady scrape failed: %s\n%s", e, traceback.format_exc())
        return ScrapeResponse(leadCount=0, leads=[], errors=[str(e)], metadata={})
