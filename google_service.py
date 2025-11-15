from sqlalchemy import Column, Integer, String, Text, DateTime, Index, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import re

Base = declarative_base()


class Blog(Base):
    __tablename__ = "blogs"

    id = Column(Integer, primary_key=True, index=True)
    titlu = Column(String(255), nullable=False, index=True)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    categorie = Column(String(100), nullable=False, index=True)
    taguri = Column(String(500), nullable=False)  # stocat ca string cu virgule
    extras = Column(Text)
    imagine_url = Column(String(500))
    continut = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    search_vector = Column(Text, index=True)  # pentru indexare full-text

    # Index compozit pentru căutări rapide
    __table_args__ = (
        Index('idx_search', 'search_vector'),
        Index('idx_categorie_data', 'categorie', 'created_at'),
    )

    def generate_search_vector(self):
        """Generează vectorul de căutare pentru indexare"""
        text_parts = [
            self.titlu.lower(),
            self.taguri.lower(),
            self.categorie.lower(),
            self.continut.lower() if self.continut else "",
            self.extras.lower() if self.extras else ""
        ]
        # Curăță și normalizează textul
        search_text = " ".join(text_parts)
        # Înlocuiește caractere speciale și diacritice
        search_text = re.sub(r'[^\w\s]', ' ', search_text)
        self.search_vector = search_text

    def to_dict(self):
        return {
            "id": self.id,
            "titlu": self.titlu,
            "slug": self.slug,
            "categorie": self.categorie,
            "taguri": self.taguri.split(",") if self.taguri else [],
            "extras": self.extras,
            "imagine_url": self.imagine_url,
            "continut": self.continut,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


# Configurare database
DATABASE_URL = "sqlite:///./blog.db"  # Schimbă cu PostgreSQL pentru producție
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Creează tabelele
Base.metadata.create_all(bind=engine)