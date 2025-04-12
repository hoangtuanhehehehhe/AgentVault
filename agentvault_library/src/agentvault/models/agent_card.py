"""
Pydantic models representing the structure of an A2A Agent Card.

Based on A2A Agent Card Schema concepts (specific draft version TBD).
"""

from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field, HttpUrl, field_validator, AnyUrl

# Note: Using standard Python types and Pydantic types.
# Field aliases can be used if the JSON field names differ from Pythonic names.

class AgentProvider(BaseModel):
    """Information about the provider or developer of the agent."""
    name: str = Field(..., description="Name of the agent provider or developer.")
    url: Optional[HttpUrl] = Field(None, description="Homepage URL of the provider.")
    support_contact: Optional[str] = Field(None, description="Contact information for support (e.g., email or URL).")
    # Add other relevant provider fields as needed (e.g., legal info URL)

class AgentSkill(BaseModel):
    """Describes a specific skill or capability of the agent."""
    id: str = Field(..., description="Unique identifier for the skill within the agent's context.")
    name: str = Field(..., description="Human-readable name of the skill.")
    description: str = Field(..., description="Detailed description of what the skill does.")
    input_schema: Optional[Dict[str, Any]] = Field(None, description="JSON Schema describing the expected input format for this skill (if applicable).")
    output_schema: Optional[Dict[str, Any]] = Field(None, description="JSON Schema describing the output format produced by this skill (if applicable).")
    # Add other skill-related fields (e.g., tags, categories)

class AgentAuthentication(BaseModel):
    """Describes an authentication scheme supported by the agent's A2A endpoint."""
    scheme: Literal['apiKey', 'bearer', 'oauth2', 'none'] = Field(..., description="The type of authentication scheme required.")
    description: Optional[str] = Field(None, description="Human-readable description of how to obtain and use credentials for this scheme.")
    # Add scheme-specific fields if necessary (e.g., tokenUrl for oauth2)
    # For 'apiKey', convention often implies an 'X-Api-Key' header, but details might be in description.
    service_identifier: Optional[str] = Field(None, description="Optional identifier used by the client's KeyManager to retrieve the correct local key (e.g., 'openai', 'agent-specific-id'). If omitted, might default to agent's humanReadableId or require explicit client configuration.")

class AgentCapabilities(BaseModel):
    """Defines the overall capabilities and supported protocols."""
    a2a_version: str = Field(..., alias="a2aVersion", description="Version of the A2A protocol supported by the agent endpoint.")
    mcp_version: Optional[str] = Field(None, alias="mcpVersion", description="Version of the Model Context Protocol supported (if any).")
    supported_message_parts: Optional[List[str]] = Field(None, alias="supportedMessageParts", description="List of message part types supported (e.g., 'text', 'file', 'data'). If omitted, client may assume basic types.")
    # Add other capability fields (e.g., max input size, supported SSE features)

class AgentCard(BaseModel):
    """
    Represents the A2A Agent Card, providing metadata about a remote agent.
    """
    schema_version: str = Field(..., alias="schemaVersion", description="Version of the Agent Card schema itself.")
    human_readable_id: str = Field(..., alias="humanReadableId", description="A user-friendly, unique identifier for the agent (e.g., 'my-org/weather-reporter'). Often used for discovery and key management.")
    agent_version: str = Field(..., alias="agentVersion", description="Version string of the agent software itself.")
    name: str = Field(..., description="Human-readable display name of the agent.")
    description: str = Field(..., description="Detailed description of the agent's purpose and functionality.")
    url: AnyUrl = Field(..., description="The primary A2A endpoint URL for interacting with the agent.")
    provider: AgentProvider = Field(..., description="Information about the agent's provider.")
    capabilities: AgentCapabilities = Field(..., description="Defines the agent's protocol capabilities.")
    auth_schemes: List[AgentAuthentication] = Field(..., alias="authSchemes", min_length=1, description="List of authentication schemes supported by the endpoint. Client must support at least one.")
    skills: Optional[List[AgentSkill]] = Field(None, description="Optional list detailing specific skills the agent possesses.")
    tags: Optional[List[str]] = Field(None, description="Keywords or tags for categorization and discovery.")
    privacy_policy_url: Optional[HttpUrl] = Field(None, alias="privacyPolicyUrl", description="URL to the agent's privacy policy.")
    terms_of_service_url: Optional[HttpUrl] = Field(None, alias="termsOfServiceUrl", description="URL to the agent's terms of service.")
    icon_url: Optional[HttpUrl] = Field(None, alias="iconUrl", description="URL to an icon representing the agent.")
    last_updated: Optional[str] = Field(None, alias="lastUpdated", description="Timestamp (ISO 8601 format recommended) indicating when the card was last updated.") # Consider using datetime

    # Example validator: Ensure URL is HTTPS if not localhost
    @field_validator('url')
    @classmethod
    def check_url_scheme(cls, v: AnyUrl) -> AnyUrl:
        if isinstance(v, str): # Pydantic might parse it already
             url_str = str(v)
        else:
             url_str = str(v)

        if not url_str.startswith('http://localhost') and not url_str.startswith('https://'):
             raise ValueError('Agent URL must use HTTPS unless it is localhost')
        # Pydantic v2 handles AnyUrl parsing well, this is more of an example
        return v

    @field_validator('auth_schemes')
    @classmethod
    def check_auth_schemes_not_empty(cls, v: List[AgentAuthentication]) -> List[AgentAuthentication]:
        if not v:
            raise ValueError('auth_schemes must not be empty')
        return v

    # Add more validators as needed (e.g., version formats)

#
