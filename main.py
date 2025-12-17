import os
import logging
import json
import urllib.parse
import xml.etree.ElementTree as ET
import base64
import math
import uuid
from io import BytesIO
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

# --- NOU: IMPORT PENTRU IMAGINI ---
from PIL import Image  # pip install Pillow

# --- IMPORT RESEND ---
import resend

# FastAPI Imports
from fastapi import FastAPI, Depends, HTTPException, Form, File, UploadFile, Query, Body, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware

# Pydantic Imports
from pydantic import BaseModel, EmailStr

# Database Imports
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, JSON, desc, Index, func
from sqlalchemy.orm import declarative_base
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

# Căi pentru articole HTML
ARTICLES_UPLOAD_PATH_FTP = os.getenv("ARTICLES_UPLOAD_PATH_FTP", "/public_html/noutati")
ARTICLES_URL_SUBDIR = os.getenv("ARTICLES_URL_SUBDIR", "noutati")

# Căi pentru SITEMAP
SITEMAP_UPLOAD_PATH_FTP = os.getenv("SITEMAP_UPLOAD_PATH_FTP", "/public_html")

# Căi pentru IMAGINI (Uploads)
# Implicit: /public_html/uploads -> https://site.ro/uploads
IMAGES_UPLOAD_PATH_FTP = os.getenv("IMAGES_UPLOAD_PATH_FTP", "/public_html/uploads")
IMAGES_PUBLIC_URL = os.getenv("IMAGES_PUBLIC_URL", "https://frunza-asociatii.ro/uploads")

SITE_URL = os.getenv("SITE_URL", "https://frunza-asociatii.ro")

# --- EMAIL CONFIG (RESEND) ---
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")

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

