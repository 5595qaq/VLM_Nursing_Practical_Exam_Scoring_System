from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from typing import List, Optional, Any, Dict, Literal

AgentName = Literal["Agent_A", "Agent_B", "Agent_C", "Agent_D"]
# agents: Time_cuting + selected Agent_A ~ Agent_D; sop_skill: only sop-video-skill segmentation;
# sop_grading: sop-video-skill segmentation, then sop-video-grading.
Pipeline = Literal["agents", "sop_skill", "sop_grading"]

class EvaluationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exam_topic: str
    video_paths: List[str] = Field(min_length=1)  # gs:// URIs; excess videos queue in PostgreSQL
    processing_mode: Optional[str] = Field(default="standard")
    pipeline: Pipeline = "agents"
    selected_agents: Optional[List[AgentName]] = None

    @field_validator("selected_agents")
    @classmethod
    def unique_agents(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("selected_agents must not contain duplicates")
        return value

    @model_validator(mode="after")
    def agents_for_pipeline(self):
        if self.pipeline in ("sop_skill", "sop_grading"):
            if self.selected_agents:
                raise ValueError(f"selected_agents must be omitted or empty for the {self.pipeline} pipeline")
            self.selected_agents = []
        elif self.selected_agents is None:
            self.selected_agents = ["Agent_A", "Agent_B", "Agent_C", "Agent_D"]
        elif not self.selected_agents:
            raise ValueError("selected_agents must contain at least 1 agent")
        return self

class EvaluationResponse(BaseModel):
    id: str
    exam_topic: str
    status: str
    video_paths: List[str]
    selected_agents: List[AgentName] = Field(default_factory=lambda: ["Agent_A", "Agent_B", "Agent_C", "Agent_D"])
    pipeline: Pipeline = "agents"
    result: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)
