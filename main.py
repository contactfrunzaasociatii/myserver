from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List, Optional
import re

from models import SessionLocal, Blog
from schemas import BlogCreate, BlogUpdate, BlogResponse, BlogSearchResponse

app = FastAPI(title="Blog API", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Dependency pentru database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.post("/api/blogs", response_model=BlogResponse, status_code=201)
async def create_blog(blog_data: BlogCreate, db: Session = Depends(get_db)):
    """Creează un blog nou și îl indexează automat"""

    # Verifică dacă slug-ul există deja
    existing = db.query(Blog).filter(Blog.slug == blog_data.slug).first()
    if existing:
        raise HTTPException(status_code=400, detail="Slug-ul există deja")

    # Creează blogul
    new_blog = Blog(
        titlu=blog_data.titlu,
        slug=blog_data.slug,
        categorie=blog_data.categorie,
        taguri=",".join(blog_data.taguri),  # Salvează ca string
        extras=blog_data.extras,
        imagine_url=blog_data.imagine_url,
        continut=blog_data.continut
    )

    # Generează vectorul de căutare pentru indexare automată
    new_blog.generate_search_vector()

    db.add(new_blog)
    db.commit()
    db.refresh(new_blog)

    # Convertește la dict cu taguri ca listă
    result = new_blog.to_dict()
    return result


@app.get("/api/blogs", response_model=List[BlogResponse])
async def get_blogs(
        skip: int = 0,
        limit: int = 20,
        categorie: Optional[str] = None,
        db: Session = Depends(get_db)
):
    """Obține lista de bloguri cu paginare și filtrare"""
    query = db.query(Blog)

    if categorie:
        query = query.filter(Blog.categorie == categorie)

    blogs = query.order_by(Blog.created_at.desc()).offset(skip).limit(limit).all()
    return [blog.to_dict() for blog in blogs]


@app.get("/api/blogs/{slug}", response_model=BlogResponse)
async def get_blog(slug: str, db: Session = Depends(get_db)):
    """Obține un blog după slug"""
    blog = db.query(Blog).filter(Blog.slug == slug).first()
    if not blog:
        raise HTTPException(status_code=404, detail="Blogul nu a fost găsit")
    return blog.to_dict()


@app.put("/api/blogs/{slug}", response_model=BlogResponse)
async def update_blog(
        slug: str,
        blog_data: BlogUpdate,
        db: Session = Depends(get_db)
):
    """Actualizează un blog și re-indexează"""
    blog = db.query(Blog).filter(Blog.slug == slug).first()
    if not blog:
        raise HTTPException(status_code=404, detail="Blogul nu a fost găsit")

    # Actualizează câmpurile
    if blog_data.titlu:
        blog.titlu = blog_data.titlu
    if blog_data.categorie:
        blog.categorie = blog_data.categorie
    if blog_data.taguri:
        blog.taguri = ",".join(blog_data.taguri)
    if blog_data.extras is not None:
        blog.extras = blog_data.extras
    if blog_data.imagine_url is not None:
        blog.imagine_url = blog_data.imagine_url
    if blog_data.continut:
        blog.continut = blog_data.continut

    # Re-generează vectorul de căutare
    blog.generate_search_vector()

    db.commit()
    db.refresh(blog)
    return blog.to_dict()


@app.delete("/api/blogs/{slug}", status_code=204)
async def delete_blog(slug: str, db: Session = Depends(get_db)):
    """Șterge un blog"""
    blog = db.query(Blog).filter(Blog.slug == slug).first()
    if not blog:
        raise HTTPException(status_code=404, detail="Blogul nu a fost găsit")

    db.delete(blog)
    db.commit()
    return None


@app.get("/api/search", response_model=BlogSearchResponse)
async def search_blogs(
        q: str = Query(..., min_length=2, description="Query de căutare"),
        limit: int = Query(20, le=100),
        db: Session = Depends(get_db)
):
    """
    Căutare full-text în bloguri.
    Caută în titlu, taguri, categorie, conținut și extras.
    """
    # Curăță și normalizează query-ul
    search_query = q.lower().strip()
    search_terms = re.sub(r'[^\w\s]', ' ', search_query).split()

    # Construiește query-ul de căutare
    query = db.query(Blog)

    # Caută fiecare termen în vectorul de căutare
    for term in search_terms:
        query = query.filter(Blog.search_vector.contains(term))

    # Execută query-ul
    results = query.order_by(Blog.created_at.desc()).limit(limit).all()

    return {
        "total": len(results),
        "results": [blog.to_dict() for blog in results],
        "query": q
    }


@app.get("/api/blogs/tag/{tag}", response_model=List[BlogResponse])
async def get_blogs_by_tag(
        tag: str,
        limit: int = Query(20, le=100),
        db: Session = Depends(get_db)
):
    """Obține bloguri după un tag specific"""
    tag_normalized = tag.lower().strip()

    # Caută bloguri care conțin tagul
    blogs = db.query(Blog).filter(
        Blog.taguri.contains(tag_normalized)
    ).order_by(Blog.created_at.desc()).limit(limit).all()

    return [blog.to_dict() for blog in blogs]


@app.get("/api/categories", response_model=List[str])
async def get_categories(db: Session = Depends(get_db)):
    """Obține lista de categorii unice"""
    categories = db.query(Blog.categorie).distinct().all()
    return [cat[0] for cat in categories]


@app.get("/api/tags", response_model=List[str])
async def get_popular_tags(limit: int = 50, db: Session = Depends(get_db)):
    """Obține cele mai populare taguri"""
    blogs = db.query(Blog.taguri).all()

    # Colectează toate tagurile
    tag_count = {}
    for blog in blogs:
        if blog[0]:
            tags = blog[0].split(",")
            for tag in tags:
                tag = tag.strip()
                tag_count[tag] = tag_count.get(tag, 0) + 1

    # Sortează după popularitate
    sorted_tags = sorted(tag_count.items(), key=lambda x: x[1], reverse=True)
    return [tag[0] for tag in sorted_tags[:limit]]


@app.get("/")
async def root():
    return {
        "message": "Blog API",
        "endpoints": {
            "create": "POST /api/blogs",
            "list": "GET /api/blogs",
            "get": "GET /api/blogs/{slug}",
            "update": "PUT /api/blogs/{slug}",
            "delete": "DELETE /api/blogs/{slug}",
            "search": "GET /api/search?q=query",
            "by_tag": "GET /api/blogs/tag/{tag}",
            "categories": "GET /api/categories",
            "tags": "GET /api/tags"
        }
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)