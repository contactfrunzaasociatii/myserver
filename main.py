import os
import json
import logging
import urllib.parse
import xml.etree.ElementTree as ET
import base64
import math
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

# FastAPI Imports
from fastapi import FastAPI, Depends, HTTPException, Form, File, UploadFile, Query, Body
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware

# Database Imports
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, JSON, desc
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# FTP Import
from ftplib import FTP, error_perm

# Auth & Utils
import jwt
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader

# Google API
from google.oauth2 import service_account
from googleapiclient.discovery import build
import requests

# ==================== 1. SETUP & CONFIGURARE ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# --- DATABASE ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./cms_database.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- FTP & SITE ---
CPANEL_HOST = os.getenv("CPANEL_HOST")
CPANEL_PORT = int(os.getenv("CPANEL_PORT", 21))
CPANEL_USER = os.getenv("CPANEL_USERNAME")
CPANEL_PASSWORD = os.getenv("CPANEL_PASSWORD")

# Căi FTP (fără slash la final)
ARTICLES_UPLOAD_PATH_FTP = os.getenv("ARTICLES_UPLOAD_PATH_FTP", "/public_html/noutati")
SITEMAP_UPLOAD_PATH_FTP = os.getenv("SITEMAP_UPLOAD_PATH_FTP", "/public_html")

ARTICLES_URL_SUBDIR = os.getenv("ARTICLES_URL_SUBDIR", "noutati")
SITE_URL = os.getenv("SITE_URL", "https://frunza-asociatii.ro")

# --- AUTH ---
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "secret")
JWT_ALGORITHM = "HS256"

SCOPES = ["https://www.googleapis.com/auth/indexing"]
GENERATED_DIR = "generated"
os.makedirs(GENERATED_DIR, exist_ok=True)

if not os.path.exists("templates"):
    os.makedirs("templates")
env = Environment(loader=FileSystemLoader("templates"))


# ==================== 2. MODEL BAZĂ DE DATE ====================
class ArticleDB(Base):
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=False)
    slug = Column(String(500), unique=True, index=True, nullable=False)
    category = Column(String(100), nullable=False)
    tags = Column(JSON, default=[])
    excerpt = Column(Text, nullable=True)
    cover_image = Column(Text, nullable=True)  # Base64 string
    content = Column(Text, nullable=False)

    # Status este salvat ca 'Published' sau 'Draft'
    status = Column(String(50), default="Draft")

    author = Column(String(100), default="Frunză & Asociații")
    url = Column(String(500), nullable=True)  # URL Public (Clean URL)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    published_at = Column(DateTime, nullable=True)


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==================== 3. FTP UTILS ====================

def get_ftp_connection():
    ftp = FTP()
    ftp.connect(CPANEL_HOST, CPANEL_PORT, timeout=30)
    ftp.login(CPANEL_USER, CPANEL_PASSWORD)
    return ftp


def upload_file_ftp(local_path: str, remote_filename: str, remote_dir: str):
    """Urcă un fișier pe FTP. Creează folderul dacă nu există."""
    ftp = None
    try:
        ftp = get_ftp_connection()
        try:
            ftp.cwd(remote_dir)
        except error_perm:
            try:
                ftp.mkd(remote_dir)
                ftp.cwd(remote_dir)
            except:
                return False

        with open(local_path, 'rb') as f:
            ftp.storbinary(f'STOR {remote_filename}', f)
        logger.info(f"FTP: Uploaded {remote_filename}")
        return True
    except Exception as e:
        logger.error(f"FTP Upload Error: {e}")
        return False
    finally:
        if ftp:
            try: ftp.quit();
            except: pass


def delete_file_ftp(remote_filename: str, remote_dir: str):
    """Șterge un fișier de pe FTP"""
    ftp = None
    try:
        ftp = get_ftp_connection()
        ftp.cwd(remote_dir)
        ftp.delete(remote_filename)
        logger.info(f"FTP: Deleted {remote_filename}")
        return True
    except error_perm:
        # Fișierul poate nu există, ceea ce e OK în contextul ștergerii
        return True
    except Exception as e:
        logger.error(f"FTP Delete Error: {e}")
        return False
    finally:
        if ftp:
            try: ftp.quit();
            except: pass


def download_from_cpanel(remote_filename: str, local_path: str, remote_dir: str):
    ftp = None
    try:
        ftp = get_ftp_connection()
        ftp.cwd(remote_dir)
        with open(local_path, 'wb') as f:
            ftp.retrbinary(f'RETR {remote_filename}', f.write)
        return True
    except:
        return False
    finally:
        if ftp:
            try: ftp.quit();
            except: pass


# ==================== 4. GOOGLE & SEO UTILS ====================

