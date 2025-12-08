import os
import logging
import json
import urllib.parse
import xml.etree.ElementTree as ET
import base64
import math
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

# --- NOU: IMPORT RESEND ---
import resend

# FastAPI Imports
from fastapi import FastAPI, Depends, HTTPException, Form, File, UploadFile, Query, Body
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware

# Pydantic Imports
from pydantic import BaseModel, EmailStr

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

ARTICLES_UPLOAD_PATH_FTP = os.getenv("ARTICLES_UPLOAD_PATH_FTP", "/public_html/noutati")
SITEMAP_UPLOAD_PATH_FTP = os.getenv("SITEMAP_UPLOAD_PATH_FTP", "/public_html")
ARTICLES_URL_SUBDIR = os.getenv("ARTICLES_URL_SUBDIR", "noutati")
SITE_URL = os.getenv("SITE_URL", "https://frunza-asociatii.ro")

# --- EMAIL CONFIG (RESEND) ---
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")

# Inițializare Resend
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

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


# ==================== 2. MODELE (DB & Validare) ====================

# Model DB - Articole
class ArticleDB(Base):
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=False)
    slug = Column(String(500), unique=True, index=True, nullable=False)
    category = Column(String(100), nullable=False)
    tags = Column(JSON, default=[])
    excerpt = Column(Text, nullable=True)
    cover_image = Column(Text, nullable=True)
    content = Column(Text, nullable=False)
    status = Column(String(50), default="Draft")
    author = Column(String(100), default="Frunză & Asociații")
    url = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    published_at = Column(DateTime, nullable=True)


Base.metadata.create_all(bind=engine)


# Model Validare - Contact
class ContactForm(BaseModel):
    name: str
    email: EmailStr
    phone: str
    subject: str
    message: str


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==================== 3. EMAIL UTILS (RESEND API) ====================

def send_email(form_data: ContactForm):
    """
    Trimite email folosind Resend API (HTTP).
    Folosește domeniul verificat pentru livrabilitate maximă.
    """
    try:
        if not RESEND_API_KEY:
            logger.error("Lipseste RESEND_API_KEY din variabilele de mediu")
            return False

        html_body = f"""
        <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <h2 style="color: #8B2635;">Mesaj nou de pe site</h2>
                <p><strong>Nume:</strong> {form_data.name}</p>
                <p><strong>Email Client:</strong> {form_data.email}</p>
                <p><strong>Telefon:</strong> {form_data.phone}</p>
                <p><strong>Subiect:</strong> {form_data.subject}</p>
                <hr>
                <div style="background-color: #f9f9f9; padding: 15px; border-left: 4px solid #8B2635;">
                    <strong>Mesaj:</strong><br>
                    {form_data.message}
                </div>
                <p style="font-size: 12px; color: #888; margin-top: 20px;">
                    Acest mesaj a fost trimis de pe site-ul {SITE_URL}.<br>
                    Răspunde la acest email pentru a contacta direct clientul.
                </p>
            </body>
        </html>
        """

        # --- MODIFICAREA ESTE AICI ---
        # 1. Asigură-te că domeniul 'frunza-asociatii.ro' are statusul "Verified" în Resend Dashboard -> Domains.
        # 2. Folosește o adresă de pe domeniul tău (ex: contact@, notificari@, no-reply@).
        sender_email = "contact@frunza-asociatii.ro"

        params = {
            # "from": Aici serverele văd că expeditorul este legitim (domeniul tău)
            "from": f"Formular Site <{sender_email}>",

            # "to": Aici primești tu notificarea (probabil tot pe contact@ sau pe adresa ta personală)
            "to": [EMAIL_RECEIVER],

            "subject": f"[Contact Site] {form_data.subject}",
            "html": html_body,

            # "reply_to": CRUCIAL - Când dai "Reply" în Outlook/Gmail, răspunsul se duce la client
            "reply_to": form_data.email
        }

        r = resend.Emails.send(params)
        logger.info(f"Email trimis cu succes via Resend. ID: {r.get('id')}")
        return True

    except Exception as e:
        logger.error(f"Eroare Resend: {e}")
        return False


# ==================== 4. FTP UTILS ====================

def get_ftp_connection():
    ftp = FTP()
    ftp.connect(CPANEL_HOST, CPANEL_PORT, timeout=30)
    ftp.login(CPANEL_USER, CPANEL_PASSWORD)
    return ftp


def upload_file_ftp(local_path: str, remote_filename: str, remote_dir: str):
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
        logger.info(f"FTP Uploaded: {remote_filename}")
        return True
    except Exception as e:
        logger.error(f"FTP Upload Error: {e}")
        return False
    finally:
        if ftp:
            try:
                ftp.quit();
            except:
                pass


def delete_file_ftp(remote_filename: str, remote_dir: str):
    ftp = None
    try:
        ftp = get_ftp_connection()
        ftp.cwd(remote_dir)
        ftp.delete(remote_filename)
        return True
    except:
        return True
    finally:
        if ftp:
            try:
                ftp.quit();
            except:
                pass


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
            try:
                ftp.quit();
            except:
                pass


# ==================== 5. SEO & GOOGLE UTILS ====================

def request_google_indexing(url: str, type="URL_UPDATED"):
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
        return True
    except Exception as e:
        logger.error(f"Google Error: {e}")
        return False


def update_sitemap(new_url: str):
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
    except Exception:
        return False


