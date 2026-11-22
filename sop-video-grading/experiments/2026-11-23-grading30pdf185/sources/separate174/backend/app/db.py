from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/vlm_eval")

# SQLAlchemy 2.1 changed PostgreSQL's default driver from psycopg2 to psycopg.
# Select the driver we install explicitly instead of depending on that default.
SQLALCHEMY_DATABASE_URL = DATABASE_URL.replace(
    "postgresql://", "postgresql+psycopg2://", 1,
)

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def asyncpg_dsn() -> str:
    return (
        DATABASE_URL
        .replace("postgresql+psycopg2://", "postgresql://", 1)
        .replace("postgresql+psycopg://", "postgresql://", 1)
    )

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    from app.models import evaluation  # register all models before creating tables

    # Dynamic table creation
    Base.metadata.create_all(bind=engine)
    
    # Create PostgreSQL Trigger for Pub/Sub over job_branches
    with engine.begin() as conn:
        conn.execute(text("""
        CREATE OR REPLACE FUNCTION notify_branch_status_change()
        RETURNS TRIGGER AS $$
        DECLARE
          payload JSON;
        BEGIN
          payload = json_build_object(
            'job_id', NEW.job_id,
            'branch_name', NEW.branch_name,
            'status', NEW.status,
            'progress', NEW.progress,
            'message', NEW.message
          );
          PERFORM pg_notify('branch_updates', payload::text);
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """))
        
        conn.execute(text("DROP TRIGGER IF EXISTS trigger_branch_status_update ON job_branches;"))
        conn.execute(text("""
        CREATE TRIGGER trigger_branch_status_update
        AFTER INSERT OR UPDATE ON job_branches
        FOR EACH ROW
        EXECUTE PROCEDURE notify_branch_status_change();
        """))