def request_google_indexing(url: str, type="URL_UPDATED"):
    """Trimite ping la Google Indexing API cu URL-ul CURAT (fără .html)"""
    try:
        if not os.getenv("GOOGLE_CLIENT_EMAIL"): return False
        creds_dict = {
            "type": "service_account",
            "project_id": os.getenv("GOOGLE_PROJECT_ID"),
            "private_key_id": os.getenv("GOOGLE_PRIVATE_KEY_ID"),
            "private_key": os.getenv("GOOGLE_PRIVATE_KEY", "").replace('\\n', '\n'),
            "client_email": os.getenv("GOOGLE_CLIENT_EMAIL"),
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        service = build('indexing', 'v3', credentials=creds)
        service.urlNotifications().publish(body={"url": url, "type": type}).execute()
        logger.info(f"Google Ping: {url} [{type}]")
        return True
    except Exception as e:
        logger.error(f"Google Indexing Error: {e}")
        return False


def update_sitemap(new_url: str):
    """Adaugă URL-ul CURAT în sitemap.xml"""
    LOCAL_SITEMAP = os.path.join(GENERATED_DIR, "sitemap.xml")
    try:
        download_from_cpanel("sitemap.xml", LOCAL_SITEMAP, SITEMAP_UPLOAD_PATH_FTP)
        root = None
        if os.path.exists(LOCAL_SITEMAP):
            try:
                ET.register_namespace('', "http://www.sitemaps.org/schemas/sitemap/0.9")
                tree = ET.parse(LOCAL_SITEMAP)
                root = tree.getroot()
            except:
                pass

        if root is None:
            root = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")

        ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        found = False

        # Căutăm dacă există deja URL-ul
        for url in root.findall("s:url", ns):
            loc = url.find("s:loc", ns)
            if loc is not None and loc.text == new_url:
                found = True
                lastmod = url.find("s:lastmod", ns)
                if lastmod is not None: lastmod.text = datetime.utcnow().strftime("%Y-%m-%d")
                break

        if not found:
            url_elem = ET.Element("url")
            ET.SubElement(url_elem, "loc").text = new_url
            ET.SubElement(url_elem, "lastmod").text = datetime.utcnow().strftime("%Y-%m-%d")
            root.append(url_elem)

        tree = ET.ElementTree(root)
        ET.register_namespace('', "http://www.sitemaps.org/schemas/sitemap/0.9")
        tree.write(LOCAL_SITEMAP, encoding="utf-8", xml_declaration=True)

        upload_file_ftp(LOCAL_SITEMAP, "sitemap.xml", SITEMAP_UPLOAD_PATH_FTP)
        requests.get(f"https://www.google.com/ping?sitemap={urllib.parse.quote(SITE_URL + '/sitemap.xml')}", timeout=5)
        return True
    except Exception as e:
        logger.error(f"Sitemap Error: {e}")
        return False


def remove_from_sitemap(target_url: str):
    """Șterge URL-ul din sitemap"""
    LOCAL_SITEMAP = os.path.join(GENERATED_DIR, "sitemap.xml")
    try:
        download_from_cpanel("sitemap.xml", LOCAL_SITEMAP, SITEMAP_UPLOAD_PATH_FTP)
        if not os.path.exists(LOCAL_SITEMAP): return False

        ET.register_namespace('', "http://www.sitemaps.org/schemas/sitemap/0.9")
        tree = ET.parse(LOCAL_SITEMAP)
        root = tree.getroot()
        ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}

        found = False
        urls = root.findall("s:url", ns)
        for url in urls:
            loc = url.find("s:loc", ns)
            if loc is not None and loc.text == target_url:
                root.remove(url)
                found = True

        if found:
            tree.write(LOCAL_SITEMAP, encoding="utf-8", xml_declaration=True)
            upload_file_ftp(LOCAL_SITEMAP, "sitemap.xml", SITEMAP_UPLOAD_PATH_FTP)
            requests.get(f"https://www.google.com/ping?sitemap={urllib.parse.quote(SITE_URL + '/sitemap.xml')}",
                         timeout=5)
            logger.info(f"Sitemap: Removed {target_url}")
            return True

        return False
    except Exception as e:
        logger.error(f"Sitemap Remove Error: {e}")
        return False


def generate_tags_html(tags_list):
    html = []
    for tag in tags_list:
        html.append(f'<span class="tag">{tag}</span>')
    return "\n".join(html) if html else '<span class="tag">General</span>'


# ==================== 5. PIPELINES (STATE MACHINE) ====================

