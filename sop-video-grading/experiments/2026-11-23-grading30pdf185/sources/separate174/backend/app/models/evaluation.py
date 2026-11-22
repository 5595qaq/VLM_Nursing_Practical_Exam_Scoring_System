import uuid
from sqlalchemy import (Column, String, JSON, ForeignKey, Integer, Boolean, UniqueConstraint, CheckConstraint,
                        DateTime, func, text)
from app.db import Base

class EvaluationJob(Base):
    __tablename__ = "evaluation_jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    exam_topic = Column(String)
    processing_mode = Column(String) # standard or batch
    status = Column(String, default="pending") 
    generation = Column(Integer, nullable=False, default=0, server_default="0")
    video_paths = Column(JSON, default=list)
    selected_agents = Column(
        JSON, nullable=False,
        default=lambda: ["Agent_A", "Agent_B", "Agent_C", "Agent_D"],
        server_default=text("'[\"Agent_A\",\"Agent_B\",\"Agent_C\",\"Agent_D\"]'::json"),
    )
    pipeline = Column(String, nullable=False, default="agents", server_default="agents")
    result = Column(JSON, nullable=True)
    
class JobBranch(Base):
    __tablename__ = "job_branches"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id = Column(String, ForeignKey("evaluation_jobs.id", ondelete="CASCADE"), index=True)
    branch_name = Column(String) # e.g. 'GEMINI_UPLOAD', 'GEMINI_PROCESSING', 'LLM_SCORING'
    status = Column(String, default="pending") # pending, processing, finished, failed, or retired
    progress = Column(String, nullable=True) # e.g. '1/3'
    message = Column(String, nullable=True)


class EvaluationVideo(Base):
    __tablename__ = "evaluation_videos"
    __table_args__ = (UniqueConstraint("job_id", "position"),)

    id = Column(String, primary_key=True)
    job_id = Column(String, ForeignKey("evaluation_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    position = Column(Integer, nullable=False)
    uri = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")
    segments = Column(JSON, nullable=True)
    error = Column(String, nullable=True)
    verified = Column(Boolean, nullable=False, default=False)
    gaze_overlay_uri = Column(String, nullable=True)
    gaze_metadata_uri = Column(String, nullable=True)
    gaze_artifacts = Column(JSON, nullable=True)
    gaze_status = Column(String, nullable=False, default="pending", server_default="pending")
    gaze_error = Column(String, nullable=True)
    sop_result = Column(JSON, nullable=True)
    grading_result = Column(JSON, nullable=True)  # sop-video-grading outcome (sop_grading pipeline)
    grading_partial = Column(JSON, nullable=True)  # graded criteria of a failed attempt, resumed next time


class EvaluationAgentRun(Base):
    __tablename__ = "evaluation_agent_runs"
    __table_args__ = (UniqueConstraint("video_id", "agent_name"),)

    id = Column(String, primary_key=True)
    video_id = Column(String, ForeignKey("evaluation_videos.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_name = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")
    result = Column(JSON, nullable=True)


class EvaluationProgress(Base):
    __tablename__ = "evaluation_progress"
    __table_args__ = (CheckConstraint("completed_steps >= 0 AND completed_steps <= total_steps"),)

    job_id = Column(String, ForeignKey("evaluation_jobs.id", ondelete="CASCADE"), primary_key=True)
    total_steps = Column(Integer, nullable=False)
    completed_steps = Column(Integer, nullable=False, default=0)


class ModelUsage(Base):
    """Tokens of one model response (see app.services.usage for the column meanings)."""
    __tablename__ = "model_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String, ForeignKey("evaluation_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    video_id = Column(String, nullable=True)
    generation = Column(Integer, nullable=False, default=0, server_default="0")
    stage = Column(String, nullable=True)  # Time_cuting, Agent_A–D, SOP_skill
    queue_attempt = Column(Integer, nullable=True)
    provider = Column(String, nullable=False)  # gemini or claude
    model = Column(String, nullable=False)
    input_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    cached_input_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    cache_write_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    output_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    thinking_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