class ArticleDB(Base):
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    slug = Column(String(500), unique=True, index=True, nullable=False)
    category = Column(String(100), index=True)
    tags = Column(JSON, default=list)
    excerpt = Column(Text)
    cover_image = Column(Text)
    content = Column(Text, nullable=False)
    status = Column(String(50), index=True, default="Draft")
    author = Column(String(100), default="Frunză & Asociații")
    url = Column(String(500))
    created_at = Column(DateTime, index=True, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    published_at = Column(DateTime)

    __table_args__ = (
        Index("ix_articles_status_created", "status", "created_at"),
    )


Base.metadata.create_all(bind=engine)


class ArticleListItem(BaseModel):
    id: int
    title: str
    slug: str
    category: str
    excerpt: Optional[str]
    cover_image: Optional[str]
    author: str
    created_at: datetime
    published_at: Optional[datetime]
    status: str


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


# ==================== 3. EMAIL UTILS (RESEND) ====================

def send_email(form_data: ContactForm):
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

        sender_email = "contact@frunza-asociatii.ro"

        params = {
            "from": f"Formular Site <{sender_email}>",
            "to": [EMAIL_RECEIVER],
            "subject": f"[Contact Site] {form_data.subject}",
            "html": html_body,
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
                ftp.quit()
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
                ftp.quit()
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
                ftp.quit()
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

origins = [
    "https://frunza-asociatii.ro",  # Domeniul tău principal
    "https://www.frunza-asociatii.ro",  # Varianta cu www
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # <--- Folosește lista de mai sus, nu ["*"]
    allow_credentials=True,  # Obligatoriu True pentru logare/tokeni
    allow_methods=["*"],  # Permite orice metodă (GET, POST, PUT, DELETE, OPTIONS)
    allow_headers=["*"],  # Permite orice header (Authorization, Content-Type, etc.)
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


# --- UPLOAD IMAGES TO CPANEL (OPTIMIZED) ---
@app.post("/upload")
async def upload_image(
        file: UploadFile = File(...),
        token: str = Depends(oauth2_scheme)
):
    """
    Urcă imagini direct pe hosting (cPanel/Cyberfolks) în /uploads.
    Include optimizare (resize/compress) cu Pillow.
    """
    verify_jwt_token(token)

    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Fișierul trebuie să fie o imagine.")

    try:
        # 1. Generare nume unic
        file_ext = file.filename.split(".")[-1].lower()
        unique_filename = f"{uuid.uuid4()}.{file_ext}"

        # 2. Procesare Imagine (Optimizare)
        file_content = await file.read()
        image = Image.open(BytesIO(file_content))
        output_io = BytesIO()

        # Compresie smart
        if file_ext in ['jpg', 'jpeg', 'png', 'webp']:
            if image.mode in ("RGBA", "P"): image = image.convert("RGB")
            save_format = 'JPEG' if file_ext in ['jpg', 'jpeg'] else file_ext.upper()
            # Optimizare: Calitate 85% reduce drastic dimensiunea fără a pierde vizual
            image.save(output_io, format=save_format, quality=85, optimize=True)
        else:
            # SVG/GIF trec direct
            output_io.write(file_content)

        output_io.seek(0)

        # 3. Upload FTP
        ftp = get_ftp_connection()
        try:
            # Verificăm dacă există folderul, dacă nu îl creăm
            try:
                ftp.cwd(IMAGES_UPLOAD_PATH_FTP)
            except error_perm:
                ftp.mkd(IMAGES_UPLOAD_PATH_FTP)
                ftp.cwd(IMAGES_UPLOAD_PATH_FTP)

            ftp.storbinary(f'STOR {unique_filename}', output_io)
        finally:
            try:
                ftp.quit()
            except:
                pass

        # 4. Return URL Public
        public_url = f"{IMAGES_PUBLIC_URL}/{unique_filename}"
        return {"status": "success", "location": public_url}

    except Exception as e:
        logger.error(f"Upload Error: {e}")
        raise HTTPException(status_code=500, detail=f"Eroare la upload: {str(e)}")


# --- CONTACT FORM ---
@app.post("/contact")
async def submit_contact(form: ContactForm):
    success = send_email(form)
    if success:
        return {"status": "success", "message": "Email trimis via Resend!"}
    else:
        raise HTTPException(status_code=500, detail="Eroare server email.")


# --- ARTICLES CRUD ---
@app.post("/articles")
async def create_article(
    payload: dict = Body(...),
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None
):

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
        background_tasks.add_task(publish_content_pipeline, new_article)
    return {"status": "success", "article": new_article}


@app.get("/articles")
def get_articles(
        page: int = 1,
        limit: int = 6,
        db: Session = Depends(get_db)
):
    # Optimizare: Selectăm doar câmpurile necesare pentru listă (fără 'content')
    base_query = db.query(ArticleDB)
    total = base_query.with_entities(func.count()).scalar()

    rows = (
        base_query
        .order_by(desc(ArticleDB.created_at))
        .offset((page - 1) * limit)
        .limit(limit)
        .with_entities(
            ArticleDB.id,
            ArticleDB.title,
            ArticleDB.slug,
            ArticleDB.category,
            ArticleDB.excerpt,
            ArticleDB.cover_image,  # Acesta va fi URL-ul scurt către imagine
            ArticleDB.author,
            ArticleDB.created_at,
            ArticleDB.published_at,
            ArticleDB.status,
        )
        .all()
    )

    articles = [ArticleListItem(**dict(r._mapping)) for r in rows]

    return {
        "status": "success",
        "data": articles,
        "pagination": {
            "current_page": page,
            "items_per_page": limit,
            "total_items": total,
            "total_pages": math.ceil(total / limit)
        }
    }


@app.get("/articles/{article_id}")
def get_article(article_id: int, db: Session = Depends(get_db)):
    art = db.query(ArticleDB).filter(ArticleDB.id == article_id).first()
    if not art: raise HTTPException(status_code=404)
    return art


@app.get("/articles/slug/{slug}")
def get_article_by_slug(slug: str, db: Session = Depends(get_db)):
    article = db.query(ArticleDB).filter(
        ArticleDB.slug == slug,
        ArticleDB.status == "Published"
    ).first()
    if not article: raise HTTPException(status_code=404)
    return article


@app.put("/articles/{article_id}")
async def update_article(
        article_id: int,
        payload: dict = Body(...),
        token: str = Depends(oauth2_scheme),
        db: Session = Depends(get_db),
        background_tasks: BackgroundTasks = None  # <--- PARAMETRU NOU
):
    verify_jwt_token(token)
    article = db.query(ArticleDB).filter(ArticleDB.id == article_id).first()
    if not article: raise HTTPException(status_code=404)

    old_slug = article.slug
    old_status = article.status
    new_status = "Published" if payload.get('status', '').lower() == "published" else "Draft"
    new_slug = payload.get('slug', old_slug)

    # Verificare slug unic
    if new_slug != old_slug and db.query(ArticleDB).filter(ArticleDB.slug == new_slug).first():
        raise HTTPException(status_code=400, detail="Slug ocupat.")

    # Actualizare câmpuri
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
    db.refresh(article)  # Important: Reimprospătăm datele înainte de a le trimite în background

    # --- LOGICA BACKGROUND TASKS (Nu mai aștepți 9s) ---
    if old_status == "Published" and new_status == "Draft":
        # A devenit Draft -> Ștergem de pe site
        background_tasks.add_task(unpublish_content_pipeline, old_slug)

    elif old_status == "Published" and new_status == "Published" and old_slug != new_slug:
        # Slug schimbat -> Ștergem vechiul URL, publicăm pe cel nou
        background_tasks.add_task(unpublish_content_pipeline, old_slug)
        background_tasks.add_task(publish_content_pipeline, article)

    elif new_status == "Published":
        # Doar actualizare conținut sau publicare nouă
        background_tasks.add_task(publish_content_pipeline, article)

    return {"status": "success", "article": article}


@app.patch("/articles/{article_id}")
async def patch_status(
    article_id: int,
    payload: dict = Body(...),
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None  # <--- PARAMETRU NOU
):
    verify_jwt_token(token)
    article = db.query(ArticleDB).filter(ArticleDB.id == article_id).first()
    if not article: raise HTTPException(status_code=404)

    old_status = article.status
    new_status = "Published" if payload.get('status', '').lower() == "published" else "Draft"

    article.status = new_status
    if new_status == "Published" and not article.published_at:
        article.published_at = datetime.utcnow()

    db.commit()
    db.refresh(article) # Reimprospătăm obiectul

    # --- LOGICA BACKGROUND TASKS ---
    if old_status == "Published" and new_status == "Draft":
        # Retragem de pe site
        background_tasks.add_task(unpublish_content_pipeline, article.slug)
    elif new_status == "Published":
        # Publicăm pe site
        background_tasks.add_task(publish_content_pipeline, article)

    return {"status": "success", "article": article}


@app.delete("/articles/{article_id}")
async def delete_article(
        article_id: int,
        token: str = Depends(oauth2_scheme),
        db: Session = Depends(get_db),
        background_tasks: BackgroundTasks = None  # <--- PARAMETRU NOU
):
    verify_jwt_token(token)
    article = db.query(ArticleDB).filter(ArticleDB.id == article_id).first()
    if not article: raise HTTPException(status_code=404)

    slug = article.slug
    was_published = (article.status == "Published")

    db.delete(article)
    db.commit()

    # --- LOGICA BACKGROUND TASKS ---
    if was_published:
        # Ștergem fișierul HTML și actualizăm sitemap-ul în fundal
        background_tasks.add_task(unpublish_content_pipeline, slug)

    return {"status": "success"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)