def publish_content_pipeline(article: ArticleDB):
    """
    Executat când articolul este PUBLICAT.
    1. Generează fișierul fizic (.html).
    2. Folosește URL-ul curat (fără extensie) pentru SEO.
    """
    try:
        tags_list = article.tags if article.tags else []
        if isinstance(tags_list, str): tags_list = tags_list.split(",")

        # URL PUBLIC (Curat - Fără .html) -> Pentru Google & Sitemap
        public_url = f"{SITE_URL}/{ARTICLES_URL_SUBDIR}/{article.slug}"

        # NUME FIȘIER FIZIC (Cu .html) -> Pentru FTP
        file_name = f"{article.slug}.html"

        # 1. Render Template
        template = env.get_template("article_template.html")
        html_content = template.render(
            ARTICLE_TITLE=article.title,
            ARTICLE_CATEGORY=article.category,
            ARTICLE_COVER_IMAGE=article.cover_image,
            ARTICLE_AUTHOR=article.author,
            ARTICLE_DATE=(article.published_at or datetime.utcnow()).strftime("%d %B %Y"),
            ARTICLE_CONTENT=article.content,
            ARTICLE_TAGS_HTML=generate_tags_html(tags_list),
            ARTICLE_URL=public_url,
            SITE_URL=SITE_URL,
            ARTICLE_EXCERPT=article.excerpt or ""
        )

        # 2. Salvare Temp & Upload FTP
        local_path = os.path.join(GENERATED_DIR, file_name)
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        success = upload_file_ftp(local_path, file_name, ARTICLES_UPLOAD_PATH_FTP)
        if os.path.exists(local_path): os.remove(local_path)

        if not success:
            logger.error("FTP Upload Failed")
            return False

        # 3. SEO (Folosim URL-ul curat)
        update_sitemap(public_url)
        request_google_indexing(public_url, "URL_UPDATED")

        return True
    except Exception as e:
        logger.error(f"Publish Pipeline Failed: {e}")
        return False


def unpublish_content_pipeline(slug: str):
    """
    Executat când articolul este ASCUNS sau ȘTERS.
    1. Șterge fișierul .html de pe server.
    2. Șterge URL-ul curat din Google/Sitemap.
    """
    try:
        file_name = f"{slug}.html"
        public_url = f"{SITE_URL}/{ARTICLES_URL_SUBDIR}/{slug}"

        # 1. Șterge Fișier FTP
        delete_file_ftp(file_name, ARTICLES_UPLOAD_PATH_FTP)

        # 2. Șterge SEO
        remove_from_sitemap(public_url)
        request_google_indexing(public_url, "URL_DELETED")

        return True
    except Exception as e:
        logger.error(f"Unpublish Pipeline Failed: {e}")
        return False


# ==================== 6. API ENDPOINTS ====================

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


def verify_jwt_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        if payload.get("sub") != ADMIN_USERNAME: raise HTTPException(status_code=401)
    except:
        raise HTTPException(status_code=401, detail="Invalid Token")


