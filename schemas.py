from pydantic import BaseModel, validator
from typing import List, Optional
from datetime import datetime
import re


class BlogCreate(BaseModel):
    titlu: str
    slug: str
    categorie: str
    taguri: List[str]
    extras: Optional[str] = None
    imagine_url: Optional[str] = None
    continut: str

    @validator('titlu')
    def validate_titlu(cls, v):
        if not v or len(v.strip()) < 3:
            raise ValueError('Titlul trebuie să aibă minim 3 caractere')
        if len(v) > 255:
            raise ValueError('Titlul este prea lung (max 255 caractere)')
        return v.strip()

    @validator('slug')
    def validate_slug(cls, v):
        if not re.match(r'^[a-z0-9-]+$', v):
            raise ValueError('Slug-ul trebuie să conțină doar litere mici, cifre și cratime')
        return v

    @validator('categorie')
    def validate_categorie(cls, v):
        if not v or len(v.strip()) < 2:
            raise ValueError('Categoria este obligatorie')
        return v.strip()

    @validator('taguri')
    def validate_taguri(cls, v):
        if not v or len(v) == 0:
            raise ValueError('Trebuie să adaugi cel puțin un tag')
        # Curăță și normalizează tagurile
        cleaned = [tag.strip().lower() for tag in v if tag.strip()]
        if len(cleaned) == 0:
            raise ValueError('Tagurile nu pot fi goale')
        return cleaned

    @validator('continut')
    def validate_continut(cls, v):
        if not v or len(v.strip()) < 50:
            raise ValueError('Conținutul trebuie să aibă minim 50 caractere')
        return v.strip()


class BlogUpdate(BaseModel):
    titlu: Optional[str] = None
    categorie: Optional[str] = None
    taguri: Optional[List[str]] = None
    extras: Optional[str] = None
    imagine_url: Optional[str] = None
    continut: Optional[str] = None


class BlogResponse(BaseModel):
    id: int
    titlu: str
    slug: str
    categorie: str
    taguri: List[str]
    extras: Optional[str]
    imagine_url: Optional[str]
    continut: str
    created_at: datetime

    class Config:
        from_attributes = True


class BlogSearchResponse(BaseModel):
    total: int
    results: List[BlogResponse]
    query: str