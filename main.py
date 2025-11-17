from fastapi import FastAPI, Depends, HTTPException, Form
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
import jwt
import os
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader
import xml.etree.ElementTree as ET
import requests
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# ==================== CONFIG ====================
# SFTP / cPanel
CPANEL_HOST = os.getenv("CPANEL_HOST")
CPANEL_PORT = int(os.getenv("CPANEL_PORT", 22))
CPANEL_USER = os.getenv("CPANEL_USERNAME")
CPANEL_PASSWORD = os.getenv("CPANEL_PASSWORD")
UPLOAD_PATH = os.getenv("UPLOAD_PATH")
SITE_URL = os.getenv("SITE_URL")

# Admin / JWT
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXP_MINUTES = int(os.getenv("JWT_EXP_MINUTES", 60))

# Google Indexing API
GOOGLE_PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID")
GOOGLE_PRIVATE_KEY_ID = os.getenv("GOOGLE_PRIVATE_KEY_ID")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY")
GOOGLE_CLIENT_EMAIL = os.getenv("GOOGLE_CLIENT_EMAIL")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")

# Validare configurație
required_vars = {
    "CPANEL_HOST": CPANEL_HOST,
    "CPANEL_USERNAME": CPANEL_USER,
    "CPANEL_PASSWORD": CPANEL_PASSWORD,
    "UPLOAD_PATH": UPLOAD_PATH,
    "SITE_URL": SITE_URL,
    "ADMIN_USERNAME": ADMIN_USERNAME,
    "ADMIN_PASSWORD": ADMIN_PASSWORD,
    "JWT_SECRET_KEY": JWT_SECRET_KEY,
}

missing_vars = [key for key, value in required_vars.items() if not value]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

# Check Google Indexing API credentials (optional but recommended)
google_indexing_enabled = all([
    GOOGLE_PROJECT_ID,
    GOOGLE_PRIVATE_KEY_ID,
    GOOGLE_PRIVATE_KEY,
    GOOGLE_CLIENT_EMAIL,
    GOOGLE_CLIENT_ID
])

if google_indexing_enabled:
    logger.info("Google Indexing API is enabled")
else:
    logger.warning("Google Indexing API credentials not configured - indexing requests will be skipped")

# Frontend / API
GENERATED_DIR = "generated"
os.makedirs(GENERATED_DIR, exist_ok=True)
SITEMAP_FILE = os.path.join(GENERATED_DIR, "sitemap.xml")

# Templates
env = Environment(loader=FileSystemLoader("templates"))

# ==================== APP ====================
app = FastAPI(title="Frunză & Asociații CMS API")