@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    if form_data.username == ADMIN_USERNAME and form_data.password == ADMIN_PASSWORD:
        token = jwt.encode({"sub": form_data.username, "exp": datetime.utcnow() + timedelta(minutes=120)},
                           JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
        return {"access_token": token, "token_type": "bearer"}
    raise HTTPException(status_code=400, detail="Invalid credentials")


# --- CREATE ---
@app.post("/articles")
async def create_article(payload: dict = Body(...), token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    verify_jwt_token(token)

    slug = payload.get('slug')

    # VERIFICARE UNICITATE SLUG (DB Check)
    if db.query(ArticleDB).filter(ArticleDB.slug == slug).first():
        raise HTTPException(status_code=400, detail="Acest slug există deja. Alegeți alt titlu sau slug.")

    status = "Published" if payload.get('status', '').lower() == "published" else "Draft"

    # Salvăm URL-ul curat în DB (fără .html)
    public_url = f"{SITE_URL}/{ARTICLES_URL_SUBDIR}/{slug}"

    new_article = ArticleDB(
        title=payload.get('title'), slug=slug, category=payload.get('category'),
        tags=payload.get('tags', []), excerpt=payload.get('excerpt', ''),
        cover_image=payload.get('coverImage'), content=payload.get('content'),
        status=status, url=public_url,
        published_at=datetime.utcnow() if status == "Published" else None
    )

    db.add(new_article)
    db.commit()
    db.refresh(new_article)

    # Doar dacă e Published declanșăm pipeline-ul extern
    if status == "Published":
        publish_content_pipeline(new_article)

    return {"status": "success", "article": new_article}


# --- READ ---
@app.get("/articles")
def get_articles(page: int = 1, limit: int = 6, search: str = None, db: Session = Depends(get_db)):
    query = db.query(ArticleDB)
    if search: query = query.filter(ArticleDB.title.ilike(f"%{search}%"))
    query = query.order_by(desc(ArticleDB.created_at))

    total = query.count()
    articles = query.offset((page - 1) * limit).limit(limit).all()

    return {
        "status": "success", "data": articles,
        "pagination": {
            "current_page": page, "items_per_page": limit, "total_items": total,
            "total_pages": math.ceil(total / limit), "has_next": page < math.ceil(total / limit), "has_prev": page > 1
        }
    }


@app.get("/articles/{article_id}")
def get_article_by_id(article_id: int, db: Session = Depends(get_db)):
    article = db.query(ArticleDB).filter(ArticleDB.id == article_id).first()
    if not article: raise HTTPException(status_code=404)
    return article


@app.get("/articles/slug/{slug}")
def get_article_by_slug(slug: str, db: Session = Depends(get_db)):
    article = db.query(ArticleDB).filter(ArticleDB.slug == slug).first()
    if not article: raise HTTPException(status_code=404)
    return article


# --- UPDATE ---
@app.put("/articles/{article_id}")
async def update_article(article_id: int, payload: dict = Body(...), token: str = Depends(oauth2_scheme),
                         db: Session = Depends(get_db)):
    verify_jwt_token(token)
    article = db.query(ArticleDB).filter(ArticleDB.id == article_id).first()
    if not article: raise HTTPException(status_code=404, detail="Article not found")

    old_slug = article.slug
    old_status = article.status

    new_status = "Published" if payload.get('status', '').lower() == "published" else "Draft"
    new_slug = payload.get('slug', old_slug)

    # Verificare slug unic doar dacă s-a schimbat
    if new_slug != old_slug:
        if db.query(ArticleDB).filter(ArticleDB.slug == new_slug).first():
            raise HTTPException(status_code=400, detail="Noul slug există deja.")

    # Update DB
    article.title = payload.get('title', article.title)
    article.slug = new_slug
    article.category = payload.get('category', article.category)
    article.content = payload.get('content', article.content)
    article.excerpt = payload.get('excerpt', article.excerpt)
    article.tags = payload.get('tags', article.tags)
    article.cover_image = payload.get('coverImage', article.cover_image)
    article.status = new_status
    article.updated_at = datetime.utcnow()
    article.url = f"{SITE_URL}/{ARTICLES_URL_SUBDIR}/{new_slug}"  # URL Curat

    if new_status == "Published" and not article.published_at:
        article.published_at = datetime.utcnow()

    db.commit()
    db.refresh(article)

    # State Machine Logic

    # Cazul 1: Era Published -> Acum e Draft (Unpublish)
    if old_status == "Published" and new_status == "Draft":
        unpublish_content_pipeline(old_slug)

    # Cazul 2: Slug Change (Published -> Published cu slug nou)
    elif old_status == "Published" and new_status == "Published" and old_slug != new_slug:
        unpublish_content_pipeline(old_slug)  # Ștergem vechiul (inclusiv din sitemap/google)
        publish_content_pipeline(article)  # Creăm noul

    # Cazul 3: Content Update / Publish (Draft -> Published SAU Published -> Published)
    elif new_status == "Published":
        publish_content_pipeline(article)

    return {"status": "success", "article": article}


# --- PATCH STATUS ---
@app.patch("/articles/{article_id}")
async def patch_status(article_id: int, payload: dict = Body(...), token: str = Depends(oauth2_scheme),
                       db: Session = Depends(get_db)):
    verify_jwt_token(token)
    article = db.query(ArticleDB).filter(ArticleDB.id == article_id).first()
    if not article: raise HTTPException(status_code=404)

    old_status = article.status
    new_status = "Published" if payload.get('status', '').lower() == "published" else "Draft"

    article.status = new_status
    if new_status == "Published" and not article.published_at:
        article.published_at = datetime.utcnow()

    db.commit()

    # Logică simplificată pentru Toggle
    if old_status == "Published" and new_status == "Draft":
        unpublish_content_pipeline(article.slug)
    elif new_status == "Published":
        publish_content_pipeline(article)

    return {"status": "success", "article": article}


# --- DELETE ---
@app.delete("/articles/{article_id}")
async def delete_article(article_id: int, token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    verify_jwt_token(token)
    article = db.query(ArticleDB).filter(ArticleDB.id == article_id).first()
    if not article: raise HTTPException(status_code=404)

    slug_to_remove = article.slug
    status_was = article.status

    db.delete(article)
    db.commit()

    # Doar dacă era publicat ștergem de pe net
    if status_was == "Published":
        unpublish_content_pipeline(slug_to_remove)

    return {"status": "success", "message": "Deleted"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)