"""
Database setup and models using SQLAlchemy + SQLite (local) or PostgreSQL (Supabase).
SQLite for dev, Supabase PostgreSQL for production.
"""
import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean,
    DateTime, Text, ForeignKey, Enum, JSON
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import enum

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///wholesale.db")
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


# ── Enums ─────────────────────────────────────────────────────────
class LeadStatus(str, enum.Enum):
    NEW = "new"
    CONTACTED = "contacted"
    INTERESTED = "interested"
    NEGOTIATING = "negotiating"
    UNDER_CONTRACT = "under_contract"
    CLOSED = "closed"
    DEAD = "dead"

class ContactMethod(str, enum.Enum):
    SMS = "sms"
    EMAIL = "email"
    VOICE = "voice"
    MAIL = "mail"

class Language(str, enum.Enum):
    EN = "en"
    ES = "es"

class DealGrade(str, enum.Enum):
    A = "A"  # Hot deal, auto-pursue
    B = "B"  # Good deal, worth pursuing
    C = "C"  # Marginal, maybe
    D = "D"  # Bad deal, skip
    F = "F"  # No deal


# ── Models ────────────────────────────────────────────────────────
class Property(Base):
    __tablename__ = "properties"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Address
    street = Column(String(255), nullable=False)
    city = Column(String(100), nullable=False)
    county = Column(String(100))
    state = Column(String(2), nullable=False, default="TX")
    zip_code = Column(String(10))

    # Property details
    property_type = Column(String(50))  # single_family, multi_family, etc.
    bedrooms = Column(Integer)
    bathrooms = Column(Float)
    sqft = Column(Integer)
    lot_size_sqft = Column(Integer)
    year_built = Column(Integer)

    # Tax / Legal
    parcel_id = Column(String(100), unique=True)
    tax_assessed_value = Column(Float)
    tax_delinquent = Column(Boolean, default=False)
    tax_delinquent_amount = Column(Float)
    pre_foreclosure = Column(Boolean, default=False)
    probate = Column(Boolean, default=False)
    vacant = Column(Boolean, default=False)
    code_violations = Column(Boolean, default=False)

    # Source
    source = Column(String(100))  # county_tax, zillow, craigslist, etc.
    source_url = Column(Text)
    scraped_at = Column(DateTime)

    # Relationships
    owner = relationship("Owner", back_populates="property", uselist=False)
    deal = relationship("Deal", back_populates="property", uselist=False)
    contacts = relationship("ContactLog", back_populates="property")


class Owner(Base):
    __tablename__ = "owners"

    id = Column(Integer, primary_key=True, autoincrement=True)
    property_id = Column(Integer, ForeignKey("properties.id"), unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Owner info
    first_name = Column(String(100))
    last_name = Column(String(100))
    full_name = Column(String(255))
    mailing_address = Column(String(255))
    mailing_city = Column(String(100))
    mailing_state = Column(String(2))
    mailing_zip = Column(String(10))

    # Contact info (skip traced)
    phone_1 = Column(String(20))
    phone_2 = Column(String(20))
    phone_3 = Column(String(20))
    email_1 = Column(String(255))
    email_2 = Column(String(255))
    email_verified = Column(Boolean, default=False)
    phone_verified = Column(Boolean, default=False)

    # Language preference (for bilingual outreach)
    preferred_language = Column(String(2), default="en")  # en or es

    # Skip trace source
    skip_trace_source = Column(String(100))
    skip_traced_at = Column(DateTime)

    # Relationships
    property = relationship("Property", back_populates="owner")


class Deal(Base):
    __tablename__ = "deals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    property_id = Column(Integer, ForeignKey("properties.id"), unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Analysis
    arv = Column(Float)                    # After Repair Value
    repair_estimate = Column(Float)
    repair_level = Column(String(20))      # light, medium, heavy
    max_offer = Column(Float)              # ARV * 0.50 - repairs - assignment_fee
    assignment_fee = Column(Float, default=5000.0)
    comp_count = Column(Integer)
    comp_avg_price = Column(Float)
    comp_data = Column(JSON)               # Raw comp data

    # Scoring
    grade = Column(String(1))              # A, B, C, D, F
    score = Column(Float)                  # 0-100
    motivation_score = Column(Float)       # How motivated is the seller (0-100)

    # Status
    status = Column(String(20), default="new")
    offered_price = Column(Float)
    accepted_price = Column(Float)

    # Contract
    contract_path = Column(Text)           # Path to generated PDF
    contract_signed = Column(Boolean, default=False)
    contract_signed_at = Column(DateTime)
    signature_data = Column(Text)          # Base64 signature image

    # JV Partner
    sent_to_jv = Column(Boolean, default=False)
    sent_to_jv_at = Column(DateTime)
    jv_response = Column(String(50))

    # Relationships
    property = relationship("Property", back_populates="deal")


class ContactLog(Base):
    __tablename__ = "contact_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    property_id = Column(Integer, ForeignKey("properties.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    # Contact details
    method = Column(String(10))            # sms, email, voice
    language = Column(String(2), default="en")  # en or es
    direction = Column(String(10))         # outbound, inbound
    phone_number = Column(String(20))
    email_address = Column(String(255))

    # Content
    message = Column(Text)
    template_name = Column(String(100))
    ai_response = Column(Text)             # What the AI said/wrote

    # Voice call specific
    call_duration = Column(Integer)        # seconds
    call_recording_url = Column(Text)
    call_transcript = Column(Text)
    call_sentiment = Column(String(20))    # positive, neutral, negative

    # Result
    responded = Column(Boolean, default=False)
    response_text = Column(Text)
    interested = Column(Boolean)
    asking_price = Column(Float)           # What seller wants
    notes = Column(Text)

    # Relationships
    property = relationship("Property", back_populates="contacts")


class SMSDrip(Base):
    __tablename__ = "sms_drips"

    id = Column(Integer, primary_key=True, autoincrement=True)
    property_id = Column(Integer, ForeignKey("properties.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    # Drip sequence
    sequence_number = Column(Integer, default=1)
    max_sequence = Column(Integer, default=5)
    language = Column(String(2), default="en")
    phone = Column(String(20))  # Recipient phone number
    next_send_at = Column(DateTime)
    sent = Column(Boolean, default=False)
    message = Column(Text)
    active = Column(Boolean, default=True)  # False if they responded or opted out


class EmailCampaign(Base):
    __tablename__ = "email_campaigns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    property_id = Column(Integer, ForeignKey("properties.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    sequence_number = Column(Integer, default=1)
    max_sequence = Column(Integer, default=3)
    language = Column(String(2), default="en")
    recipient_email = Column(String(255))  # Where to send
    subject = Column(String(255))
    body = Column(Text)
    next_send_at = Column(DateTime)
    sent = Column(Boolean, default=False)
    opened = Column(Boolean, default=False)
    clicked = Column(Boolean, default=False)
    active = Column(Boolean, default=True)


def init_db():
    """Create all tables."""
    Base.metadata.create_all(engine)

def get_db():
    """Get a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

if __name__ == "__main__":
    init_db()
    print("Database tables created successfully.")
