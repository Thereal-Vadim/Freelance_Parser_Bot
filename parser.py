import os
import re
import json
import logging
import sqlite3
import argparse
import time
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import ml

# Logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {
            "keywords": ["React", "Next.js", "Сайт под ключ", "Node.js", "Vue", "Python"],
            "minus_words": ["доработка", "студенческая", "за отзыв", "курсовая", "дипломная"],
            "scan_interval_minutes": 5
        }
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error reading config.json: {e}")
        return {
            "keywords": [],
            "minus_words": [],
            "scan_interval_minutes": 5
        }

def clean_html(raw_html):
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    return soup.get_text(separator=" ").strip()

def fetch_kwork_jobs():
    jobs = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
    }
    
    # Parse the first 5 pages of Kwork to gather a larger volume of jobs
    for page in range(1, 6):
        url = f"https://kwork.ru/projects?c=all&page={page}"
        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code != 200:
                logger.error(f"Failed to load Kwork (page {page}), status code: {res.status_code}")
                time.sleep(2)
                continue
                
            match = re.search(r"window\.stateData\s*=\s*(\{.*?\});", res.text, flags=re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                wants = data.get("wants", [])
                for want in wants:
                    want_id = want.get("id")
                    if not want_id:
                        continue
                    
                    title = want.get("name") or "Untitled"
                    desc_raw = want.get("description") or ""
                    description = clean_html(desc_raw)
                    
                    # Price formatting
                    price_limit = want.get("priceLimit")
                    possible_limit = want.get("possiblePriceLimit")
                    
                    try:
                        price_val = int(float(price_limit)) if price_limit else 0
                        poss_val = int(float(possible_limit)) if possible_limit else 0
                    except (ValueError, TypeError):
                        price_val = 0
                        poss_val = 0
                        
                    if price_val > 0 and poss_val > price_val:
                        price_str = f"{price_val} - {poss_val} ₽"
                    elif price_val > 0:
                        price_str = f"{price_val} ₽"
                    else:
                        price_str = "Negotiable"
                        
                    jobs.append({
                        "source": "kwork",
                        "external_id": f"kwork_{want_id}",
                        "title": title,
                        "url": f"https://kwork.ru/projects/{want_id}",
                        "description": description,
                        "price": price_str
                    })
            else:
                logger.error(f"Could not find window.stateData on Kwork page {page}")
        except Exception as e:
            logger.error(f"Error parsing Kwork (page {page}): {e}")
            
        # Delay between pages to avoid IP ban from Cloudflare
        time.sleep(2)
            
    return jobs

def fetch_fl_jobs():
    jobs = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    url = "https://www.fl.ru/rss/all.xml"
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            logger.error(f"Failed to load FL.ru RSS, status code: {res.status_code}")
            return jobs
            
        root = ET.fromstring(res.content)
        channel = root.find("channel")
        if channel is None:
            return jobs
            
        items = channel.findall("item")
        for item in items:
            title_raw = item.find("title").text if item.find("title") is not None else "Untitled"
            link = item.find("link").text if item.find("link") is not None else ""
            desc_raw = item.find("description").text if item.find("description") is not None else ""
            
            title = clean_html(title_raw)
            description = clean_html(desc_raw)
            
            # Extract ID from URL
            id_match = re.search(r"/projects/(\d+)/", link)
            if not id_match:
                continue
            ext_id = f"fl_{id_match.group(1)}"
            
            # Extract budget from title
            price = "Negotiable"
            price_match = re.search(r"Бюджет:\s*([^,)]+)", title)
            if price_match:
                price = price_match.group(1).replace("&#8381;", "₽").strip()
                # Remove budget mention from the title
                title = re.sub(r"\s*\(Бюджет:[^)]+\)", "", title).strip()
                
            jobs.append({
                "source": "fl",
                "external_id": ext_id,
                "title": title,
                "url": link,
                "description": description,
                "price": price
            })
    except Exception as e:
        logger.error(f"Error parsing FL.ru RSS: {e}")
        
    return jobs

def apply_filter(title, description, keywords, minus_words):
    text_to_check = f"{title}\n{description}".lower()
    
    # Check negative keywords
    for mw in minus_words:
        if mw.lower() in text_to_check:
            return False
            
    # Check keywords
    if not keywords:
        return True  # If keywords list is empty, allow everything
        
    for kw in keywords:
        if kw.lower() in text_to_check:
            return True
            
    return False

def run_cycle(dry_run=False):
    import db
    db.init_db()
    db.cleanup_old_jobs()
    
    logger.info("Starting parsing cycle...")
    config = load_config()
    keywords = config.get("keywords", [])
    minus_words = config.get("minus_words", [])
    
    logger.info(f"Filters loaded. Keywords: {keywords}. Negative keywords: {minus_words}")
    
    # Data collection
    kwork_jobs = fetch_kwork_jobs()
    fl_jobs = fetch_fl_jobs()
    all_jobs = kwork_jobs + fl_jobs
    
    logger.info(f"Total jobs parsed: {len(all_jobs)} (Kwork: {len(kwork_jobs)}, FL.ru: {len(fl_jobs)})")
    
    # Filtering and preparing for storage
    processed_jobs = []
    matched_count = 0
    
    for job in all_jobs:
        matched = apply_filter(job["title"], job["description"], keywords, minus_words)
        status = "new"
        
        if matched:
            # Attempt ML prediction
            prob = ml.predict_job(job["title"], job["description"])
            ml_threshold = config.get("ml_rejection_threshold", 0.35)
            if prob is not None and prob < ml_threshold:
                status = "ml_rejected"
                
        job_data = {
            "source": job["source"],
            "external_id": job["external_id"],
            "title": job["title"],
            "url": job["url"],
            "description": job["description"],
            "price": job["price"],
            "matched": 1 if matched else 0,
            "status": status
        }
        processed_jobs.append(job_data)
        if matched:
            matched_count += 1
            if dry_run:
                print(f"\n[MATCHED] Source: {job['source'].upper()} | ID: {job['external_id']} | Price: {job['price']}")
                print(f"Title: {job['title']}")
                print(f"URL: {job['url']}")
                print(f"Description (snippet): {job['description'][:200]}...")
                
    if dry_run:
        print(f"\n--- DRY RUN SUMMARY ---")
        print(f"Total parsed: {len(all_jobs)}")
        print(f"Total matched: {matched_count}")
        return
        
    # Save to DB
    inserted = db.insert_jobs(processed_jobs)
    logger.info(f"Saved {inserted} new unique jobs to the database (matched filters: {matched_count})")

def main():
    parser = argparse.ArgumentParser(description="Parser daemon for Kwork and FL.ru")
    parser.add_argument("-d", "--dry-run", action="store_true", help="Run the parser once and print output to console without saving to DB")
    args = parser.parse_args()
    
    if args.dry_run:
        run_cycle(dry_run=True)
        return
        
    config = load_config()
    interval = config.get("scan_interval_minutes", 5)
    
    logger.info(f"Parser daemon started. Scan interval: {interval} min.")
    cycle_count = 0
    while True:
        try:
            run_cycle()
            cycle_count += 1
            # Periodic ML model retraining every 5 cycles
            if cycle_count % 5 == 0:
                logger.info("Triggering background retraining of ML model...")
                ml.train_model()
        except Exception as e:
            logger.error(f"Unexpected error in parsing cycle: {e}")
        
        # Reload config in case of changes
        config = load_config()
        interval = config.get("scan_interval_minutes", 5)
        logger.info(f"Waiting {interval} minutes before the next cycle...")
        time.sleep(interval * 60)

if __name__ == "__main__":
    main()
