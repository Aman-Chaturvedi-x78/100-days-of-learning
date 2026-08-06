"""
Storage layer using SQLAlchemy (sync) for demo simplicity.
"""
import os
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./shortener.db")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class URL(Base):
    __tablename__ = "urls"
    id = Column(Integer, primary_key=True, index=True)
    short_id = Column(String(128), unique=True, index=True, nullable=False)
    original_url = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    ttl = Column(Integer, nullable=True)
    owner_id = Column(String(64), nullable=True)
    last_accessed = Column(DateTime(timezone=True), nullable=True)


def init_db():
    Base.metadata.create_all(bind=engine)


def create_mapping(short_id, original_url, ttl=None, owner_id=None):
    db = SessionLocal()
    try:
        url = URL(short_id=short_id, original_url=original_url, ttl=ttl, owner_id=owner_id)
        db.add(url)
        db.commit()
        db.refresh(url)
        return url
    except IntegrityError:
        db.rollback()
        raise
    finally:
        db.close()


def get_mapping(short_id):
    db = SessionLocal()
    try:
        return db.query(URL).filter(URL.short_id == short_id).first()
    finally:
        db.close()


def update_last_accessed(short_id):
    db = SessionLocal()
    try:
        db.query(URL).filter(URL.short_id == short_id).update({"last_accessed": func.now()})
        db.commit()
    finally:
        db.close()