def remove_from_sitemap(target_url: str):
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
            return True
        return False
    except Exception:
        return False


def generate_tags_html(tags_list):
    html = []
    for tag in tags_list:
        html.append(f'<span class="tag">{tag}</span>')
    return "\n".join(html) if html else '<span class="tag">General</span>'


# ==================== 6. PIPELINES (PUBLISH/UNPUBLISH) ====================

def publish_content_pipeline(article: ArticleDB):
    try:
        tags_list = article.tags if article.tags else []
        if isinstance(tags_list, str): tags_list = tags_list.split(",")

        public_url = f"{SITE_URL}/{ARTICLES_URL_SUBDIR}/{article.slug}"
        file_name = f"{article.slug}.html"

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

        local_path = os.path.join(GENERATED_DIR, file_name)
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        if upload_file_ftp(local_path, file_name, ARTICLES_UPLOAD_PATH_FTP):
            update_sitemap(public_url)
            request_google_indexing(public_url, "URL_UPDATED")
            return True
        return False
    except Exception as e:
        logger.error(f"Publish Failed: {e}")
        return False


def unpublish_content_pipeline(slug: str):
    try:
        file_name = f"{slug}.html"
        public_url = f"{SITE_URL}/{ARTICLES_URL_SUBDIR}/{slug}"
        delete_file_ftp(file_name, ARTICLES_UPLOAD_PATH_FTP)
        remove_from_sitemap(public_url)
        request_google_indexing(public_url, "URL_DELETED")
        return True
    except Exception:
        return False


# ==================== 7. API ENDPOINTS ====================

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Permite oricui (Pentru dev/Railway)
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


# --- CONTACT FORM (RESEND) ---
@app.post("/contact")
async def submit_contact(form: ContactForm):
    success = send_email(form)
    if success:
        return {"status": "success", "message": "Email trimis via Resend!"}
    else:
        raise HTTPException(status_code=500, detail="Eroare server email.")


# --- ARTICLES CRUD ---
@app.post("/articles")
async def create_article(payload: dict = Body(...), token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    verify_jwt_token(token)
    slug = payload.get('slug')
    if db.query(ArticleDB).filter(ArticleDB.slug == slug).first():
        raise HTTPException(status_code=400, detail="Slug existent.")

    status = "Published" if payload.get('status', '').lower() == "published" else "Draft"
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

    if status == "Published":
        publish_content_pipeline(new_article)

    return {"status": "success", "article": new_article}


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
            "total_pages": math.ceil(total / limit)
        }
    }


@app.get("/articles/{article_id}")
def get_article(article_id: int, db: Session = Depends(get_db)):
    art = db.query(ArticleDB).filter(ArticleDB.id == article_id).first()
    if not art: raise HTTPException(status_code=404)
    return art


@app.get("/articles/slug/{slug}")
def get_article_slug(slug: str, db: Session = Depends(get_db)):
    art = db.query(ArticleDB).filter(ArticleDB.slug == slug).first()
    if not art: raise HTTPException(status_code=404)
    return art


@app.put("/articles/{article_id}")
async def update_article(article_id: int, payload: dict = Body(...), token: str = Depends(oauth2_scheme),
                         db: Session = Depends(get_db)):
    verify_jwt_token(token)
    article = db.query(ArticleDB).filter(ArticleDB.id == article_id).first()
    if not article: raise HTTPException(status_code=404)

    old_slug = article.slug
    old_status = article.status
    new_status = "Published" if payload.get('status', '').lower() == "published" else "Draft"
    new_slug = payload.get('slug', old_slug)

    if new_slug != old_slug and db.query(ArticleDB).filter(ArticleDB.slug == new_slug).first():
        raise HTTPException(status_code=400, detail="Slug ocupat.")

    article.title = payload.get('title', article.title)
    article.slug = new_slug
    article.category = payload.get('category', article.category)
    article.content = payload.get('content', article.content)
    article.excerpt = payload.get('excerpt', article.excerpt)
    article.tags = payload.get('tags', article.tags)
    article.cover_image = payload.get('coverImage', article.cover_image)
    article.status = new_status
    article.updated_at = datetime.utcnow()
    article.url = f"{SITE_URL}/{ARTICLES_URL_SUBDIR}/{new_slug}"

    if new_status == "Published" and not article.published_at:
        article.published_at = datetime.utcnow()

    db.commit()
    db.refresh(article)

    # State Machine Update
    if old_status == "Published" and new_status == "Draft":
        unpublish_content_pipeline(old_slug)
    elif old_status == "Published" and new_status == "Published" and old_slug != new_slug:
        unpublish_content_pipeline(old_slug)
        publish_content_pipeline(article)
    elif new_status == "Published":
        publish_content_pipeline(article)

    return {"status": "success", "article": article}


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

    if old_status == "Published" and new_status == "Draft":
        unpublish_content_pipeline(article.slug)
    elif new_status == "Published":
        publish_content_pipeline(article)

    return {"status": "success", "article": article}


@app.delete("/articles/{article_id}")
async def delete_article(article_id: int, token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    verify_jwt_token(token)
    article = db.query(ArticleDB).filter(ArticleDB.id == article_id).first()
    if not article: raise HTTPException(status_code=404)

    slug = article.slug
    was_published = (article.status == "Published")
    db.delete(article)
    db.commit()

    if was_published:
        unpublish_content_pipeline(slug)

    return {"status": "success"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
