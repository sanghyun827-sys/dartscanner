from sqlalchemy import Column, Integer, String, Text, DateTime, SmallInteger, ForeignKey, JSON
from sqlalchemy.orm import relationship
from datetime import datetime
from pgvector.sqlalchemy import Vector
from ..database import Base
from ..config import settings


class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    corp_code = Column(String(8), unique=True, nullable=False, index=True)
    corp_name = Column(String(255), nullable=False, index=True)
    stock_code = Column(String(6))
    modify_date = Column(String(8))


class Disclosure(Base):
    __tablename__ = "disclosures"

    id = Column(Integer, primary_key=True)
    rcp_no = Column(String(14), unique=True, nullable=False, index=True)
    corp_code = Column(String(8), nullable=False)
    corp_name = Column(String(255))
    report_nm = Column(String(500))
    rcept_dt = Column(String(8))
    flr_nm = Column(String(255))
    rm = Column(String(20))
    # 0: 미처리, 1: 처리중, 2: 완료, 3: 실패
    is_embedded = Column(SmallInteger, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    chunks = relationship("DocumentChunk", back_populates="disclosure", cascade="all, delete-orphan")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id = Column(Integer, primary_key=True)
    disclosure_id = Column(Integer, ForeignKey("disclosures.id", ondelete="CASCADE"))
    rcp_no = Column(String(14), nullable=False, index=True)
    corp_name = Column(String(255), index=True)
    report_nm = Column(String(500))
    chunk_text = Column(Text, nullable=False)
    chunk_index = Column(Integer)
    embedding = Column(Vector(settings.embedding_dim))
    meta = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)

    disclosure = relationship("Disclosure", back_populates="chunks")


class IFRSChunk(Base):
    __tablename__ = "ifrs_chunks"

    id = Column(Integer, primary_key=True)
    standard_name = Column(String(50), nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    chunk_text = Column(Text, nullable=False)
    chunk_index = Column(Integer)
    embedding = Column(Vector(settings.embedding_dim))
    created_at = Column(DateTime, default=datetime.utcnow)