# ==================== CORS ====================
origins = [
    "http://localhost:3000",
    "http://localhost:5173",
    "https://frunza-asociatii.ro",
    "https://www.frunza-asociatii.ro",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


# ==================== JWT FUNCTIONS ====================
def create_jwt_token(username: str):
    """Create JWT token for authenticated user"""
    expire = datetime.utcnow() + timedelta(minutes=JWT_EXP_MINUTES)
    payload = {"sub": username, "exp": expire}
    token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return token


def verify_jwt_token(token: str):
    """Verify JWT token and return username"""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        username = payload.get("sub")
        if username != ADMIN_USERNAME:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        return username
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


# ==================== AUTH ENDPOINT ====================
@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """Login endpoint to get JWT token"""
    if form_data.username == ADMIN_USERNAME and form_data.password == ADMIN_PASSWORD:
        token = create_jwt_token(form_data.username)
        return {"access_token": token, "token_type": "bearer"}
    raise HTTPException(status_code=400, detail="Incorrect username or password")


# ==================== FTP UPLOAD ====================
def upload_to_cpanel(local_path: str, remote_filename: str):
    """
    Upload file to cPanel via FTP with improved error handling

    Args:
        local_path: Local file path to upload
        remote_filename: Remote filename (without path)

    Returns:
        str: Remote file path

    Raises:
        ValueError: If upload fails
    """
    from ftplib import FTP, error_perm

    ftp = None

    try:
        logger.info(f"Attempting FTP connection to {CPANEL_HOST}:{CPANEL_PORT}")

        # Create FTP connection
        ftp = FTP()
        ftp.connect(CPANEL_HOST, CPANEL_PORT, timeout=30)

        # Login
        logger.info(f"Logging in as user: {CPANEL_USER}")
        ftp.login(CPANEL_USER, CPANEL_PASSWORD)

        logger.info(f"FTP connection successful. Current directory: {ftp.pwd()}")

        # Change to upload directory
        try:
            ftp.cwd(UPLOAD_PATH)
            logger.info(f"Changed to directory: {UPLOAD_PATH}")
        except error_perm as e:
            logger.error(f"Cannot access directory {UPLOAD_PATH}: {e}")
            # Try to create directory
            try:
                # Navigate to parent and create
                parts = UPLOAD_PATH.strip('/').split('/')
                current = '/'
                for part in parts:
                    current = f"{current}{part}/"
                    try:
                        ftp.cwd(current)
                    except:
                        ftp.mkd(current)
                        ftp.cwd(current)
                logger.info(f"Created and changed to directory: {UPLOAD_PATH}")
            except Exception as create_error:
                logger.error(f"Could not create directory: {create_error}")
                raise ValueError(f"Remote directory {UPLOAD_PATH} does not exist and cannot be created")

        # Upload file in binary mode
        remote_path = f"{UPLOAD_PATH}/{remote_filename}".replace('//', '/')
        logger.info(f"Uploading {local_path} to {remote_path}")

        with open(local_path, 'rb') as f:
            ftp.storbinary(f'STOR {remote_filename}', f)

        logger.info(f"File uploaded successfully to {remote_path}")

        return remote_path

    except error_perm as e:
        logger.error(f"FTP permission error: {e}")
        if "530" in str(e):
            raise ValueError(f"FTP authentication failed. Check username and password.")
        elif "550" in str(e):
            raise ValueError(f"FTP permission denied. Check directory permissions.")
        else:
            raise ValueError(f"FTP error: {str(e)}")

    except FileNotFoundError as e:
        logger.error(f"File not found: {e}")
        raise ValueError(f"Local file not found: {local_path}")

    except Exception as e:
        logger.error(f"Unexpected error during FTP upload: {e}")
        raise ValueError(f"Upload failed: {str(e)}")

    finally:
        # Close FTP connection
        if ftp:
            try:
                ftp.quit()
            except:
                try:
                    ftp.close()
                except:
                    pass


# ==================== SITEMAP ====================
def update_sitemap(new_url: str):
    """Add new URL to sitemap.xml without duplicates"""
    try:
        if os.path.exists(SITEMAP_FILE):
            tree = ET.parse(SITEMAP_FILE)
            root = tree.getroot()
        else:
            root = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")

        # Verifică dacă URL-ul există deja
        existing_urls = [url.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc").text
                         if url.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc") is not None
                         else url.find("loc").text
                         for url in root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url")]

        if new_url in existing_urls:
            logger.info(f"URL already exists in sitemap: {new_url}")
            return  # nu adăuga duplicate

        url_elem = ET.Element("url")
        ET.SubElement(url_elem, "loc").text = new_url
        ET.SubElement(url_elem, "lastmod").text = datetime.utcnow().strftime("%Y-%m-%d")
        ET.SubElement(url_elem, "changefreq").text = "weekly"
        ET.SubElement(url_elem, "priority").text = "0.8"

        root.append(url_elem)
        tree = ET.ElementTree(root)
        tree.write(SITEMAP_FILE, encoding="utf-8", xml_declaration=True)

        logger.info(f"Sitemap updated with new URL: {new_url}")

    except Exception as e:
        logger.error(f"Error updating sitemap: {e}")
        raise ValueError(f"Failed to update sitemap: {str(e)}")


def ping_google(sitemap_url: str):
    """Notify Google about sitemap update"""
    try:
        response = requests.get(
            f"http://www.google.com/ping?sitemap={sitemap_url}",
            timeout=10
        )
        logger.info(f"Google sitemap pinged successfully. Status: {response.status_code}")
    except Exception as e:
        logger.warning(f"Failed to ping Google sitemap (non-critical): {e}")


def notify_google_indexing_api(url: str):
    """
    Notify Google Indexing API to request immediate indexing of a URL

    Args:
        url: The URL to be indexed
    """
    if not google_indexing_enabled:
        logger.warning("Google Indexing API not configured - skipping indexing request")
        return

    try:
        # Construim service account info din variabile de mediu
        service_account_info = {
            "type": "service_account",
            "project_id": GOOGLE_PROJECT_ID,
            "private_key_id": GOOGLE_PRIVATE_KEY_ID,
            # Railway pune liniile private key într-o singură linie, trebuie să le transformăm în multiline
            "private_key": GOOGLE_PRIVATE_KEY.replace("\\n", "\n"),
            "client_email": GOOGLE_CLIENT_EMAIL,
            "client_id": GOOGLE_CLIENT_ID,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": f"https://www.googleapis.com/robot/v1/metadata/x509/{GOOGLE_CLIENT_EMAIL}",
        }

        # Creează credentials și client Indexing API
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=["https://www.googleapis.com/auth/indexing"]
        )
        service = build("indexing", "v3", credentials=credentials)

        # Trimite cerere de indexare
        body = {"url": url, "type": "URL_UPDATED"}
        response = service.urlNotifications().publish(body=body).execute()

        logger.info(f"Google Indexing API notified successfully for URL: {url}")
        logger.info(f"Response: {response}")

    except Exception as e:
        logger.warning(f"Google Indexing API request failed (non-critical): {e}")


# ==================== CREATE ARTICLE ====================
@app.post("/create-article/")
async def create_article(
        title: str = Form(...),
        slug: str = Form(...),
        category: str = Form(...),
        tags: str = Form(...),
        extras: str = Form(None),
        cover_image: str = Form(None),
        content: str = Form(...),
        token: str = Depends(oauth2_scheme)
):
    """
    Create a new article and upload to cPanel

    Requires valid JWT token for authentication
    """
    # Verify authentication
    verify_jwt_token(token)

    local_path = None

    try:
        logger.info(f"Creating article: {title} (slug: {slug})")

        # Render template
        template = env.get_template("article_template.html")
        html_content = template.render(
            title=title,
            slug=slug,
            category=category,
            tags=tags,
            extras=extras,
            cover_image=cover_image,
            content=content,
            created_date=datetime.utcnow().strftime("%Y-%m-%d")
        )

        # Generate filename
        filename = slug.lower().replace(" ", "-") + ".html"
        local_path = os.path.join(GENERATED_DIR, filename)

        # Write local file
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        logger.info(f"Article file created locally: {local_path}")

        # Upload to cPanel
        try:
            upload_to_cpanel(local_path, filename)
        except ValueError as e:
            logger.error(f"Upload failed: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to upload article to server: {str(e)}"
            )

        # Generate file URL
        file_url = f"{SITE_URL}/noutati/{filename}"

        # Update sitemap and notify Google
        try:
            # 1. Update sitemap
            update_sitemap(file_url)

            # 2. Upload updated sitemap
            upload_to_cpanel(SITEMAP_FILE, "sitemap.xml")

            # 3. Ping Google about sitemap update
            sitemap_url = f"{SITE_URL}/sitemap.xml"
            ping_google(sitemap_url)

            # 4. Request immediate indexing via Google Indexing API
            notify_google_indexing_api(file_url)

            logger.info(f"SEO operations completed for: {file_url}")

        except Exception as e:
            logger.warning(f"SEO operations failed (non-critical): {e}")
            # Don't fail the whole operation if SEO operations fail

        logger.info(f"Article published successfully: {file_url}")

        return {
            "status": "success",
            "message": "Article created and published successfully",
            "file": filename,
            "url": file_url,
            "indexing_requested": google_indexing_enabled
        }

    except HTTPException:
        # Re-raise HTTP exceptions (auth, validation, etc.)
        raise

    except Exception as e:
        logger.error(f"Unexpected error creating article: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create article: {str(e)}"
        )

    finally:
        # Clean up local file
        if local_path and os.path.exists(local_path):
            try:
                os.remove(local_path)
                logger.info(f"Local file cleaned up: {local_path}")
            except Exception as e:
                logger.warning(f"Failed to remove local file: {e}")


# ==================== HEALTH CHECK ====================
@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "online",
        "service": "Frunză & Asociații CMS API",
        "version": "1.0.0"
    }


@app.get("/health")
async def health_check():
    """Detailed health check"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "config": {
            "cpanel_host": CPANEL_HOST,
            "cpanel_port": CPANEL_PORT,
            "site_url": SITE_URL,
            "upload_path_configured": bool(UPLOAD_PATH),
            "google_indexing_enabled": google_indexing_enabled,
        }
    }


# ==================== RUN UVICORN ====================
if __name__ == "__main__":
    import uvicorn

    PORT = int(os.getenv("PORT", 8000))

    logger.info(f"Starting server on port {PORT}")
    logger.info(f"CORS origins: {origins}")
    logger.info(f"cPanel host: {CPANEL_HOST}:{CPANEL_PORT}")
    logger.info(f"Google Indexing API: {'enabled' if google_indexing_enabled else 'disabled'}")

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        reload=False  # Set to True only in development
    )